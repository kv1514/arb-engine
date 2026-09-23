#!/usr/bin/env python3
"""Backtest the arb detections on recorded days: what fired, and what acting on it made.

    python3 scripts/arb_backtest.py --db out/history.db --date 2026-09-20 [--date 2026-09-21] [--json FILE]

Read-only. Two parts:

**In-play ARB (the live slate).** Every recorded tick (``inplay_ticks.l1_json``) is replayed
through the production detector - ``scanner.analyze_event`` with the bankroll as budget, the
live tiers (``arb_push_min_margin`` / ``arb_big_margin``) and the 30 s per-game throttle -
so the alerts are the ones the current code would have sent. Each alert is then acted on
against the prices that were really there afterwards, three ways:

* ``instant``  both legs at the alert's prices: what the alert promised (a ceiling).
* ``guided``   a person following the ticket: the stale leg first after ``l1`` seconds, only at
               or below its alert price (limit); the second leg after ``l2`` seconds, only at or
               below its "still locks" price; whatever cannot be matched is sold back to the bid
               5 s later. Only prices that were quoted count, for the size shown.
* ``naive``    Robinhood first, then Kalshi at whatever it costs by then (no limits).

Fees are each venue's ``FeeModel.fee`` at the real count, on every buy and every sale back.
A matched set pays $1 whoever wins (a tie is ignored and counted: a Kalshi YES + Rothera YES
pair pays $0.50 on one).

**Pre-game / line TAKER ARB (the maker).** The maker's ``TAKER ARB`` detections
(``out/maker_journal.jsonl``) are grouped into episodes per line (a gap over ``gap_s`` ends
one), re-priced with taker fees on both asks at the maker's size, and measured for how long
they stayed on offer - the maker kept seeing them, which is the evidence a person had time.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arb_engine.fees.registry import fee_model_for_quote  # noqa: E402
from arb_engine.models import OutcomeQuote  # noqa: E402
from arb_engine.scanner import analyze_event  # noqa: E402
from arb_engine.tickreplay import merged_event_from  # noqa: E402
from arb_engine.quant.microdata import tie_value  # noqa: E402

EXECUTABLE = {"kalshi", "robinhood"}


def _money(x: float) -> str:
    return ("-$" if x < 0 else "$") + f"{abs(x):,.2f}"


class Series:
    """Per (event, venue, outcome) quote history for acting on an alert afterwards."""

    def __init__(self) -> None:
        self.rows: dict[tuple, list[tuple[float, OutcomeQuote]]] = defaultdict(list)

    def add(self, ts: float, me: Any) -> None:
        for v, qs in me.quotes_by_venue.items():
            for q in qs:
                if (q.meta or {}).get("side") == "no":
                    continue
                self.rows[(me.event_key, v, q.outcome)].append((ts, q))

    def at(self, key: tuple, t: float, window: float = 6.0) -> Optional[OutcomeQuote]:
        """The first quote seen in [t, t + window] (a 5 s recorder shows the book once per tick)."""
        for ts, q in self.rows.get(key, []):
            if t <= ts <= t + window:
                return q
            if ts > t + window:
                return None
        return None


def _fee(q: OutcomeQuote, price: float, n: int) -> float:
    try:
        return float(fee_model_for_quote(q).fee(price, n, "taker"))
    except Exception:
        return 0.0


UNKNOWN_SIZE_CAP = 10     # a quote recorded without its size: assume only a small order fills
USED_WINDOW_S = 120.0     # contracts we bought stay gone from the book this long


class Ledger:
    """Liquidity we already took: an earlier alert's purchase is not on offer to the next one
    (the recorder shows the book as it was, not as it would be after our order)."""

    def __init__(self) -> None:
        self.used: dict[tuple, list[tuple[float, int]]] = defaultdict(list)

    def available(self, key: tuple, t: float, shown: Optional[float]) -> int:
        taken = sum(k for tt, k in self.used[key] if t - tt <= USED_WINDOW_S)
        base = UNKNOWN_SIZE_CAP if shown is None else int(shown)
        return max(0, base - taken)

    def take(self, key: tuple, t: float, k: int) -> None:
        if k > 0:
            self.used[key].append((t, k))


def _buy(q: Optional[OutcomeQuote], n: int, limit: Optional[float], ledger: Optional[Ledger] = None,
         key: Optional[tuple] = None, t: float = 0.0) -> tuple[int, float]:
    """(contracts bought, cash incl. fee) from one quote, at or under ``limit`` (None = any),
    for no more than the size shown minus what we already took."""
    if q is None or q.ask is None or not 0 < q.ask < 1 or (limit is not None and q.ask > limit + 1e-9):
        return 0, 0.0
    avail = ledger.available(key, t, q.ask_size) if ledger is not None else (UNKNOWN_SIZE_CAP if q.ask_size is None else int(q.ask_size))
    k = min(n, avail)
    if k <= 0:
        return 0, 0.0
    if ledger is not None:
        ledger.take(key, t, k)
    return k, q.ask * k + _fee(q, q.ask, k)


def _sell(q: Optional[OutcomeQuote], n: int) -> float:
    """Cash back from selling ``n`` to the bid (0 when no bid: the contracts are stuck and
    counted at zero - the conservative reading)."""
    if n <= 0 or q is None or q.bid is None or q.bid <= 0:
        return 0.0
    return q.bid * n - _fee(q, q.bid, n)


def act(series: Series, alert: dict[str, Any], policy: str, l1: float = 5.0, l2: float = 15.0, n: Optional[int] = None,
        ledger: Optional[Ledger] = None) -> dict[str, Any]:
    """P&L of acting on one alert under ``policy`` (see the module docstring), for ``n``
    contracts (default: the alert's size). ``tied`` is the cash a locked set holds until the
    game settles; ``cash_out`` what the trade costs up front beyond that."""
    legs, t, ev = alert["legs"], alert["t"], alert["event_key"]
    n = int(alert["contracts"]) if n is None else int(n)
    if n <= 0:
        return {"pnl": 0.0, "matched": 0, "status": "missed", "spent": 0.0, "back": 0.0}
    if policy == "instant":
        per = alert["cost"] / alert["contracts"]
        if ledger is not None:   # the alert's own quotes, less what earlier alerts already took
            for l in legs:
                n = min(n, ledger.available((ev, l["venue"], l["outcome"]), t, l.get("size")))
            for l in legs:
                ledger.take((ev, l["venue"], l["outcome"]), t, n)
        if n <= 0:
            return {"pnl": 0.0, "matched": 0, "status": "missed", "spent": 0.0, "back": 0.0}
        return {"pnl": (1.0 - per) * n, "matched": n, "status": "locked", "spent": per * n, "back": 0.0}
    if policy == "guided":
        order = alert["order"]
        limits = [legs[order[0]]["price"], alert["max_prices"].get(order[1])]
    else:   # naive: Robinhood first (a person opens the app they know), then Kalshi at any price
        order = sorted(range(len(legs)), key=lambda i: 0 if legs[i]["venue"] == "robinhood" else 1)
        limits = [None, None]
    a, b = legs[order[0]], legs[order[1]]
    ka, kb = (ev, a["venue"], a["outcome"]), (ev, b["venue"], b["outcome"])
    na, ca = _buy(series.at(ka, t + l1), n, limits[0], ledger, ka, t + l1)
    if na == 0:
        return {"pnl": 0.0, "matched": 0, "status": "missed", "spent": 0.0, "back": 0.0}
    nb, cb = _buy(series.at(kb, t + l2), na, limits[1], ledger, kb, t + l2)
    back = _sell(series.at(ka, t + l2 + 5.0), na - nb) if na > nb else 0.0
    # Matched sets pay $1 each at settlement; the unmatched first-leg contracts are sold back.
    pnl = nb * 1.0 + back - ca - cb
    return {"pnl": pnl, "matched": nb, "status": "locked" if nb == na else ("partial" if nb else "unwound"),
            "spent": ca + cb, "back": back}


def replay_inplay(db: str, dates: list[str], bankroll: float = 500.0, min_margin: float = 0.01, big_margin: float = 0.03,
                  throttle_s: float = 30.0) -> tuple[list[dict[str, Any]], Series]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cond = " or ".join("event_key like ?" for _ in dates)
    ticks = con.execute(f"select ts, event_key, live, home, away, l1_json from inplay_ticks where l1_json is not null and ({cond}) order by ts",
                        [f"%:{d}" for d in dates]).fetchall()
    con.close()
    series, alerts = Series(), []
    replay_inplay.ends = {}
    mids: dict[tuple, list[tuple[float, float]]] = defaultdict(list)
    last: dict[str, float] = {}
    for row in ticks:
        tick = dict(row)
        me = merged_event_from(tick)
        if me is None:
            continue
        ts = float(tick["ts"])
        series.add(ts, me)
        replay_inplay.ends[me.event_key] = ts
        home = me.info.outcomes[-1]
        for v, qs in me.quotes_by_venue.items():
            q = next((x for x in qs if x.outcome == home and x.bid is not None and x.ask is not None), None)
            if q is not None:
                mids[(me.event_key, v)].append((ts, (q.bid + q.ask) / 2))
        rep = analyze_event(me, {}, contracts=100, target_margin=0.0, max_quote_age=10.0, now=ts, executable_venues=EXECUTABLE, budget=bankroll)
        arb = rep.arb or {}
        if not (arb.get("is_arb") and rep.fillable and "stale-quote" not in (rep.flags or [])):
            continue
        if ts - last.get(me.event_key, -1e18) < throttle_s:
            continue
        last[me.event_key] = ts
        sized = rep.sized_arb or arb
        legs = []
        for l in sized["legs"]:
            q = next((x for x in me.quotes_by_venue.get(l["venue"], []) if x.venue_market_id == l.get("market_id")), None)
            row = {"venue": l["venue"], "outcome": l["outcome"], "side": l.get("side") or "yes", "event_key": me.event_key,
                   "exchange": (q.meta or {}).get("exchange") if q else None, "fee_params": q.fee_params if q else {}, "book_id": q.book_id if q else l["venue"]}
            legs.append({"venue": l["venue"], "outcome": l["outcome"], "price": l["price"], "market_id": l.get("market_id"),
                         "size": q.ask_size if q else None, "tie": tie_value(row)})
        if len(legs) != 2:
            continue
        # stale leg first: the venue whose home mid moved least over the last 30 s
        moves = []
        for l in legs:
            h = [m for tt, m in mids[(me.event_key, l["venue"])] if ts - 35 <= tt <= ts]
            moves.append(abs(h[-1] - h[0]) if len(h) >= 2 else None)
        if None not in moves and abs(moves[0] - moves[1]) >= 0.01:
            order = [min((0, 1), key=lambda i: moves[i])]
        else:
            order = [0]
        order.append(1 - order[0])
        maxp = {}
        for i, l in enumerate(legs):
            for o in rep.outcomes or []:
                if o.outcome == l["outcome"]:
                    for v in o.venues:
                        if v.venue == l["venue"] and v.market_id == l["market_id"] and v.max_buy_price is not None:
                            maxp[i] = v.max_buy_price
        margin = float(sized["margin"])
        tier = "BIG ARB" if margin >= big_margin else ("ARB" if margin >= min_margin else "ARB SMALL")
        alerts.append({"t": ts, "event_key": me.event_key, "live": bool(tick["live"]), "tier": tier, "margin": margin,
                       "contracts": int(sized["contracts"]), "cost": float(sized["total_cost"]), "profit": float(sized["profit"]),
                       "legs": legs, "order": order, "max_prices": maxp,
                       "tie_safe": (sum(l["tie"] for l in legs) >= 1.0 - 1e-9) if all(l["tie"] is not None for l in legs) else None})
    return alerts, series


def with_bankroll(series: Series, sel: list[dict[str, Any]], policy: str, l1: float, l2: float, bankroll: float,
                  ends: dict[str, float], max_per_alert: Optional[float] = None) -> list[dict[str, Any]]:
    """Act on the alerts in time order with one bankroll. Both legs are paid for up front;
    a sold-back leg returns its cash at once; a locked set pays $1 when its game ends (its
    last recorded tick). Each alert is sized down to the cash on hand, so a day's alerts
    cannot all spend the same $500."""
    cash, held, out = bankroll, [], []
    ledger = Ledger()
    for a in sorted(sel, key=lambda x: x["t"]):
        for h in [h for h in held if h[0] <= a["t"]]:
            cash += h[1]
            held.remove(h)
        per = a["cost"] / a["contracts"]
        budget = min(cash, max_per_alert) if max_per_alert else cash
        n = min(a["contracts"], int(budget // per)) if per > 0 else 0
        r = act(series, a, policy, l1, l2, n, ledger)
        cash += r["back"] - r["spent"]
        if r["matched"]:
            held.append((ends.get(a["event_key"], a["t"]), r["matched"] * 1.0))
        out.append({**r, "sized": n})
    return out


def summarize_inplay(alerts: list[dict[str, Any]], series: Series, l1: float, l2: float, bankroll: Optional[float] = None,
                     ends: Optional[dict[str, float]] = None, max_per_alert: Optional[float] = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for tier in ("BIG ARB", "ARB", "ARB SMALL", "all"):
        sel = [a for a in alerts if tier == "all" or a["tier"] == tier]
        row: dict[str, Any] = {"alerts": len(sel), "games": len({a["event_key"] for a in sel})}
        for policy in ("instant", "guided", "naive"):
            if bankroll:
                res = with_bankroll(series, sel, policy, l1, l2, bankroll, ends or {}, max_per_alert)
            else:
                led = Ledger()
                res = [act(series, a, policy, l1, l2, None, led) for a in sorted(sel, key=lambda x: x["t"])]
            done = [r for r in res if r["status"] != "missed"]
            row[policy] = {"pnl": round(sum(r["pnl"] for r in res), 2), "per_alert": round(sum(r["pnl"] for r in res) / len(res), 3) if res else None,
                           "acted": len(done), "locked": sum(1 for r in res if r["status"] == "locked"),
                           "unwound": sum(1 for r in res if r["status"] in ("unwound", "partial")), "missed": len(res) - len(done),
                           "won": sum(1 for r in done if r["pnl"] > 0), "lost": sum(1 for r in done if r["pnl"] < 0)}
        out[tier] = row
    return out


# ---- the maker's line arbs --------------------------------------------------------------
PAT = re.compile(r"at the ask ([0-9.]+) and (.+?) on (\w+) at ([0-9.]+)")


def maker_episodes(path: str, dates: list[str], size: int = 10, gap_s: float = 120.0) -> dict[str, Any]:
    from arb_engine.fees.kalshi import KalshiFees
    from arb_engine.fees.robinhood import RobinhoodFees

    kf, rf = KalshiFees(), RobinhoodFees(exchange="rothera")
    rows = []
    for line in open(path, encoding="utf-8"):
        r = json.loads(line)
        if r.get("kind") != "alert" or r.get("title") != "TAKER ARB":
            continue
        day = time.strftime("%Y-%m-%d", time.localtime(r["ts"]))
        if dates and day not in dates:
            continue
        m = PAT.search(r.get("msg", ""))
        if not m:
            continue
        ka, ha = float(m.group(1)), float(m.group(4))
        cost = ka + float(kf.fee(ka, size)) / size + ha + float(rf.fee(ha, size)) / size
        rows.append((r["ts"], r.get("watch"), day, 1.0 - cost))
    rows.sort()
    eps: dict[str, list] = defaultdict(list)
    for ts, w, day, margin in rows:
        cur = eps[w][-1] if eps[w] else None
        if cur is None or ts - cur["end"] > gap_s:
            eps[w].append({"start": ts, "end": ts, "day": day, "margins": [margin], "watch": w})
        else:
            cur["end"] = ts
            cur["margins"].append(margin)
    flat = [e for es in eps.values() for e in es]
    out: dict[str, Any] = {"detections": len(rows), "episodes": len(flat), "lines": len(eps), "by_day": {}}
    for day in sorted({e["day"] for e in flat}):
        es = [e for e in flat if e["day"] == day]
        durs = sorted(e["end"] - e["start"] for e in es)
        first = [e["margins"][0] for e in es]
        pos = [m for m in first if m > 0]
        out["by_day"][day] = {"episodes": len(es), "positive_after_taker_fees": len(pos),
                              "median_minutes_on_offer": round(durs[len(durs) // 2] / 60, 1) if durs else None,
                              "on_offer_over_5min": sum(1 for d in durs if d >= 300),
                              "median_margin_c": round(sorted(first)[len(first) // 2] * 100, 2) if first else None,
                              "dollars_at_size": round(sum(m * size for m in pos), 2)}
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Backtest arb detections on recorded days (read-only).")
    ap.add_argument("--db", default=str(ROOT / "out/history.db"))
    ap.add_argument("--date", action="append", required=True, help="ET date in the event keys, e.g. 2026-09-20 (repeatable)")
    ap.add_argument("--maker-journal", default=str(ROOT / "out/maker_journal.jsonl"))
    ap.add_argument("--bankroll", type=float, default=500.0)
    ap.add_argument("--l1", type=float, default=5.0, help="seconds until the first leg is bought by hand")
    ap.add_argument("--l2", type=float, default=15.0, help="seconds until the second leg is bought by hand")
    ap.add_argument("--json", help="write the full result")
    a = ap.parse_args(argv)
    alerts, series = replay_inplay(a.db, a.date, bankroll=a.bankroll)
    report = {"dates": a.date, "bankroll": a.bankroll, "l1_s": a.l1, "l2_s": a.l2,
              "inplay": summarize_inplay(alerts, series, a.l1, a.l2, a.bankroll, replay_inplay.ends),
              "inplay_unlimited_cash": summarize_inplay(alerts, series, a.l1, a.l2),
              "inplay_stake_caps": {str(cap): {t: summarize_inplay(alerts, series, a.l1, a.l2, a.bankroll, replay_inplay.ends, cap)[t]["guided"]
                                               for t in ("BIG ARB", "all")} for cap in (50.0, 100.0, 250.0)},
              "inplay_sensitivity_l2": {str(l2): summarize_inplay(alerts, series, a.l1, l2, a.bankroll, replay_inplay.ends)["all"]["guided"] for l2 in (10.0, 15.0, 30.0)},
              "tie_unsafe_alerts": sum(1 for x in alerts if x["tie_safe"] is False),
              "alerts": [{**{k: v for k, v in x.items() if k not in ("legs", "max_prices")}, "legs": [f"{l['outcome']}@{l['venue']} {l['price']:.2f}" for l in x["legs"]]} for x in alerts]}
    if Path(a.maker_journal).exists():
        report["maker_lines"] = maker_episodes(a.maker_journal, a.date + [d for d in []])
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=2, default=str) + "\n")
    s = report["inplay"]
    print(f"In-play arb alerts ({', '.join(a.date)}, bankroll {_money(a.bankroll)}; hand legs at {a.l1:g}s / {a.l2:g}s):")
    for tier in ("BIG ARB", "ARB", "ARB SMALL", "all"):
        r = s[tier]
        if not r["alerts"]:
            continue
        line = f"  {tier:<9} {r['alerts']:>3} alerts in {r['games']} games"
        for p in ("instant", "guided", "naive"):
            x = r[p]
            line += f" | {p}: {_money(x['pnl'])} ({x['locked']} locked, {x['unwound']} unwound, {x['missed']} missed, {x['won']}W/{x['lost']}L)"
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

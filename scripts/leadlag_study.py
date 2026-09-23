#!/usr/bin/env python3
"""Lead-lag and "buy the dip" study over recorded in-play ticks (``live --record``).

    python scripts/leadlag_study.py --db out/history.db --date 2026-09-20 [--sport nfl] [--out results.json]

Two questions, answered from the per-venue L1 ticks the live slate records:

1. **Do big moves continue or revert?**  Every Kalshi mid move of ≥ ``--move`` (5¢) between
   two polls ≤ 30 s apart is an event; the later mid at +30 s / +2 min / +5 min is compared
   with the post-move level. "Continue" = the price kept going the same way by > 0.5¢,
   "revert" = it came back by > 0.5¢. Split by whether the score changed on that poll
   (a scoring play) or not (a drive, turnover, momentum). Mean(later move / initial move)
   > 0 is under-reaction (momentum), < 0 is over-reaction (a dip to buy).
2. **Who leads?**  For every ≥ ``--move`` move on venue A, had venue B already moved ≥ half
   as much the same way over the previous 60 s, and how long until B caught up to half the
   move (within 5 min)?

Historical result files created before the strict horizon and two-sided-fee accounting
changes are not evidence of net profitability. Re-run this script on the source database
before quoting a result.
"""

from __future__ import annotations

import argparse
import collections
import datetime as _dt
import json
import os
import sqlite3
import statistics
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # run from anywhere

VENUES = ("kalshi", "robinhood", "polymarket")
HORIZONS = (30, 120, 300)
HORIZON_TOLERANCE_S = 10


def load_ticks(db: str, date: str, sport: str) -> dict[str, list[dict[str, Any]]]:
    """In-play ticks of one local date, grouped per event, in time order."""
    day = _dt.datetime.strptime(date, "%Y-%m-%d")
    t0, t1 = day.timestamp(), (day + _dt.timedelta(days=1)).timestamp()
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    cols = ["ts", "event_key", "live", "home_score", "away_score", "model_p", "game_line"]
    for v in VENUES:
        cols += [f"{v}_home_bid", f"{v}_home_ask", f"{v}_home_quote_time"]
    rows = c.execute(f"select {', '.join(cols)} from inplay_ticks where ts >= ? and ts < ? and event_key like ? and live = 1 order by event_key, ts", (t0, t1, f"{sport}:%")).fetchall()
    c.close()
    by: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for r in rows:
        by[r["event_key"]].append(dict(r))
    return by


def mid(r: dict[str, Any], venue: str) -> Optional[float]:
    b, a = r.get(f"{venue}_home_bid"), r.get(f"{venue}_home_ask")
    return None if b is None or a is None else (b + a) / 2.0


def move_study(by: dict[str, list[dict[str, Any]]], venue: str = "kalshi", move: float = 0.05, max_gap: float = 30.0) -> dict[str, Any]:
    events = []
    for key, rs in by.items():
        for i in range(1, len(rs)):
            r0, r1 = rs[i - 1], rs[i]
            if r1["ts"] - r0["ts"] > max_gap:
                continue
            m0, m1 = mid(r0, venue), mid(r1, venue)
            if m0 is None or m1 is None or abs(m1 - m0) < move:
                continue
            d = m1 - m0
            dm = None
            if r0["model_p"] is not None and r1["model_p"] is not None:
                dm = r1["model_p"] - r0["model_p"]
            fut = {}
            for h in HORIZONS:
                later = next((x for x in rs[i:] if x["ts"] - r1["ts"] >= h), None)
                if later is not None and later["ts"] - r1["ts"] <= h + HORIZON_TOLERANCE_S and mid(later, venue) is not None:
                    fut[h] = mid(later, venue) - m1
            events.append({"key": key, "ts": r1["ts"], "d": d, "dm": dm, "fut": fut, "score_changed": (r0["home_score"], r0["away_score"]) != (r1["home_score"], r1["away_score"])})

    def summarize(evs: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {"n": len(evs), "horizons": {}}
        for h in HORIZONS:
            xs = [(e["d"], e["fut"][h]) for e in evs if h in e["fut"]]
            if not xs:
                continue
            cont = sum(1 for d, f in xs if d * f > 0 and abs(f) > 0.005)
            rev = sum(1 for d, f in xs if d * f < 0 and abs(f) > 0.005)
            out["horizons"][str(h)] = {"n": len(xs), "continue": cont, "revert": rev, "flat": len(xs) - cont - rev, "mean_later_over_initial": round(statistics.mean(f / d for d, f in xs), 4)}
        dms = [abs(e["dm"]) / abs(e["d"]) for e in evs if e["dm"] is not None]
        out["mean_model_move_over_market_move"] = round(statistics.mean(dms), 4) if dms else None
        return out

    return {"venue": venue, "move": move, "all": summarize(events), "score_changed": summarize([e for e in events if e["score_changed"]]), "no_score_change": summarize([e for e in events if not e["score_changed"]])}


def leadlag_study(by: dict[str, list[dict[str, Any]]], move: float = 0.05, max_gap: float = 30.0) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for lead in VENUES:
        for follow in VENUES:
            if lead == follow:
                continue
            n = 0
            already = 0
            lags: list[float] = []
            never = 0
            for key, rs in by.items():
                for i in range(1, len(rs)):
                    r0, r1 = rs[i - 1], rs[i]
                    if r1["ts"] - r0["ts"] > max_gap:
                        continue
                    a0, a1, b0, b1 = mid(r0, lead), mid(r1, lead), mid(r0, follow), mid(r1, follow)
                    if None in (a0, a1, b0, b1):
                        continue
                    d = a1 - a0
                    if abs(d) < move:
                        continue
                    n += 1
                    prev = [x for x in rs[:i] if r0["ts"] - x["ts"] <= 60 and mid(x, follow) is not None]
                    pre = (b0 - mid(prev[0], follow)) if prev else 0.0
                    if pre * d > 0 and abs(pre) >= move / 2:
                        already += 1
                    t = None
                    for x in rs[i:]:
                        if x["ts"] - r1["ts"] > 300:
                            break
                        bm = mid(x, follow)
                        if bm is not None and (bm - b0) * d > 0 and abs(bm - b0) >= abs(d) / 2:
                            t = x["ts"] - r1["ts"]
                            break
                    if t is None:
                        never += 1
                    else:
                        lags.append(t)
            out[f"{lead}->{follow}"] = {"lead_moves": n, "follower_moved_first": already, "caught_up_5min": len(lags), "never_5min": never, "median_lag_s": round(statistics.median(lags), 1) if lags else None, "p25_lag_s": round(sorted(lags)[len(lags) // 4], 1) if lags else None}
    return out


def lag_replay(db: str, date: str, sport: str, bankroll: float = 500.0, horizons: tuple[int, ...] = (30, 60, 300)) -> dict[str, Any]:
    """Run ``strategy.leadlag.LeadLagTracker`` (default settings) over the recorded L1 JSON
    and score every signal by selling to the follower's BID ``h`` seconds later, net of the
    entry AND exit fees. Marks must be within 10 s of the requested horizon.
    Fills at the recorded ask are assumed — the optimistic part."""
    from arb_engine.models import OutcomeQuote
    from arb_engine.fees.registry import fee_model_for_quote
    from arb_engine.strategy.leadlag import LeadLagTracker, _fresh, _mid

    day = _dt.datetime.strptime(date, "%Y-%m-%d")
    t0, t1 = day.timestamp(), (day + _dt.timedelta(days=1)).timestamp()
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    rows = c.execute("select ts, event_key, l1_json, game_line from inplay_ticks where ts >= ? and ts < ? and event_key like ? and live = 1 and l1_json is not null order by ts", (t0, t1, f"{sport}:%")).fetchall()
    tr = LeadLagTracker.from_settings(None, executable={"kalshi", "robinhood"}, fresh_s=15)
    sigs = []
    for r in rows:
        l1 = json.loads(r["l1_json"])
        qbv: dict[str, list[OutcomeQuote]] = {}
        for v, by in l1.items():
            if v == "rows" or not isinstance(by, dict):
                continue
            for o, q in by.items():
                if q.get("refreshed") in (False, 0):
                    continue
                qbv.setdefault(v, []).append(OutcomeQuote(v, q.get("venue_market_id") or "", r["event_key"], o, ask=q.get("ask"), bid=q.get("bid"), ask_size=q.get("ask_size"), ts=q.get("obs_ts", q.get("ts", r["ts"])), book_id=q.get("book_id") or ("kalshi" if v == "robinhood" and str(q.get("venue_market_id", "")).startswith("KX") else v), quote_time=q.get("quote_time"), fee_params=q.get("fee_params") or {}, meta={"exchange": q.get("exchange")} if q.get("exchange") else {}))
        try:
            away, home = r["event_key"].split(":")[1].split("|")
        except ValueError:
            continue
        for sg in tr.observe(r["event_key"], r["game_line"] or "", [away, home], {}, qbv, None, r["ts"], bankroll, 0.25):
            sigs.append((r["ts"], sg))

    def net_bid_at(key: str, venue: str, outcome: str, t: float, contracts: int) -> Optional[float]:
        r = c.execute("select ts, l1_json from inplay_ticks where event_key = ? and ts >= ? and ts <= ? and l1_json is not null order by ts limit 1", (key, t, t + 10)).fetchone()
        if not r:
            return None
        data = json.loads(r["l1_json"]).get(venue, {}).get(outcome)
        if not data or data.get("refreshed") in (False, 0):
            return None
        q = OutcomeQuote(venue, data.get("venue_market_id", ""), key, outcome,
            bid=data.get("bid"), ask=data.get("ask"), ts=data.get("obs_ts", data.get("ts", r["ts"])),
            quote_time=data.get("quote_time"), fee_params=data.get("fee_params") or {},
            meta={"exchange": data.get("exchange")} if data.get("exchange") else {})
        if _mid(q) is None or not _fresh(q, r["ts"], 15):
            return None
        try:
            return q.bid - fee_model_for_quote(q).per_contract(q.bid, contracts, "taker")
        except Exception:
            return None

    out: dict[str, Any] = {"signals": len(sigs), "by_leader": dict(collections.Counter(s.leader for _, s in sigs)), "by_follower": dict(collections.Counter(s.follower for _, s in sigs)), "edge_median": round(statistics.median(s.edge for _, s in sigs), 4) if sigs else None, "exit_at_bid": {}}
    # Hold to settlement: the final score from the ESPN ticks decides each signal's side.
    finals = {r["event_key"]: dict(r) for r in c.execute("select * from espn_ticks where status = 'final' and ts >= ? and ts < ? + 86400 order by ts", (t0, t1)).fetchall()}
    settled = []
    for ts, sg in sigs:
        f = finals.get(sg.event_key)
        if not f or f.get("home_score") is None or f.get("away_score") is None:
            continue
        hs, as_ = f["home_score"], f["away_score"]
        winner = None if hs == as_ else (f.get("home") if hs > as_ else f.get("away"))
        if winner is None:
            continue  # tie payouts differ by contract; unknown settlement is not $0.50
        value = 1.0 if sg.outcome == winner else 0.0
        settled.append(value - sg.follower_all_in)
    if settled:
        out["hold_to_settlement"] = {"n": len(settled), "win": sum(1 for x in settled if x > 0), "loss": sum(1 for x in settled if x <= 0), "mean_pnl_per_contract": round(statistics.mean(settled), 4), "total_per_contract": round(sum(settled), 2)}
    for h in horizons:
        wins = losses = unknown = 0
        pnl = []
        for ts, sg in sigs:
            b = net_bid_at(sg.event_key, sg.follower, sg.outcome, ts + h, sg.suggested_contracts or 1)
            if b is None:
                unknown += 1
                continue
            g = b - sg.follower_all_in
            pnl.append(g)
            wins, losses = wins + (g > 0), losses + (g <= 0)
        out["exit_at_bid"][str(h)] = {"win": wins, "loss": losses, "unknown": unknown, "mean_pnl_per_contract": round(statistics.mean(pnl), 4) if pnl else None}
    c.close()
    return out


def paper_summary(db: str, date: Optional[str] = None) -> dict[str, Any]:
    """What the live paper book (``lag_paper``) recorded: fill rate, latency, P&L at the marks
    and at settlement — the fill-adjusted answer the replay cannot give."""
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in c.execute("select * from lag_paper order by ts")]
    except sqlite3.OperationalError:
        c.close()
        return {"orders": 0}
    if date:
        day = _dt.datetime.strptime(date, "%Y-%m-%d")
        t0, t1 = day.timestamp(), (day + _dt.timedelta(days=1)).timestamp()
        rows = [r for r in rows if t0 <= r["ts"] < t1]
    filled = [r for r in rows if r["filled_at"] is not None]
    out: dict[str, Any] = {"orders": len(rows), "filled": len(filled), "expired": sum(1 for r in rows if r["expired_at"] is not None), "by_follower": dict(collections.Counter(r["follower"] for r in rows))}
    if filled:
        lat = sorted(r["filled_at"] - r["ts"] for r in filled)
        out["fill_latency_median_s"] = round(lat[len(lat) // 2], 2)
        for off in (30, 60, 300):
            xs = []
            for r in filled:
                if r.get(f"bid_{off}") is None:
                    continue
                extra = json.loads(r.get("extra_json") or "{}")
                exit_fee = (extra.get("marks") or {}).get(f"exit_fee_{off}")
                if exit_fee is not None:
                    xs.append(r[f"bid_{off}"] - exit_fee - r["all_in"])
            if xs:
                out[f"pnl_bid_{off}"] = {"n": len(xs), "wins": sum(1 for x in xs if x > 0), "mean": round(statistics.mean(xs), 4)}
        st = [r["pnl_settle"] for r in filled if r["settled"] and r["pnl_settle"] is not None]
        if st:
            out["pnl_settle"] = {"n": len(st), "wins": sum(1 for x in st if x > 0), "mean": round(statistics.mean(st), 4)}
    c.close()
    return out


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="out/history.db")
    p.add_argument("--date", required=True, help="local date of the ticks, YYYY-MM-DD")
    p.add_argument("--sport", default="nfl")
    p.add_argument("--move", type=float, default=0.05)
    p.add_argument("--out", help="write the metrics JSON here")
    a = p.parse_args(argv)
    by = load_ticks(a.db, a.date, a.sport)
    res = {"date": a.date, "sport": a.sport, "games": len(by), "ticks": sum(len(v) for v in by.values()), "moves": move_study(by, "kalshi", a.move), "leadlag": leadlag_study(by, a.move), "lag_replay": lag_replay(a.db, a.date, a.sport), "paper": paper_summary(a.db, a.date)}
    ms = res["moves"]
    print(f"{res['games']} games, {res['ticks']} in-play ticks; Kalshi moves >= {a.move:.0%}: {ms['all']['n']} (score changed {ms['score_changed']['n']}, no score change {ms['no_score_change']['n']})")
    for label in ("all", "no_score_change"):
        for h, v in ms[label]["horizons"].items():
            print(f"  [{label}] +{h}s: n={v['n']} continue {v['continue']} revert {v['revert']} flat {v['flat']}  mean later/initial {v['mean_later_over_initial']:+.2f}")
    for k, v in res["leadlag"].items():
        print(f"  {k}: {v['lead_moves']} moves; follower moved first {v['follower_moved_first']}; caught up within 5 min {v['caught_up_5min']} (median {v['median_lag_s']} s), never {v['never_5min']}")
    lr = res["lag_replay"]
    print(f"  LAG rule replayed: {lr['signals']} signals; " + "; ".join(f"sell at bid +{h}s: {v['win']}W/{v['loss']}L mean {(v['mean_pnl_per_contract'] if v['mean_pnl_per_contract'] is not None else 0.0):+.4f}/ct" for h, v in lr["exit_at_bid"].items()))
    if lr.get("hold_to_settlement"):
        h = lr["hold_to_settlement"]
        print(f"  … or held to settlement: {h['win']}W/{h['loss']}L mean {h['mean_pnl_per_contract']:+.4f}/ct (sum {h['total_per_contract']:+.2f} per contract-lot)")
    pp = res["paper"]
    print(f"  live paper book: {pp.get('orders', 0)} orders, {pp.get('filled', 0)} filled, {pp.get('expired', 0)} expired" + (f", fill latency median {pp['fill_latency_median_s']} s" if pp.get("fill_latency_median_s") is not None else "") + "".join(f"; +{k[8:]}s {v['wins']}/{v['n']} wins mean {v['mean']:+.4f}" for k, v in pp.items() if k.startswith("pnl_bid_")) + (f"; settled {pp['pnl_settle']['wins']}/{pp['pnl_settle']['n']} mean {pp['pnl_settle']['mean']:+.4f}" if pp.get("pnl_settle") else ""))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1, sort_keys=True)
            f.write("\n")
        print("wrote", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

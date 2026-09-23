"""Leakage-free samples for the football microstructure experiment (H1 momentum, H2 dip-
recovery, H3 independent-book lead-lag, H4 arbitrage). Offline, stdlib only, read-only.

Input rows are *observations of one contract*: ``obs_ts`` (when our request's answer was in
hand), ``refreshed`` (0 = carried forward, not an observation), ``venue`` / ``book_id`` /
``venue_market_id`` / ``side`` / ``outcome``, L1 ``bid`` / ``ask`` / sizes, the venue's
``quote_time``, ``tie_payout``, fee params, and the tick's ``in_play``. ``load_db`` flattens
the recorder's ``inplay_ticks.l1_json`` into that shape (new ``rows`` lists, and the legacy
outcome maps with ``approx_time = 1``: those have no observation time or refresh flag).

Rules (docs/MODEL.md, "Microstructure experiment"):

* **Identity.** One economic contract = (event, book, outcome, side). YES and NO are different
  contracts (a Kalshi NO of KC trades near 1 - KC); a book reached through two venues
  (Robinhood's Kalshi-routed rows) is one contract, the direct venue's row preferred.
* **Causality.** Rows are processed in observation order; a sample at ``t`` sees only rows
  with ``obs_ts <= t``, ESPN rows with our receipt ``ts <= t`` and Kalshi prints stamped at
  or before ``t - 1``. ``features_at`` raises on a future row; the streaming builder cannot
  see one by construction, and a test proves appending the future changes no past sample.
* **Decision points.** A refreshed, two-sided (0 < bid <= ask < 1), in-play observation
  whose venue timestamp (when given) is <= 10 s old. ``trigger`` samples: |dmid_30| >= 0.05
  with bid and ask each >= 40 % of it, same sign, once per contract per 60 s.
  ``unconditional`` samples: once per contract per 5 s.
* **Labels.** For h in 5 / 15 / 30 / 60 s, the first refreshed observation of the same
  contract in [t+h, t+h+max(1, 0.2 h)]: dbid / dask / dmid, never carried forward (missing =
  excluded and counted). ``ret_long_h`` is the executable round trip from
  ``quant.paperexec`` (IOC at the decision ask after 1 s, sold to the bid at h, both fees).
"""
from __future__ import annotations

import bisect
import json
import math
import sqlite3
from collections import defaultdict, deque
from typing import Any, Iterable, Optional

HORIZONS = (5, 15, 30, 60)
LOOKBACKS = (5, 15, 30, 60)
FAST_VENUES = ("kalshi", "robinhood")   # refreshed every second by the fast lane
TRIGGER_MOVE, TRIGGER_SHARE, TRIGGER_COOLDOWN_S, UNCONDITIONAL_EVERY_S = 0.05, 0.40, 60.0, 5.0
VENUE_LAG_MAX_S = 10.0
HISTORY_S = 130.0
REF_CONTRACTS = 10


def _f(value: Any) -> Optional[float]:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def observation_time(row: dict[str, Any], tick_ts: Optional[float] = None) -> tuple[Optional[float], int]:
    """(time, approx_time). Legacy rows have no obs_ts and fall back to their tick's time."""
    obs = _f(row.get("obs_ts"))
    if obs is not None:
        return obs, int(bool(row.get("approx_time", 0)))
    return _f(tick_ts if tick_ts is not None else (row.get("tick_ts") if row.get("tick_ts") is not None else row.get("ts"))), 1


def side_of(row: dict[str, Any]) -> str:
    return str(row.get("side") or "yes").lower()


def contract_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    """(event, book, outcome, side): one economic contract, whichever venue displayed it."""
    return (str(row.get("event_key") or ""), str(row.get("book_id") or row.get("venue") or ""),
            str(row.get("outcome") or ""), side_of(row))


def _mid(row: dict[str, Any]) -> float:
    return (float(row["bid"]) + float(row["ask"])) / 2.0


def is_observation(row: dict[str, Any]) -> bool:
    """A usable observation: refreshed (legacy rows count), two-sided, in play, venue fresh."""
    if row.get("refreshed") in (0, False):
        return False
    if not row.get("in_play", True):
        return False
    bid, ask = _f(row.get("bid")), _f(row.get("ask"))
    if bid is None or ask is None or not 0 < bid <= ask < 1:
        return False
    obs, _ = observation_time(row)
    if obs is None:
        return False
    qt = _f(row.get("quote_time"))
    return qt is None or 0 <= obs - qt <= VENUE_LAG_MAX_S


# ---------------------------------------------------------------------------------------
# streaming state per contract
class _Book:
    __slots__ = ("hist", "last_change_t", "last_mid", "row")

    def __init__(self) -> None:
        self.hist: deque = deque()          # (t, bid, ask, mid)
        self.last_change_t: Optional[float] = None
        self.last_mid: Optional[float] = None
        self.row: Optional[dict[str, Any]] = None

    def push(self, t: float, row: dict[str, Any]) -> None:
        bid, ask = float(row["bid"]), float(row["ask"])
        mid = (bid + ask) / 2.0
        if self.last_mid is None or abs(mid - self.last_mid) > 1e-9:
            self.last_change_t = t
        self.last_mid, self.row = mid, row
        self.hist.append((t, bid, ask, mid))
        while self.hist and t - self.hist[0][0] > HISTORY_S:
            self.hist.popleft()

    def anchor(self, t: float, k: float) -> Optional[tuple]:
        """Latest observation at or before t-k, and no older than t-k-max(1, 0.2k)."""
        tol = max(1.0, 0.2 * k)
        best = None
        for obs in self.hist:
            if obs[0] <= t - k:
                best = obs
            else:
                break
        return best if best is not None and best[0] >= t - k - tol else None


def _book_features(b: _Book, t: float) -> dict[str, Any]:
    cur = b.hist[-1]
    out: dict[str, Any] = {"mid": cur[3], "bid": cur[1], "ask": cur[2], "spread": cur[2] - cur[1]}
    for k in LOOKBACKS:
        a = b.anchor(t, k)
        out[f"dmid_{k}"] = cur[3] - a[3] if a else None
        if k == 30:
            out["dbid_30"] = cur[1] - a[1] if a else None
            out["dask_30"] = cur[2] - a[2] if a else None
    w30 = [h for h in b.hist if h[0] >= t - 30]
    path30 = sum(abs(y[3] - x[3]) for x, y in zip(w30, w30[1:]))
    out["efficiency_30"] = abs(out["dmid_30"]) / path30 if out["dmid_30"] is not None and path30 > 0 else None
    w60 = [h for h in b.hist if h[0] >= t - 60]
    out["var_60"] = sum(abs(y[3] - x[3]) for x, y in zip(w60, w60[1:]))
    out["changes_60"] = sum(1 for x, y in zip(w60, w60[1:]) if abs(y[3] - x[3]) > 1e-9)
    out["since_change_s"] = t - b.last_change_t if b.last_change_t is not None else None
    out["peak_60"] = max(h[3] for h in w60) if w60 else cur[3]
    out["trough_60"] = min(h[3] for h in w60) if w60 else cur[3]
    return out


def _two_sided(f: dict[str, Any]) -> bool:
    d, db, da = f.get("dmid_30"), f.get("dbid_30"), f.get("dask_30")
    if d is None or db is None or da is None or d == 0:
        return False
    return db * d > 0 and da * d > 0 and abs(db) >= TRIGGER_SHARE * abs(d) and abs(da) >= TRIGGER_SHARE * abs(d)


def _game_key(event_key: str) -> str:
    for tag in (":spread:", ":total:"):
        if tag in event_key:
            return event_key.split(tag, 1)[0]
    return event_key


class _Espn:
    """ESPN rows per game, consumed in our receipt order (ESPN posts after the play)."""

    def __init__(self, rows: Iterable[dict[str, Any]]) -> None:
        by: dict[str, list] = defaultdict(list)
        for r in rows or []:
            ts = _f(r.get("ts"))
            if ts is not None:
                by[str(r.get("event_key"))].append((ts, r))
        self.by = {k: sorted(v, key=lambda x: x[0]) for k, v in by.items()}
        self.times = {k: [x[0] for x in v] for k, v in self.by.items()}
        self.score_change: dict[str, list[float]] = {}
        for k, v in self.by.items():
            ch, last = [], None
            for ts, r in v:
                sc = (r.get("home_score"), r.get("away_score"))
                if last is not None and sc != last:
                    ch.append(ts)
                last = sc
            self.score_change[k] = ch

    def at(self, game: str, t: float, outcome: str) -> dict[str, Any]:
        times = self.times.get(game)
        if not times:
            return {}
        i = bisect.bisect_right(times, t) - 1
        if i < 0:
            return {}
        r = self.by[game][i][1]
        hs, as_ = r.get("home_score"), r.get("away_score")
        sign = 1 if outcome == r.get("home") else -1 if outcome == r.get("away") else 0
        ch = self.score_change.get(game, [])
        j = bisect.bisect_right(ch, t) - 1
        return {"espn_age_s": t - self.by[game][i][0], "period": r.get("period"), "clock": r.get("clock"),
                "score_diff": (sign * ((hs or 0) - (as_ or 0))) if sign and hs is not None and as_ is not None else None,
                "since_score_s": t - ch[j] if j >= 0 else None, "last_play_type": r.get("last_play_type")}


class _Prints:
    """Kalshi public prints per ticker; a sample at t sees prints stamped <= t - 1."""

    def __init__(self, rows: Iterable[dict[str, Any]]) -> None:
        by: dict[str, list] = defaultdict(list)
        for r in rows or []:
            ts, n = _f(r.get("ts")), _f(r.get("count"))
            if ts is not None and n is not None:
                s = 1 if str(r.get("taker_side")).lower() == "yes" else -1 if str(r.get("taker_side")).lower() == "no" else 0
                by[str(r.get("ticker"))].append((ts, s * n, _f(r.get("price"))))
        self.by = {k: sorted(v) for k, v in by.items()}
        self.times = {k: [x[0] for x in v] for k, v in self.by.items()}

    def at(self, ticker: str, t: float, side: str, mid: float) -> dict[str, Any]:
        times = self.times.get(ticker)
        if not times:
            return {}
        hi = bisect.bisect_right(times, t - 1.0)
        rows = self.by[ticker][:hi]
        sgn = -1 if side == "no" else 1
        out = {}
        for w in (30, 60):
            lo = bisect.bisect_left(times, t - 1.0 - w, 0, hi)
            out[f"flow_{w}"] = sgn * sum(x[1] for x in rows[lo:])
        last = next((x for x in reversed(rows) if x[2] is not None), None)
        out["last_print_minus_mid"] = ((last[2] if sgn == 1 else 1 - last[2]) - mid) if last else None
        return out


# ---------------------------------------------------------------------------------------
def _dedupe(rows: Iterable[dict[str, Any]]) -> list[tuple[float, int, dict[str, Any]]]:
    """(t, approx, row) observations sorted by time, one per contract per instant: the
    direct venue's row wins over a reseller's (Robinhood's Kalshi-routed rows)."""
    best: dict[tuple, tuple[float, int, dict[str, Any]]] = {}
    for r in rows:
        if not is_observation(r):
            continue
        t, approx = observation_time(r)
        key = (contract_key(r), t)
        prev = best.get(key)
        direct = str(r.get("venue")) == str(r.get("book_id") or r.get("venue"))
        if prev is None or (direct and str(prev[2].get("venue")) != str(prev[2].get("book_id") or prev[2].get("venue"))):
            best[key] = (t, approx, r)
    return sorted(best.values(), key=lambda x: (x[0], contract_key(x[2])))


def features_at(rows: Iterable[dict[str, Any]], t: float) -> dict[tuple, dict[str, Any]]:
    """Causal per-contract features at ``t`` from ``rows`` (all must be observed <= t)."""
    rows = list(rows)
    for r in rows:
        obs, _ = observation_time(r)
        if obs is not None and obs > t:
            raise ValueError("features_at received an observation from the future")
    books: dict[tuple, _Book] = defaultdict(_Book)
    for tt, _, r in _dedupe(rows):
        books[contract_key(r)].push(tt, r)
    return {k: {**_book_features(b, t), "t": t} for k, b in books.items() if b.hist and b.hist[-1][0] == t}


def build(rows: Iterable[dict[str, Any]], espn: Iterable[dict[str, Any]] = (), prints: Iterable[dict[str, Any]] = (),
          horizons: Iterable[int] = HORIZONS, sample: str = "trigger", settlement: Optional[dict[tuple, float]] = None,
          fee_for_row: Any = None, ref_contracts: int = REF_CONTRACTS, latency_s: float = 1.0,
          entry_tol_s: float = 2.0) -> list[dict[str, Any]]:
    """Samples (``trigger``, ``unconditional`` or ``all``) with causal features and forward labels.

    ``settlement`` maps (event_key, outcome, side) to the contract's settlement value for the
    executable return's roll-to-settlement; ``fee_for_row(row)`` returns the venue fee model
    (default: ``fees.registry.fee_model_for_quote`` on the row's venue and fee params).
    ``latency_s`` / ``entry_tol_s`` are the order's arrival and how stale a book it may meet:
    a 5 s recorder cannot show the book 1 s after a decision, so legacy data needs
    latency_s >= its poll interval, or every executable return is (rightly) a missed fill."""
    obs = _dedupe(rows)
    espn_idx, prints_idx = _Espn(espn), _Prints(prints)
    books: dict[tuple, _Book] = defaultdict(_Book)
    by_contract: dict[tuple, list[tuple[float, dict[str, Any]]]] = defaultdict(list)
    for t, _, r in obs:
        by_contract[contract_key(r)].append((t, r))
    times = {k: [x[0] for x in v] for k, v in by_contract.items()}
    last_trigger: dict[tuple, float] = {}
    last_uncond: dict[tuple, float] = {}
    out: list[dict[str, Any]] = []
    fee_for_row = fee_for_row or _default_fee
    for t, approx, r in obs:
        key = contract_key(r)
        b = books[key]
        b.push(t, r)
        f = _book_features(b, t)
        kinds = []
        if sample in ("trigger", "all") and f["dmid_30"] is not None and abs(f["dmid_30"]) >= TRIGGER_MOVE and _two_sided(f) \
                and t - last_trigger.get(key, -math.inf) >= TRIGGER_COOLDOWN_S:
            last_trigger[key] = t
            kinds.append("trigger")
        if sample in ("unconditional", "all") and t - last_uncond.get(key, -math.inf) >= UNCONDITIONAL_EVERY_S:
            last_uncond[key] = t
            kinds.append("unconditional")
        if not kinds:
            continue
        s: dict[str, Any] = {"t": t, "approx_time": approx, "event_key": key[0], "book_id": key[1], "outcome": key[2], "side": key[3],
                             "venue": r.get("venue"), "venue_market_id": r.get("venue_market_id"), "tie_payout": _f(r.get("tie_payout")), **f}
        qt = _f(r.get("quote_time"))
        s["venue_lag_s"] = t - qt if qt is not None else None
        bs, as_ = _f(r.get("bid_size")), _f(r.get("ask_size"))
        s["imbalance"] = (bs - as_) / (bs + as_) if bs is not None and as_ is not None and bs + as_ > 0 else None   # displayed; may be cancelled
        # cross-book: the same contract on independent books, as last observed and still fresh
        cross = {}
        for (ev, book, oc, sd), ob in books.items():
            if ev != key[0] or oc != key[2] or sd != key[3] or book == key[1] or not ob.hist:
                continue
            age = t - ob.hist[-1][0]
            if age > (2.0 if (ob.row or {}).get("venue") in FAST_VENUES else 6.0):
                continue
            of = _book_features(ob, t)
            cross[book] = {"gap": of["mid"] - f["mid"], "dmid_30": of["dmid_30"], "age_s": age,
                           "tie_mismatch": _f((ob.row or {}).get("tie_payout")) != s["tie_payout"]}
        s["cross"] = cross
        leader = max(cross.items(), key=lambda kv: abs(kv[1]["dmid_30"] or 0), default=None)
        s["leader_book"] = leader[0] if leader else None
        s["leader_dmid_30"] = leader[1]["dmid_30"] if leader else None
        s["gap_leader"] = leader[1]["gap"] if leader else None
        s.update(espn_idx.at(_game_key(key[0]), t, key[2]))
        if key[1] == "kalshi":
            ticker = str(r.get("venue_market_id") or "").split("#", 1)[0]
            s.update(prints_idx.at(ticker, t, key[3], f["mid"]))
        # labels
        series, ts_list = by_contract[key], times[key]
        for h in horizons:
            lo, hi = t + h, t + h + max(1.0, 0.2 * h)
            i = bisect.bisect_left(ts_list, lo)
            if i < len(ts_list) and ts_list[i] <= hi:
                m = series[i][1]
                s[f"dbid_{h}"], s[f"dask_{h}"] = float(m["bid"]) - f["bid"], float(m["ask"]) - f["ask"]
                s[f"dmid_{h}_fwd"] = _mid(m) - f["mid"]
            else:
                s[f"dbid_{h}"] = s[f"dask_{h}"] = s[f"dmid_{h}_fwd"] = None
            s[f"ret_long_{h}"] = _exec_return(series, t, f["ask"], h, fee_for_row(r), ref_contracts,
                                              (settlement or {}).get((key[0], key[2], key[3])), latency_s, entry_tol_s)
        for kind in kinds:
            out.append({**s, "kind": kind})
    return out


def _exec_return(series: list[tuple[float, dict[str, Any]]], t: float, ask: float, h: int, fee_model: Any, n: int,
                 settle: Optional[float], latency_s: float = 1.0, entry_tol_s: float = 2.0) -> Optional[float]:
    if fee_model is None:
        return None
    from .paperexec import ioc_round_trip

    times = [tt for tt, _ in series]
    lo, hi = bisect.bisect_right(times, t), bisect.bisect_right(times, t + latency_s + entry_tol_s + h + 90)
    rows = [dict(r, obs_ts=tt) for tt, r in series[lo:hi]]
    trade = ioc_round_trip(rows, t, ask, n, fee_model, latency_s=latency_s, horizon_s=float(h), settlement=settle, entry_tol_s=entry_tol_s)
    return trade.pnl_per_contract


def _default_fee(row: dict[str, Any]) -> Any:
    try:
        from ..fees.registry import fee_model_for_quote
        from ..models import OutcomeQuote

        meta = {"exchange": row.get("exchange")} if row.get("exchange") else {}
        q = OutcomeQuote(str(row.get("venue")), str(row.get("venue_market_id") or ""), str(row.get("event_key") or ""),
                         str(row.get("outcome") or ""), ask=_f(row.get("ask")), bid=_f(row.get("bid")),
                         fee_params=dict(row.get("fee_params") or {}), meta=meta)
        return fee_model_for_quote(q)
    except Exception:
        return None


def trigger_events(rows: Iterable[dict[str, Any]], cooldown_s: float = TRIGGER_COOLDOWN_S) -> list[dict[str, Any]]:
    return [s for s in build(rows, sample="trigger", horizons=()) if s["kind"] == "trigger"]


def unconditional_samples(rows: Iterable[dict[str, Any]], every_s: float = UNCONDITIONAL_EVERY_S) -> list[dict[str, Any]]:
    return [s for s in build(rows, sample="unconditional", horizons=()) if s["kind"] == "unconditional"]


decision_points = unconditional_samples


def lead_lag_label(rows: Iterable[dict[str, Any]], leader: dict[str, Any], follower: dict[str, Any], horizon: int = 30) -> Optional[dict[str, Any]]:
    """H3 target: the follower's move over ``horizon`` toward the leader's decision-time gap.
    Pairs on one book are not lead-lag (a reseller shows the same book)."""
    if leader["book_id"] == follower["book_id"]:
        return None
    lo, hi = follower["t"] + horizon, follower["t"] + horizon + max(1.0, 0.2 * horizon)
    marks = sorted((observation_time(r)[0], r) for r in rows if is_observation(r)
                   and str(r.get("book_id") or r.get("venue")) == follower["book_id"]
                   and (follower.get("outcome") is None or r.get("outcome") == follower.get("outcome"))
                   and lo <= (observation_time(r)[0] or -1) <= hi)
    if not marks:
        return None
    t, m = marks[0]
    gap = leader["mid"] - follower["mid"]
    d = _mid(m) - follower["mid"]
    return {"horizon": horizon, "label_ts": t, "dmid_h": d, "gap": gap,
            "convergence": d * (1 if gap > 0 else -1 if gap < 0 else 0)}


# ---------------------------------------------------------------------------------------
def rows_from_tick(tick: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten one ``inplay_ticks`` row's ``l1_json`` into contract observations."""
    try:
        l1 = json.loads(tick.get("l1_json") or "{}")
    except (TypeError, ValueError):
        return []
    base = {"event_key": tick.get("event_key"), "tick_ts": _f(tick.get("ts")), "in_play": bool(tick.get("live", 1)),
            "source": tick.get("source") or "full"}
    out = []
    if isinstance(l1.get("rows"), list):
        for r in l1["rows"]:
            out.append({**base, **r, "book_id": r.get("book_id") or r.get("venue")})
        return out
    for venue, per in l1.items():   # legacy: {venue: {outcome: {...}}}, no side / obs_ts / refreshed
        if not isinstance(per, dict):
            continue
        for outcome, d in per.items():
            if isinstance(d, dict):
                out.append({**base, **d, "venue": venue, "outcome": outcome, "side": d.get("side") or "yes",
                            "book_id": d.get("book_id") or venue, "obs_ts": d.get("obs_ts"), "approx_time": 0 if d.get("obs_ts") else 1})
    for r in out:
        if r.get("obs_ts") is None:
            r["obs_ts"], r["approx_time"] = r["tick_ts"], 1
    return out


def load_db(path: str, event_keys: Optional[Iterable[str]] = None, date: Optional[str] = None) -> dict[str, Any]:
    """Read-only load of rows, ESPN states, prints and finals for some games (or an ET date
    as it appears in the event keys, e.g. ``2026-09-20``)."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    keys = list(event_keys or [])
    if not keys and date:
        keys = [r[0] for r in con.execute("select distinct event_key from inplay_ticks where event_key like ?", (f"%:{date}%",))]
    q = ",".join("?" * len(keys)) or "''"
    cols = {r[1] for r in con.execute("pragma table_info(inplay_ticks)")}
    src = "source" if "source" in cols else "null as source"
    rows: list[dict[str, Any]] = []
    for t in con.execute(f"select ts, event_key, live, l1_json, {src} from inplay_ticks where event_key in ({q}) and l1_json is not null order by ts", keys):
        rows.extend(rows_from_tick(dict(t)))
    games = sorted({_game_key(k) for k in keys})
    gq = ",".join("?" * len(games)) or "''"
    espn = [dict(r) for r in con.execute(f"select ts, event_key, status, period, clock, home, away, home_score, away_score, last_play_type from espn_ticks where event_key in ({gq}) order by ts", games)]
    prints: list[dict[str, Any]] = []
    if con.execute("select 1 from sqlite_master where name='trade_prints'").fetchone():
        tickers = sorted({str(r.get("venue_market_id") or "").split("#", 1)[0] for r in rows if r.get("book_id") == "kalshi"})
        if tickers:
            tq = ",".join("?" * len(tickers))
            prints = [dict(r) for r in con.execute(f"select ticker, ts, price, count, taker_side from trade_prints where ticker in ({tq})", tickers)]
    finals = {}
    for g in games:
        r = con.execute("select home, away, home_score, away_score from espn_ticks where event_key = ? and status = 'final' order by ts desc limit 1", (g,)).fetchone()
        if r and r["home_score"] is not None and r["away_score"] is not None:
            finals[g] = (r["home"], r["away"], r["home"] if r["home_score"] > r["away_score"] else r["away"] if r["away_score"] > r["home_score"] else None)
    con.close()
    return {"rows": rows, "espn": espn, "prints": prints, "finals": finals, "event_keys": keys}


def settlement_values(rows: Iterable[dict[str, Any]], finals: dict[str, tuple]) -> dict[tuple, float]:
    """(event_key, outcome, side) -> settlement dollars for moneyline contracts of finished
    games. A tie is valued only when the row carries its own tie payout."""
    out: dict[tuple, float] = {}
    for r in rows:
        ev = str(r.get("event_key") or "")
        if ev != _game_key(ev) or ev not in finals:
            continue
        _, _, winner = finals[ev]
        side = side_of(r)
        if winner is None:
            tp = _f(r.get("tie_payout"))
            if tp is None:
                continue
            out[(ev, r.get("outcome"), side)] = tp
        else:
            yes = 1.0 if r.get("outcome") == winner else 0.0
            out[(ev, r.get("outcome"), side)] = yes
    return out


def series_by_contract(rows: Iterable[dict[str, Any]]) -> dict[tuple, list[dict[str, Any]]]:
    """Deduplicated observations per contract identity, time-ordered, each with its obs_ts."""
    out: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for t, _, r in _dedupe(rows):
        out[contract_key(r)].append(dict(r, obs_ts=t))
    return dict(out)


def tie_value(row: dict[str, Any]) -> Optional[float]:
    """What this contract pays on a tie, from the settlement registry (None when unknown)."""
    if row.get("tie_payout") is not None:
        return _f(row.get("tie_payout"))
    try:
        from ..matching.settlement_rules import rule_for_quote
        from ..models import OutcomeQuote

        ev = str(row.get("event_key") or "")
        mtype = "spread" if ":spread:" in ev else ("total" if ":total:" in ev else "moneyline")
        meta = {"exchange": row.get("exchange")} if row.get("exchange") else {}
        if row.get("side"):
            meta["side"] = row["side"]
        q = OutcomeQuote(str(row.get("venue")), str(row.get("venue_market_id") or ""), ev, str(row.get("outcome") or ""),
                         fee_params=dict(row.get("fee_params") or {}), meta=meta, book_id=str(row.get("book_id") or row.get("venue")))
        tv = {"half": 0.5, "no_winner": 0.0}.get((rule_for_quote(q, ev.split(":", 1)[0].lower(), mtype) or {}).get("tie"))
    except Exception:
        return None
    return (1.0 - tv) if tv is not None and side_of(row) == "no" else tv

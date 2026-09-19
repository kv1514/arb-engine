"""SQLite recorder for scans, in-play ticks and STEAL observations (``--record``), so the
STEAL signal, arb frequency, venue lag and the feed gates can be measured after the fact
instead of guessed.

Tables (all appended, never rewritten; ``Store.__init__`` migrates older files in place with
``PRAGMA table_info`` + ``ALTER TABLE ADD COLUMN`` so a history file survives every schema
step of the plan):

* ``scans`` / ``quotes`` — one row per event / per venue quote of a scan (arb frequency).
* ``inplay_ticks`` — one row per priced game per poll: the blended numbers for the home
  side plus per-venue L1 for *both* outcomes (``<venue>_<home|away>_<bid|ask|bid_size|
  ask_size|quote_time>``, ``<venue>_book_id``) for kalshi / polymarket / robinhood, the
  complete L1 (any venue, fee params included) as ``l1_json``, the feed-freshness dict as
  ``freshness_json``, ``gated_reasons`` and the ESPN ``state_hash`` — enough to rebuild a
  ``MergedEvent`` per tick and replay it (``arb_engine.tickreplay``).
* ``espn_ticks`` — one row per game per poll of the ESPN state: status / period / clock /
  scores / last play / ESPN WP, the StateGuard flags (``suspect``, ``review_pending``,
  ``state_source``) and the raw situation, so score reversals and review windows can be
  counted (``quant.eventstudy.reversal_episodes``).
* ``steal_observations`` — every STEAL (and every *gated* would-be STEAL) with the entry
  price and an observation ladder filled in later by ``update_ladder``: the venue's bid and
  mid at +10 s / +60 s / +300 s / +900 s, the last in-play bid, and the settlement P&L once
  the ESPN feed shows the game final. ``convergence`` turns the ladder into toward/away
  ratios and CLV per edge bucket.
* ``pregame_lines`` — one row per event: the last pre-kickoff sportsbook moneylines and the
  Kalshi mid, the CLV anchor for the maker journal (idempotent per event).

``record_tick(view, quotes_by_venue=None, freshness=None)`` takes the plain dicts the live
scanner already has, so this module imports nothing from the strategy package: ``freshness``
is ``{"last_state_change_ts": float|None, "last_score_change_ts": float|None,
"mids": {venue: {outcome: mid}}}`` (any extra keys are kept in ``freshness_json``).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict, is_dataclass
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
  ts REAL, sport TEXT, event_key TEXT, market_type TEXT, title TEXT, venues TEXT,
  gross_sum REAL, margin REAL, fillable INTEGER, live INTEGER, sized_contracts REAL, sized_profit REAL, flags TEXT,
  start_time TEXT
);
CREATE INDEX IF NOT EXISTS scans_event ON scans(event_key, ts);
CREATE TABLE IF NOT EXISTS quotes (
  ts REAL, event_key TEXT, outcome TEXT, venue TEXT, exchange TEXT, ask REAL, bid REAL, ask_size REAL, all_in REAL, max_buy REAL, max_buy_maker REAL,
  bid_size REAL, venue_ts REAL, is_mm INTEGER
);
CREATE INDEX IF NOT EXISTS quotes_event ON quotes(event_key, ts);
CREATE TABLE IF NOT EXISTS inplay_ticks (
  ts REAL, event_key TEXT, live INTEGER, game_line TEXT, home_score INTEGER, away_score INTEGER, period INTEGER,
  model_p REAL, market_p REAL, espn_p REAL, blend_p REAL, disagreement REAL, actions TEXT, view TEXT
);
CREATE INDEX IF NOT EXISTS ticks_event ON inplay_ticks(event_key, ts);
CREATE TABLE IF NOT EXISTS espn_ticks (
  ts REAL, event_key TEXT, event_id TEXT, home TEXT, away TEXT, status TEXT, period INTEGER, clock INTEGER,
  home_score INTEGER, away_score INTEGER, last_play_id TEXT, last_play_type TEXT, last_play_text TEXT,
  espn_home_wp REAL, suspect INTEGER, review_pending INTEGER, state_hash TEXT, state_source TEXT, situation_json TEXT
);
CREATE INDEX IF NOT EXISTS espn_ticks_event ON espn_ticks(event_key, ts);
CREATE TABLE IF NOT EXISTS steal_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, event_key TEXT, outcome TEXT, venue TEXT,
  ask REAL, bid REAL, all_in REAL, fair REAL, edge REAL, model_p REAL, market_p REAL, espn_p REAL,
  gated INTEGER, gated_reasons TEXT, suggested_contracts REAL, state_hash TEXT, period INTEGER, extra_json TEXT,
  last_bid REAL, last_mid REAL, last_ts REAL, settled INTEGER DEFAULT 0, settle_value REAL, pnl_settle REAL
);
CREATE INDEX IF NOT EXISTS steal_event ON steal_observations(event_key, ts);
CREATE TABLE IF NOT EXISTS pregame_lines (
  event_key TEXT PRIMARY KEY, ts REAL, sportsbook_ml_home REAL, sportsbook_ml_away REAL, kalshi_mid REAL
);
"""

# Venues with flat L1 columns on inplay_ticks (others still land in l1_json).
L1_VENUES = ("kalshi", "polymarket", "robinhood")
L1_FIELDS = ("bid", "ask", "bid_size", "ask_size", "quote_time")
LADDER_OFFSETS = (10, 60, 300, 900)
EDGE_BUCKETS = ((0.03, 0.05, "3-5%"), (0.05, 0.08, "5-8%"), (0.08, 10.0, "8%+"))


def _l1_columns() -> dict[str, str]:
    cols: dict[str, str] = {}
    for v in L1_VENUES:
        for side in ("home", "away"):
            for f in L1_FIELDS:
                cols[f"{v}_{side}_{f}"] = "REAL"
        cols[f"{v}_book_id"] = "TEXT"
    return cols


TICK_EXTRA_COLUMNS = {**_l1_columns(), "home": "TEXT", "away": "TEXT", "gated_reasons": "TEXT", "state_hash": "TEXT", "freshness_json": "TEXT", "l1_json": "TEXT"}


def _ladder_columns(offsets: Iterable[int]) -> dict[str, str]:
    cols: dict[str, str] = {}
    for off in offsets:
        cols[f"bid_{int(off)}"] = "REAL"
        cols[f"ask_{int(off)}"] = "REAL"
        cols[f"mid_{int(off)}"] = "REAL"
        cols[f"ts_{int(off)}"] = "REAL"
    return cols


def _mid(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    """Mid when both sides are quoted, else whichever side exists (None when neither)."""
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return bid if bid is not None else ask


def state_hash(gs: Any) -> Optional[str]:
    """Short digest of the parts of an ESPN state that define 'the game moved on' (status,
    period, clock, scores, possession, down/distance/yardline, timeouts). Two polls with the
    same hash saw the same game state, whatever the venues did in between."""
    if gs is None:
        return None
    g = _get(gs)
    keys = ("status", "period", "clock_seconds_remaining_in_period", "home_score", "away_score", "possession", "down", "distance", "yardline_100", "home_timeouts", "away_timeouts")
    payload = json.dumps([g(k) for k in keys], default=str)
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def _get(obj: Any):
    """Uniform attribute/key access for dataclasses, objects and dicts."""
    if isinstance(obj, dict):
        return lambda k, d=None: obj.get(k, d)
    return lambda k, d=None: getattr(obj, k, d)


def _f(x: Any) -> Optional[float]:
    try:
        return float(x) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None


class Store:
    def __init__(self, path: str = "out/history.db"):
        import os

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # Older files predate these columns; add them in place (SQLite appends, defaults NULL).
        self._ensure_columns("scans", {"start_time": "TEXT"})
        self._ensure_columns("quotes", {"bid_size": "REAL", "venue_ts": "REAL", "is_mm": "INTEGER"})
        self._ensure_columns("inplay_ticks", TICK_EXTRA_COLUMNS)
        self._ensure_columns("steal_observations", _ladder_columns(LADDER_OFFSETS))

    # ---- schema helpers ------------------------------------------------------------------
    def columns(self, table: str) -> list[str]:
        return [r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")]

    def _ensure_columns(self, table: str, cols: dict[str, str]) -> list[str]:
        have = set(self.columns(table))
        added = []
        with self.conn:
            for name, typ in cols.items():
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
                    added.append(name)
        return added

    def _insert(self, table: str, row: dict[str, Any]) -> int:
        keys = list(row)
        cur = self.conn.execute(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", [row[k] for k in keys])
        return int(cur.lastrowid or 0)

    # ---- scans ---------------------------------------------------------------------------
    def record_scan(self, result: Any) -> int:
        """Persist a ScanResult (every event + every venue quote). Returns rows written."""
        ts = float(getattr(result, "fetched_at", None) or time.time())
        n = 0
        with self.conn:
            for ev in result.events:
                sized = ev.sized_arb or {}
                self.conn.execute("INSERT INTO scans (ts, sport, event_key, market_type, title, venues, gross_sum, margin, fillable, live, sized_contracts, sized_profit, flags, start_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (ts, result.sport, ev.event_key, ev.market_type, ev.title, ",".join(ev.venues), ev.gross_sum, ev.margin, int(ev.fillable), int(ev.live), sized.get("contracts"), sized.get("profit"), ",".join(ev.flags), ev.start_time))
                for o in ev.outcomes:
                    for v in o.venues:
                        # bid_size / venue_ts / is_mm are optional on the scanner's VenuePrice
                        # (later items add them); age_s gives the venue timestamp meanwhile.
                        age = getattr(v, "age_s", None)
                        venue_ts = getattr(v, "venue_ts", None)
                        if venue_ts is None and age is not None:
                            venue_ts = ts - age
                        is_mm = getattr(v, "is_mm", None)
                        self.conn.execute("INSERT INTO quotes (ts, event_key, outcome, venue, exchange, ask, bid, ask_size, all_in, max_buy, max_buy_maker, bid_size, venue_ts, is_mm) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (ts, ev.event_key, o.outcome, v.venue, v.exchange, v.ask, v.bid, v.ask_size, v.all_in, v.max_buy_price, v.max_buy_maker, getattr(v, "bid_size", None), venue_ts, None if is_mm is None else int(bool(is_mm))))
                        n += 1
                n += 1
        return n

    # ---- in-play ticks ---------------------------------------------------------------------
    @staticmethod
    def l1_from_quotes(quotes_by_venue: Any) -> dict[str, dict[str, dict[str, Any]]]:
        """``{venue: [OutcomeQuote]}`` (or ``{venue: {outcome: quote}}``) -> plain nested dict
        ``{venue: {outcome: {bid, ask, bid_size, ask_size, quote_time, book_id, venue_market_id,
        fee_params, exchange}}}`` — everything the tick replay needs to rebuild the quote."""
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for venue, qs in (quotes_by_venue or {}).items():
            items = qs.values() if isinstance(qs, dict) else (qs if isinstance(qs, (list, tuple)) else [qs])
            for q in items:
                g = _get(q)
                outcome = g("outcome")
                if outcome is None:
                    continue
                meta = g("meta") or {}
                fee_params = dict(g("fee_params") or {})
                out.setdefault(venue, {})[outcome] = {
                    "bid": _f(g("bid")), "ask": _f(g("ask")), "bid_size": _f(g("bid_size")), "ask_size": _f(g("ask_size")), "quote_time": _f(g("quote_time")),
                    "book_id": g("book_id") or venue, "venue_market_id": g("venue_market_id"), "fee_params": fee_params,
                    "exchange": (meta.get("exchange") if isinstance(meta, dict) else None) or fee_params.get("exchange"),
                }
        return out

    def _tick_row(self, ts: float, event_key: str, live: Optional[bool], game_state: Any, l1: dict[str, dict[str, dict[str, Any]]], home: Optional[str], away: Optional[str], freshness: Any = None, gated_reasons: Any = None) -> dict[str, Any]:
        g = _get(game_state) if game_state is not None else (lambda k, d=None: d)
        row: dict[str, Any] = {"ts": ts, "event_key": event_key, "live": None if live is None else int(live), "home_score": g("home_score"), "away_score": g("away_score"), "period": g("period"), "home": home, "away": away, "state_hash": state_hash(game_state) if game_state is not None else None, "l1_json": json.dumps(l1, default=str) if l1 else None, "freshness_json": json.dumps(freshness, default=str) if freshness is not None else None}
        if gated_reasons:
            row["gated_reasons"] = ",".join(sorted({str(r) for r in gated_reasons})) if not isinstance(gated_reasons, str) else gated_reasons
        for v in L1_VENUES:
            per = l1.get(v) or {}
            row[f"{v}_book_id"] = next((d.get("book_id") for d in per.values() if d.get("book_id")), None)
            for side, outcome in (("home", home), ("away", away)):
                d = per.get(outcome) if outcome is not None else None
                for f in L1_FIELDS:
                    row[f"{v}_{side}_{f}"] = d.get(f) if d else None
        return row

    def record_tick(self, view: Any, quotes_by_venue: Any = None, freshness: Any = None, ts: Optional[float] = None) -> int:
        """One row per priced game per poll. ``view`` is an ``InplayView`` (or a dict of one);
        ``quotes_by_venue`` is the merged event's ``{venue: [OutcomeQuote]}`` for the per-venue
        L1 columns; ``freshness`` the plain dict described in the module docstring."""
        gv = _get(view)
        gs = gv("game_state") or {}
        sides = list(gv("sides") or [])
        sd = [asdict(s) if is_dataclass(s) else dict(s) for s in sides]
        home = gs.get("home") if isinstance(gs, dict) else getattr(gs, "home", None)
        away = gs.get("away") if isinstance(gs, dict) else getattr(gs, "away", None)
        outcomes = [s["outcome"] for s in sd]
        if home not in outcomes or away not in outcomes:
            # No usable state: mirror evaluate_inplay's bookkeeping (first outcome = "home").
            home, away = (outcomes[0] if outcomes else None), (outcomes[1] if len(outcomes) > 1 else None)
        side = next((s for s in sd if s["outcome"] == home), sd[0] if sd else None)
        gated = sorted({r for s in sd for r in (s.get("gated_reasons") or [])})
        l1 = self.l1_from_quotes(quotes_by_venue)
        row = self._tick_row(ts or time.time(), gv("event_key"), bool(gv("live")), gs or None, l1, home, away, freshness, gated)
        row.update({"game_line": gv("game_line"), "model_p": side.get("model_p") if side else None, "market_p": side.get("market_p") if side else None, "espn_p": side.get("espn_p") if side else None, "blend_p": side.get("fair") if side else None, "disagreement": gv("disagreement"), "actions": "\n".join(gv("actions") or []), "view": json.dumps(asdict(view) if is_dataclass(view) else view, default=str)})
        with self.conn:
            return self._insert("inplay_ticks", row)

    def record_l1(self, ts: float, event_key: str, quotes_by_venue: Any, game_state: Any = None, home: Optional[str] = None, away: Optional[str] = None, live: Optional[bool] = None, freshness: Any = None) -> int:
        """L1-only tick (no evaluation): what a recorder writes when it has quotes and a game
        state but ran no strategy — also how fixtures are loaded for the tick replay."""
        g = _get(game_state) if game_state is not None else (lambda k, d=None: d)
        home, away = home or g("home"), away or g("away")
        l1 = self.l1_from_quotes(quotes_by_venue)
        if home is None or away is None:
            outs = sorted({o for per in l1.values() for o in per})
            home, away = home or (outs[0] if outs else None), away or (outs[1] if len(outs) > 1 else None)
        if live is None and game_state is not None:
            live = g("status") == "live"
        with self.conn:
            return self._insert("inplay_ticks", self._tick_row(ts, event_key, live, game_state, l1, home, away, freshness))

    def record_espn_tick(self, gs: Any, ts: Optional[float] = None, state_source: Optional[str] = None) -> int:
        """One row per game per poll of the ESPN state (GameState or its ``as_dict()``)."""
        g = _get(gs)
        situation = gs if isinstance(gs, dict) else (gs.as_dict() if hasattr(gs, "as_dict") else (asdict(gs) if is_dataclass(gs) else {}))
        flag = lambda k: None if g(k) is None else int(bool(g(k)))  # noqa: E731
        row = {"ts": ts or time.time(), "event_key": g("event_key"), "event_id": g("event_id"), "home": g("home"), "away": g("away"), "status": g("status"), "period": g("period"), "clock": g("clock_seconds_remaining_in_period"), "home_score": g("home_score"), "away_score": g("away_score"), "last_play_id": None if g("last_play_id") is None else str(g("last_play_id")), "last_play_type": g("last_play_type"), "last_play_text": g("last_play_text"), "espn_home_wp": _f(g("espn_home_wp")), "suspect": flag("suspect"), "review_pending": flag("review_pending"), "state_hash": state_hash(gs), "state_source": state_source or g("state_source"), "situation_json": json.dumps(situation, default=str)}
        with self.conn:
            return self._insert("espn_ticks", row)

    def espn_tick_rows(self, event_key: Optional[str] = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM espn_ticks" + (" WHERE event_key=?" if event_key else "") + " ORDER BY event_key, ts"
        return [dict(r) for r in self.conn.execute(q, (event_key,) if event_key else ())]

    def tick_rows(self, event_key: Optional[str] = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM inplay_ticks" + (" WHERE event_key=?" if event_key else "") + " ORDER BY event_key, ts"
        return [dict(r) for r in self.conn.execute(q, (event_key,) if event_key else ())]

    def event_keys(self, table: str = "inplay_ticks") -> list[str]:
        return [r[0] for r in self.conn.execute(f"SELECT DISTINCT event_key FROM {table} ORDER BY event_key")]

    def anomaly_counts(self) -> dict[str, Any]:
        """StateGuard anomalies and state-source reasons counted from espn_ticks."""
        out: dict[str, Any] = {"ticks": 0, "games": 0, "suspect": 0, "review_pending": 0, "by_source": {}, "episodes": {}}
        rows = self.espn_tick_rows()
        out["ticks"], out["games"] = len(rows), len({r["event_key"] for r in rows})
        out["suspect"] = sum(1 for r in rows if r.get("suspect"))
        out["review_pending"] = sum(1 for r in rows if r.get("review_pending"))
        for r in rows:
            src = r.get("state_source") or "scoreboard"
            out["by_source"][src] = out["by_source"].get(src, 0) + 1
        try:
            from .quant.eventstudy import reversal_episodes

            eps = reversal_episodes(rows)
            for e in eps:
                out["episodes"][e["kind"]] = out["episodes"].get(e["kind"], 0) + 1
        except Exception:
            pass
        return out

    # ---- STEAL observations + ladder -----------------------------------------------------
    def record_steal(self, ts: float, event_key: str, outcome: str, venue: str, ask: Optional[float], all_in: Optional[float], fair: Optional[float], edge: Optional[float] = None, model_p: Optional[float] = None, market_p: Optional[float] = None, espn_p: Optional[float] = None, gated: bool = False, gated_reasons: Any = None, suggested_contracts: Optional[float] = None, state_hash: Optional[str] = None, period: Optional[int] = None, bid: Optional[float] = None, **extra: Any) -> int:
        """One observation per STEAL (or GATED would-be STEAL) action. Returns the row id."""
        if edge is None and fair is not None and all_in is not None:
            edge = fair - all_in
        reasons = gated_reasons if isinstance(gated_reasons, str) or gated_reasons is None else ",".join(str(r) for r in gated_reasons)
        row = {"ts": ts, "event_key": event_key, "outcome": outcome, "venue": venue, "ask": ask, "bid": bid, "all_in": all_in, "fair": fair, "edge": edge, "model_p": model_p, "market_p": market_p, "espn_p": espn_p, "gated": int(bool(gated)), "gated_reasons": reasons, "suggested_contracts": suggested_contracts, "state_hash": state_hash, "period": period, "extra_json": json.dumps(extra, default=str) if extra else None, "settled": 0}
        with self.conn:
            return self._insert("steal_observations", row)

    def _tick_quote(self, tick: dict[str, Any], venue: str, outcome: str) -> tuple[Optional[float], Optional[float]]:
        """(bid, ask) of ``venue``/``outcome`` on a tick row: flat columns first, l1_json otherwise."""
        side = "home" if outcome == tick.get("home") else "away" if outcome == tick.get("away") else None
        if venue in L1_VENUES and side:
            b, a = tick.get(f"{venue}_{side}_bid"), tick.get(f"{venue}_{side}_ask")
            if b is not None or a is not None:
                return b, a
        try:
            d = (json.loads(tick.get("l1_json") or "{}").get(venue) or {}).get(outcome) or {}
        except json.JSONDecodeError:
            d = {}
        return d.get("bid"), d.get("ask")

    def quote_at_or_after(self, event_key: str, venue: str, outcome: str, t: float, max_ticks: int = 50) -> Optional[tuple[float, Optional[float], Optional[float]]]:
        """``(tick_ts, bid, ask)`` of the first tick at or after ``t`` that actually carries a
        quote for ``venue``/``outcome``. A poll where one adapter hiccupped still writes a tick
        (the other venues' L1), so taking the first tick regardless would leave that rung NULL
        for good; scanning forward (bounded, ~a few minutes at the live cadence) does not."""
        for tick in self.conn.execute("SELECT * FROM inplay_ticks WHERE event_key=? AND ts>=? ORDER BY ts LIMIT ?", (event_key, t, int(max_ticks))):
            bid, ask = self._tick_quote(dict(tick), venue, outcome)
            if bid is not None or ask is not None:
                return float(tick["ts"]), bid, ask
        return None

    def update_ladder(self, now: Optional[float] = None, offsets: Iterable[int] = LADDER_OFFSETS) -> int:
        """Fill every observation's ladder slot whose time has come (first tick at or after
        ``ts + offset`` that quotes the observation's venue), refresh the last in-play bid/mid,
        and settle observations of games the ESPN feed shows final. Returns the number of cells
        written."""
        now = now if now is not None else time.time()
        offsets = tuple(int(o) for o in offsets)
        self._ensure_columns("steal_observations", _ladder_columns(offsets))
        pending = [f"bid_{o} IS NULL" for o in offsets]
        obs = [dict(r) for r in self.conn.execute(f"SELECT * FROM steal_observations WHERE settled=0 OR {' OR '.join(pending)}")]
        if not obs:
            return 0
        finals = {r["event_key"]: r for r in (dict(x) for x in self.conn.execute("SELECT * FROM espn_ticks WHERE status='final' ORDER BY ts"))}
        written = 0
        with self.conn:
            for o in obs:
                sets: dict[str, Any] = {}
                for off in offsets:
                    if o.get(f"bid_{off}") is not None or now < o["ts"] + off:
                        continue
                    hit = self.quote_at_or_after(o["event_key"], o["venue"], o["outcome"], o["ts"] + off)
                    if hit is None:
                        continue
                    tick_ts, bid, ask = hit
                    sets[f"bid_{off}"], sets[f"ask_{off}"], sets[f"ts_{off}"], sets[f"mid_{off}"] = bid, ask, tick_ts, _mid(bid, ask)
                if not o.get("settled"):
                    last = self.conn.execute("SELECT * FROM inplay_ticks WHERE event_key=? AND ts<=? AND (live IS NULL OR live=1) ORDER BY ts DESC LIMIT 1", (o["event_key"], now)).fetchone()
                    if last is not None and last["ts"] >= o["ts"]:
                        bid, ask = self._tick_quote(dict(last), o["venue"], o["outcome"])
                        if bid is not None or ask is not None:
                            sets["last_bid"], sets["last_ts"], sets["last_mid"] = bid, last["ts"], _mid(bid, ask)
                    fin = finals.get(o["event_key"])
                    if fin is not None and fin["ts"] <= now:
                        value = self.settle_value(fin, o["outcome"])
                        if value is not None:
                            sets["settled"], sets["settle_value"] = 1, value
                            sets["pnl_settle"] = (value - o["all_in"]) if o.get("all_in") is not None else None
                if sets:
                    self.conn.execute(f"UPDATE steal_observations SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", [*sets.values(), o["id"]])
                    written += len(sets)
        return written

    @staticmethod
    def settle_value(final_tick: dict[str, Any], outcome: str) -> Optional[float]:
        hs, as_ = final_tick.get("home_score"), final_tick.get("away_score")
        if hs is None or as_ is None:
            return None
        if hs == as_:
            return 0.5
        winner = final_tick.get("home") if hs > as_ else final_tick.get("away")
        if winner is None:
            return None
        return 1.0 if outcome == winner else 0.0

    def settle_event(self, event_key: str, winner: Optional[str], now: Optional[float] = None) -> int:
        """Manual settlement (winner None = tie). Returns observations settled."""
        n = 0
        with self.conn:
            for o in self.conn.execute("SELECT id, outcome, all_in FROM steal_observations WHERE event_key=? AND settled=0", (event_key,)).fetchall():
                value = 0.5 if winner is None else (1.0 if o["outcome"] == winner else 0.0)
                self.conn.execute("UPDATE steal_observations SET settled=1, settle_value=?, pnl_settle=? WHERE id=?", (value, (value - o["all_in"]) if o["all_in"] is not None else None, o["id"]))
                n += 1
        return n

    def steal_rows(self, event_key: Optional[str] = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM steal_observations" + (" WHERE event_key=?" if event_key else "") + " ORDER BY ts"
        return [dict(r) for r in self.conn.execute(q, (event_key,) if event_key else ())]

    def convergence(self, min_games: int = 30, offsets: Iterable[int] = LADDER_OFFSETS, buckets: Iterable[tuple[float, float, str]] = EDGE_BUCKETS) -> dict[str, Any]:
        """Toward/away ratios and CLV per edge bucket (split by gated / ungated). Ratios and
        CLV are reported only for cells with at least ``min_games`` distinct games — ``n`` and
        ``n_games`` always — because a handful of Sunday games is not a convergence estimate.

        Toward/away compares like with like: entry mid vs rung mid when the entry bid was
        recorded, else entry ask vs rung ask (falling back to the mid when the rung has no
        ask). Comparing the entry ask with a later mid would call a market that never moved
        "away" by half the spread."""
        offsets = tuple(int(o) for o in offsets)
        rows = self.steal_rows()
        cells: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for r in rows:
            edge = r.get("edge")
            label = next((b[2] for b in buckets if edge is not None and b[0] <= edge < b[1]), "<3%" if edge is not None else "n/a")
            cells.setdefault((label, int(r.get("gated") or 0)), []).append(r)
        out: dict[str, Any] = {"min_games": min_games, "n": len(rows), "n_games": len({r["event_key"] for r in rows}), "complete_ladders": sum(1 for r in rows if all(r.get(f"bid_{o}") is not None for o in offsets)), "cells": []}
        order = [b[2] for b in buckets]
        for (label, gated), rs in sorted(cells.items(), key=lambda kv: (order.index(kv[0][0]) if kv[0][0] in order else -1, kv[0][1])):
            n_games = len({r["event_key"] for r in rs})
            cell: dict[str, Any] = {"bucket": label, "gated": bool(gated), "n": len(rs), "n_games": n_games, "n_settled": sum(1 for r in rs if r.get("settled")), "ok": n_games >= min_games}
            if n_games >= min_games:
                for off in offsets:
                    toward = away = 0
                    clv_bid, clv_mid = [], []
                    for r in rs:
                        mid, bid, ask = r.get(f"mid_{off}"), r.get(f"bid_{off}"), r.get(f"ask_{off}")
                        if r.get("all_in") is None or r.get("fair") is None:
                            continue
                        if mid is not None:
                            if r.get("bid") is not None and r.get("ask") is not None:
                                entry, later = (r["bid"] + r["ask"]) / 2.0, mid
                            else:
                                entry = r.get("ask") if r.get("ask") is not None else r["all_in"]
                                later = ask if ask is not None else mid
                            d_entry, d_now = r["fair"] - entry, r["fair"] - later
                            if abs(d_now) < abs(d_entry) - 1e-12:
                                toward += 1
                            elif abs(d_now) > abs(d_entry) + 1e-12:
                                away += 1
                            clv_mid.append(mid - r["all_in"])
                        if bid is not None:
                            clv_bid.append(bid - r["all_in"])
                    cell[f"toward_{off}"], cell[f"away_{off}"] = toward, away
                    cell[f"toward_away_ratio_{off}"] = round(toward / away, 3) if away else None  # counts carry the away=0 case
                    cell[f"clv_bid_{off}"] = round(sum(clv_bid) / len(clv_bid), 4) if clv_bid else None
                    cell[f"clv_mid_{off}"] = round(sum(clv_mid) / len(clv_mid), 4) if clv_mid else None
                pnl = [r["pnl_settle"] for r in rs if r.get("pnl_settle") is not None]
                cell["pnl_settle_mean"] = round(sum(pnl) / len(pnl), 4) if pnl else None
            out["cells"].append(cell)
        return out

    # ---- pre-game lines (CLV anchor) -----------------------------------------------------
    def record_pregame_line(self, event_key: str, sportsbook_ml_home: Optional[float] = None, sportsbook_ml_away: Optional[float] = None, kalshi_mid: Optional[float] = None, ts: Optional[float] = None) -> bool:
        """First pre-kickoff line per event wins (idempotent): returns True when inserted."""
        with self.conn:
            cur = self.conn.execute("INSERT OR IGNORE INTO pregame_lines (event_key, ts, sportsbook_ml_home, sportsbook_ml_away, kalshi_mid) VALUES (?,?,?,?,?)", (event_key, ts or time.time(), sportsbook_ml_home, sportsbook_ml_away, kalshi_mid))
            return cur.rowcount == 1

    def pregame_line(self, event_key: str) -> Optional[dict[str, Any]]:
        r = self.conn.execute("SELECT * FROM pregame_lines WHERE event_key=?", (event_key,)).fetchone()
        return dict(r) if r else None

    def clv_report(self, min_games: int = 30) -> dict[str, Any]:
        """Convergence ladder plus the pre-game anchor per event: entry all-in vs the pre-kickoff
        Kalshi mid (and the de-vigged sportsbook fair when ``quant.odds`` can price moneylines)."""
        conv = self.convergence(min_games=min_games)
        anchors = []
        book_fair = None
        try:
            from .quant.odds import sportsbook_probs_from_moneylines as book_fair  # P13, optional
        except Exception:
            book_fair = None
        for line in (dict(r) for r in self.conn.execute("SELECT * FROM pregame_lines ORDER BY ts")):
            obs = self.steal_rows(line["event_key"])
            fair_home = None
            if book_fair is not None and line.get("sportsbook_ml_home") is not None and line.get("sportsbook_ml_away") is not None:
                try:
                    fair_home = book_fair(line["sportsbook_ml_home"], line["sportsbook_ml_away"])
                    fair_home = fair_home[0] if isinstance(fair_home, (list, tuple)) else (fair_home.get("home") if isinstance(fair_home, dict) else fair_home)
                except Exception:
                    fair_home = None
            anchors.append({"event_key": line["event_key"], "kalshi_mid": line.get("kalshi_mid"), "sportsbook_p_home": fair_home, "observations": len(obs)})
        conv["pregame_anchors"] = anchors
        return conv

    # ---- arb frequency (unchanged) -------------------------------------------------------
    def arb_stats(self, sport: Optional[str] = None) -> dict[str, Any]:
        q = "SELECT market_type, COUNT(*), SUM(fillable), AVG(margin), MAX(margin) FROM scans" + (" WHERE sport=?" if sport else "") + " GROUP BY market_type"
        rows = self.conn.execute(q, (sport,) if sport else ()).fetchall()
        return {r[0]: {"events": r[1], "fillable_arbs": r[2] or 0, "avg_margin": r[3], "max_margin": r[4]} for r in rows}

    # Hours-to-kickoff buckets for the frequency question ("when do arbs exist?").
    BUCKETS = ((0, 1, "<1h"), (1, 6, "1-6h"), (6, 24, "6-24h"), (24, 72, "1-3d"), (72, 24 * 365, "3d+"))

    def arb_frequency(self, sport: Optional[str] = None, min_margin: float = 0.0) -> dict[str, Any]:
        """How often a fillable arb above ``min_margin`` was present, by market type and hours to
        kickoff: rows = scan snapshots of one event; an *episode* is a run of consecutive scans
        of the same event where the arb persisted (so duration = episodes' length in scans)."""
        from .matching.normalize import parse_iso

        q = "SELECT ts, event_key, market_type, margin, fillable, live, start_time, sized_profit FROM scans" + (" WHERE sport=?" if sport else "") + " ORDER BY event_key, ts"
        rows = self.conn.execute(q, (sport,) if sport else ()).fetchall()
        by_bucket: dict[str, dict[str, Any]] = {}
        episodes: list[dict[str, Any]] = []
        cur: dict[str, Any] = {}
        prev_key = None
        for ts, key, mtype, margin, fillable, live, start, profit in rows:
            st = parse_iso(start) if start else None
            hours = (st.timestamp() - ts) / 3600.0 if st else None
            label = next((b[2] for b in self.BUCKETS if hours is not None and b[0] <= hours < b[1]), "live/unknown" if live or hours is None else "3d+")
            b = by_bucket.setdefault(f"{mtype}:{label}", {"market_type": mtype, "bucket": label, "snapshots": 0, "arb_snapshots": 0, "max_margin": None, "profit_sum": 0.0})
            b["snapshots"] += 1
            is_arb = bool(fillable) and (margin or 0) > min_margin and not live
            if is_arb:
                b["arb_snapshots"] += 1
                b["max_margin"] = margin if b["max_margin"] is None else max(b["max_margin"], margin)
                b["profit_sum"] += profit or 0.0
            # Episodes: consecutive arb snapshots of the same event.
            if key != prev_key and cur:
                episodes.append(cur)
                cur = {}
            if is_arb:
                if cur and cur.get("event_key") == key:
                    cur["end"], cur["scans"], cur["max_margin"] = ts, cur["scans"] + 1, max(cur["max_margin"], margin)
                else:
                    if cur:
                        episodes.append(cur)
                    cur = {"event_key": key, "market_type": mtype, "start": ts, "end": ts, "scans": 1, "max_margin": margin, "bucket": label}
            elif cur and cur.get("event_key") == key:
                episodes.append(cur)
                cur = {}
            prev_key = key
        if cur:
            episodes.append(cur)
        for b in by_bucket.values():
            b["arb_share"] = round(b["arb_snapshots"] / b["snapshots"], 4) if b["snapshots"] else None
        return {"snapshots": len(rows), "buckets": sorted(by_bucket.values(), key=lambda b: (b["market_type"], [x[2] for x in self.BUCKETS].index(b["bucket"]) if b["bucket"] in [x[2] for x in self.BUCKETS] else 99)), "episodes": episodes, "episode_scans_mean": round(sum(e["scans"] for e in episodes) / len(episodes), 2) if episodes else None, "episode_seconds_mean": round(sum(e["end"] - e["start"] for e in episodes) / len(episodes), 1) if episodes else None}

    def close(self) -> None:
        self.conn.close()

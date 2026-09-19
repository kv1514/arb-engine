"""Replay recorded ticks through the in-play watcher: the P&L / CLV metric for the gates.

``arb-engine live --record`` stores, per poll, the ESPN state (``espn_ticks``) and every
venue's L1 for both outcomes (``inplay_ticks``). ``replay_ticks`` rebuilds a ``MergedEvent``
and a ``GameState`` per tick and feeds them through ``InplayWatcher`` — the same
``evaluate_inplay`` the live scanner runs — once with the feed gates on and once off, and
reports what differs: STEAL count, the venue's bid 60 s / 300 s after each STEAL (CLV_bid =
bid - all-in, i.e. what selling back would have made) and the settled P&L per contract.

Gates: when ``evaluate_inplay`` accepts a ``freshness`` kwarg (the in-play item's gates)
the watcher passes the recorded / recomputed freshness dict and the gate decisions are the
live code's. Until then this module applies the same contract itself (``fallback_gate``):
``feed-stale`` (no ESPN state change for > ``stale_after_s`` while a venue mid moved
>= 0.02), ``score-pending`` (a score change until ``last_play_id`` advances, or 20 s when
the feed has no play id), ``suspect`` / ``review-pending`` from the recorded flags. A gated
STEAL becomes ``GATED STEAL: ...`` and is not an observation.

Policies reported: ``every`` (each STEAL tick is one contract) and ``first`` (one contract
per outcome x venue, at the first STEAL). Cadence is whatever the recorder polled at; a
websocket feed would only change what is recorded, not this replay.
"""

from __future__ import annotations

import inspect
import json
import os
import tempfile
from bisect import bisect_left
from dataclasses import fields as dc_fields
from typing import Any, Iterable, Optional

from .matching.matcher import MergedEvent
from .models import EventInfo, OutcomeQuote
from .store import Store, state_hash
from .strategy.alerts import Alerter
from .strategy.inplay import InplayWatcher, evaluate_inplay
from .venues.espn import GameState

CLV_OFFSETS = (60, 300)
DEFAULT_KALSHI_FEES = {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}


# ---- fixtures ------------------------------------------------------------------------------

def load_fixture(store: Store, path: str) -> str:
    """Write a synthetic tick fixture into ``store`` and return its event key.

    Fixture: ``{"event_key", "sport", "home", "away", "ticks": [{"ts", "state": {GameState
    fields...}, "quotes": {venue: [{outcome, bid, ask, bid_size, ask_size, quote_time,
    book_id, fee_params}]}}]}``. ``state`` may carry the StateGuard flags (``suspect``,
    ``review_pending``, ``last_play_id``); they are recorded on ``espn_ticks``.
    """
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    key = doc["event_key"]
    for t in doc["ticks"]:
        st = dict(t.get("state") or {})
        st.setdefault("event_key", key)
        st.setdefault("home", doc.get("home"))
        st.setdefault("away", doc.get("away"))
        st.setdefault("sport", doc.get("sport", "nfl"))
        store.record_espn_tick(st, ts=float(t["ts"]), state_source=st.get("state_source"))
        store.record_l1(float(t["ts"]), key, t.get("quotes") or {}, game_state=st, home=st.get("home"), away=st.get("away"), live=st.get("status") == "live")
    return key


# ---- rebuilding the inputs -------------------------------------------------------------------

_GS_FIELDS = {f.name for f in dc_fields(GameState)}


def game_state_from(situation: Optional[dict[str, Any]]) -> Optional[GameState]:
    """``GameState`` from a recorded ``situation_json`` dict; keys the dataclass does not know
    (StateGuard flags, later items' fields) are set as attributes so gates can read them."""
    if not situation:
        return None
    from .matching.normalize import parse_iso

    known = {k: v for k, v in situation.items() if k in _GS_FIELDS}
    if isinstance(known.get("start_time"), str):
        known["start_time"] = parse_iso(known["start_time"])
    known.setdefault("event_id", str(situation.get("event_id") or ""))
    gs = GameState(**known)
    for k, v in situation.items():
        if k not in _GS_FIELDS and k != "score_diff_home":
            try:
                setattr(gs, k, v)
            except Exception:
                pass
    return gs


def merged_event_from(tick: dict[str, Any], sport: str = "nfl") -> Optional[MergedEvent]:
    """``MergedEvent`` from one ``inplay_ticks`` row (its ``l1_json``)."""
    try:
        l1 = json.loads(tick.get("l1_json") or "{}")
    except json.JSONDecodeError:
        return None
    home, away = tick.get("home"), tick.get("away")
    outcomes = sorted({o for per in l1.values() for o in per} | {o for o in (home, away) if o})
    if len(outcomes) != 2:
        return None
    key = tick["event_key"]
    sport = key.split(":", 1)[0] if ":" in key else sport
    info = EventInfo(event_key=key, sport=sport, market_type="moneyline", outcomes=outcomes, labels={o: o for o in outcomes}, in_play=bool(tick.get("live")))
    qbv: dict[str, list[OutcomeQuote]] = {}
    for venue, per in l1.items():
        for outcome, d in per.items():
            fee_params = dict(d.get("fee_params") or {})
            if venue == "kalshi" and not fee_params:
                fee_params = dict(DEFAULT_KALSHI_FEES)
            if venue == "robinhood" and d.get("exchange") and "exchange" not in fee_params:
                fee_params["exchange"] = d["exchange"]
            qbv.setdefault(venue, []).append(OutcomeQuote(venue=venue, venue_market_id=str(d.get("venue_market_id") or f"{venue}-{outcome}"), event_key=key, outcome=outcome, outcome_label=outcome, ask=d.get("ask"), bid=d.get("bid"), ask_size=d.get("ask_size"), bid_size=d.get("bid_size"), fee_params=fee_params, ts=float(tick["ts"]), meta={"exchange": d.get("exchange")} if d.get("exchange") else {}, book_id=d.get("book_id") or venue, quote_time=d.get("quote_time")))
    return MergedEvent(key, info, qbv)


# ---- freshness + fallback gate ----------------------------------------------------------------

class FreshnessTracker:
    """The plain freshness dict the gates consume, recomputed from the recorded sequence:
    ``{last_state_change_ts, last_score_change_ts, mids: {venue: {outcome: mid}},
    mids_at_state_change, last_play_id, score_changed_at, play_id_at_score, flags}``."""

    def __init__(self) -> None:
        self.last_hash: Optional[str] = None
        self.last_state_change_ts: Optional[float] = None
        self.last_score_change_ts: Optional[float] = None
        self.last_score: Optional[tuple[Any, Any]] = None
        self.play_id_at_score: Optional[str] = None
        self.mids_at_state_change: dict[str, dict[str, float]] = {}
        self.mids: dict[str, dict[str, float]] = {}

    @staticmethod
    def _mids(me: MergedEvent) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for v, qs in me.quotes_by_venue.items():
            for q in qs:
                if q.mid is not None:
                    out.setdefault(v, {})[q.outcome] = q.mid
        return out

    def update(self, ts: float, gs: Optional[GameState], me: MergedEvent) -> dict[str, Any]:
        h = state_hash(gs) if gs is not None else None
        mids = self._mids(me)
        if h != self.last_hash or self.last_state_change_ts is None:
            self.last_hash, self.last_state_change_ts, self.mids_at_state_change = h, ts, mids
        score = (gs.home_score, gs.away_score) if gs is not None else None
        if score != self.last_score:
            if self.last_score is not None:
                self.last_score_change_ts = ts
                self.play_id_at_score = str(getattr(gs, "last_play_id", None)) if getattr(gs, "last_play_id", None) is not None else None
            self.last_score = score
        self.mids = mids
        return {"last_state_change_ts": self.last_state_change_ts, "last_score_change_ts": self.last_score_change_ts, "mids": mids, "mids_at_state_change": self.mids_at_state_change, "last_play_id": getattr(gs, "last_play_id", None) if gs is not None else None, "play_id_at_score": self.play_id_at_score, "flags": {"suspect": bool(getattr(gs, "suspect", False)), "review_pending": bool(getattr(gs, "review_pending", False))} if gs is not None else {}}


def fallback_gate(ts: float, fresh: dict[str, Any], stale_after_s: float = 15.0, score_hold_s: float = 20.0, mid_move: float = 0.02) -> list[str]:
    """Gate reasons from a freshness dict (the contract in the module docstring)."""
    reasons: list[str] = []
    flags = fresh.get("flags") or {}
    if flags.get("suspect"):
        reasons.append("suspect")
    if flags.get("review_pending"):
        reasons.append("review-pending")
    lsc = fresh.get("last_state_change_ts")
    if lsc is not None and ts - lsc > stale_after_s:
        base, now = fresh.get("mids_at_state_change") or {}, fresh.get("mids") or {}
        if any(abs(now[v][o] - base[v][o]) >= mid_move for v in now for o in now[v] if v in base and o in base[v]):
            reasons.append("feed-stale")
    lsch = fresh.get("last_score_change_ts")
    if lsch is not None:
        pid, pid_at = fresh.get("last_play_id"), fresh.get("play_id_at_score")
        if pid is not None and pid_at is not None:
            if str(pid) == str(pid_at):
                reasons.append("score-pending")
        elif ts - lsch < score_hold_s:
            reasons.append("score-pending")
    return reasons


def _accepts(fn: Any, name: str) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


class RecordedWatcher(InplayWatcher):
    """``InplayWatcher`` over a recorded sequence: ``advance(tick)`` sets what ``fetch`` /
    ``fetch_state`` return, ``step`` adds the freshness kwarg (live gates) or the fallback."""

    def __init__(self, gates: bool = True, stale_after_s: float = 15.0, **kw: Any) -> None:
        self._me: Optional[MergedEvent] = None
        self._gs: Optional[GameState] = None
        self._ts: float = 0.0
        self.gates = gates
        self.stale_after_s = stale_after_s
        self.tracker = FreshnessTracker()
        self.fresh_dict: Optional[dict[str, Any]] = None  # the recomputed plain dict (fallback gate + reporting)
        self.gate_reasons: dict[str, int] = {}
        self.gated_actions: list[tuple[float, str]] = []
        super().__init__(fetch=lambda: self._me, fetch_state=lambda: self._gs, **kw)
        self.settings = dict(self.settings)
        self.settings.setdefault("inplay_stale_after_s", stale_after_s)
        # The in-play item's FeedFreshness (``self.freshness``, set by InplayWatcher) observes
        # at the recorded clock, so its thresholds apply to the tape exactly as they would live.
        live_fresh = getattr(self, "freshness", None)
        if live_fresh is not None and hasattr(live_fresh, "stale_after_s"):
            live_fresh.stale_after_s = stale_after_s

    def advance(self, ts: float, me: MergedEvent, gs: Optional[GameState]) -> None:
        self._ts, self._me, self._gs = ts, me, gs
        self.fresh_dict = self.tracker.update(ts, gs, me)

    def _live_gates(self) -> bool:
        return self.gates and _accepts(evaluate_inplay, "freshness") and hasattr(getattr(self, "freshness", None), "observe")

    def step(self, *a: Any, **k: Any):  # type: ignore[override]
        # Evaluate directly (not via InplayWatcher.step): the tape is the store, so nothing is
        # re-recorded, and alerts are journaled once, below.
        if self._live_gates():
            view = evaluate_inplay(self._me, self.lots, self.settings, self.steal_edge, self.target_margin, game_state=self._gs, model=self.model, blend_weights=self.blend_weights, bankroll=self.bankroll, kelly_fraction=self.kelly_fraction, freshness=self.freshness, now=self._ts)
            for r in getattr(view, "gated_reasons", None) or []:
                self.gate_reasons[r] = self.gate_reasons.get(r, 0) + 1
        else:
            view = evaluate_inplay(self._me, self.lots, self.settings, self.steal_edge, self.target_margin, game_state=self._gs, model=self.model, blend_weights=self.blend_weights, bankroll=self.bankroll, kelly_fraction=self.kelly_fraction)
            if self.gates:
                self._apply_fallback(view)
        for a in view.actions:
            key = a.split("(")[0]
            if key not in self.last_actions and (a.startswith("LOCK NOW") or a.startswith("STEAL")):
                self.alerts.alert(a.split(":")[0], a, event=view.event_key)
            elif a.startswith("GATED"):
                self.gated_actions.append((self._ts, a))
        self.last_actions = {a.split("(")[0] for a in view.actions}
        return view

    def _apply_fallback(self, view: Any) -> None:
        reasons = fallback_gate(self._ts, self.fresh_dict or {}, stale_after_s=self.stale_after_s)
        if not reasons or not view.live:
            return
        for r in reasons:
            self.gate_reasons[r] = self.gate_reasons.get(r, 0) + 1
        actions = []
        for a in view.actions:
            if a.startswith("STEAL") or a.startswith("LOCK NOW"):
                actions.append("GATED " + a)
                actions.append(f"wait: {', '.join(reasons)}")
            else:
                actions.append(a)
        view.actions = actions
        for sv in view.sides:
            if getattr(sv, "steal", False):
                sv.steal = False
                setattr(sv, "gated_reasons", list(reasons))
            sv.lock_available = False


# ---- the replay --------------------------------------------------------------------------------

def resolve_event(store: Store, game: Optional[str]) -> str:
    keys = store.event_keys("inplay_ticks")
    if not keys:
        raise ValueError("no inplay_ticks in this file")
    if game is None:
        if len(keys) == 1:
            return keys[0]
        raise ValueError("several games recorded; pass --game: " + ", ".join(keys))
    if game in keys:
        return game
    hits = [k for k in keys if game.lower() in k.lower()]
    if len(hits) != 1:
        raise ValueError(f"--game {game!r} matches {hits or 'nothing'}; recorded: {', '.join(keys)}")
    return hits[0]


def _bid_at(store: Store, ticks: list[dict[str, Any]], event_key: str, venue: str, outcome: str, t: float, max_ticks: int = 50) -> Optional[float]:
    """Bid of the first tick at or after ``t`` that quotes ``venue``/``outcome`` — not the
    first tick regardless, which would score a rung as missing whenever one poll lost that
    venue's L1 (same rule as ``Store.quote_at_or_after``)."""
    i = bisect_left([tk["ts"] for tk in ticks], t)
    for tk in ticks[i : i + max_ticks]:
        bid, ask = store._tick_quote(tk, venue, outcome)
        if bid is not None or ask is not None:
            return bid
    return None


def replay_ticks(db: Any, game: Optional[str] = None, gates: bool = True, steal_edge: float = 0.03, settings: Optional[dict[str, Any]] = None, offsets: Iterable[int] = CLV_OFFSETS, stale_after_s: float = 15.0, model: Any = None, sport: str = "nfl") -> dict[str, Any]:
    """Feed one recorded game through the watcher; ``db`` is a path or an open ``Store``
    (a path is opened and closed here)."""
    if not isinstance(db, Store):
        store = Store(db)
        try:
            return replay_ticks(store, game, gates=gates, steal_edge=steal_edge, settings=settings, offsets=offsets, stale_after_s=stale_after_s, model=model, sport=sport)
        finally:
            store.close()
    store = db
    key = resolve_event(store, game)
    ticks = store.tick_rows(key)
    espn = store.espn_tick_rows(key)
    offsets = tuple(int(o) for o in offsets)
    journal = os.path.join(tempfile.gettempdir(), "arb_tickreplay_journal.jsonl")
    watcher = RecordedWatcher(gates=gates, stale_after_s=stale_after_s, lots=[], alerter=Alerter(journal_path=journal, quiet=True, desktop=False), settings=settings or {}, steal_edge=steal_edge, model=model)
    observations: list[dict[str, Any]] = []
    views = 0
    last_state: Optional[GameState] = None
    ei = 0
    for tk in ticks:
        while ei < len(espn) and espn[ei]["ts"] <= tk["ts"]:
            try:
                last_state = game_state_from(json.loads(espn[ei].get("situation_json") or "{}"))
            except (json.JSONDecodeError, TypeError):
                pass
            ei += 1
        gs = last_state
        if gs is None and tk.get("view"):
            try:
                gs = game_state_from((json.loads(tk["view"]) or {}).get("game_state"))
            except (json.JSONDecodeError, TypeError):
                gs = None
        me = merged_event_from(tk, sport=sport)
        if me is None:
            continue
        watcher.advance(float(tk["ts"]), me, gs)
        view = watcher.step()
        views += 1
        for sv in view.sides:
            if getattr(sv, "steal", False) and sv.best_venue and sv.best_all_in is not None:
                observations.append({"ts": float(tk["ts"]), "outcome": sv.outcome, "venue": sv.best_venue, "ask": sv.best_ask, "all_in": sv.best_all_in, "fair": sv.fair, "edge": sv.steal_edge, "period": getattr(gs, "period", None) if gs else None})
    # Outcome of the game for settlement: the last final ESPN state, else the last state seen.
    final = next((e for e in reversed(espn) if e.get("status") == "final"), None)
    winner: Optional[str] = None
    settled = False
    if final is not None and final.get("home_score") is not None and final.get("away_score") is not None:
        settled = True
        winner = None if final["home_score"] == final["away_score"] else (final["home"] if final["home_score"] > final["away_score"] else final["away"])
    for o in observations:
        for off in offsets:
            bid = _bid_at(store, ticks, key, o["venue"], o["outcome"], o["ts"] + off)
            o[f"bid_{off}"] = bid
            o[f"clv_bid_{off}"] = round(bid - o["all_in"], 4) if bid is not None else None
        if settled:
            value = 0.5 if winner is None else (1.0 if o["outcome"] == winner else 0.0)
            o["settle_value"], o["pnl_settle"] = value, round(value - o["all_in"], 4)
    policies = {"every": observations, "first": list({(o["outcome"], o["venue"]): o for o in reversed(observations)}.values())}
    per_policy: dict[str, Any] = {}
    for name, obs in policies.items():
        obs = sorted(obs, key=lambda o: o["ts"])
        d: dict[str, Any] = {"n": len(obs)}
        for off in offsets:
            xs = [o[f"clv_bid_{off}"] for o in obs if o.get(f"clv_bid_{off}") is not None]
            d[f"clv_bid_{off}_mean"], d[f"clv_bid_{off}_n"] = (round(sum(xs) / len(xs), 4) if xs else None), len(xs)
        pnl = [o["pnl_settle"] for o in obs if o.get("pnl_settle") is not None]
        d["pnl_settle_sum"], d["pnl_settle_mean"] = (round(sum(pnl), 4) if pnl else None), (round(sum(pnl) / len(pnl), 4) if pnl else None)
        per_policy[name] = d
    return {"event_key": key, "gates": gates, "ticks": len(ticks), "views": views, "espn_ticks": len(espn), "steals": len(observations), "gated": len(watcher.gated_actions), "gate_reasons": dict(watcher.gate_reasons), "gate_source": "evaluate_inplay" if watcher._live_gates() or (gates and _accepts(evaluate_inplay, "freshness")) else "fallback", "settled": settled, "winner": winner, "policies": per_policy, "observations": observations}


def replay_both(db: Any, game: Optional[str] = None, **kw: Any) -> dict[str, dict[str, Any]]:
    """Gates on and off over the same recording; a path is opened once and closed here."""
    if not isinstance(db, Store):
        store = Store(db)
        try:
            return replay_both(store, game, **kw)
        finally:
            store.close()
    return {"on": replay_ticks(db, game, gates=True, **kw), "off": replay_ticks(db, game, gates=False, **kw)}


def format_replay(results: dict[str, dict[str, Any]], offsets: Iterable[int] = CLV_OFFSETS) -> str:
    offsets = tuple(int(o) for o in offsets)
    any_r = next(iter(results.values()))
    lines = [f"{any_r['event_key']}: {any_r['ticks']} ticks, {any_r['espn_ticks']} ESPN states, gate source {any_r['gate_source']}" + (f", winner {any_r['winner'] or 'tie'}" if any_r["settled"] else ", not settled")]
    lines.append("  gates  policy   steals  gated  " + "  ".join(f"CLV_bid+{o}s" for o in offsets) + "   P&L/contract   P&L sum")
    for name, r in results.items():
        for pol, d in r["policies"].items():
            clv = "  ".join(f"{d[f'clv_bid_{o}_mean']:+.4f}({d[f'clv_bid_{o}_n']})" if d[f"clv_bid_{o}_mean"] is not None else f"{'-':>12}" for o in offsets)
            pnl = f"{d['pnl_settle_mean']:+.4f}" if d["pnl_settle_mean"] is not None else "   -   "
            tot = f"{d['pnl_settle_sum']:+.3f}" if d["pnl_settle_sum"] is not None else "  -  "
            lines.append(f"  {name:<6} {pol:<8} {d['n']:>6}  {r['gated']:>5}  {clv}   {pnl:>12}   {tot}")
        if r["gate_reasons"]:
            lines.append("         gate reasons: " + ", ".join(f"{k} x{v}" for k, v in sorted(r["gate_reasons"].items())))
    return "\n".join(lines)

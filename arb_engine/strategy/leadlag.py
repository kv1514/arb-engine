"""Lead-lag signals: buy the *lagging* venue when the leading one has already repriced.

Measured on the first recorded NFL Sunday (2026-09-20, 13 games, 5 s ticks — see
``scripts/leadlag_study.py`` and docs/MODEL.md): when Robinhood/Rothera's mid moved ≥ 5¢ in
play, Kalshi had moved first only 32/132 times and caught up within a median 25 s (105/132
within five minutes); the reverse (Kalshi first) happened 21/120 times. Polymarket trailed
both by minutes. Big moves *continued* over the next five minutes on average (+21 % of the
initial move), i.e. the market under-reacts to drives — so "buy the dip" against the move
loses, while buying the side the leader just repriced *on the venue that has not moved yet*
is the dip that is actually cheap. That is this module.

The rule is market-vs-market, so it needs no ESPN state and none of the feed gates: the
leader's quote must be fresh (its venue-reported ``quote_time`` within ``fresh_s``, when
the venue reports one), the follower's mid must not have moved ≥ ``follow_fraction`` of the
leader's move in the same direction over the same window, and the follower's all-in ask on
the side the leader moved toward must sit ≥ ``min_edge`` below the leader's mid. The
follower must be executable for this account; the leader may be any venue (Polymarket's
print is a fine signal even though the account cannot trade there).

Each signal is journalled as an observation through ``store.record_steal`` with
``kind="lag"`` so the +10 s … +15 min ladder measures convergence, exactly as for STEAL —
today's convergence rate is the number the next Sunday should update. Sizing is by the
follower's displayed depth and a fraction of bankroll (``bankroll * kelly_fraction / ask``),
which is deliberately conservative: a convergence trade's payoff is the gap, not $1.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Iterable, Optional

from ..fees.registry import fee_model_for_quote
from ..models import OutcomeQuote

try:  # settings registry (config.declare_setting); the module must import without it
    from ..config import declare_setting as _declare_setting  # type: ignore
except Exception:  # pragma: no cover
    _declare_setting = None

SETTINGS = {
    "leadlag_move": ("LEADLAG_MOVE", 0.05, float, "lead-lag: leader mid move (dollars) within the window that counts as a repricing"),
    "leadlag_window_s": ("LEADLAG_WINDOW_S", 30.0, float, "lead-lag: seconds over which the leader's move and the follower's (non-)move are measured"),
    "leadlag_min_edge": ("LEADLAG_MIN_EDGE", 0.02, float, "lead-lag: minimum leader mid minus follower all-in ask to signal"),
    "leadlag_cooldown_s": ("LEADLAG_COOLDOWN_S", 60.0, float, "lead-lag: seconds before the same (event, follower, side) may signal again unless the edge grew"),
    "leadlag_leaders": ("LEADLAG_LEADERS", "robinhood,kalshi", str, "lead-lag: comma list of venues whose repricing may lead a signal (Polymarket is thin and sometimes stale; measure before adding it)"),
}
if _declare_setting is not None:
    for _k, (_env, _default, _cast, _doc) in SETTINGS.items():
        try:
            _declare_setting(_k, env=_env, default=_default, cast=_cast, doc=_doc)
        except Exception:  # pragma: no cover
            pass


def _setting(settings: Optional[dict[str, Any]], key: str) -> Any:
    env, default, cast, _ = SETTINGS[key]
    if settings and settings.get(key) is not None:
        return cast(settings[key])
    import os

    raw = os.environ.get(env)
    if raw not in (None, ""):
        try:
            return cast(raw)
        except (TypeError, ValueError):
            pass
    return default


@dataclass
class LagSignal:
    event_key: str
    title: str
    leader: str
    follower: str
    outcome: str
    label: str
    lead_move: float          # leader's home-mid change over the window, signed
    follower_move: float      # follower's home-mid change over the same window, signed
    leader_mid: float         # leader's current mid for ``outcome``
    follower_ask: float
    follower_all_in: float
    edge: float               # leader_mid - follower_all_in
    depth: Optional[float]
    suggested_contracts: Optional[int]
    lag_s: float              # seconds since the leader's move completed
    ts: float

    def text(self) -> str:
        size = f" → buy {self.suggested_contracts} ct" if self.suggested_contracts else ""
        return (f"LAG: {self.leader} moved {self.lead_move:+.2f} on {self.title}, {self.follower} has not ({self.follower_move:+.2f}) — "
                f"buy {self.label} on {self.follower} at {self.follower_ask:.2f} (all-in {self.follower_all_in:.3f}) vs {self.leader} mid {self.leader_mid:.3f} (+{self.edge:.1%}){size}")


def _mid(q: OutcomeQuote) -> Optional[float]:
    if q.bid is None or q.ask is None:
        return None
    return (q.bid + q.ask) / 2.0


def _fresh(q: OutcomeQuote, now: float, fresh_s: float) -> bool:
    qt = q.quote_time
    if qt is None:
        return True  # venues that do not report one are fetched per poll
    return now - qt <= fresh_s


@dataclass
class LeadLagTracker:
    """Per-event mid history for every venue; ``observe`` returns the signals for one poll."""
    move: float = 0.05
    window_s: float = 30.0
    min_edge: float = 0.02
    cooldown_s: float = 60.0
    follow_fraction: float = 0.5
    fresh_s: float = 10.0
    history_s: float = 600.0
    executable: Optional[set[str]] = None
    leaders: Optional[set[str]] = None   # None = any venue may lead
    _hist: dict[tuple[str, str], Deque[tuple[float, float]]] = field(default_factory=dict)
    _last: dict[tuple[str, str, str], tuple[float, float]] = field(default_factory=dict)  # (event, follower, outcome) -> (ts, edge)

    @classmethod
    def from_settings(cls, settings: Optional[dict[str, Any]] = None, executable: Optional[Iterable[str]] = None, fresh_s: float = 10.0) -> "LeadLagTracker":
        leaders = {x.strip().lower() for x in str(_setting(settings, "leadlag_leaders")).split(",") if x.strip()}
        return cls(move=_setting(settings, "leadlag_move"), window_s=_setting(settings, "leadlag_window_s"), min_edge=_setting(settings, "leadlag_min_edge"), cooldown_s=_setting(settings, "leadlag_cooldown_s"), fresh_s=fresh_s, executable=set(executable) if executable is not None else None, leaders=leaders or None)

    def _push(self, event_key: str, venue: str, ts: float, mid: float) -> Deque[tuple[float, float]]:
        h = self._hist.setdefault((event_key, venue), deque())
        h.append((ts, mid))
        while h and ts - h[0][0] > self.history_s:
            h.popleft()
        return h

    def _move_over_window(self, h: Deque[tuple[float, float]], now: float) -> Optional[float]:
        """Current mid minus the mid at (or just before) ``window_s`` ago; None with no anchor."""
        if len(h) < 2:
            return None
        anchor = None
        for ts, m in h:
            if now - ts >= self.window_s:
                anchor = m
            else:
                if anchor is None:
                    anchor = m  # history shorter than the window: use its oldest point
                break
        return None if anchor is None else h[-1][1] - anchor

    def observe(self, event_key: str, title: str, outcomes: list[str], labels: dict[str, str], quotes_by_venue: dict[str, list[OutcomeQuote]], settings: Optional[dict[str, Any]] = None, now: Optional[float] = None, bankroll: Optional[float] = None, kelly_fraction: float = 0.25) -> list[LagSignal]:
        now = time.time() if now is None else float(now)
        if len(outcomes) != 2:
            return []
        home = outcomes[1] if len(outcomes) == 2 else outcomes[0]  # event keys are AWAY|HOME; outcomes list is [away, home]
        away = outcomes[0]
        # One home-mid per venue (a NO-side row is the other contract: skip it).
        cur: dict[str, dict[str, OutcomeQuote]] = {}
        for venue, qs in quotes_by_venue.items():
            for q in qs:
                if (q.meta or {}).get("side") == "no":
                    continue
                cur.setdefault(venue, {})[q.outcome] = q
        mids: dict[str, float] = {}
        for venue, by_out in cur.items():
            qh, qa = by_out.get(home), by_out.get(away)
            mh = _mid(qh) if qh is not None else None
            if mh is None and qa is not None and _mid(qa) is not None:
                mh = 1.0 - _mid(qa)  # type: ignore[operator]
            if mh is None or not (_fresh(qh, now, self.fresh_s) if qh is not None else True):
                continue
            mids[venue] = mh
            self._push(event_key, venue, now, mh)
        signals: list[LagSignal] = []
        for leader, lmid in mids.items():
            if self.leaders is not None and leader not in self.leaders:
                continue
            lmove = self._move_over_window(self._hist[(event_key, leader)], now)
            if lmove is None or abs(lmove) < self.move:
                continue
            for follower, fmid in mids.items():
                if follower == leader or (self.executable is not None and follower not in self.executable):
                    continue
                fmove = self._move_over_window(self._hist[(event_key, follower)], now)
                if fmove is None:
                    fmove = 0.0
                if abs(fmove) >= self.follow_fraction * abs(lmove):
                    continue  # the follower already caught up (same way) or disagrees (moved the other way): no lag to buy
                # The leader moved toward ``outcome``: buy it on the follower.
                outcome = home if lmove > 0 else away
                q = cur.get(follower, {}).get(outcome)
                if q is None or q.ask is None or not _fresh(q, now, self.fresh_s):
                    continue
                leader_mid_out = lmid if outcome == home else 1.0 - lmid
                try:
                    fee = float(fee_model_for_quote(q, settings).per_contract(q.ask, 100))
                except Exception:
                    fee = 0.0
                all_in = q.ask + fee
                edge = leader_mid_out - all_in
                if edge < self.min_edge:
                    continue
                key = (event_key, follower, outcome)
                last = self._last.get(key)
                if last is not None and now - last[0] < self.cooldown_s and edge <= last[1] + 0.005:
                    continue
                self._last[key] = (now, edge)
                depth = q.ask_size
                contracts = None
                if bankroll:
                    cap = int(math.floor(bankroll * kelly_fraction / q.ask)) if q.ask > 0 else 0
                    contracts = min(cap, int(depth)) if depth is not None else cap
                    contracts = contracts if contracts > 0 else None
                signals.append(LagSignal(event_key=event_key, title=title, leader=leader, follower=follower, outcome=outcome, label=labels.get(outcome, outcome), lead_move=lmove, follower_move=fmove, leader_mid=leader_mid_out, follower_ask=q.ask, follower_all_in=all_in, edge=edge, depth=depth, suggested_contracts=contracts, lag_s=0.0, ts=now))
        return signals

"""Lead-lag signals: buy the *lagging* venue when the leading one has already repriced.

A leader repricing is evidence of a possible convergence trade, not a guaranteed
arbitrage or a calibrated win probability. Recorded results depend on fill assumptions,
feed alignment and both entry and exit fees. Independent books, valid fresh quotes,
comparable history and fee-inclusive capital/depth limits are required here.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Iterable, Optional

from ..fees.base import D
from ..fees.registry import fee_model_for_quote
from ..matching.settlement_rules import _status_flags, pair_flags, rule_for_quote
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
    url: Optional[str] = None # the follower's market page, to act on it
    fee_total: Optional[float] = None  # the follower's fee for the whole order at ``suggested_contracts``
    settlement_flags: tuple[str, ...] = ()  # the follower contract's own rule missing/unverified: blocks execution
    pair_flags: tuple[str, ...] = ()        # leader vs follower rule differences: informational only
    tie_value: Optional[float] = None

    def text(self) -> str:
        """The signal as an order ticket (sport, venue, count, price, fee, cash)."""
        from .ticket import lag_ticket

        return lag_ticket(self, self.fee_total)


def _mid(q: OutcomeQuote) -> Optional[float]:
    if q.bid is None or q.ask is None or not all(math.isfinite(x) for x in (q.bid, q.ask)) or not 0 <= q.bid <= q.ask <= 1:
        return None
    return (q.bid + q.ask) / 2.0


# Timestamps may sit slightly *after* the decision time and still be real: a venue clock a
# few milliseconds ahead of ours (Robinhood's ask_venue_timestamp), or a fast-lane quote
# stamped when its answer arrived. Only a time further ahead than this is treated as bogus.
CLOCK_SKEW_S = 2.0


def _fresh(q: OutcomeQuote, now: float, fresh_s: float) -> bool:
    return all(math.isfinite(t) and -CLOCK_SKEW_S <= now - t <= fresh_s
               for t in (q.ts, q.quote_time) if t is not None)



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
    _hist: dict[tuple[str, str], Deque[tuple[float, float, Optional[float], Optional[float]]]] = field(default_factory=dict)
    _identity: dict = field(default_factory=dict)
    _last: dict[tuple[str, str, str], tuple[float, float]] = field(default_factory=dict)  # (event, follower, outcome) -> (ts, edge)

    @classmethod
    def from_settings(cls, settings: Optional[dict[str, Any]] = None, executable: Optional[Iterable[str]] = None, fresh_s: float = 10.0) -> "LeadLagTracker":
        leaders = {x.strip().lower() for x in str(_setting(settings, "leadlag_leaders")).split(",") if x.strip()}
        return cls(move=_setting(settings, "leadlag_move"), window_s=_setting(settings, "leadlag_window_s"), min_edge=_setting(settings, "leadlag_min_edge"), cooldown_s=_setting(settings, "leadlag_cooldown_s"), fresh_s=fresh_s, executable=set(executable) if executable is not None else None, leaders=leaders or None)

    def _push(self, event_key: str, venue: str, ts: float, mid: float, bid: Optional[float] = None, ask: Optional[float] = None) -> Deque[tuple[float, float, Optional[float], Optional[float]]]:
        h = self._hist.setdefault((event_key, venue), deque())
        if h and ts - h[-1][0] > max(self.window_s, 1.5 * self.fresh_s):
            h.clear()  # a feed outage is not a price impulse
        h.append((ts, mid, bid, ask))
        while h and ts - h[0][0] > self.history_s:
            h.popleft()
        return h

    def _anchor(self, h: Deque[tuple[float, float, Optional[float], Optional[float]]], now: float) -> Optional[tuple[float, float, Optional[float], Optional[float]]]:
        """The history point at (or just before) ``window_s`` ago; the oldest point when the
        history is shorter than the window; None with fewer than two points."""
        if len(h) < 2:
            return None
        anchor = None
        for row in h:
            if now - row[0] >= self.window_s:
                anchor = row
            else:
                if anchor is None:
                    anchor = row
                break
        return anchor

    def _move_over_window(self, h: Deque[tuple[float, float, Optional[float], Optional[float]]], now: float) -> Optional[float]:
        """Current mid minus the anchor mid; None with no anchor."""
        a = self._anchor(h, now)
        return None if a is None else h[-1][1] - a[1]

    def _repriced(self, h: Deque[tuple[float, float, Optional[float], Optional[float]]], now: float, lmove: float) -> bool:
        """A genuine repricing moves BOTH the bid and the ask the same way (each ≥ 40 % of
        the mid move); a pulled ask or a lone bid widening the spread also moves the mid but
        is not a price the market agrees on, and must not lead a signal."""
        a = self._anchor(h, now)
        if a is None:
            return False
        _, _, b0, a0 = a
        _, _, b1, a1 = h[-1]
        if None in (b0, a0, b1, a1):
            return True  # a venue that reports only a mid cannot be checked; keep the old behaviour
        db, da = b1 - b0, a1 - a0
        return db * lmove > 0 and da * lmove > 0 and abs(db) >= 0.4 * abs(lmove) and abs(da) >= 0.4 * abs(lmove)

    def observe(self, event_key: str, title: str, outcomes: list[str], labels: dict[str, str], quotes_by_venue: dict[str, list[OutcomeQuote]], settings: Optional[dict[str, Any]] = None, now: Optional[float] = None, bankroll: Optional[float] = None, kelly_fraction: float = 0.25) -> list[LagSignal]:
        now = time.time() if now is None else float(now)
        if (len(outcomes) != 2 or len(set(outcomes)) != 2 or not math.isfinite(now)
                or self.window_s <= 0 or self.fresh_s <= 0 or self.move <= 0
                or not 0 <= self.follow_fraction <= 1):
            return []
        home = outcomes[1] if len(outcomes) == 2 else outcomes[0]  # event keys are AWAY|HOME; outcomes list is [away, home]
        away = outcomes[0]
        # One home-mid per venue (a NO-side row is the other contract: skip it).
        cur: dict[str, dict[str, OutcomeQuote]] = {}
        for venue, qs in quotes_by_venue.items():
            for q in qs:
                if q.event_key != event_key or q.venue != venue or (q.meta or {}).get("side") == "no" or _mid(q) is None or not _fresh(q, now, self.fresh_s):
                    continue
                previous = cur.setdefault(venue, {}).get(q.outcome)
                if previous is None or q.ts > previous.ts:
                    cur[venue][q.outcome] = q
        mids: dict[str, float] = {}
        sources: dict[str, OutcomeQuote] = {}
        for venue, by_out in cur.items():
            qh, qa = by_out.get(home), by_out.get(away)
            mh = _mid(qh) if qh is not None else None
            bid = ask = None
            if mh is not None:
                bid, ask = qh.bid, qh.ask  # type: ignore[union-attr]
            elif qa is not None and _mid(qa) is not None:
                mh = 1.0 - _mid(qa)  # type: ignore[operator]
                bid, ask = (1.0 - qa.ask) if qa.ask is not None else None, (1.0 - qa.bid) if qa.bid is not None else None
            source = qh if qh is not None else qa
            if mh is None or source is None:
                continue
            key = (event_key, venue)
            identity = (source.book_id, source.venue_market_id, source.outcome)
            if self._identity.get(key) != identity:
                self._hist.pop(key, None)
                self._identity[key] = identity
            h = self._hist.get(key)
            if h and source.ts <= h[-1][0]:
                continue  # cached or out-of-order observations cannot create a move
            mids[venue] = mh
            sources[venue] = source
            self._push(event_key, venue, source.ts, mh, bid, ask)
        signals: list[LagSignal] = []
        for leader, lmid in mids.items():
            if self.leaders is not None and leader not in self.leaders:
                continue
            lmove = self._move_over_window(self._hist[(event_key, leader)], now)
            if lmove is None or abs(lmove) < self.move or not self._repriced(self._hist[(event_key, leader)], now, lmove):
                continue
            for follower, fmid in mids.items():
                if follower == leader or sources[follower].book_id == sources[leader].book_id or (self.executable is not None and follower not in self.executable):
                    continue
                fmove = self._move_over_window(self._hist[(event_key, follower)], now)
                la = self._anchor(self._hist[(event_key, leader)], now)
                fa = self._anchor(self._hist[(event_key, follower)], now)
                if fmove is None or la is None or fa is None or abs(la[0] - fa[0]) > self.fresh_s:
                    continue
                if abs(fmove) >= self.follow_fraction * abs(lmove):
                    continue  # the follower already caught up (same way) or disagrees (moved the other way): no lag to buy
                # The leader moved toward ``outcome``: buy it on the follower.
                outcome = home if lmove > 0 else away
                q = cur.get(follower, {}).get(outcome)
                if q is None or q.ask is None or q.book_id == sources[leader].book_id or not _fresh(q, now, self.fresh_s):
                    continue
                leader_mid_out = lmid if outcome == home else 1.0 - lmid
                sport = event_key.split(":", 1)[0].lower()
                market_type = "spread" if ":spread:" in event_key else ("total" if ":total:" in event_key else "moneyline")
                # Only the contract we buy settles this trade: the leader is a price signal and
                # is never traded, so a leader/follower rule difference (Rothera's tie rule vs
                # Kalshi's) biases the gap by ~P(tie)/2 and is informational (pair_flags). What
                # blocks execution is the follower's *own* rule being missing or unverified.
                try:
                    pair = tuple(pair_flags(sources[leader], q, sport, market_type))
                    follower_rule = rule_for_quote(q, sport, market_type)
                    settlement_flags = tuple(_status_flags(follower_rule)) if follower_rule else (f"settlement-rule-missing:{follower}",)
                    settlement_flags = tuple(f for f in settlement_flags if not f.startswith("settlement-rule-derived:"))
                    tie_value = {"half": 0.5, "no_winner": 0.0}.get((follower_rule or {}).get("tie"))
                except Exception:
                    pair, settlement_flags, tie_value = (), ("settlement-rules-error",), None
                depth = q.ask_size
                if depth is not None and (not math.isfinite(depth) or depth < 1):
                    continue
                contracts = None
                try:
                    fee_model = fee_model_for_quote(q, settings)
                    if bankroll is not None:
                        if not math.isfinite(bankroll) or bankroll <= 0 or not 0 < kelly_fraction <= 1 or depth is None or q.ask <= 0:
                            continue
                        budget = D(bankroll) * D(kelly_fraction)
                        lo, hi = 0, min(int(depth), int(budget / D(q.ask)))
                        while lo < hi:
                            n = (lo + hi + 1) // 2
                            if D(q.ask) * n + fee_model.fee(q.ask, n, "taker") <= budget:
                                lo = n
                            else:
                                hi = n - 1
                        if not lo:
                            continue
                        contracts = lo
                    # Unfunded observations use a conservative one-contract fee.
                    n = contracts or 1
                    total = fee_model.fee(q.ask, n, "taker")
                    if not total.is_finite() or total < 0:
                        continue
                    all_in = float(D(q.ask) + total / n)
                except Exception:
                    continue  # unknown fee is not zero fee
                edge = leader_mid_out - all_in
                if edge < self.min_edge:
                    continue
                key = (event_key, follower, outcome)
                last = self._last.get(key)
                if last is not None and now - last[0] < self.cooldown_s and edge <= last[1] + 0.005:
                    continue
                self._last[key] = (now, edge)
                fee_total = float(total) if contracts else None
                signals.append(LagSignal(fee_total=fee_total, settlement_flags=settlement_flags, pair_flags=pair, tie_value=tie_value, event_key=event_key, title=title, leader=leader, follower=follower, outcome=outcome, label=labels.get(outcome, outcome), lead_move=lmove, follower_move=fmove, leader_mid=leader_mid_out, follower_ask=q.ask, follower_all_in=all_in, edge=edge, depth=depth, suggested_contracts=contracts, lag_s=0.0, ts=now, url=q.url))
        return signals

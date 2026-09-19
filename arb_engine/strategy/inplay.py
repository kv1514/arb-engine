"""In-play position watcher: buy one side, wait, lock the other side on a dip.

The workflow you described: hold Broncos bought at 50¢; the market swings, Chiefs go to
60¢ and Broncos to 40¢ — buying more Broncos at 40¢ lowers the average, and the moment
*Chiefs* can be bought for less than (1 − your all-in Broncos cost − fees) the pair pays $1
either way for less than $1: a locked profit. Two signals per loop:

* STEAL — a side's all-in cost (ask + fee) is below the cross-venue consensus fair value by
  at least ``steal_edge``. Fair value comes from the other venues' de-vigged mids (Kalshi,
  Polymarket, Rothera); in play it is a *market* consensus, not a game model.
* LOCK — given what you hold, the maximum price you can pay for the other side (on the
  cheapest venue, fees included) so that both outcomes pay more than the total cost, and
  whether that price is available now. Also reports the locked P&L if you are already flat.

Execution gates
---------------
STEAL and LOCK fire on the gap between the ESPN-derived fair and a venue ask, but ESPN's
feed lags the broadcast by 5–20 s and the venues do not: a "cheap" quote that is ahead of our
information is adverse selection, not an edge. ``FeedFreshness`` remembers, per event,
when the ESPN state and score last changed and where every venue's mid was on the previous
poll, and ``evaluate_inplay(freshness=...)`` turns that into gate reasons:

* ``feed-stale``    — no ESPN state change for > ``stale_after_s`` while a venue mid moved
                      ≥ 0.02: the market knows something the feed has not shown us yet.
* ``clock-frozen``  — identical state for ≥ ``frozen_polls`` polls with the clock running.
* ``quote-old``     — the venue's own quote timestamp is older than the poll interval
                      (only venues that report one; CDNA quotes carry +3 s for its delay).
* ``score-pending`` — a score changed but ``last_play_id`` has not advanced (the play,
                      and ESPN's post-play win probability, are not published yet); a timer
                      when the feed has no play ids.
* ``suspect`` / ``review-pending`` — the ESPN feed's own flags (StateGuard).

A gated STEAL is downgraded to ``wait: <reason>`` and journaled with a ``GATED`` prefix so
the tick replay can count what the gates cost or saved; a gated LOCK NOW likewise.
CDNA-routed Robinhood contracts have a 3 s order delay, so a STEAL there needs an extra
``inplay_delay_haircut_cdna`` of edge and they never LOCK NOW in play.

Nothing here places orders; it tells you what to do and journals it.
"""

from __future__ import annotations

import inspect
import logging
import os
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Optional

from ..fees.base import ZeroFees
from ..fees.registry import fee_model_for, fee_model_for_quote
from ..matching.matcher import MergedEvent
from ..models import OutcomeQuote
from ..quant.arbitrage import Leg, max_price_for_leg
from ..quant.fairvalue import consensus_fair_value
from ..quant.inplay_fair import BlendedFair, blended_fair, market_confidence_from_spread
from .alerts import Alerter

log = logging.getLogger(__name__)

# ---- settings (declared through config.declare_setting when that helper exists) ----------
try:  # P01's settings helper; this module must import without it
    from ..config import declare_setting as _declare_setting  # type: ignore
    from ..config import setting as _config_setting  # type: ignore
except Exception:  # pragma: no cover - exercised when config.py predates declare_setting
    _declare_setting = None
    _config_setting = None

SETTINGS: dict[str, tuple[str, Any, Callable[[str], Any], str]] = {
    "inplay_stale_after_s": ("INPLAY_STALE_AFTER_S", 15.0, float, "seconds without an ESPN state change (while a venue mid moves) before STEAL/LOCK are gated as feed-stale"),
    "inplay_delay_haircut_cdna": ("INPLAY_DELAY_HAIRCUT_CDNA", 0.02, float, "extra edge a STEAL on a CDNA-routed Robinhood contract needs, for its 3 s order delay"),
    "inplay_slate_cap": ("INPLAY_SLATE_CAP", None, float, "dollars the live slate may deploy per tick across every STEAL (default: the bankroll); stakes scale proportionally"),
    "line_fair": ("LINE_FAIR", False, lambda s: str(s).strip().lower() in ("1", "true", "yes", "on"), "scan(): attach quant.lines fair values to spread/total events when that module exists"),  # same key/default as scanner.py: one knob attaches AND uses line fairs
}
if _declare_setting is not None:
    for _k, (_env, _default, _cast, _doc) in SETTINGS.items():
        try:
            _declare_setting(_k, env=_env, default=_default, cast=_cast, doc=_doc)
        except Exception:  # pragma: no cover
            pass


def setting(settings: Optional[dict[str, Any]], key: str, default: Any = None) -> Any:
    """settings dict -> config.setting (env, declared default) -> this module's default."""
    env, declared, cast, _ = SETTINGS.get(key, (None, None, None, ""))
    if settings and settings.get(key) is not None:
        return settings[key]
    if _config_setting is not None:
        try:
            v = _config_setting(settings or {}, key)
            if v is not None:
                return v
        except Exception:
            pass
    if env:
        raw = os.environ.get(env)
        if raw not in (None, ""):
            try:
                return cast(raw) if cast else raw
            except (TypeError, ValueError):
                pass
    return declared if default is None else default


def _call_optional(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call ``fn`` with only the keyword arguments its signature accepts (cross-item hooks
    such as store.record_tick or wp.home_win_probability may predate a kwarg)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(*args, **kwargs)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kwargs)
    return fn(*args, **{k: v for k, v in kwargs.items() if k in params})


def call_store(store: Any, name: str, *args: Any, **kwargs: Any) -> bool:
    """Invoke ``store.<name>`` when the store has it (P07's tables land in another item).
    Returns whether it ran; the store's own exceptions propagate to the caller's journal."""
    fn = getattr(store, name, None) if store is not None else None
    if fn is None:
        return False
    _call_optional(fn, *args, **kwargs)
    return True


@dataclass
class Lot:
    venue: str
    outcome: str
    price: float
    count: float
    exchange: str = "rothera"   # for Robinhood lots: which exchange it routed to (fee)
    role: str = "taker"
    # Venue fee inputs as the quote carried them (Kalshi: fee_type / fee_multiplier). Empty
    # means "the venue's usual schedule"; ``evaluate_inplay`` fills it from the matching quote.
    fee_params: dict[str, Any] = field(default_factory=dict)

    @property
    def fee(self) -> float:
        if self.fee_params:
            params = dict(self.fee_params)
            if self.venue == "robinhood":
                params.setdefault("exchange", self.exchange)
        else:
            params = {"exchange": self.exchange} if self.venue == "robinhood" else {"fee_type": "quadratic_with_maker_fees"} if self.venue == "kalshi" else {}
        return float(fee_model_for(self.venue, params).fee(self.price, self.count, self.role))

    @property
    def cost(self) -> float:
        return self.price * self.count + self.fee

    def priced_like(self, q: OutcomeQuote) -> "Lot":
        """Copy with the quote's fee inputs when this lot has none (Kalshi fee multiplier,
        Polymarket fee schedule). Robinhood lots keep their explicit exchange."""
        if self.fee_params or self.venue == "robinhood" or not q.fee_params:
            return self
        return replace(self, fee_params=dict(q.fee_params))

    @staticmethod
    def parse(spec: str) -> "Lot":
        """'robinhood:DEN:0.50:100[:rothera]' -> Lot; 'kalshi:DEN:0.50:100[:0.5]' sets the
        series fee multiplier."""
        parts = spec.split(":")
        if len(parts) < 4:
            raise ValueError("position format: venue:outcome:price:count[:exchange|fee_multiplier]")
        lot = Lot(venue=parts[0], outcome=parts[1], price=float(parts[2]), count=float(parts[3]))
        if len(parts) > 4 and parts[4]:
            try:
                mult = float(parts[4])
            except ValueError:
                lot.exchange = parts[4]
            else:
                lot.fee_params = {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": mult}
        return lot


@dataclass
class SideView:
    outcome: str
    label: str
    held: float
    cost: float                      # dollars paid incl. fees for this side
    avg_all_in: Optional[float]
    fair: Optional[float]
    best_venue: Optional[str]
    best_ask: Optional[float]
    best_all_in: Optional[float]
    steal_edge: Optional[float]      # fair - best_all_in (blended fair)
    steal: bool
    market_p: Optional[float] = None  # consensus of the venues' de-vigged mids
    model_p: Optional[float] = None   # our win-probability model on the ESPN game state
    espn_p: Optional[float] = None    # ESPN's own win probability
    # to balance the book: buy `need` contracts of this side at <= lock_price
    need: float = 0.0
    lock_price: Optional[float] = None
    lock_available: bool = False
    lock_profit_if_now: Optional[float] = None
    hold_ev: Optional[float] = None  # expected profit of holding as-is at the blended fair (sum fair*held - cost)
    # Sizing for a directional buy of this side at the best all-in (only meaningful on a STEAL):
    depth_contracts: Optional[float] = None    # contracts on offer at the best ask (top of book)
    kelly_contracts: Optional[int] = None      # fractional-Kelly stake on the configured bankroll, in contracts
    suggested_contracts: Optional[int] = None  # min(kelly, depth): what to actually buy now
    kelly_stake: Optional[float] = None        # dollars, fees included
    kelly_hedge: Optional[int] = None          # Kelly-optimal hedge size for the lock (sizing.hedge_kelly)
    # Execution gates (module docstring): reasons that held a signal back this poll.
    gated_reasons: list[str] = field(default_factory=list)
    steal_gated: bool = False        # would be a STEAL but for gated_reasons
    lock_gated: bool = False         # lock price is available but held back
    steal_threshold: Optional[float] = None    # edge actually required (base, CDNA haircut, 2x for lines)
    best_exchange: Optional[str] = None        # Robinhood routing of the best quote (rothera/kalshi/cdna)


@dataclass
class InplayView:
    event_key: str
    title: str
    live: bool
    sides: list[SideView]
    total_cost: float
    payout_if: dict[str, float]      # outcome -> payout if it happens (with current holdings)
    locked_pnl: Optional[float]      # min payout - cost when balanced (>=0 means locked profit)
    balanced: bool
    actions: list[str] = field(default_factory=list)
    game_line: Optional[str] = None            # 'Q3 04:12 · DEN 17-14 KC · KC ball 2nd & 7 at DEN 35 · TO 2/3'
    fair_line: Optional[str] = None            # 'model 0.58 / market 0.55 / espn 0.57 (home)'
    game_state: Optional[dict] = None
    blend: Optional[dict] = None
    disagreement: Optional[float] = None
    gated_reasons: list[str] = field(default_factory=list)   # event-level gate reasons this poll
    spread_source: Optional[str] = None        # where the model's pre-game spread came from
    freshness: Optional[dict] = None           # FeedFreshness.as_dict() snapshot


# ---- feed freshness -------------------------------------------------------------------------

@dataclass
class FeedFreshness:
    """Per-event memory across polls: when the ESPN state last moved, when the score last
    changed, where each venue's mid was. ``as_dict()`` is the plain contract the recorder's
    ``record_tick(freshness=...)`` accepts (keys ``last_state_change_ts``,
    ``last_score_change_ts``, ``mids`` plus the gate bookkeeping).

    Thresholds: ``stale_after_s`` (feed-stale), ``frozen_polls`` (clock-frozen),
    ``interval_s`` (quote-old), ``score_hold_s`` (score-pending without play ids),
    ``score_hold_max_s`` (cap so a feed whose ids never advance cannot gate forever).
    """
    stale_after_s: float = 15.0
    frozen_polls: int = 2
    interval_s: float = 10.0
    score_hold_s: float = 20.0
    score_hold_max_s: float = 120.0
    mid_move: float = 0.02
    cdna_delay_s: float = 3.0
    # observed
    polls: int = 0
    now: Optional[float] = None
    status: Optional[str] = None
    last_state_change_ts: Optional[float] = None
    last_score_change_ts: Optional[float] = None
    last_state: Optional[tuple] = None
    last_score: Optional[tuple] = None
    last_play_id: Optional[str] = None
    frozen: int = 0                                # consecutive polls with no state change
    score_pending: bool = False
    score_pending_play_id: Optional[str] = None    # play id seen when the score changed (None = timer fallback)
    mids: dict[str, dict[str, float]] = field(default_factory=dict)   # venue -> outcome -> mid (latest poll)
    mid_moves: dict[str, float] = field(default_factory=dict)         # venue -> max |mid move| vs the previous poll
    # per-event caches for the spread fallback chain
    pregame_home_p: Optional[float] = None         # last pre-game blended P(home)
    spread_home: Optional[float] = None
    spread_source: Optional[str] = None
    spread_logged: bool = False

    @classmethod
    def from_settings(cls, settings: Optional[dict[str, Any]] = None, interval_s: float = 10.0, **kw: Any) -> "FeedFreshness":
        return cls(stale_after_s=float(setting(settings, "inplay_stale_after_s")), interval_s=float(interval_s), **kw)

    @staticmethod
    def _signature(gs: Any) -> tuple:
        return tuple(getattr(gs, k, None) for k in ("home_score", "away_score", "period", "clock_seconds_remaining_in_period", "possession", "down", "distance", "yardline_100"))

    def observe(self, gs: Any, quotes_by_venue: Any, now: Optional[float] = None) -> None:
        """Record one poll. Call once per poll before ``evaluate_inplay`` (which does it for
        you when handed the object)."""
        now = time.time() if now is None else float(now)
        self.polls += 1
        self.now = now
        # venue mids
        new: dict[str, dict[str, float]] = {}
        for venue, qs in (quotes_by_venue or {}).items():
            vals = qs.values() if isinstance(qs, dict) else (qs if isinstance(qs, (list, tuple)) else [qs])
            for q in vals:
                m = getattr(q, "mid", None)
                if m is not None:
                    new.setdefault(venue, {})[q.outcome] = float(m)
        moves: dict[str, float] = {}
        for venue, per in new.items():
            old = self.mids.get(venue) or {}
            deltas = [abs(per[o] - old[o]) for o in per if o in old]
            if deltas:
                moves[venue] = max(deltas)
        self.mids, self.mid_moves = new, moves
        if gs is None:
            return
        self.status = getattr(gs, "status", None)
        sig = self._signature(gs)
        score = sig[:2]
        pid = getattr(gs, "last_play_id", None)
        pid = str(pid) if pid not in (None, "") else None
        if self.last_state is None or sig != self.last_state:
            self.last_state_change_ts = now
            self.frozen = 0
        else:
            self.frozen += 1
        if self.last_score is not None and score != self.last_score:
            self.last_score_change_ts = now
            self.score_pending = True
            # The scoring play may post in the same poll as the score: only a *new* id after
            # this poll's counts as "the play is in".
            self.score_pending_play_id = pid
        elif self.score_pending:
            elapsed = now - (self.last_score_change_ts or now)
            if self.score_pending_play_id is not None:
                if (pid is not None and pid != self.score_pending_play_id) or elapsed > self.score_hold_max_s:
                    self.score_pending = False
            elif pid is not None and self.last_play_id is not None and pid != self.last_play_id:
                self.score_pending = False   # ids appeared after the score: the next play is in
            elif elapsed > self.score_hold_s:
                self.score_pending = False
        self.last_state, self.last_score, self.last_play_id = sig, score, pid

    # -- gate reasons ---------------------------------------------------------------------
    def event_reasons(self, gs: Any, now: Optional[float] = None) -> list[str]:
        """Event-level reasons (in the order the actions report them)."""
        now = self.now if now is None else float(now)
        reasons: list[str] = []
        if gs is None or getattr(gs, "status", None) != "live":
            return reasons
        if getattr(gs, "suspect", False):
            reasons.append("suspect")
        if getattr(gs, "review_pending", False):
            reasons.append("review-pending")
        if self.score_pending:
            reasons.append("score-pending")
        moved = max(self.mid_moves.values()) if self.mid_moves else 0.0
        if self.last_state_change_ts is not None and now is not None and (now - self.last_state_change_ts) > self.stale_after_s and moved >= self.mid_move:
            reasons.append("feed-stale")
        clock = getattr(gs, "clock_seconds_remaining_in_period", None)
        if self.frozen >= self.frozen_polls and clock not in (None, 0):
            reasons.append("clock-frozen")
        return reasons

    def quote_age(self, q: OutcomeQuote) -> Optional[float]:
        age = getattr(q, "age", None)
        if age is None:
            return None
        if _exchange_of(q) == "cdna":
            age += self.cdna_delay_s
        return age

    def quote_reasons(self, q: OutcomeQuote) -> list[str]:
        age = self.quote_age(q)
        if age is not None and age > self.interval_s:
            return [f"quote-old:{q.venue}"]
        return []

    def as_dict(self) -> dict[str, Any]:
        return {
            "last_state_change_ts": self.last_state_change_ts, "last_score_change_ts": self.last_score_change_ts,
            "mids": {v: dict(per) for v, per in self.mids.items()}, "mid_moves": dict(self.mid_moves),
            "polls": self.polls, "frozen": self.frozen, "score_pending": self.score_pending, "score_pending_play_id": self.score_pending_play_id,
            "last_play_id": self.last_play_id, "stale_after_s": self.stale_after_s, "interval_s": self.interval_s,
            "spread_home": self.spread_home, "spread_source": self.spread_source, "pregame_home_p": self.pregame_home_p,
        }


def _exchange_of(q: OutcomeQuote) -> Optional[str]:
    """Robinhood routing of a quote: meta.exchange (present today), else fee_params.exchange."""
    ex = (q.meta or {}).get("exchange") or (q.fee_params or {}).get("exchange")
    return str(ex).lower() if ex else None


# ---- sportsbook anchor and spread fallback chain ---------------------------------------------

def sportsbook_probs(gs: Any, home_o: str, away_o: str) -> Optional[dict[str, float]]:
    """De-vigged P(home)/P(away) from the ESPN pickcenter moneylines (``sportsbook_ml_home``
    / ``sportsbook_ml_away``, American odds), or None."""
    ml_h, ml_a = getattr(gs, "sportsbook_ml_home", None), getattr(gs, "sportsbook_ml_away", None)
    if ml_h is None or ml_a is None:
        return None
    try:
        from ..quant.odds import american_to_decimal, devig_multiplicative, implied_from_decimal
        imp = [implied_from_decimal(american_to_decimal(float(ml_h))), implied_from_decimal(american_to_decimal(float(ml_a)))]
        ph, pa = devig_multiplicative(imp)
    except Exception:
        return None
    if not (0 < ph < 1):
        return None
    return {home_o: float(ph), away_o: float(pa)}


def _spread_from_probability(p_home: float, model: Any = None) -> Optional[float]:
    """Pre-game spread whose neutral-state model P(home) equals ``p_home``: P05's
    ``wp.spread_from_pregame_probability`` when present, else a bisection on the model here."""
    try:
        from ..models import wp as _wp
    except Exception:
        return None
    fn = getattr(_wp, "spread_from_pregame_probability", None)
    if fn is not None:
        try:
            return _call_optional(fn, p_home, model=model)
        except Exception:
            return None
    try:
        f = lambda s: _wp.home_win_probability(home_score=0, away_score=0, game_seconds_remaining=3600, vegas_spread_home=s, model=model)  # noqa: E731
        lo, hi = -30.0, 30.0   # P(home) falls as the home line rises (positive = home underdog)
        if not (f(hi) <= p_home <= f(lo)):
            return hi if p_home < f(hi) else lo
        for _ in range(24):
            mid = (lo + hi) / 2
            if f(mid) > p_home:
                lo = mid
            else:
                hi = mid
        return round((lo + hi) / 2, 1)
    except Exception:
        return None


def resolve_spread_home(gs: Any, freshness: Optional[FeedFreshness] = None, home_o: Optional[str] = None, away_o: Optional[str] = None, model: Any = None) -> tuple[Optional[float], Optional[str]]:
    """Home line for the model, first source that answers:
    ESPN summary odds (``vegas_spread_home``) -> scoreboard pickcenter spread -> de-vigged
    sportsbook moneylines inverted through the model -> the last pre-game blended fair
    inverted the same way (cached per event) -> None (logged once per event)."""
    if gs is None:
        return None, None
    v = getattr(gs, "vegas_spread_home", None)
    if v is not None:
        return float(v), "espn-odds"
    v = getattr(gs, "pickcenter_spread", None)
    if v is not None:
        return float(v), "pickcenter"
    if freshness is not None and freshness.spread_home is not None:
        return freshness.spread_home, freshness.spread_source
    sb = sportsbook_probs(gs, home_o or "home", away_o or "away")
    if sb:
        s = _spread_from_probability(sb[home_o or "home"], model)
        if s is not None:
            if freshness is not None:
                freshness.spread_home, freshness.spread_source = s, "sportsbook-ml"
            return s, "sportsbook-ml"
    if freshness is not None and freshness.pregame_home_p is not None:
        s = _spread_from_probability(freshness.pregame_home_p, model)
        if s is not None:
            freshness.spread_home, freshness.spread_source = s, "pregame-fair"
            return s, "pregame-fair"
    if freshness is not None and not freshness.spread_logged:
        freshness.spread_logged = True
        log.info("no pre-game spread for %s @ %s: model runs at a pick'em line", getattr(gs, "away", "?"), getattr(gs, "home", "?"))
    return None, None


def model_home_wp(gs: Any, model: Any = None, spread_home: Optional[float] = None) -> Optional[float]:
    """P(home wins) from our WP model on an ESPN GameState-like object (None if no state).

    ``spread_home`` overrides the state's ``vegas_spread_home`` (the fallback chain's answer).
    P02's extra fields (play_class, overtime, season, ...) are forwarded when P05's model
    accepts them; a college game past regulation (``overtime_sentinel``) has no clock the
    model understands, so it returns None and the blend runs on market + ESPN.
    """
    if gs is None or getattr(gs, "game_seconds_remaining", None) is None:
        return None
    if getattr(gs, "sport", "nfl") not in ("nfl", "ncaaf"):
        return None  # the WP model is football-only; other sports blend market + ESPN
    if getattr(gs, "overtime_sentinel", False):
        return None
    try:
        from ..models.wp import home_win_probability
    except Exception:
        return None
    spread = spread_home if spread_home is not None else gs.vegas_spread_home
    extra = {k: getattr(gs, k) for k in ("play_class", "overtime", "season") if getattr(gs, k, None) is not None}
    if getattr(gs, "status", None) == "final":
        extra["final"] = True
    try:
        return _call_optional(
            home_win_probability,
            home_score=gs.home_score, away_score=gs.away_score, game_seconds_remaining=gs.game_seconds_remaining,
            possession=gs.possession, down=gs.down, distance=gs.distance, yardline_100=gs.yardline_100,
            home_timeouts=gs.home_timeouts if gs.home_timeouts is not None else 3, away_timeouts=gs.away_timeouts if gs.away_timeouts is not None else 3,
            vegas_spread_home=spread or 0.0, receive_2h_ko_home=getattr(gs, "receive_2h_ko_home", None), model=model, **extra,
        )
    except Exception:
        return None


def game_line(gs: Any) -> Optional[str]:
    if gs is None:
        return None
    if gs.status == "pre":
        when = gs.start_time.strftime("%a %H:%M UTC") if getattr(gs, "start_time", None) else "scheduled"
        spread = f" · {gs.home} {gs.vegas_spread_home:+g}" if gs.vegas_spread_home is not None else ""
        return f"PRE {gs.away} @ {gs.home} {when}{spread}"
    q = "OT" if gs.period and gs.period > 4 else f"Q{gs.period}" if gs.period else "?"
    clock = f"{(gs.clock_seconds_remaining_in_period or 0) // 60:02d}:{(gs.clock_seconds_remaining_in_period or 0) % 60:02d}"
    parts = [f"{q} {clock}" if gs.status == "live" else "FINAL", f"{gs.away} {gs.away_score}-{gs.home_score} {gs.home}"]
    if gs.status == "live" and gs.possession:
        team = gs.home if gs.possession == "home" else gs.away
        dd = f"{gs.down}{'st' if gs.down == 1 else 'nd' if gs.down == 2 else 'rd' if gs.down == 3 else 'th'} & {gs.distance}" if gs.down else ""
        at = f"{gs.yardline_100} to go" if gs.yardline_100 is not None else ""
        parts.append(" ".join(x for x in (f"{team} ball", dd, at) if x))
    if gs.home_timeouts is not None and gs.away_timeouts is not None:
        parts.append(f"TO {gs.away_timeouts}/{gs.home_timeouts}")
    return " · ".join(parts)


def _best_spread(quotes_by_venue: Any) -> Optional[float]:
    """Narrowest two-sided book (ask - bid) across venues and outcomes, or None."""
    best = None
    for per_outcome in (quotes_by_venue or {}).values():
        vals = per_outcome.values() if isinstance(per_outcome, dict) else (per_outcome if isinstance(per_outcome, (list, tuple)) else [per_outcome])
        for q in vals:
            ask, bid = getattr(q, "ask", None), getattr(q, "bid", None)
            if ask is not None and bid is not None and ask >= bid:
                s = ask - bid
                best = s if best is None else min(best, s)
    return best


def _line_fair(me: MergedEvent, settings: Optional[dict[str, Any]]) -> Optional[dict[str, float]]:
    """Per-outcome fair for a spread/total event attached upstream (P09's scanner hook /
    P13's quant.lines): ``me.line_fair`` or ``info.venues['_line_fair']``. Moneylines never."""
    if me.info.market_type not in ("spread", "total"):
        return None
    if not setting(settings, "line_fair", False):  # the one line_fair knob (shared with scanner.py) turns this on
        return None
    lf = getattr(me, "line_fair", None) or (me.info.venues or {}).get("_line_fair")
    if isinstance(lf, dict) and lf.get("fair") and isinstance(lf.get("fair"), dict):
        lf = lf["fair"]
    if not isinstance(lf, dict) or not all(lf.get(o) is not None for o in me.info.outcomes):
        return None
    return {o: float(lf[o]) for o in me.info.outcomes}


def _p_tie(gs: Any) -> Optional[float]:
    """P(tie) from the ESPN state: P02's ``espn_tie`` or the last win-probability row's tie mass."""
    if gs is None:
        return None
    t = getattr(gs, "espn_tie", None)
    if t is None:
        series = getattr(gs, "espn_wp_series", None) or []
        if series and isinstance(series[-1], dict):
            t = series[-1].get("tie")
    try:
        t = float(t) if t is not None else None
    except (TypeError, ValueError):
        return None
    return t if t and t > 0 else None


def evaluate_inplay(me: MergedEvent, lots: Iterable[Lot], settings: Optional[dict[str, Any]] = None, steal_edge: float = 0.03, target_margin: float = 0.0, game_state: Any = None, model: Any = None, blend_weights: Optional[dict[str, float]] = None, bankroll: Optional[float] = None, kelly_fraction: float = 0.25, freshness: Optional[FeedFreshness] = None, now: Optional[float] = None, pool: str = "linear") -> InplayView:
    settings = settings or {}
    from ..quant.sizing import hedge_kelly as _hedge_kelly
    from ..quant.sizing import kelly_stake as _kelly
    info = me.info
    # Two-way markets only: the lock buys *the* other side and the blend renormalises two
    # probabilities. A three-way (soccer 1X2) event would get a "guaranteed" lock that leaves
    # the third outcome uncovered and a mis-scaled fair value, so refuse it outright.
    if len(info.outcomes) != 2:
        raise ValueError(f"in-play evaluation needs exactly two outcomes, got {list(info.outcomes)}")
    if freshness is not None:
        freshness.observe(game_state, me.quotes_by_venue, now)
    all_quotes = [q for qs in me.quotes_by_venue.values() for q in qs]
    lots = list(lots)
    held: dict[str, float] = {o: 0.0 for o in info.outcomes}
    cost: dict[str, float] = {o: 0.0 for o in info.outcomes}
    for lot in lots:
        if lot.outcome not in held:
            raise ValueError(f"unknown outcome {lot.outcome!r}; expected one of {info.outcomes}")
        q_same = next((q for q in all_quotes if q.venue == lot.venue and q.outcome == lot.outcome), None)
        priced = lot.priced_like(q_same) if q_same is not None else lot
        held[lot.outcome] += lot.count
        cost[lot.outcome] += priced.cost
    total_cost = sum(cost.values())
    # Which outcome is the home team: from the game state when we have it, else assume the
    # second code of "away @ home" ordering is unknown -> treat the first outcome as home only
    # for the blend's bookkeeping (the blend is symmetric, so this does not change results).
    home_o, away_o = info.outcomes[0], info.outcomes[1]
    if game_state is not None and getattr(game_state, "home", None) in info.outcomes and getattr(game_state, "away", None) in info.outcomes:
        home_o, away_o = game_state.home, game_state.away
    # ESPN's status is authoritative when we have it (the venues' in-play flag is a heuristic).
    gs_status = getattr(game_state, "status", None) if game_state is not None else None
    live = (gs_status == "live") if gs_status in ("pre", "live", "final") else bool(info.in_play)
    # The sportsbook line is a pre-game anchor only: in play its moneylines are the closing
    # numbers, and the live fair must not be dragged back to kickoff.
    sb = sportsbook_probs(game_state, home_o, away_o) if gs_status == "pre" else None
    market = consensus_fair_value(me.quotes_by_venue, info.outcomes, venue_weights=settings.get("venue_weights"), sportsbook_probs=sb)
    market_probs = {o: (market[o].fair if market.get(o) else None) for o in info.outcomes}
    spread_home, spread_source = resolve_spread_home(game_state, freshness, home_o, away_o, model) if live else (getattr(game_state, "vegas_spread_home", None), "espn-odds" if getattr(game_state, "vegas_spread_home", None) is not None else None)
    m_wp = _call_optional(model_home_wp, game_state, model, spread_home=spread_home)  # tests stub this name
    e_wp = getattr(game_state, "espn_home_wp", None) if game_state is not None else None
    p_tie = _p_tie(game_state) if live else None
    blend: BlendedFair = blended_fair(market_probs, m_wp, e_wp, home_o, away_o, weights=blend_weights, live=live, market_confidence=market_confidence_from_spread(_best_spread(me.quotes_by_venue)), sport=info.sport, p_tie=p_tie, pool=pool)
    if freshness is not None and not live and gs_status == "pre" and blend.home_p is not None:
        freshness.pregame_home_p = blend.home_p
    line_fair = _line_fair(me, settings)
    if line_fair is not None:
        fair = dict(line_fair)
        blend = BlendedFair(fair=dict(line_fair), home_p=line_fair.get(home_o), disagreement=None, sources={"line": line_fair.get(home_o)}, weights={"line": 1.0}, live=live)
    else:
        fair = {o: blend.fair.get(o) for o in info.outcomes} if blend.fair else {o: market_probs[o] for o in info.outcomes}
    model_probs = {home_o: m_wp, away_o: (1 - m_wp) if m_wp is not None else None}
    espn_probs = {home_o: e_wp, away_o: (1 - e_wp) if e_wp is not None else None}
    max_held = max(held.values()) if held else 0.0
    event_reasons = freshness.event_reasons(game_state, now) if (freshness is not None and live) else []
    cdna_haircut = float(setting(settings, "inplay_delay_haircut_cdna"))
    base_threshold = steal_edge * (2.0 if line_fair is not None else 1.0)

    sides: list[SideView] = []
    actions: list[str] = []
    for o in info.outcomes:
        quotes = [q for q in all_quotes if q.outcome == o and q.ask is not None]
        best: Optional[tuple[float, OutcomeQuote, Any]] = None
        lock_best: Optional[tuple[float, OutcomeQuote, Any]] = None
        cdna_seen: Optional[tuple[float, OutcomeQuote, Any]] = None
        for q in quotes:
            fm = fee_model_for_quote(q, settings)
            all_in = q.ask + fm.per_contract(q.ask, max(1.0, max_held - held[o]) if max_held > held[o] else 100.0)
            if best is None or all_in < best[0]:
                best = (all_in, q, fm)
            is_cdna = _exchange_of(q) == "cdna"
            if live and is_cdna:
                if cdna_seen is None or all_in < cdna_seen[0]:
                    cdna_seen = (all_in, q, fm)
            elif lock_best is None or all_in < lock_best[0]:
                lock_best = (all_in, q, fm)
        # Per-leg fair: the blend's tie mass is paid out by this contract's own tie rule
        # (Rothera YES pays nothing on a tie, Kalshi half). Without p_tie this is blend.fair.
        fv = fair.get(o)
        if best is not None and line_fair is None and blend.p_tie:
            fv = blend.leg_fair(o, float((best[1].meta or {}).get("tie_payout", 0.5)))
        edge = (fv - best[0]) if (fv is not None and best) else None
        best_ex = _exchange_of(best[1]) if best else None
        threshold = base_threshold + (cdna_haircut if best_ex == "cdna" else 0.0)
        # In play, a STEAL needs the blended fair AND the state model to agree (a stale quote
        # on one venue can drag the market consensus; the model does not see quotes at all).
        market_edge = (market_probs[o] - best[0]) if (market_probs.get(o) is not None and best) else None
        model_edge = (model_probs[o] - best[0]) if (model_probs.get(o) is not None and best) else None
        agree = True
        if live and model_edge is not None and line_fair is None:
            agree = model_edge >= threshold
        would_steal = bool(edge is not None and edge >= threshold and agree)
        reasons = list(event_reasons) + (freshness.quote_reasons(best[1]) if (freshness is not None and live and best is not None) else [])
        gated = would_steal and bool(reasons)
        sv = SideView(outcome=o, label=info.labels.get(o, o), held=held[o], cost=cost[o], avg_all_in=(cost[o] / held[o]) if held[o] else None, fair=fv, best_venue=best[1].venue if best else None, best_ask=best[1].ask if best else None, best_all_in=best[0] if best else None, steal_edge=edge, steal=would_steal and not gated, market_p=market_probs.get(o), model_p=model_probs.get(o), espn_p=espn_probs.get(o), gated_reasons=reasons if (would_steal or reasons) else [], steal_gated=gated, steal_threshold=threshold, best_exchange=best_ex)
        # How much to buy if this is a STEAL: fractional Kelly on the bankroll (fees are already
        # inside all_in), capped by what is actually offered at that price. Pure arbs are sized
        # by depth elsewhere (size_from_books); this is the directional case.
        if best is not None:
            sv.depth_contracts = best[1].ask_size
            if sv.steal and bankroll and fv is not None:
                ks = _kelly(bankroll, fv, best[0], fraction=kelly_fraction)
                sv.kelly_stake = round(ks["stake"], 2)
                sv.kelly_contracts = ks["contracts"]
                depth = int(sv.depth_contracts) if sv.depth_contracts is not None else None
                sv.suggested_contracts = min(ks["contracts"], depth) if depth is not None else ks["contracts"]
        # Lock: how many of this side are needed to equalise payouts, and the max price for them.
        need = max_held - held[o]
        if need > 0 and lock_best is None and cdna_seen is not None:
            sv.need = need
            actions.append(f"wait: {sv.label} only offered on {cdna_seen[1].venue} (cdna) at {cdna_seen[1].ask:.2f} — delayed 3 s - not lockable in play")
        elif need > 0 and lock_best is not None:
            # After buying `need` more, this side pays max_held if it wins. max_price_for_leg
            # budgets `need * (1 - target_margin)` for the leg, so the fixed leg must carry
            # everything already paid *net of* the payout the contracts already held on this
            # side contribute: budget = max_held * (1 - target_margin) - total_cost.
            fixed = total_cost - held[o] * (1.0 - target_margin)
            fixed_leg = Leg("held", "held", fixed / need, ZeroFees())  # per-contract share, may be negative
            lock = max_price_for_leg([fixed_leg], lock_best[2], need, target_margin, role="taker")
            sv.need, sv.lock_price = need, lock
            lock_reasons = list(event_reasons) + (freshness.quote_reasons(lock_best[1]) if (freshness is not None and live) else [])
            if lock is not None and lock_best[1].ask <= lock + 1e-9:
                sv.lock_available = True
                buy_cost = lock_best[1].ask * need + float(lock_best[2].fee(lock_best[1].ask, need))
                sv.lock_profit_if_now = max_held - (total_cost + buy_cost)
                hold_ev = sum((fair.get(o) or 0.0) * held[o] for o in info.outcomes) - total_cost if all(fair.get(o) is not None for o in info.outcomes) else None
                sv.hold_ev = hold_ev
                other = next(x for x in info.outcomes if x != o)
                if bankroll and held[other] > 0 and fair.get(other) is not None:
                    hk = _hedge_kelly(bankroll, need, cost[other] / held[other], lock_best[0], fair[other])
                    sv.kelly_hedge = hk["contracts"]
                # Week-1 replay: locking at break-even gave the model's edge back (docs/MODEL.md),
                # so show what holding is worth next to the guarantee and let the human choose.
                vs = f"; holding is worth ${hold_ev:.2f} at fair" + (" — lock" if hold_ev <= sv.lock_profit_if_now else " — holding has more EV") if hold_ev is not None else ""
                kh = f" (kelly hedge {sv.kelly_hedge})" if sv.kelly_hedge is not None else ""
                text = f"buy {need:g}{kh} x {sv.label} on {lock_best[1].venue} at ≤ {lock:.2f} (ask {lock_best[1].ask:.2f}) → guaranteed ≥ ${sv.lock_profit_if_now:.2f} on ${total_cost + buy_cost:.2f}{vs}"
                if lock_reasons:
                    sv.lock_gated = True
                    sv.gated_reasons = sorted(set(sv.gated_reasons) | set(lock_reasons), key=(sv.gated_reasons + lock_reasons).index)
                    actions.append(f"GATED LOCK NOW: wait: {', '.join(lock_reasons)} — {text}")
                else:
                    actions.append(f"LOCK NOW: {text}")
            elif lock is not None:
                actions.append(f"wait: {sv.label} locks a profit at ≤ {lock:.2f} on {lock_best[1].venue} (ask now {lock_best[1].ask:.2f})")
            else:
                actions.append(f"no lock possible for {sv.label} at current holdings (avg cost too high)")
            if cdna_seen is not None and (lock_best is None or cdna_seen[0] < lock_best[0]):
                actions.append(f"note: {sv.label} is cheaper on {cdna_seen[1].venue} (cdna, ask {cdna_seen[1].ask:.2f}) but delayed 3 s - not lockable in play")
        if sv.steal or sv.steal_gated:
            src = f" [market {sv.market_p:.2f} / model {sv.model_p:.2f}]" if sv.model_p is not None and sv.market_p is not None else ""
            size = ""
            if sv.suggested_contracts:
                offered = f", {int(sv.depth_contracts)} offered" if sv.depth_contracts is not None else ""
                size = f" → buy {sv.suggested_contracts} contracts ({kelly_fraction:g}×Kelly ${sv.kelly_stake:.2f} of ${bankroll:,.0f}{offered})"
            elif sv.suggested_contracts == 0:
                size = " → size 0 at this bankroll/depth"
            hc = f" [cdna +{cdna_haircut:.0%} haircut]" if best_ex == "cdna" else ""
            ln = " [line 2x edge]" if line_fair is not None else ""
            text = f"{sv.label} all-in {sv.best_all_in:.3f} on {sv.best_venue} vs fair {sv.fair:.3f} (+{sv.steal_edge:.1%}){hc}{ln}{src}{size}"
            actions.append(f"STEAL: {text}" if sv.steal else f"GATED STEAL: wait: {', '.join(reasons)} — {text}")
        elif live and market_edge is not None and market_edge >= threshold and not agree:
            actions.append(f"market says {sv.label} is cheap (+{market_edge:.1%}) but the model does not ({model_probs[o]:.2f} vs all-in {best[0]:.3f}) — likely a stale quote, not a steal")
        sides.append(sv)
    payout_if = {o: held[o] for o in info.outcomes}
    balanced = len({round(v, 6) for v in held.values()}) == 1 and max_held > 0
    locked = (min(payout_if.values()) - total_cost) if max_held > 0 else None
    if balanced and locked is not None:
        actions.insert(0, f"FLAT: both sides held {max_held:g}; locked P&L ${locked:.2f} on ${total_cost:.2f}")
    if blend.disagreement is not None and blend.disagreement > 0.05:
        srcs = ", ".join(f"{k} {v:.2f}" for k, v in blend.sources.items() if v is not None)
        actions.append(f"sources disagree by {blend.disagreement:.2f} on P({info.labels.get(home_o, home_o)}): {srcs}")
    fair_line = None
    if any(v is not None for v in blend.sources.values()):
        fair_line = "P(" + info.labels.get(home_o, home_o) + "): " + " / ".join(f"{k} {v:.2f}" for k, v in blend.sources.items() if v is not None) + (f" → blended {blend.home_p:.2f}" if blend.home_p is not None else "")
    return InplayView(event_key=me.event_key, title=info.title(), live=live, sides=sides, total_cost=total_cost, payout_if=payout_if, locked_pnl=locked, balanced=balanced, actions=actions, game_line=game_line(game_state), fair_line=fair_line, game_state=(game_state.as_dict() if game_state is not None and hasattr(game_state, "as_dict") else None), blend=blend.as_dict(), disagreement=blend.disagreement, gated_reasons=event_reasons, spread_source=spread_source, freshness=freshness.as_dict() if freshness is not None else None)


# ---- recording hooks (P07's store tables; every call guarded by hasattr) ---------------------

def steal_record(view: InplayView, sv: SideView, action: str, now: Optional[float] = None) -> dict[str, Any]:
    """Structured fields for one STEAL / GATED STEAL: what the recorder's ``record_steal``
    and the alerter's structured kwargs receive."""
    gs = view.game_state or {}
    return {
        "ts": time.time() if now is None else float(now), "event_key": view.event_key, "outcome": sv.outcome, "label": sv.label,
        "venue": sv.best_venue, "exchange": sv.best_exchange, "ask": sv.best_ask, "all_in": sv.best_all_in, "fair": sv.fair, "edge": sv.steal_edge,
        "threshold": sv.steal_threshold, "market_p": sv.market_p, "model_p": sv.model_p, "espn_p": sv.espn_p, "depth": sv.depth_contracts,
        "suggested_contracts": sv.suggested_contracts, "kelly_stake": sv.kelly_stake, "gated": sv.steal_gated, "reasons": list(sv.gated_reasons),
        "live": view.live, "period": gs.get("period"), "clock": gs.get("clock_seconds_remaining_in_period"), "home_score": gs.get("home_score"), "away_score": gs.get("away_score"),
        "last_play_id": gs.get("last_play_id"), "game_line": view.game_line, "action": action,
    }


def record_view(store: Any, view: InplayView, me: Optional[MergedEvent] = None, gs: Any = None, freshness: Optional[FeedFreshness] = None, now: Optional[float] = None, update_ladder: bool = True) -> list[str]:
    """Write one poll to the store: ``record_espn_tick(gs)``, ``record_tick(view, quotes_by_venue=, freshness=)``,
    ``record_steal(**fields)`` per STEAL / GATED STEAL, ``update_ladder(now)`` (the slate does
    that once per tick itself, so it passes ``update_ladder=False``). Each hook runs only when
    the store has it (a pre-P07 store has ``record_tick(view)`` alone). Returns the error
    strings to journal."""
    errors: list[str] = []
    if store is None:
        return errors
    now = time.time() if now is None else float(now)
    if gs is not None:
        try:
            call_store(store, "record_espn_tick", gs)
        except Exception as e:
            errors.append(f"record_espn_tick: {e!r}")
    try:
        call_store(store, "record_tick", view, quotes_by_venue=(me.quotes_by_venue if me is not None else None), freshness=(freshness.as_dict() if freshness is not None else None))
    except Exception as e:
        errors.append(f"record_tick: {e!r}")
    for a in view.actions:
        if a.startswith("STEAL") or a.startswith("GATED STEAL"):
            sv = next((s for s in view.sides if f" {s.label} all-in " in a), None)
            if sv is None:
                continue
            try:
                call_store(store, "record_steal", **steal_record(view, sv, a, now))
            except Exception as e:
                errors.append(f"record_steal: {e!r}")
    if update_ladder:
        try:
            call_store(store, "update_ladder", now)
        except Exception as e:
            errors.append(f"update_ladder: {e!r}")
    return errors


class InplayWatcher:
    """Poll one event and alert on STEAL / LOCK changes. ``fetch`` returns a MergedEvent."""

    def __init__(self, fetch, lots: list[Lot], alerter: Optional[Alerter] = None, settings: Optional[dict[str, Any]] = None, steal_edge: float = 0.03, target_margin: float = 0.0, fetch_state=None, model: Any = None, blend_weights: Optional[dict[str, float]] = None, store: Any = None, bankroll: Optional[float] = None, kelly_fraction: float = 0.25, interval: float = 5.0, freshness: Optional[FeedFreshness] = None):
        self.fetch = fetch
        self.store = store  # optional arb_engine.store.Store
        self.bankroll, self.kelly_fraction = bankroll, kelly_fraction
        self.fetch_state = fetch_state  # () -> GameState | None (ESPN); optional
        self.model = model
        self.blend_weights = blend_weights
        self.lots = lots
        self.alerts = alerter or Alerter(journal_path="out/inplay_journal.jsonl")
        self.settings = settings or {}
        self.steal_edge = steal_edge
        self.target_margin = target_margin
        self.interval = interval
        self.freshness = freshness or FeedFreshness.from_settings(self.settings, interval_s=interval)
        self.last_actions: set[str] = set()

    def step(self, now: Optional[float] = None) -> InplayView:
        gs = None
        if self.fetch_state is not None:
            try:
                gs = self.fetch_state()
            except Exception as e:
                self.alerts.info(f"game state unavailable: {e!r}")
        me = self.fetch()
        view = evaluate_inplay(me, self.lots, self.settings, self.steal_edge, self.target_margin, game_state=gs, model=self.model, blend_weights=self.blend_weights, bankroll=self.bankroll, kelly_fraction=self.kelly_fraction, freshness=self.freshness, now=now)
        if view.game_line:
            self.alerts.info(f"{view.game_line}  |  {view.fair_line or ''}", event=view.event_key, game_state=view.game_state, blend=view.blend)
        for err in record_view(self.store, view, me, gs, self.freshness, now):
            self.alerts.info(f"record failed: {err}")
        for a in view.actions:
            key = a.split("(")[0]
            if key not in self.last_actions and (a.startswith("LOCK NOW") or a.startswith("STEAL")):
                extra: dict[str, Any] = {}
                if a.startswith("STEAL"):
                    sv = next((s for s in view.sides if f" {s.label} all-in " in a), None)
                    extra = {"steal": steal_record(view, sv, a, now)} if sv is not None else {}
                _call_optional(self.alerts.alert, a.split(":")[0], a, event=view.event_key, game_state=view.game_state, **extra)
            else:
                self.alerts.info(a, event=view.event_key)
        self.last_actions = {a.split("(")[0] for a in view.actions}
        return view

    def run(self, interval: Optional[float] = None, duration: float = 4 * 3600, max_iterations: Optional[int] = None) -> None:
        interval = self.interval if interval is None else float(interval)
        self.freshness.interval_s = interval
        start, n = time.time(), 0
        while time.time() - start < duration and (max_iterations is None or n < max_iterations):
            try:
                self.step()
            except Exception as e:
                self.alerts.info(f"step error: {e!r}")
            n += 1
            time.sleep(interval)

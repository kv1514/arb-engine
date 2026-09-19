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

Nothing here places orders; it tells you what to do and journals it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..fees.base import ZeroFees
from ..fees.registry import fee_model_for, fee_model_for_quote
from ..matching.matcher import MergedEvent
from ..models import OutcomeQuote
from ..quant.arbitrage import Leg, max_price_for_leg
from ..quant.fairvalue import consensus_fair_value
from ..quant.inplay_fair import BlendedFair, blended_fair, market_confidence_from_spread
from .alerts import Alerter


@dataclass
class Lot:
    venue: str
    outcome: str
    price: float
    count: float
    exchange: str = "rothera"   # for Robinhood lots: which exchange it routed to (fee)
    role: str = "taker"

    @property
    def fee(self) -> float:
        params = {"exchange": self.exchange} if self.venue == "robinhood" else {"fee_type": "quadratic_with_maker_fees"} if self.venue == "kalshi" else {}
        return float(fee_model_for(self.venue, params).fee(self.price, self.count, self.role))

    @property
    def cost(self) -> float:
        return self.price * self.count + self.fee

    @staticmethod
    def parse(spec: str) -> "Lot":
        """'robinhood:DEN:0.50:100[:rothera]' -> Lot."""
        parts = spec.split(":")
        if len(parts) < 4:
            raise ValueError("position format: venue:outcome:price:count[:exchange]")
        return Lot(venue=parts[0], outcome=parts[1], price=float(parts[2]), count=float(parts[3]), exchange=parts[4] if len(parts) > 4 else "rothera")


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


def model_home_wp(gs: Any, model: Any = None) -> Optional[float]:
    """P(home wins) from our WP model on an ESPN GameState-like object (None if no state)."""
    if gs is None or getattr(gs, "game_seconds_remaining", None) is None:
        return None
    try:
        from ..models.wp import home_win_probability
    except Exception:
        return None
    try:
        return home_win_probability(
            home_score=gs.home_score, away_score=gs.away_score, game_seconds_remaining=gs.game_seconds_remaining,
            possession=gs.possession, down=gs.down, distance=gs.distance, yardline_100=gs.yardline_100,
            home_timeouts=gs.home_timeouts if gs.home_timeouts is not None else 3, away_timeouts=gs.away_timeouts if gs.away_timeouts is not None else 3,
            vegas_spread_home=gs.vegas_spread_home or 0.0, receive_2h_ko_home=getattr(gs, "receive_2h_ko_home", None), model=model,
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


def evaluate_inplay(me: MergedEvent, lots: Iterable[Lot], settings: Optional[dict[str, Any]] = None, steal_edge: float = 0.03, target_margin: float = 0.0, game_state: Any = None, model: Any = None, blend_weights: Optional[dict[str, float]] = None) -> InplayView:
    settings = settings or {}
    info = me.info
    # Two-way markets only: the lock buys *the* other side and the blend renormalises two
    # probabilities. A three-way (soccer 1X2) event would get a "guaranteed" lock that leaves
    # the third outcome uncovered and a mis-scaled fair value, so refuse it outright.
    if len(info.outcomes) != 2:
        raise ValueError(f"in-play evaluation needs exactly two outcomes, got {list(info.outcomes)}")
    lots = list(lots)
    held: dict[str, float] = {o: 0.0 for o in info.outcomes}
    cost: dict[str, float] = {o: 0.0 for o in info.outcomes}
    for lot in lots:
        if lot.outcome not in held:
            raise ValueError(f"unknown outcome {lot.outcome!r}; expected one of {info.outcomes}")
        held[lot.outcome] += lot.count
        cost[lot.outcome] += lot.cost
    total_cost = sum(cost.values())
    market = consensus_fair_value(me.quotes_by_venue, info.outcomes, venue_weights=settings.get("venue_weights"))
    market_probs = {o: (market[o].fair if market.get(o) else None) for o in info.outcomes}
    # Which outcome is the home team: from the game state when we have it, else assume the
    # second code of "away @ home" ordering is unknown -> treat the first outcome as home only
    # for the blend's bookkeeping (the blend is symmetric, so this does not change results).
    home_o, away_o = info.outcomes[0], info.outcomes[1]
    if game_state is not None and getattr(game_state, "home", None) in info.outcomes and getattr(game_state, "away", None) in info.outcomes:
        home_o, away_o = game_state.home, game_state.away
    # ESPN's status is authoritative when we have it (the venues' in-play flag is a heuristic).
    gs_status = getattr(game_state, "status", None) if game_state is not None else None
    live = (gs_status == "live") if gs_status in ("pre", "live", "final") else bool(info.in_play)
    m_wp = model_home_wp(game_state, model)
    e_wp = getattr(game_state, "espn_home_wp", None) if game_state is not None else None
    blend: BlendedFair = blended_fair(market_probs, m_wp, e_wp, home_o, away_o, weights=blend_weights, live=live, market_confidence=market_confidence_from_spread(_best_spread(me.quotes_by_venue)), sport=info.sport)
    fair = {o: blend.fair.get(o) for o in info.outcomes} if blend.fair else {o: market_probs[o] for o in info.outcomes}
    model_probs = {home_o: m_wp, away_o: (1 - m_wp) if m_wp is not None else None}
    espn_probs = {home_o: e_wp, away_o: (1 - e_wp) if e_wp is not None else None}
    max_held = max(held.values()) if held else 0.0

    sides: list[SideView] = []
    actions: list[str] = []
    for o in info.outcomes:
        quotes = [q for qs in me.quotes_by_venue.values() for q in qs if q.outcome == o and q.ask is not None]
        best: Optional[tuple[float, OutcomeQuote, Any]] = None
        for q in quotes:
            fm = fee_model_for_quote(q, settings)
            all_in = q.ask + fm.per_contract(q.ask, max(1.0, max_held - held[o]) if max_held > held[o] else 100.0)
            if best is None or all_in < best[0]:
                best = (all_in, q, fm)
        fv = fair.get(o)
        edge = (fv - best[0]) if (fv is not None and best) else None
        # In play, a STEAL needs the blended fair AND the state model to agree (a stale quote
        # on one venue can drag the market consensus; the model does not see quotes at all).
        market_edge = (market_probs[o] - best[0]) if (market_probs.get(o) is not None and best) else None
        model_edge = (model_probs[o] - best[0]) if (model_probs.get(o) is not None and best) else None
        agree = True
        if live and model_edge is not None:
            agree = model_edge >= steal_edge
        sv = SideView(outcome=o, label=info.labels.get(o, o), held=held[o], cost=cost[o], avg_all_in=(cost[o] / held[o]) if held[o] else None, fair=fv, best_venue=best[1].venue if best else None, best_ask=best[1].ask if best else None, best_all_in=best[0] if best else None, steal_edge=edge, steal=bool(edge is not None and edge >= steal_edge and agree), market_p=market_probs.get(o), model_p=model_probs.get(o), espn_p=espn_probs.get(o))
        # Lock: how many of this side are needed to equalise payouts, and the max price for them.
        need = max_held - held[o]
        if need > 0 and best is not None:
            # After buying `need` more, this side pays max_held if it wins. max_price_for_leg
            # budgets `need * (1 - target_margin)` for the leg, so the fixed leg must carry
            # everything already paid *net of* the payout the contracts already held on this
            # side contribute: budget = max_held * (1 - target_margin) - total_cost.
            fixed = total_cost - held[o] * (1.0 - target_margin)
            fixed_leg = Leg("held", "held", fixed / need, ZeroFees())  # per-contract share, may be negative
            lock = max_price_for_leg([fixed_leg], best[2], need, target_margin, role="taker")
            sv.need, sv.lock_price = need, lock
            if lock is not None and best[0] is not None and best[1].ask <= lock + 1e-9:
                sv.lock_available = True
                buy_cost = best[1].ask * need + float(best[2].fee(best[1].ask, need))
                sv.lock_profit_if_now = max_held - (total_cost + buy_cost)
                hold_ev = sum((fair.get(o) or 0.0) * held[o] for o in info.outcomes) - total_cost if all(fair.get(o) is not None for o in info.outcomes) else None
                sv.hold_ev = hold_ev
                # Week-1 replay: locking at break-even gave the model's edge back (docs/MODEL.md),
                # so show what holding is worth next to the guarantee and let the human choose.
                vs = f"; holding is worth ${hold_ev:.2f} at fair" + (" — lock" if hold_ev <= sv.lock_profit_if_now else " — holding has more EV") if hold_ev is not None else ""
                actions.append(f"LOCK NOW: buy {need:g} x {sv.label} on {best[1].venue} at ≤ {lock:.2f} (ask {best[1].ask:.2f}) → guaranteed ≥ ${sv.lock_profit_if_now:.2f} on ${total_cost + buy_cost:.2f}{vs}")
            elif lock is not None:
                actions.append(f"wait: {sv.label} locks a profit at ≤ {lock:.2f} on {best[1].venue} (ask now {best[1].ask:.2f})")
            else:
                actions.append(f"no lock possible for {sv.label} at current holdings (avg cost too high)")
        if sv.steal:
            src = f" [market {sv.market_p:.2f} / model {sv.model_p:.2f}]" if sv.model_p is not None and sv.market_p is not None else ""
            actions.append(f"STEAL: {sv.label} all-in {sv.best_all_in:.3f} on {sv.best_venue} vs fair {sv.fair:.3f} (+{sv.steal_edge:.1%}){src}")
        elif live and market_edge is not None and market_edge >= steal_edge and not agree:
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
    return InplayView(event_key=me.event_key, title=info.title(), live=live, sides=sides, total_cost=total_cost, payout_if=payout_if, locked_pnl=locked, balanced=balanced, actions=actions, game_line=game_line(game_state), fair_line=fair_line, game_state=(game_state.as_dict() if game_state is not None and hasattr(game_state, "as_dict") else None), blend=blend.as_dict(), disagreement=blend.disagreement)


class InplayWatcher:
    """Poll one event and alert on STEAL / LOCK changes. ``fetch`` returns a MergedEvent."""

    def __init__(self, fetch, lots: list[Lot], alerter: Optional[Alerter] = None, settings: Optional[dict[str, Any]] = None, steal_edge: float = 0.03, target_margin: float = 0.0, fetch_state=None, model: Any = None, blend_weights: Optional[dict[str, float]] = None, store: Any = None):
        self.fetch = fetch
        self.store = store  # optional arb_engine.store.Store
        self.fetch_state = fetch_state  # () -> GameState | None (ESPN); optional
        self.model = model
        self.blend_weights = blend_weights
        self.lots = lots
        self.alerts = alerter or Alerter(journal_path="out/inplay_journal.jsonl")
        self.settings = settings or {}
        self.steal_edge = steal_edge
        self.target_margin = target_margin
        self.last_actions: set[str] = set()

    def step(self) -> InplayView:
        gs = None
        if self.fetch_state is not None:
            try:
                gs = self.fetch_state()
            except Exception as e:
                self.alerts.info(f"game state unavailable: {e!r}")
        view = evaluate_inplay(self.fetch(), self.lots, self.settings, self.steal_edge, self.target_margin, game_state=gs, model=self.model, blend_weights=self.blend_weights)
        if view.game_line:
            self.alerts.info(f"{view.game_line}  |  {view.fair_line or ''}", event=view.event_key, game_state=view.game_state, blend=view.blend)
        if self.store is not None:
            try:
                self.store.record_tick(view)
            except Exception as e:
                self.alerts.info(f"record failed: {e!r}")
        for a in view.actions:
            key = a.split("(")[0]
            if key not in self.last_actions and (a.startswith("LOCK NOW") or a.startswith("STEAL")):
                self.alerts.alert(a.split(":")[0], a, event=view.event_key, game_state=view.game_state)
            else:
                self.alerts.info(a, event=view.event_key)
        self.last_actions = {a.split("(")[0] for a in view.actions}
        return view

    def run(self, interval: float = 5.0, duration: float = 4 * 3600, max_iterations: Optional[int] = None) -> None:
        start, n = time.time(), 0
        while time.time() - start < duration and (max_iterations is None or n < max_iterations):
            try:
                self.step()
            except Exception as e:
                self.alerts.info(f"step error: {e!r}")
            n += 1
            time.sleep(interval)

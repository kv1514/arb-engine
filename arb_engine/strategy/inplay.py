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
    steal_edge: Optional[float]      # fair - best_all_in
    steal: bool
    # to balance the book: buy `need` contracts of this side at <= lock_price
    need: float = 0.0
    lock_price: Optional[float] = None
    lock_available: bool = False
    lock_profit_if_now: Optional[float] = None


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


def evaluate_inplay(me: MergedEvent, lots: Iterable[Lot], settings: Optional[dict[str, Any]] = None, steal_edge: float = 0.03, target_margin: float = 0.0) -> InplayView:
    settings = settings or {}
    info = me.info
    lots = list(lots)
    held: dict[str, float] = {o: 0.0 for o in info.outcomes}
    cost: dict[str, float] = {o: 0.0 for o in info.outcomes}
    for lot in lots:
        if lot.outcome not in held:
            raise ValueError(f"unknown outcome {lot.outcome!r}; expected one of {info.outcomes}")
        held[lot.outcome] += lot.count
        cost[lot.outcome] += lot.cost
    total_cost = sum(cost.values())
    fair = consensus_fair_value(me.quotes_by_venue, info.outcomes, venue_weights=settings.get("venue_weights"))
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
        fv = fair.get(o).fair if fair.get(o) else None
        edge = (fv - best[0]) if (fv is not None and best) else None
        sv = SideView(outcome=o, label=info.labels.get(o, o), held=held[o], cost=cost[o], avg_all_in=(cost[o] / held[o]) if held[o] else None, fair=fv, best_venue=best[1].venue if best else None, best_ask=best[1].ask if best else None, best_all_in=best[0] if best else None, steal_edge=edge, steal=bool(edge is not None and edge >= steal_edge))
        # Lock: how many of this side are needed to equalise payouts, and the max price for them.
        need = max_held - held[o]
        if need > 0 and best is not None:
            other_cost = total_cost - cost[o]  # money already spent on the other side(s) + this side so far
            # Existing spend on this side counts against the same payout, so it is part of the fixed cost.
            fixed = total_cost
            fixed_leg = Leg("held", "held", fixed / need, ZeroFees())  # per-contract share of everything already paid
            lock = max_price_for_leg([fixed_leg], best[2], need, target_margin, role="taker")
            sv.need, sv.lock_price = need, lock
            if lock is not None and best[0] is not None and best[1].ask <= lock + 1e-9:
                sv.lock_available = True
                buy_cost = best[1].ask * need + float(best[2].fee(best[1].ask, need))
                sv.lock_profit_if_now = max_held - (total_cost + buy_cost)
                actions.append(f"LOCK NOW: buy {need:g} x {sv.label} on {best[1].venue} at ≤ {lock:.2f} (ask {best[1].ask:.2f}) → guaranteed ≥ ${sv.lock_profit_if_now:.2f} on ${total_cost + buy_cost:.2f}")
            elif lock is not None:
                actions.append(f"wait: {sv.label} locks a profit at ≤ {lock:.2f} on {best[1].venue} (ask now {best[1].ask:.2f})")
            else:
                actions.append(f"no lock possible for {sv.label} at current holdings (avg cost too high)")
        if sv.steal:
            actions.append(f"STEAL: {sv.label} all-in {sv.best_all_in:.3f} on {sv.best_venue} vs fair {sv.fair:.3f} (+{sv.steal_edge:.1%})")
        sides.append(sv)
    payout_if = {o: held[o] for o in info.outcomes}
    balanced = len({round(v, 6) for v in held.values()}) == 1 and max_held > 0
    locked = (min(payout_if.values()) - total_cost) if max_held > 0 else None
    if balanced and locked is not None:
        actions.insert(0, f"FLAT: both sides held {max_held:g}; locked P&L ${locked:.2f} on ${total_cost:.2f}")
    return InplayView(event_key=me.event_key, title=info.title(), live=bool(info.in_play), sides=sides, total_cost=total_cost, payout_if=payout_if, locked_pnl=locked, balanced=balanced, actions=actions)


class InplayWatcher:
    """Poll one event and alert on STEAL / LOCK changes. ``fetch`` returns a MergedEvent."""

    def __init__(self, fetch, lots: list[Lot], alerter: Optional[Alerter] = None, settings: Optional[dict[str, Any]] = None, steal_edge: float = 0.03, target_margin: float = 0.0):
        self.fetch = fetch
        self.lots = lots
        self.alerts = alerter or Alerter(journal_path="out/inplay_journal.jsonl")
        self.settings = settings or {}
        self.steal_edge = steal_edge
        self.target_margin = target_margin
        self.last_actions: set[str] = set()

    def step(self) -> InplayView:
        view = evaluate_inplay(self.fetch(), self.lots, self.settings, self.steal_edge, self.target_margin)
        for a in view.actions:
            key = a.split("(")[0]
            if key not in self.last_actions and (a.startswith("LOCK NOW") or a.startswith("STEAL")):
                self.alerts.alert(a.split(":")[0], a, event=view.event_key)
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

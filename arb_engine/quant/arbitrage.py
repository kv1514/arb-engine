"""Fee-aware arbitrage across mutually exclusive outcomes.

Model
-----
Every leg is a $1-payout contract on one outcome bought at ``price`` with a venue fee
model. Buy ``c`` contracts of *every* outcome and exactly one outcome pays ``c`` dollars
(ties on NFL/tennis settle $0.50/$0.50 on all venues we cover, which still pays ``c`` in
total). So:

    cost(c)   = sum_i ( price_i * c + fee_i(price_i, c) )
    profit(c) = c - cost(c)
    margin    = profit / c          (dollars locked in per $1 of payout)
    roi       = profit / cost

An arbitrage exists when ``margin > 0``. Because fees round up per order, margin is
computed at the actual contract count rather than per-contract.

``max_price_for_leg`` answers the overlay's main question — "what is the most I can pay
for this outcome on this venue so that hedging the other outcomes at their current asks
still locks in at least ``target_margin``" — by scanning the venue's price grid, which
keeps the fee rounding exact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Mapping, Optional, Sequence

from ..fees.base import D, FeeModel, ZeroFees
from ..models import Book, Level, OutcomeQuote


@dataclass
class Leg:
    outcome: str
    venue: str
    price: float
    fee_model: FeeModel = field(default_factory=ZeroFees)
    role: str = "taker"
    quote: Optional[OutcomeQuote] = None
    label: str = ""

    def fee(self, contracts: float) -> Decimal:
        return self.fee_model.fee(self.price, contracts, self.role)


@dataclass
class LegResult:
    outcome: str
    venue: str
    price: float
    contracts: float
    fee: float
    cost: float
    all_in_per_contract: float
    role: str
    label: str = ""
    market_id: str = ""
    url: Optional[str] = None
    vwap: Optional[float] = None


@dataclass
class ArbResult:
    contracts: float
    total_cost: float
    payout: float
    profit: float
    margin: float
    roi: float
    legs: list[LegResult]
    is_arb: bool
    gross_sum: float  # sum of prices before fees


def leg_all_in_cost(leg: Leg, contracts: float) -> Decimal:
    c = D(contracts)
    return D(leg.price) * c + leg.fee(contracts)


def evaluate(legs: Sequence[Leg], contracts: float = 100) -> ArbResult:
    """Evaluate buying ``contracts`` of every leg."""
    c = D(contracts)
    results: list[LegResult] = []
    total = Decimal("0")
    gross = Decimal("0")
    for leg in legs:
        fee = leg.fee(contracts)
        cost = D(leg.price) * c + fee
        total += cost
        gross += D(leg.price)
        results.append(
            LegResult(
                outcome=leg.outcome,
                venue=leg.venue,
                price=float(leg.price),
                contracts=float(c),
                fee=float(fee),
                cost=float(cost),
                all_in_per_contract=float(cost / c) if c else 0.0,
                role=leg.role,
                label=leg.label or (leg.quote.outcome_label if leg.quote else ""),
                market_id=leg.quote.venue_market_id if leg.quote else "",
                url=leg.quote.url if leg.quote else None,
            )
        )
    payout = c
    profit = payout - total
    margin = profit / c if c else Decimal("0")
    roi = profit / total if total else Decimal("0")
    return ArbResult(
        contracts=float(c),
        total_cost=float(total),
        payout=float(payout),
        profit=float(profit),
        margin=float(margin),
        roi=float(roi),
        legs=results,
        is_arb=profit > 0,
        gross_sum=float(gross),
    )


def best_leg_per_outcome(
    quotes_by_outcome: Mapping[str, Iterable[OutcomeQuote]],
    fee_for_quote,
    contracts: float = 100,
    role: str = "taker",
    allowed_venues: Optional[set[str]] = None,
) -> list[Leg]:
    """For each outcome pick the venue with the lowest all-in cost (price + fee/contract)."""
    legs: list[Leg] = []
    for outcome, quotes in quotes_by_outcome.items():
        best: Optional[Leg] = None
        best_cost: Optional[Decimal] = None
        for q in quotes:
            if q.ask is None:
                continue
            if allowed_venues and q.venue not in allowed_venues:
                continue
            leg = Leg(outcome=outcome, venue=q.venue, price=float(q.ask), fee_model=fee_for_quote(q), role=role, quote=q, label=q.outcome_label)
            cost = leg_all_in_cost(leg, contracts)
            if best_cost is None or cost < best_cost:
                best, best_cost = leg, cost
        if best is not None:
            legs.append(best)
    return legs


def max_price_for_leg(
    other_legs: Sequence[Leg],
    fee_model: FeeModel,
    contracts: float = 100,
    target_margin: float = 0.0,
    tick: float = 0.01,
    role: str = "taker",
    price_floor: float = 0.01,
    price_cap: float = 0.99,
) -> Optional[float]:
    """Highest price on the ``tick`` grid at which buying this leg (given the other legs at
    their prices) still yields ``margin >= target_margin``. ``None`` if no price works."""
    c = D(contracts)
    if c <= 0:
        return None
    others = sum((leg_all_in_cost(l, contracts) for l in other_legs), Decimal("0"))
    budget = c * (Decimal(1) - D(target_margin)) - others  # dollars left for this leg incl. fee
    if budget <= 0:
        return None
    t = D(tick)
    # Fee-free upper bound, then walk down the grid until the fee-inclusive cost fits.
    upper = min(D(price_cap), (budget / c).quantize(t, rounding="ROUND_FLOOR"))
    p = upper
    floor = D(price_floor)
    while p >= floor:
        cost = p * c + fee_model.fee(p, contracts, role)
        if cost <= budget:
            return float(p)
        p -= t
    return None


def walk_book(levels: Sequence[Level], contracts: float) -> Optional[tuple[float, list[tuple[float, float]]]]:
    """Consume ``contracts`` from ascending ask levels. Returns (vwap, fills) or None if
    the book is too thin."""
    need = float(contracts)
    fills: list[tuple[float, float]] = []
    spent = 0.0
    for lvl in levels:
        if need <= 1e-9:
            break
        take = min(need, float(lvl.size))
        if take <= 0:
            continue
        fills.append((float(lvl.price), take))
        spent += take * float(lvl.price)
        need -= take
    if need > 1e-9:
        return None
    return spent / float(contracts), fills


def _cost_with_fills(leg: Leg, fills: list[tuple[float, float]]) -> Decimal:
    total = Decimal("0")
    for price, qty in fills:
        total += D(price) * D(qty) + leg.fee_model.fee(price, qty, leg.role)
    return total


def size_from_books(legs: Sequence[Leg], max_contracts: Optional[float] = None, min_margin: float = 0.0, step: float = 1.0) -> Optional[ArbResult]:
    """Largest whole contract count (multiple of ``step``) such that filling every leg from
    its order book still clears ``min_margin``. Legs without a book use their top-of-book
    size (or unlimited if unknown). Returns the ArbResult at that size, or None."""
    caps: list[float] = []
    for leg in legs:
        q = leg.quote
        if q is not None and q.book is not None and q.book.asks:
            caps.append(sum(l.size for l in q.book.asks))
        elif q is not None and q.ask_size:
            caps.append(float(q.ask_size))
    cap = min(caps) if caps else (max_contracts or 100.0)
    if max_contracts is not None:
        cap = min(cap, float(max_contracts))
    n = int(cap // step) * step
    best: Optional[ArbResult] = None
    # Marginal cost is non-decreasing in size, so scan down from the cap and stop at the
    # first size that clears the margin.
    size = n
    while size >= step:
        total = Decimal("0")
        leg_results: list[LegResult] = []
        ok = True
        for leg in legs:
            q = leg.quote
            fills: list[tuple[float, float]]
            vwap: Optional[float]
            if q is not None and q.book is not None and q.book.asks:
                walked = walk_book(q.book.asks, size)
                if walked is None:
                    ok = False
                    break
                vwap, fills = walked
            else:
                vwap, fills = float(leg.price), [(float(leg.price), float(size))]
            cost = _cost_with_fills(leg, fills)
            total += cost
            fee = sum((leg.fee_model.fee(p, qty, leg.role) for p, qty in fills), Decimal("0"))
            leg_results.append(
                LegResult(
                    outcome=leg.outcome, venue=leg.venue, price=float(leg.price), contracts=float(size),
                    fee=float(fee), cost=float(cost), all_in_per_contract=float(cost / D(size)), role=leg.role,
                    label=leg.label or (q.outcome_label if q else ""), market_id=q.venue_market_id if q else "",
                    url=q.url if q else None, vwap=vwap,
                )
            )
        if ok:
            profit = D(size) - total
            margin = profit / D(size)
            if float(margin) >= min_margin:
                best = ArbResult(
                    contracts=float(size), total_cost=float(total), payout=float(size), profit=float(profit),
                    margin=float(margin), roi=float(profit / total) if total else 0.0, legs=leg_results,
                    is_arb=profit > 0, gross_sum=sum(l.price for l in legs),
                )
                break
        size -= step
    return best

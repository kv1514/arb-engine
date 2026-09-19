"""Fee-aware arbitrage across mutually exclusive outcomes.

Model
-----
Every leg is a $1-payout contract on one outcome bought at ``price`` with a venue fee
model. Buy ``c`` contracts of *every* outcome and, when the game has a winner, exactly one
outcome pays ``c`` dollars. So:

    cost(c)   = sum_i ( price_i * c + fee_i(price_i, c) )
    profit(c) = c - cost(c)
    margin    = profit / c          (dollars locked in per $1 of payout)
    roi       = profit / cost

An arbitrage exists when ``margin > 0``. Because fees round up per order, margin is
computed at the actual contract count rather than per-contract.

Ties do **not** pay $0.50/$0.50 everywhere. Kalshi's NFL rules pay $0.50 per side on a
tie and Polymarket resolves 50-50, but Rothera's terms define the winner by *strictly
greater* points with no tie clause, so a Rothera YES pays $0 on a tie and a Rothera NO
pays $1. Each leg therefore carries ``tie_payout`` (dollars per contract on a tie, read
from ``quote.meta["tie_payout"]``, default 0.5) and every result reports

    tie_payout_total = sum_i tie_payout_i          (dollars per contract set on a tie)
    tie_margin       = tie_payout_total - cost / c  (the tie-case margin)

``is_arb`` stays a statement about the win case (``margin > 0``); a negative
``tie_margin`` means the "lock" loses on a tie (Kalshi YES-A + Rothera YES-B pays $0.50)
while Kalshi YES-A + Rothera NO-A pays $1.50 on a tie and is the leg to prefer.

``max_price_for_leg`` answers the overlay's main question — "what is the most I can pay
for this outcome on this venue so that hedging the other outcomes at their current asks
still locks in at least ``target_margin``" — by scanning the venue's price grid (``tick``:
$0.01 on Kalshi/Robinhood, $0.001 on Polymarket tails), which keeps the fee rounding exact.

"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Mapping, Optional, Sequence

from ..fees.base import D, FeeModel, ZeroFees
from ..models import Book, Level, OutcomeQuote

DEFAULT_TIE_PAYOUT = 0.5


@dataclass
class Leg:
    outcome: str
    venue: str
    price: float
    fee_model: FeeModel = field(default_factory=ZeroFees)
    role: str = "taker"
    quote: Optional[OutcomeQuote] = None
    label: str = ""
    tie_payout: float = DEFAULT_TIE_PAYOUT  # dollars this contract pays on a tie (Kalshi 0.5, Rothera YES 0 / NO 1)
    min_size: Optional[float] = None  # venue minimum order size in contracts (Polymarket: 5 shares)

    def fee(self, contracts: float) -> Decimal:
        return self.fee_model.fee(self.price, contracts, self.role)

    @classmethod
    def from_quote(cls, outcome: str, q: OutcomeQuote, fee_model: FeeModel, role: str = "taker") -> "Leg":
        """A leg at the quote's ask carrying the quote's tie payout and minimum size."""
        return cls(outcome=outcome, venue=q.venue, price=float(q.ask), fee_model=fee_model, role=role, quote=q, label=q.outcome_label, tie_payout=tie_payout_for_quote(q), min_size=min_size_for_quote(q))


def tie_payout_for_quote(q: Optional[OutcomeQuote]) -> float:
    """``meta["tie_payout"]`` when the adapter set it, else the $0.50 default."""
    if q is None:
        return DEFAULT_TIE_PAYOUT
    v = q.meta.get("tie_payout")
    return DEFAULT_TIE_PAYOUT if v is None else float(v)


def min_size_for_quote(q: Optional[OutcomeQuote]) -> Optional[float]:
    v = q.meta.get("min_size") if q is not None else None
    return float(v) if v else None


def tick_for_quote(q: Optional[OutcomeQuote], default: float = 0.01) -> float:
    """Venue price grid for this market (Polymarket reports ``orderPriceMinTickSize``)."""
    v = q.meta.get("tick") if q is not None else None
    return float(v) if v else default


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
    tie_payout: float = DEFAULT_TIE_PAYOUT
    side: Optional[str] = None  # "yes" / "no" contract on the venue, when the adapter says


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
    tie_payout_total: float = 1.0  # dollars received per contract set if the game ties
    tie_margin: float = 0.0  # tie_payout_total - cost per contract (negative: the lock loses on a tie)


def _tie_totals(legs: Sequence[Leg], total: Decimal, c: Decimal) -> tuple[float, float]:
    tie_total = sum((D(l.tie_payout) for l in legs), Decimal("0"))
    tie_margin = tie_total - total / c if c else Decimal("0")
    return float(tie_total), float(tie_margin)


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
                tie_payout=float(leg.tie_payout),
                side=leg.quote.meta.get("side") if leg.quote else None,
            )
        )
    payout = c
    profit = payout - total
    margin = profit / c if c else Decimal("0")
    roi = profit / total if total else Decimal("0")
    tie_total, tie_margin = _tie_totals(legs, total, c)
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
        tie_payout_total=tie_total,
        tie_margin=tie_margin,
    )


def best_leg_per_outcome(
    quotes_by_outcome: Mapping[str, Iterable[OutcomeQuote]],
    fee_for_quote,
    contracts: float = 100,
    role: str = "taker",
    allowed_venues: Optional[set[str]] = None,
) -> list[Leg]:
    """For each outcome pick the venue with the lowest all-in cost (price + fee/contract).
    On an all-in tie the leg that pays more if the game ties wins (a Rothera NO at the same
    all-in as a Rothera YES on the other team is strictly better)."""
    legs: list[Leg] = []
    for outcome, quotes in quotes_by_outcome.items():
        best: Optional[Leg] = None
        best_key: Optional[tuple[Decimal, float]] = None
        for q in quotes:
            if q.ask is None:
                continue
            if allowed_venues and q.venue not in allowed_venues:
                continue
            leg = Leg.from_quote(outcome, q, fee_for_quote(q), role=role)
            key = (leg_all_in_cost(leg, contracts), -leg.tie_payout)
            if best_key is None or key < best_key:
                best, best_key = leg, key
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
    price_floor: Optional[float] = None,
    price_cap: float = 0.99,
) -> Optional[float]:
    """Highest price on the ``tick`` grid at which buying this leg (given the other legs at
    their prices) still yields ``margin >= target_margin``. ``None`` if no price works.
    ``tick`` is the venue's grid ($0.01 default; Polymarket tails quote on $0.001) and
    ``price_floor`` the lowest quotable price (one tick unless given)."""
    c = D(contracts)
    if c <= 0:
        return None
    others = sum((leg_all_in_cost(l, contracts) for l in other_legs), Decimal("0"))
    budget = c * (Decimal(1) - D(target_margin)) - others  # dollars left for this leg incl. fee
    if budget <= 0:
        return None
    t = D(tick)
    if t <= 0:
        return None
    # Fee-free upper bound, then walk down the grid until the fee-inclusive cost fits.
    upper = min(D(price_cap), (budget / c).quantize(t, rounding="ROUND_FLOOR"))
    p = upper
    floor = D(price_floor) if price_floor is not None else t
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


def min_size_for_legs(legs: Sequence[Leg], step: float = 1.0) -> float:
    """Smallest size every leg can be placed at: the size grid ``step`` or the largest
    per-leg venue minimum (Polymarket refuses orders under 5 shares)."""
    return max([float(step)] + [float(l.min_size) for l in legs if l.min_size])


def size_from_books(legs: Sequence[Leg], max_contracts: Optional[float] = None, min_margin: float = 0.0, step: float = 1.0) -> Optional[ArbResult]:
    """Profit-maximising size: evaluate every size at which some leg's book changes level
    (plus the overall cap) and keep the one with the highest dollar profit whose average
    margin still clears ``min_margin``. Legs without a book use their top-of-book size (or
    unlimited when unknown). Profit is concave in size (marginal cost is non-decreasing), so
    the best size is where the marginal leg cost crosses $1 — not the largest size that is
    still break-even. Sizes sit on the ``step`` grid and never below any leg's ``min_size``
    (``min_size_for_legs``): a 3-share Polymarket tail cannot be bought at all. Returns None
    if no size qualifies."""
    caps: list[float] = []
    breakpoints: set[float] = set()
    for leg in legs:
        q = leg.quote
        if q is not None and q.book is not None and q.book.asks:
            cum = 0.0
            for lvl in q.book.asks:
                cum += lvl.size
                breakpoints.add(cum)
            caps.append(cum)
        elif q is not None and q.ask_size:
            caps.append(float(q.ask_size))
    cap = min(caps) if caps else (max_contracts or 100.0)
    if max_contracts is not None:
        cap = min(cap, float(max_contracts))
    cap = int(cap // step) * step
    floor = min_size_for_legs(legs, step)
    if cap < floor:
        return None
    candidates = sorted({int(b // step) * step for b in breakpoints if floor <= b <= cap} | {cap})

    def evaluate_at(size: float) -> Optional[ArbResult]:
        total = Decimal("0")
        leg_results: list[LegResult] = []
        for leg in legs:
            q = leg.quote
            if q is not None and q.book is not None and q.book.asks:
                walked = walk_book(q.book.asks, size)
                if walked is None:
                    return None
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
                    url=q.url if q else None, vwap=vwap, tie_payout=float(leg.tie_payout), side=q.meta.get("side") if q else None,
                )
            )
        profit = D(size) - total
        margin = profit / D(size)
        if float(margin) < min_margin:
            return None
        tie_total, tie_margin = _tie_totals(legs, total, D(size))
        return ArbResult(
            contracts=float(size), total_cost=float(total), payout=float(size), profit=float(profit),
            margin=float(margin), roi=float(profit / total) if total else 0.0, legs=leg_results,
            is_arb=profit > 0, gross_sum=sum(l.price for l in legs), tie_payout_total=tie_total, tie_margin=tie_margin,
        )

    best: Optional[ArbResult] = None
    for size in candidates:
        r = evaluate_at(size)
        if r is not None and (best is None or r.profit > best.profit):
            best = r
    return best

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_HALF_EVEN
from typing import Protocol, Union

Number = Union[int, float, str, Decimal]

CENT = Decimal("0.01")
CENTICENT = Decimal("0.0001")


def D(x: Number) -> Decimal:
    """Exact Decimal from a float/str/int without binary-float noise.

    ``Decimal(str(0.07))`` is exactly ``0.07`` whereas ``Decimal(0.07)`` is not, and the
    venue tables round to the cent, so that noise would flip fees by a cent.
    """
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        return Decimal(repr(x))
    return Decimal(x)


def ceil_to(x: Decimal, quantum: Decimal = CENT) -> Decimal:
    """Round *up* to a multiple of ``quantum`` (Kalshi / Robinhood both round fees up)."""
    return x.quantize(quantum, rounding=ROUND_CEILING)


def round_half_even_to(x: Decimal, quantum: Decimal = CENT) -> Decimal:
    """Banker's rounding to ``quantum`` (Polymarket US)."""
    return x.quantize(quantum, rounding=ROUND_HALF_EVEN)


class FeeModel(Protocol):
    name: str

    def fee(self, price: Number, contracts: Number, role: str = "taker") -> Decimal:
        """Total fee in dollars for buying/selling ``contracts`` contracts at ``price``.

        ``role`` is "taker" (order crosses the book) or "maker" (resting order that later
        fills). A fee model may ignore the role if the venue does not distinguish.
        """
        ...

    def per_contract(self, price: Number, contracts: Number, role: str = "taker") -> float:
        ...


class _Base:
    name = "base"

    def per_contract(self, price: Number, contracts: Number, role: str = "taker") -> float:
        c = D(contracts)
        if c <= 0:
            return 0.0
        return float(self.fee(price, contracts, role) / c)  # type: ignore[attr-defined]


class ZeroFees(_Base):
    """Sportsbooks and venues whose cost is embedded in the price (no explicit fee)."""

    name = "none"

    def fee(self, price: Number, contracts: Number, role: str = "taker") -> Decimal:
        return Decimal("0")

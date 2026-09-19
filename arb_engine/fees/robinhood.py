"""Robinhood event-contract fees (Robinhood Derivatives, LLC).

Source: Robinhood support article "Event contracts overview" (pricing model effective
June 1, 2026) and the Robinhood Derivatives fee schedule. Verified 2026-09-15.

    commission = min( round_up_to_cent( k x P x (1 - P) x C ),  $0.01 x C )
        k = 0.10 without Robinhood Gold, 0.05 with Gold
    exchange fee = up to $0.01 per contract, set by the routing exchange

Both are charged when you open AND when you close a position (a contract held to
settlement pays only the opening side). Robinhood's published examples (100 contracts):

    price   with Gold   without Gold
    $0.01   $0.05       $0.10
    $0.05   $0.24       $0.48
    $0.25   $0.94       $1.00   <- capped at $0.01/contract
    $0.50   $1.00       $1.00
    $0.75   $0.94       $1.00
    $0.99   $0.05       $0.10

Routing exchanges seen in the public web app: KalshiEX (symbols prefixed ``KX``),
Rothera (Robinhood/Susquehanna JV; NFL game/spread/total symbols without the ``KX``
prefix), ForecastEX (fee embedded in the spread, no explicit exchange fee) and Nadex.
We charge the documented $0.01/contract ceiling for KalshiEX, Rothera and Nadex until a
per-exchange schedule is published; ForecastEX is $0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

from .base import CENT, D, Number, _Base, ceil_to

EXCHANGE_FEE_PER_CONTRACT: dict[str, Decimal] = {
    "kalshi": Decimal("0.01"),
    "rothera": Decimal("0.01"),
    "nadex": Decimal("0.01"),
    "forecastex": Decimal("0.00"),
    "cdna": Decimal("0.01"),  # college-football games (symbols NX.F.OPT.*); assumed at the $0.01 cap, unverified
}


def exchange_from_symbol_or_enum(symbol: str | None, exchange_enum: str | None = None) -> str:
    """Map Robinhood's ``exchange`` enum / contract symbol to a short exchange key."""
    e = (exchange_enum or "").upper()
    if "ROTHERA" in e:
        return "rothera"
    if "KALSHI" in e:
        return "kalshi"
    if "FORECAST" in e:
        return "forecastex"
    if "NADEX" in e or "NORTH_AMERICAN" in e:
        return "nadex"
    if "CDNA" in e:
        return "cdna"
    s = (symbol or "").upper()
    if s.startswith("KX"):
        return "kalshi"
    if s.startswith("NX."):
        return "cdna"
    return "rothera" if s else "unknown"


@dataclass(frozen=True)
class RobinhoodFees(_Base):
    name = "robinhood"
    gold: bool = False
    exchange: str = "rothera"
    commission_cap_per_contract: Decimal = Decimal("0.01")
    exchange_fee_per_contract: Decimal | None = None
    exchange_fee_table: Mapping[str, Decimal] = field(default_factory=lambda: dict(EXCHANGE_FEE_PER_CONTRACT))

    @classmethod
    def from_params(cls, params: Mapping[str, Any] | None, **overrides: Any) -> "RobinhoodFees":
        params = params or {}
        exchange = params.get("exchange") or exchange_from_symbol_or_enum(
            params.get("symbol"), params.get("exchange_enum")
        )
        kwargs: dict[str, Any] = {"exchange": exchange, "gold": bool(params.get("gold", False))}
        kwargs.update(overrides)
        return cls(**kwargs)

    @property
    def k(self) -> Decimal:
        return Decimal("0.05") if self.gold else Decimal("0.10")

    def commission(self, price: Number, contracts: Number) -> Decimal:
        p = D(price)
        c = D(contracts)
        if c <= 0:
            return Decimal("0.00")
        raw = self.k * p * (Decimal(1) - p) * c
        rounded = ceil_to(raw, CENT)
        cap = self.commission_cap_per_contract * c
        return min(rounded, cap)

    def exchange_fee(self, contracts: Number) -> Decimal:
        c = D(contracts)
        per = self.exchange_fee_per_contract
        if per is None:
            per = self.exchange_fee_table.get(self.exchange, Decimal("0.01"))
        return (per * c).quantize(CENT)

    def fee(self, price: Number, contracts: Number, role: str = "taker") -> Decimal:
        # Robinhood charges the same commission whether the order rests or crosses.
        return self.commission(price, contracts) + self.exchange_fee(contracts)

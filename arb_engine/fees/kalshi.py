"""Kalshi trading fees.

Source: Kalshi fee schedule PDF "Fee Schedule for July 2026 - 7.7.26 Update"
(https://kalshi.com/docs/kalshi-fee-schedule.pdf) and the live series objects returned by
``GET /trade-api/v2/series`` (``fee_type`` / ``fee_multiplier``).

    taker fee = round_up( M x 0.07   x C x P x (1 - P) )
    maker fee = round_up( M x 0.0175 x C x P x (1 - P) )   only on series with maker fees

* ``M`` is the series ``fee_multiplier`` (1 for almost everything; 0.5 on e.g. KXMLBGAME,
  0 on a handful of fee-free series).
* Maker fees apply only where ``fee_type == "quadratic_with_maker_fees"`` — this includes
  the sports game series we care about (KXNFLGAME, KXNFLSPREAD, KXNFLTOTAL, KXNCAAFGAME,
  KXATPMATCH, KXWTAMATCH, KXNBAGAME, KXNHLGAME, KXMLBGAME, KXEPLGAME, ...). Everything
  else (props, quarters, futures) is ``quadratic``: taker-only.
* No settlement fee. Fees are charged per matched order, on entry and (if you close
  early) on exit.

Rounding: the July-2026 schedule text says the fee is rounded up so that
``fee + positionCost`` lands on a centicent ($0.0001), while its worked table still shows
cent rounding (100 contracts @ $0.05 -> $0.34, not $0.3325). We default to **cent**
rounding because it is the conservative (higher) estimate; switch with ``rounding``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from .base import CENT, CENTICENT, D, Number, _Base, ceil_to

TAKER_RATE = Decimal("0.07")
MAKER_RATE = Decimal("0.0175")

FEE_TYPE_QUADRATIC = "quadratic"
FEE_TYPE_QUADRATIC_MAKER = "quadratic_with_maker_fees"


@dataclass(frozen=True)
class KalshiFees(_Base):
    name = "kalshi"
    multiplier: Decimal = Decimal("1")
    maker_fees: bool = False
    taker_rate: Decimal = TAKER_RATE
    maker_rate: Decimal = MAKER_RATE
    rounding: str = "cent"  # "cent" | "centicent"

    @classmethod
    def from_series(cls, series: Mapping[str, Any] | None, **overrides: Any) -> "KalshiFees":
        """Build from a Kalshi ``series`` object (or a quote's ``fee_params`` dict)."""
        series = series or {}
        fee_type = str(series.get("fee_type") or FEE_TYPE_QUADRATIC)
        mult = series.get("fee_multiplier", 1)
        kwargs: dict[str, Any] = {
            "multiplier": D(mult if mult is not None else 1),
            "maker_fees": fee_type == FEE_TYPE_QUADRATIC_MAKER,
        }
        if fee_type in ("none", "no_fees", "flat_zero"):
            kwargs["multiplier"] = Decimal("0")
        kwargs.update(overrides)
        return cls(**kwargs)

    def _quantum(self) -> Decimal:
        return CENTICENT if self.rounding == "centicent" else CENT

    def raw(self, price: Number, contracts: Number, role: str = "taker") -> Decimal:
        p = D(price)
        c = D(contracts)
        if role == "maker":
            rate = self.maker_rate if self.maker_fees else Decimal("0")
        else:
            rate = self.taker_rate
        return self.multiplier * rate * c * p * (Decimal(1) - p)

    def fee(self, price: Number, contracts: Number, role: str = "taker") -> Decimal:
        raw = self.raw(price, contracts, role)
        if raw <= 0:
            return Decimal("0.00")
        return ceil_to(raw, self._quantum())

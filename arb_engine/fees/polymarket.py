"""Polymarket fees.

Polymarket (global CLOB) — source: docs.polymarket.com/trading/fees, changelog entries of
2026-03-30 (fee structure v2) and 2026-07-10 (sports taker rate 0.03 -> 0.05). Verified
2026-09-15 against live Gamma market objects (``feeSchedule``):

    fee (USDC) = C x rate x ( P x (1 - P) ) ^ exponent      takers only
    sports: rate 0.05, exponent 1 -> peaks at $1.25 per 100 shares at P = 0.50
    makers: no fee; a share of taker fees is rebated (``rebateRate``, 15% on sports)

Rounded to 5 decimals of USDC (min 0.00001). Buys are charged in shares, sells in USDC —
economically the same number, so we quote everything in dollars.

Polymarket US (CFTC-regulated, docs.polymarket.us/fees) — a separate product:

    fee = theta x C x P x (1 - P), banker's rounding to the cent
    taker theta 0.06 (0.0695 from 2026-09-16), maker rebate theta -0.0125

The fee page's worked example (verified 2026-09-19): 1,000 contracts at $0.50 pay a taker
fee of 0.0695 x 1,000 x 0.25 = $17.375 -> **$17.38** and earn a maker rebate of
-0.0125 x 1,000 x 0.25 = -$3.125 -> **-$3.12** (both banker's-rounded: 17.375 rounds to
the even 8, -3.125 to the even 2). ``POLY_US_WORKED_EXAMPLE`` pins it for the tests and
``check_polymarket_us_worked_example`` recomputes it from the model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping

from .base import CENT, D, Number, _Base, round_half_even_to

FIVE_DP = Decimal("0.00001")


@dataclass(frozen=True)
class PolymarketFees(_Base):
    name = "polymarket"
    rate: Decimal = Decimal("0.05")
    exponent: Decimal = Decimal("1")
    taker_only: bool = True
    rebate_rate: Decimal = Decimal("0.15")
    fees_enabled: bool = True

    @classmethod
    def from_market(cls, market: Mapping[str, Any] | None, **overrides: Any) -> "PolymarketFees":
        """Build from a Gamma market object (uses ``feeSchedule`` / ``feesEnabled``)."""
        market = market or {}
        sched = market.get("feeSchedule") or market.get("fee_schedule") or {}
        kwargs: dict[str, Any] = {}
        if "rate" in sched:
            kwargs["rate"] = D(sched["rate"])
        if "exponent" in sched:
            kwargs["exponent"] = D(sched["exponent"])
        if "takerOnly" in sched:
            kwargs["taker_only"] = bool(sched["takerOnly"])
        if "rebateRate" in sched:
            kwargs["rebate_rate"] = D(sched["rebateRate"])
        if "feesEnabled" in market:
            kwargs["fees_enabled"] = bool(market["feesEnabled"])
        kwargs.update(overrides)
        return cls(**kwargs)

    def fee(self, price: Number, contracts: Number, role: str = "taker") -> Decimal:
        if not self.fees_enabled:
            return Decimal("0")
        if role == "maker" and self.taker_only:
            return Decimal("0")
        p = D(price)
        c = D(contracts)
        base = p * (Decimal(1) - p)
        if self.exponent == 1:
            curve = base
        else:
            curve = D(float(base) ** float(self.exponent))
        raw = c * self.rate * curve
        rounded = raw.quantize(FIVE_DP, rounding=ROUND_HALF_UP)
        if raw > 0 and rounded < FIVE_DP:
            rounded = FIVE_DP
        return rounded


POLY_US_TAKER_THETA_BEFORE = Decimal("0.06")
POLY_US_TAKER_THETA_AFTER = Decimal("0.0695")
POLY_US_THETA_CHANGE_DATE = date(2026, 9, 16)
POLY_US_MAKER_THETA = Decimal("-0.0125")
#: docs.polymarket.us/fees worked example at the post-2026-09-16 taker theta.
POLY_US_WORKED_EXAMPLE = {"contracts": 1000, "price": "0.50", "taker": Decimal("17.38"), "maker": Decimal("-3.12")}


@dataclass(frozen=True)
class PolymarketUSFees(_Base):
    name = "polymarket_us"
    taker_theta: Decimal = POLY_US_TAKER_THETA_AFTER
    maker_theta: Decimal = POLY_US_MAKER_THETA
    volume_rebate: Decimal = Decimal("0")  # 0.10 / 0.25 / 0.50 by prior-month taker volume

    @classmethod
    def for_date(cls, on: date | None = None, **overrides: Any) -> "PolymarketUSFees":
        on = on or date.today()
        theta = POLY_US_TAKER_THETA_AFTER if on >= POLY_US_THETA_CHANGE_DATE else POLY_US_TAKER_THETA_BEFORE
        kwargs: dict[str, Any] = {"taker_theta": theta}
        kwargs.update(overrides)
        return cls(**kwargs)

    def fee(self, price: Number, contracts: Number, role: str = "taker") -> Decimal:
        p = D(price)
        c = D(contracts)
        theta = self.maker_theta if role == "maker" else self.taker_theta
        raw = theta * c * p * (Decimal(1) - p)
        if role != "maker" and self.volume_rebate:
            raw = raw * (Decimal(1) - self.volume_rebate)
        return round_half_even_to(raw, CENT)


def check_polymarket_us_worked_example(model: "PolymarketUSFees | None" = None) -> bool:
    """True when ``model`` (default: the post-change thetas) reproduces the fee page's
    1,000 @ $0.50 example exactly — the guard that a theta or rounding edit must trip."""
    fm = model or PolymarketUSFees(taker_theta=POLY_US_TAKER_THETA_AFTER, maker_theta=POLY_US_MAKER_THETA)
    ex = POLY_US_WORKED_EXAMPLE
    return fm.fee(ex["price"], ex["contracts"], "taker") == ex["taker"] and fm.fee(ex["price"], ex["contracts"], "maker") == ex["maker"]

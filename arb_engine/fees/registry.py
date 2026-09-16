from __future__ import annotations

from typing import Any, Mapping

from ..models import (
    VENUE_KALSHI,
    VENUE_POLYMARKET,
    VENUE_POLYMARKET_US,
    VENUE_ROBINHOOD,
    OutcomeQuote,
)
from .base import FeeModel, ZeroFees
from .kalshi import KalshiFees
from .polymarket import PolymarketFees, PolymarketUSFees
from .robinhood import RobinhoodFees


def fee_model_for(venue: str, fee_params: Mapping[str, Any] | None = None, *, settings: Mapping[str, Any] | None = None) -> FeeModel:
    """Pick and configure the fee model for a venue quote.

    ``settings`` carries user-level knobs: ``robinhood_gold`` (bool), ``kalshi_rounding``
    ("cent"|"centicent"), ``polymarket_us_volume_rebate`` (Decimal-able).
    """
    settings = settings or {}
    fee_params = fee_params or {}
    if venue == VENUE_KALSHI:
        return KalshiFees.from_series(fee_params, rounding=str(settings.get("kalshi_rounding", "cent")))
    if venue == VENUE_ROBINHOOD:
        params = dict(fee_params)
        params.setdefault("gold", settings.get("robinhood_gold", False))
        return RobinhoodFees.from_params(params)
    if venue == VENUE_POLYMARKET:
        return PolymarketFees.from_market(fee_params)
    if venue == VENUE_POLYMARKET_US:
        return PolymarketUSFees.for_date(volume_rebate=settings.get("polymarket_us_volume_rebate", 0) or 0)
    return ZeroFees()


def fee_model_for_quote(q: OutcomeQuote, settings: Mapping[str, Any] | None = None) -> FeeModel:
    return fee_model_for(q.venue, q.fee_params, settings=settings)

"""Venue fee models. Every model exposes ``fee(price, contracts, role) -> Decimal``.

All formulas are transcribed from the venues' published schedules; see
``docs/VENUES.md`` for the sources and the date each was verified.
"""

from .base import FeeModel, ZeroFees, D, ceil_to, round_half_even_to
from .kalshi import KalshiFees
from .polymarket import PolymarketFees, PolymarketUSFees
from .robinhood import RobinhoodFees
from .registry import fee_model_for

__all__ = [
    "FeeModel",
    "ZeroFees",
    "D",
    "ceil_to",
    "round_half_even_to",
    "KalshiFees",
    "PolymarketFees",
    "PolymarketUSFees",
    "RobinhoodFees",
    "fee_model_for",
]

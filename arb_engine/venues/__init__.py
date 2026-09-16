"""Venue adapters. Each exposes ``fetch(sport) -> VenueSnapshot`` plus venue-specific
clients (Kalshi also has authenticated portfolio/order calls)."""

from .http import HttpClient, HttpError
from .kalshi import KalshiClient, KalshiAdapter
from .polymarket import PolymarketAdapter
from .robinhood import RobinhoodAdapter

__all__ = ["HttpClient", "HttpError", "KalshiClient", "KalshiAdapter", "PolymarketAdapter", "RobinhoodAdapter"]

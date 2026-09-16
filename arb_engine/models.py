"""Core data model shared by venues, matching, and the arbitrage math.

Everything is expressed per *outcome* of an *event*:

* An ``EventInfo`` is one real-world question with mutually exclusive, exhaustive
  outcomes (NFL game winner: ``{"PHI", "TEN"}``; tennis match: the two players).
* An ``OutcomeQuote`` is one venue's market for one outcome, normalised so that
  ``ask`` is the price you pay per $1-payout contract to BUY the outcome and ``bid``
  is what you receive to SELL it. A NO contract on the other side of a two-way market
  is folded in by the adapter (buy NO on B == buy YES on A at ``1 - no_price``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


VENUE_KALSHI = "kalshi"
VENUE_POLYMARKET = "polymarket"
VENUE_POLYMARKET_US = "polymarket_us"
VENUE_ROBINHOOD = "robinhood"
VENUE_SPORTSBOOK = "sportsbook"


@dataclass(frozen=True)
class Level:
    """One price level of an order book, in $-per-contract and contracts."""

    price: float
    size: float


@dataclass
class Book:
    """Depth for one outcome: ``asks`` ascending (what you can buy), ``bids`` descending."""

    asks: list[Level] = field(default_factory=list)
    bids: list[Level] = field(default_factory=list)

    def best_ask(self) -> Optional[Level]:
        return self.asks[0] if self.asks else None

    def best_bid(self) -> Optional[Level]:
        return self.bids[0] if self.bids else None


@dataclass
class OutcomeQuote:
    venue: str
    venue_market_id: str
    event_key: str
    outcome: str
    outcome_label: str = ""
    ask: Optional[float] = None
    bid: Optional[float] = None
    ask_size: Optional[float] = None
    bid_size: Optional[float] = None
    book: Optional[Book] = None
    # Venue-specific inputs to the fee model (Kalshi: fee_type/fee_multiplier;
    # Polymarket: feeSchedule; Robinhood: exchange routing).
    fee_params: dict[str, Any] = field(default_factory=dict)
    url: Optional[str] = None
    ts: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)
    # Which order book this price comes from. Robinhood re-sells Kalshi's book for
    # Kalshi-routed contracts, so those quotes share ``book_id="kalshi"`` with direct
    # Kalshi quotes and must not be arbed against each other.
    book_id: str = ""
    # Venue-reported time of the last quote update (epoch seconds), if known.
    quote_time: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.book_id:
            self.book_id = self.venue

    @property
    def age(self) -> Optional[float]:
        """Seconds between the venue's last update and when we fetched it."""
        if self.quote_time is None or not self.ts:
            return None
        return max(0.0, self.ts - self.quote_time)

    @property
    def mid(self) -> Optional[float]:
        if self.ask is not None and self.bid is not None:
            return (self.ask + self.bid) / 2.0
        return self.ask if self.ask is not None else self.bid

    @property
    def spread(self) -> Optional[float]:
        if self.ask is not None and self.bid is not None:
            return self.ask - self.bid
        return None


@dataclass
class EventInfo:
    event_key: str
    sport: str
    market_type: str  # "moneyline" | "spread" | "total"
    outcomes: list[str]
    labels: dict[str, str] = field(default_factory=dict)
    start_time: Optional[datetime] = None
    line: Optional[float] = None
    # How a tie/push settles: "half" (each side pays $0.50), "void", "both_no", "unknown".
    tie_rule: str = "unknown"
    venues: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Venue-reported in-play state (None = unknown). Robinhood's event_state exposes it.
    in_play: Optional[bool] = None

    def title(self) -> str:
        return " vs ".join(self.labels.get(o, o) for o in self.outcomes)


@dataclass
class VenueSnapshot:
    """Everything one adapter returned for one sport in one pull."""

    venue: str
    events: dict[str, EventInfo] = field(default_factory=dict)
    quotes: list[OutcomeQuote] = field(default_factory=list)
    fetched_at: float = 0.0
    errors: list[str] = field(default_factory=list)

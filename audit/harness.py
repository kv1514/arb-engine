"""Offline fakes shared by the recorder / live-polling reproductions.

Nothing here touches the network, the live database or any credential: every venue is a
plain object returning a fixed ``VenueSnapshot``, the ESPN feed is a list, the clock is a
list of numbers, and every ``Store`` is built in a temporary directory.
"""

from __future__ import annotations

import os
import tempfile
import time as _real_time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from arb_engine.models import EventInfo, OutcomeQuote, VenueSnapshot
from arb_engine.strategy.alerts import Alerter
from arb_engine.venues.espn import GameState

KEY = "nfl:DEN|KC:2026-09-21"
OUTCOMES = ["KC", "DEN"]


def tmp_path(name: str) -> str:
    d = tempfile.mkdtemp(prefix="arb-audit-")
    return os.path.join(d, name)


def quiet_alerter() -> Alerter:
    return Alerter(journal_path=tmp_path("journal.jsonl"), quiet=True, desktop=False, webhook="")


def live_game(status: str = "live") -> GameState:
    return GameState(
        event_id="401", event_key=KEY, sport="nfl", home="KC", away="DEN",
        status=status, period=3, clock_seconds_remaining_in_period=240,
        home_score=17, away_score=14,
        start_time=datetime.now(timezone.utc) - timedelta(hours=1),
    )


def kalshi_quote(outcome: str = "KC", ask: float = 0.55, bid: float = 0.53, ts: float = 0.0,
                 ticker: str = "KXT-KC", side: str = "yes") -> OutcomeQuote:
    return OutcomeQuote(
        venue="kalshi", venue_market_id=ticker if side == "yes" else ticker + "#no",
        event_key=KEY, outcome=outcome, outcome_label=outcome, ask=ask, bid=bid,
        ask_size=250, bid_size=180,
        fee_params={"exchange": "kalshi", "fee_type": "standard", "fee_multiplier": 0.07},
        ts=ts, meta={"ticker": ticker, "side": side}, book_id="kalshi",
    )


def robinhood_quote(outcome: str = "KC", ask: float = 0.57, bid: float = 0.54, ts: float = 0.0,
                    contract_id: str = "rh-kc") -> OutcomeQuote:
    return OutcomeQuote(
        venue="robinhood", venue_market_id=contract_id, event_key=KEY, outcome=outcome,
        outcome_label=outcome, ask=ask, bid=bid, ask_size=90, bid_size=70,
        fee_params={"exchange": "rothera", "symbol": "NFLGAME"},
        ts=ts, meta={"contract_id": contract_id, "side": "yes", "exchange": "rothera"},
        book_id="rothera",
    )


class Adapter:
    """A venue adapter whose ``fetch`` takes ``delay`` seconds on the injected clock."""

    def __init__(self, venue: str, quotes, delay: float = 0.0, clock=None):
        self.venue = venue
        self._quotes = list(quotes)
        self.delay = delay
        self.clock = clock
        self.fetches = 0

    def fetch(self, sport: str) -> VenueSnapshot:
        self.fetches += 1
        if self.delay and self.clock is not None:
            self.clock.advance(self.delay)
        info = EventInfo(KEY, sport, "moneyline", list(OUTCOMES), labels={o: o for o in OUTCOMES})
        return VenueSnapshot(self.venue, {KEY: info}, [q for q in self._quotes],
                             fetched_at=self.clock.now() if self.clock is not None else 0.0)


class FakeFeed:
    """ESPN feed: whatever games the reproduction wants this tick."""

    def __init__(self, games):
        self.games_list = list(games)

    def games(self, date=None):
        return list(self.games_list)

    def enrich(self, g):
        return g


class Clock:
    """A monotone injected clock: ``advance`` moves it, ``now`` reads it."""

    def __init__(self, t0: float = 1_780_000_000.0):
        self.t = float(t0)
        self.reads: list[float] = []

    def now(self) -> float:
        self.reads.append(self.t)
        return self.t

    def advance(self, dt: float) -> float:
        self.t += float(dt)
        return self.t

    # a drop-in for ``arb_engine.strategy.live.time``
    def time(self) -> float:
        return self.now()

    def sleep(self, seconds: float) -> None:
        self.advance(max(0.0, float(seconds)))

    def strftime(self, fmt: str, *a) -> str:
        return _real_time.strftime(fmt, _real_time.localtime(self.t))

    def monotonic(self) -> float:
        return self.t


class RecordingAlerter:
    """Captures ``info`` / ``alert`` so a reproduction can assert what was journalled."""

    def __init__(self):
        self.infos: list[tuple[str, dict]] = []
        self.alerts: list[tuple[str, str, dict]] = []
        self.events: list = []

    def info(self, text, **kw):
        self.infos.append((text, kw))

    def alert(self, title, text, **kw):
        self.alerts.append((title, text, kw))

    def journal(self, *a, **kw):
        pass

    def start_button(self, *a, **kw):
        pass


def make_slate(*, store=None, adapters=None, games=None, clock=None, fast: float = 0.0,
               alerter=None, interval: float = 5.0, settings: Optional[dict[str, Any]] = None):
    """A LiveSlate wired to fakes only (no network, no credentials, no live db)."""
    from arb_engine.strategy.live import LiveSlate

    quotes_k = [kalshi_quote()]
    quotes_r = [robinhood_quote()]
    if adapters is None:
        adapters = [Adapter("kalshi", quotes_k, clock=clock), Adapter("robinhood", quotes_r, clock=clock)]
    slate = LiveSlate(
        adapters,
        feed=FakeFeed(games if games is not None else [live_game()]),
        settings=settings or {},
        sport="nfl",
        alerter=alerter or quiet_alerter(),
        store=store,
        interval=interval,
        fast=fast,
        bankroll=500.0,
    )
    return slate

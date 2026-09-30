"""Read-only Polymarket US NFL moneylines from its separate public gateway.

This is not the international Gamma/CLOB adapter and never signs or sends orders.
Metadata prices (including ``outcomePrices`` and ``marketSides.quote``) are not an
executable ask. Both outcomes are derived from an OPEN long/YES order book: long
asks are offers; short/NO asks are one minus long bids, at the same displayed size.

References, read 2026-09-30: docs.polymarket.us/api-reference/sports/
get-events-by-league-slug, markets/get-market-book, orders/overview and fees.
Market-specific settlement terms are retained, not replaced by international
Polymarket's rules. Until independently verified, settlement remains unknown.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional
from urllib.parse import quote as urlquote

from ..fees.polymarket import PolymarketUSFees
from ..matching.normalize import et_date, parse_iso, team_event_key
from ..matching.teams import canonical_from_ticker, nfl_team_city, nfl_team_code
from ..models import VENUE_POLYMARKET_US, Book, EventInfo, Level, OutcomeQuote, VenueSnapshot
from .http import HttpClient

GATEWAY = "https://gateway.polymarket.us"
FEES_SOURCE = "https://docs.polymarket.us/fees"
_MONEYLINE = "SPORTS_MARKET_TYPE_MONEYLINE"
_PERIOD_MARKET = re.compile(
    r"(?:\b(?:first|second|1st|2nd)[\s_-]+half\b|\b(?:first|second|third|fourth|[1-4](?:st|nd|rd|th)?)[\s_-]+quarter\b|"
    r"\bq[1-4]\b|\bh[12]\b|\bhalftime\b|\bquarter\b|\bfirst[\s_-]+score\b)", re.I)


def _positive(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _price(value: Any) -> float:
    """Prices are dollars, never cents, and the public US book must be USD."""
    if not isinstance(value, dict) or value.get("currency") != "USD":
        raise ValueError("book price must explicitly be USD")
    number = _positive(value.get("value"))
    if number is None or number >= 1:
        raise ValueError("book price must be finite and between zero and one")
    return number


def _levels(rows: Any, reverse: bool) -> list[Level]:
    if not isinstance(rows, list):
        raise ValueError("book must explicitly contain bids and offers lists")
    levels: list[Level] = []
    seen: set[float] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("malformed book level")
        px = _price(row.get("px"))
        size = _positive(row.get("qty"))
        if size is None:
            raise ValueError("book quantity must be finite and positive")
        if px in seen:
            raise ValueError("duplicate displayed price level")
        seen.add(px)
        levels.append(Level(px, size))
    return sorted(levels, key=lambda level: level.price, reverse=reverse)


def parse_us_book(raw: Any, slug: str) -> tuple[Book, Optional[float]]:
    """Validate the book identity/state; return the LONG book and exchange time.

    A malformed row invalidates the whole response. Silently dropping an invalid
    offer could manufacture a misleading top of book or an inflated depth estimate.
    """
    data = raw.get("marketData") if isinstance(raw, dict) else None
    if not isinstance(data, dict) or data.get("marketSlug") != slug:
        raise ValueError("book marketSlug does not match requested market")
    if data.get("state") != "MARKET_STATE_OPEN":
        raise ValueError("market book is not OPEN")
    book = Book(asks=_levels(data.get("offers"), False), bids=_levels(data.get("bids"), True))
    if book.asks and book.bids and book.bids[0].price > book.asks[0].price:
        raise ValueError("crossed long order book")
    reported = data.get("transactTime")
    parsed = parse_iso(reported) if isinstance(reported, str) else None
    if reported is not None and parsed is None:
        raise ValueError("invalid book transactTime")
    quote_time = parsed.timestamp() if parsed is not None else None
    if quote_time is not None and not math.isfinite(quote_time):
        raise ValueError("nonfinite book transactTime")
    return book, quote_time


def complement_book(book: Book) -> Book:
    """Buying SHORT at X crosses the long bid at 1-X, not the long offer."""
    def other(level: Level) -> Level:
        # Decimal prevents binary 1-.57 producing an off-grid .43000000000000005.
        return Level(float(Decimal("1") - Decimal(str(level.price))), level.size)
    return Book(asks=sorted((other(level) for level in book.bids), key=lambda level: level.price),
                bids=sorted((other(level) for level in book.asks), key=lambda level: -level.price))


def _team(team: Any) -> Optional[str]:
    if not isinstance(team, dict):
        return None
    league = str(team.get("league") or "").lower()
    if league and league != "nfl":
        return None
    # Explicit abbreviations must resolve exactly. Never substring-match a ticker.
    abbreviation = team.get("abbreviation")
    abbrev_code = canonical_from_ticker("nfl", abbreviation) if abbreviation else None
    name_code = nfl_team_code(team.get("name")) if team.get("name") else None
    if abbreviation and abbrev_code is None:
        return None
    if abbrev_code and name_code and abbrev_code != name_code:
        return None
    return abbrev_code or name_code


class PolymarketUSAdapter:
    venue = VENUE_POLYMARKET_US

    def __init__(self, http: Optional[HttpClient] = None, with_books: bool = False,
                 clock: Optional[Callable[[], float]] = None):
        self.http = http or HttpClient()
        self.with_books = with_books
        self.clock = clock or time.time

    def fetch(self, sport: str) -> VenueSnapshot:
        snap = VenueSnapshot(venue=self.venue)
        if sport != "nfl":
            snap.errors.append(f"unsupported sport: {sport} (NFL full-game moneyline only)")
            snap.fetched_at = self.clock()
            return snap
        requested = self.clock()
        try:
            payload = self.http.get(f"{GATEWAY}/v2/leagues/nfl/events")
            completed = self.clock()
            if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
                raise ValueError("league response missing events list")
            league = payload.get("league") or {}
            if league and (not isinstance(league, dict) or league.get("slug") != "nfl"):
                raise ValueError("league response is not NFL")
            if completed < requested or not math.isfinite(completed) or not math.isfinite(requested):
                raise ValueError("invalid request observation timestamps")
        except Exception as error:
            snap.errors.append(f"nfl events: {error}")
            snap.fetched_at = self.clock()
            return snap
        seen: dict[str, dict] = {}
        for event in payload["events"]:
            if not isinstance(event, dict):
                snap.errors.append("malformed NFL event")
                continue
            event_id = str(event.get("id") or event.get("slug") or "")
            if not event_id:
                snap.errors.append("NFL event missing identity")
                continue
            if event_id in seen:
                if event != seen[event_id]:
                    snap.errors.append(f"conflicting duplicate event {event_id}")
                    self._remove_event(snap, event_id)
                continue
            seen[event_id] = event
            if event.get("active") is not True or any(event.get(key) is True for key in ("closed", "archived", "hidden", "ended")):
                continue
            self._ingest_event(snap, event, requested, completed)
        # No executable ask is obtained from metadata. Even an L1-only fetch has
        # to read a public book; with_books controls retained depth, not provenance.
        self._attach(snap.quotes, snap.errors, keep_depth=self.with_books)
        snap.fetched_at = self.clock()
        return snap

    def _remove_event(self, snap: VenueSnapshot, event_id: str) -> None:
        snap.quotes[:] = [quote for quote in snap.quotes if quote.meta.get("event_id") != event_id]
        used = {quote.event_key for quote in snap.quotes}
        for key in list(snap.events):
            if key not in used:
                del snap.events[key]

    def _ingest_event(self, snap: VenueSnapshot, event: dict, requested: float, completed: float) -> None:
        markets = event.get("markets")
        if not isinstance(markets, list):
            snap.errors.append(f"event {event.get('id')}: missing markets list")
            return
        seen: set[str] = set()
        for market in markets:
            if not isinstance(market, dict) or market.get("sportsMarketTypeV2") != _MONEYLINE:
                continue
            if market.get("active") is not True or any(market.get(key) is True for key in ("closed", "archived", "hidden")):
                continue
            slug = str(market.get("slug") or "")
            if slug in seen:
                snap.errors.append(f"duplicate moneyline market {slug}")
                snap.quotes[:] = [quote for quote in snap.quotes if quote.meta.get("market_slug") != slug]
                continue
            seen.add(slug)
            try:
                info, quotes = self._market(event, market, requested, completed)
            except ValueError as error:
                snap.errors.append(f"moneyline {slug or market.get('id', '?')}: {error}")
                continue
            old = snap.events.get(info.event_key)
            if old is not None and old.start_time != info.start_time:
                snap.errors.append(f"moneyline {slug}: conflicting game kickoff")
                continue
            snap.events[info.event_key] = info
            snap.quotes.extend(quotes)
        used = {quote.event_key for quote in snap.quotes}
        for key in list(snap.events):
            if key not in used:
                del snap.events[key]

    def _market(self, event: dict, market: dict, requested: float, completed: float) -> tuple[EventInfo, list[OutcomeQuote]]:
        slug = market.get("slug")
        market_id = market.get("id")
        if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", slug) or not market_id:
            raise ValueError("missing/invalid market id or slug")
        period_text = " ".join(str(market.get(key) or "") for key in ("slug", "question", "title", "subtitle", "sportsMarketType"))
        if _PERIOD_MARKET.search(period_text):
            raise ValueError("period-specific market is not a full-game moneyline")
        legacy_type = market.get("sportsMarketType")
        if legacy_type and legacy_type not in ("moneyline", "football_team_full_game_winner"):
            raise ValueError("moneyline type fields disagree")
        sides = market.get("marketSides")
        if not isinstance(sides, list) or len(sides) != 2 or not all(isinstance(side, dict) for side in sides):
            raise ValueError("moneyline must have exactly two explicit sides")
        if not all(type(side.get("long")) is bool and side.get("tradable") is True for side in sides):
            raise ValueError("moneyline long/short direction or tradability unknown")
        if {side["long"] for side in sides} != {True, False}:
            raise ValueError("moneyline must have one long and one short side")
        codes = [_team(side.get("team")) for side in sides]
        if any(code is None for code in codes) or codes[0] == codes[1]:
            raise ValueError("ambiguous or conflicting team identity")
        game_teams = event.get("teams")
        if game_teams is not None:
            if not isinstance(game_teams, list) or len(game_teams) != 2 or set(_team(team) for team in game_teams) != set(codes):
                raise ValueError("market participants do not match event teams")
        # startDate is listing time and must never be used as a game date.
        start = parse_iso(market.get("gameStartTime"))
        event_start = parse_iso(event.get("startTime"))
        if market.get("gameStartTime") and start is None:
            raise ValueError("invalid gameStartTime")
        if event.get("startTime") and event_start is None:
            raise ValueError("invalid event startTime")
        if start is not None and event_start is not None and start != event_start:
            raise ValueError("market and event kickoff disagree")
        start = start or event_start
        if start is None:
            raise ValueError("missing game kickoff (listing date is not kickoff)")
        date_et = et_date(start)
        key = team_event_key("nfl", codes, date_et)
        tick = _positive(market.get("orderPriceMinTickSize"))
        minimum = _positive(market.get("minimumTradeQty"))
        if tick is None or tick >= 1 or minimum is None:
            raise ValueError("missing/invalid price tick or minimum quantity")
        description = market.get("description") or ""
        disclaimer = market.get("rulesDisclaimer") or ""
        terms = str(description) + "\n" + str(disclaimer)
        source = f"{GATEWAY}/v1/markets/{urlquote(slug, safe='')}"
        event_state = event.get("eventState") or {}
        in_play = event.get("live") if type(event.get("live")) is bool else None
        if isinstance(event_state, dict) and event_state.get("live") is True:
            in_play = True
        if start.timestamp() <= completed:
            in_play = True
        info = EventInfo(key, "nfl", "moneyline", codes,
                         labels={code: nfl_team_city(code) for code in codes}, start_time=start,
                         tie_rule="unknown", in_play=in_play,
                         venues={self.venue: {"event_id": event.get("id"), "slug": event.get("slug"), "market_id": str(market_id)}})
        fee = self._fee_params(completed, market.get("feeCoefficient"))
        quotes: list[OutcomeQuote] = []
        for side, code in zip(sides, codes):
            long = side["long"]
            meta = {"side": "yes" if long else "no", "long": long,
                    "no_of": None if long else codes[sides.index(next(s for s in sides if s['long']))],
                    "event_id": str(event.get("id") or event.get("slug")), "market_slug": slug,
                    "contract_id": str(market_id), "market_side_id": str(side.get("id") or ""),
                    "contract_identity": {"sport": "nfl", "market_type": "moneyline", "participants": sorted(codes), "date_et": date_et},
                    "tick_size": tick, "tick": tick, "min_order_size": minimum, "min_size": minimum,
                    "tie_payout": None, "settlement": {}, "settlement_verified": False,
                    "settlement_source": source, "settlement_rules_hash": hashlib.sha256(terms.encode()).hexdigest(),
                    "description": description, "rulesDisclaimer": disclaimer,
                    "resolution_source": event.get("resolutionSource"), "arb_ineligible": "book-unavailable",
                    "req_ts": requested, "obs_ts": completed, "refreshed": False,
                    "approx_time": False, "price_source": "unavailable"}
            quotes.append(OutcomeQuote(self.venue, str(market_id) + ("" if long else "#no"),
                                       key, code, info.labels[code], fee_params=dict(fee),
                                       url=f"https://polymarket.us/event/{urlquote(str(event.get('slug') or slug), safe='')}",
                                       ts=completed, meta=meta, book_id=self.venue))
        return info, quotes

    def _fee_params(self, observed: float, raw_coefficient: Any) -> dict[str, Any]:
        schedule_day = date.fromisoformat(et_date(datetime.fromtimestamp(observed, timezone.utc)))
        params: dict[str, Any] = {"taker_theta": str(PolymarketUSFees.for_date(schedule_day).taker_theta),
                                  "fee_date_et": schedule_day.isoformat(), "fee_source": FEES_SOURCE,
                                  "volume_rebate": "0"}
        # The public docs do not define feeCoefficient's units/role. Preserve it
        # for audit, never treat e.g. a legacy zero as a verified fee exemption.
        if raw_coefficient is not None:
            params["reported_fee_coefficient"] = raw_coefficient
        return params

    def attach_books_for(self, quotes: list[OutcomeQuote], errors: Optional[list[str]] = None) -> None:
        self._attach(quotes, errors, keep_depth=True)

    def _attach(self, quotes: list[OutcomeQuote], errors: Optional[list[str]], keep_depth: bool) -> None:
        groups: dict[str, list[OutcomeQuote]] = {}
        for quote in quotes:
            if quote.venue == self.venue:
                groups.setdefault(str(quote.meta.get("market_slug") or ""), []).append(quote)
        for slug, group in groups.items():
            requested = self.clock()
            try:
                raw = self.http.get(f"{GATEWAY}/v1/markets/{urlquote(slug, safe='')}/book")
                completed = self.clock()
                if completed < requested or not math.isfinite(requested) or not math.isfinite(completed):
                    raise ValueError("invalid request observation timestamps")
                long_book, quote_time = parse_us_book(raw, slug)
                if quote_time is not None and quote_time > completed:
                    raise ValueError("book transactTime is later than response completion")
                short_book = complement_book(long_book)
            except Exception as error:
                if errors is not None:
                    errors.append(f"book {slug}: {error}")
                for quote in group:
                    quote.ask = quote.bid = quote.ask_size = quote.bid_size = None
                    quote.book = None
                    quote.meta["arb_ineligible"] = "book-unavailable"
                    quote.meta["refreshed"] = False
                    quote.meta["price_source"] = "unavailable"
                continue
            for quote in group:
                book = long_book if quote.meta["long"] else short_book
                quote.book = book if keep_depth else None
                ask, bid = book.best_ask(), book.best_bid()
                quote.ask, quote.ask_size = (ask.price, ask.size) if ask else (None, None)
                quote.bid, quote.bid_size = (bid.price, bid.size) if bid else (None, None)
                quote.quote_time = quote_time
                quote.ts = completed
                quote.meta.update({"req_ts": requested, "obs_ts": completed, "refreshed": True,
                                   "approx_time": False, "price_source": "public_book"})
                quote.meta.pop("arb_ineligible", None)
                quote.fee_params.update(self._fee_params(completed, quote.fee_params.get("reported_fee_coefficient")))

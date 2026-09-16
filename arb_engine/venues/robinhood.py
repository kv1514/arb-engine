"""Robinhood event contracts (Robinhood Derivatives) — public, unauthenticated market data.

Discovery: the category pages are server-rendered Next.js pages whose ``__NEXT_DATA__``
blob carries every event in the category with its contracts:

    https://robinhood.com/us/en/prediction-markets/nfl/      (578 events on 2026-09-15)
    https://robinhood.com/us/en/prediction-markets/tennis/

Each contract has ``id``, ``symbol`` (``NFLGAME-26SEP20PHITEN-PHI`` on Rothera;
``KXWTAMATCH-...`` when routed to KalshiEX), ``exchange`` (``EXCHANGE_SOURCE_ROTHERA`` /
``EXCHANGE_SOURCE_KALSHI``), ``displayShortName``/``displayLongName``.

Quotes: ``https://api.robinhood.com/marketdata/event/contract/quotes/v1/?ids=a,b,...``
(also ``?symbols=``), max ~20 ids per call, returns yes/no bid/ask, sizes, timestamps.
Event metadata: ``/prediction-markets/v1/events?ids=`` and ``/event_state?event_ids=``.

There is no public order-entry API; the extension overlays this data onto robinhood.com
and you place orders in the app yourself.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable, Optional

from ..fees.robinhood import exchange_from_symbol_or_enum
from ..models import VENUE_ROBINHOOD, EventInfo, OutcomeQuote, VenueSnapshot
from ..matching.normalize import et_date, nfl_event_key, parse_iso, person_keys, tennis_event_key
from ..matching.teams import nfl_team_code
from .http import HttpClient

WEB = "https://robinhood.com"
API = "https://api.robinhood.com"
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"

SPORT_CATEGORY: dict[str, str] = {
    "nfl": "nfl",
    "ncaaf": "college-football",
    "tennis": "tennis",
    "nba": "nba",
    "nhl": "nhl",
    "mlb": "baseball",
}

# Contract symbol families we treat as the game-winner market per sport.
SPORT_SYMBOL_PREFIXES: dict[str, tuple[str, ...]] = {
    "nfl": ("NFLGAME-", "KXNFLGAME-"),
    "ncaaf": ("NCAAFGAME-", "KXNCAAFGAME-"),
    "tennis": ("KXATPMATCH-", "KXWTAMATCH-", "ATPMATCH-", "WTAMATCH-", "KXATPCHALLENGERMATCH-", "KXWTACHALLENGERMATCH-", "KXITFMATCH-", "KXITFWMATCH-"),
    "nba": ("NBAGAME-", "KXNBAGAME-"),
    "nhl": ("NHLGAME-", "KXNHLGAME-"),
    "mlb": ("MLBGAME-", "KXMLBGAME-"),
}


def _f(x: Any) -> Optional[float]:
    try:
        return float(x) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None


_DATE_PROGRESS = re.compile(r"^[A-Z][a-z]{2}\.? \d{1,2}(,? \d{4})?( @ .*)?$")


def _in_play_from_progress(progress: str, status: Optional[str]) -> Optional[bool]:
    """Robinhood's ``eventProgress`` is a date ('Sep 16') before the game and text like
    'Live', 'Interrupted', 'Q3 4:12', '2nd Set' once it is under way."""
    if not progress:
        return False if status == "EVENT_STATUS_UPCOMING" else None
    if _DATE_PROGRESS.match(progress):
        return False
    if any(w in progress.lower() for w in ("final", "ended", "complete", "settled")):
        return False
    return True


def _event_day_from_timeline(timeline: Any) -> Optional[Any]:
    """The 'Event day' timeline entry is the scheduled start; the first entry is when
    trading opened, which is days earlier."""
    if not isinstance(timeline, dict):
        return None
    for entry in timeline.get("entries", []):
        if entry.get("type") == "TIMELINE_ENTRY_TYPE_EVENT_DAY":
            return parse_iso(entry.get("timestamp"))
    return None


def clean_label(name: str) -> str:
    """'K. Miyoshi (b. 2004)' -> 'K. Miyoshi' (disambiguation suffixes are not display text)."""
    return re.sub(r"\s*\((?:b\.|born)[^)]*\)", "", name or "").strip()


def _epoch(ts: Any) -> Optional[float]:
    dt = parse_iso(ts) if ts else None
    return dt.timestamp() if dt else None


def extract_next_data(html: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("__NEXT_DATA__ not found (page layout changed or blocked)")
    return json.loads(m.group(1))


class RobinhoodAdapter:
    venue = VENUE_ROBINHOOD

    def __init__(self, http: Optional[HttpClient] = None, refresh_quotes: bool = True):
        self.http = http or HttpClient(headers={"User-Agent": BROWSER_UA, "Accept": "text/html,application/json"})
        self.refresh_quotes = refresh_quotes

    # ---- raw calls -------------------------------------------------------------------
    def _page_props(self, url: str) -> dict:
        html = self.http.get(url, raw=True)
        try:
            return extract_next_data(html)["props"]["pageProps"]
        except ValueError:
            # A truncated multi-MB page (proxy quirk) loses the trailing JSON; retry via curl.
            if self.http.transport != "curl" and self.http._curl:
                self.http.transport = "curl"
                html = self.http.get(url, raw=True)
                return extract_next_data(html)["props"]["pageProps"]
            raise

    def category_page(self, category: str) -> dict:
        return self._page_props(f"{WEB}/us/en/prediction-markets/{category}/")

    def event_page(self, category: str, slug: str) -> dict:
        return self._page_props(f"{WEB}/us/en/prediction-markets/{category}/events/{slug}/")

    def quotes(self, contract_ids: Iterable[str]) -> dict[str, dict]:
        ids = list(contract_ids)
        out: dict[str, dict] = {}
        for i in range(0, len(ids), 20):
            chunk = ids[i : i + 20]
            data = self.http.get(f"{API}/marketdata/event/contract/quotes/v1/", {"ids": ",".join(chunk)}, headers={"Accept": "application/json"})
            for item in data.get("data", []):
                d = item.get("data") or {}
                if d.get("instrument_id"):
                    out[d["instrument_id"]] = d
        return out

    def events_by_id(self, event_ids: Iterable[str]) -> list[dict]:
        ids = list(event_ids)
        out: list[dict] = []
        for i in range(0, len(ids), 20):
            data = self.http.get(f"{API}/prediction-markets/v1/events", {"ids": ",".join(ids[i : i + 20])}, headers={"Accept": "application/json"})
            out.extend(data.get("results", []))
        return out

    def event_state(self, event_ids: Iterable[str]) -> dict[str, dict]:
        ids = list(event_ids)
        out: dict[str, dict] = {}
        for i in range(0, len(ids), 25):
            qs = "&".join(f"event_ids={e}" for e in ids[i : i + 25])
            data = self.http.get(f"{API}/prediction-markets/v1/event_state?{qs}", headers={"Accept": "application/json"})
            for st in data.get("eventStates", []):
                out[st.get("eventId", "")] = st
        return out

    # ---- normalisation ----------------------------------------------------------------
    def fetch(self, sport: str) -> VenueSnapshot:
        snap = VenueSnapshot(venue=self.venue, fetched_at=time.time())
        category = SPORT_CATEGORY.get(sport, sport)
        try:
            pp = self.category_page(category)
        except Exception as e:
            snap.errors.append(f"category page {category}: {e}")
            return snap
        quotes: dict[str, dict] = dict(pp.get("quotes") or {})
        states: dict[str, dict] = dict(pp.get("eventStates") or {})
        events = self.select_game_events(sport, pp.get("events") or [])
        if self.refresh_quotes:
            ids = [c["id"] for ev in events for c in ev["contracts"]]
            try:
                quotes.update(self.quotes(ids))
            except Exception as e:
                snap.errors.append(f"quotes refresh: {e}")
        self.ingest(snap, sport, category, events, quotes, states)
        return snap

    @staticmethod
    def select_game_events(sport: str, events: list[dict]) -> list[dict]:
        prefixes = SPORT_SYMBOL_PREFIXES.get(sport, ())
        out: list[dict] = []
        for ev in events:
            contracts = list((ev.get("eventContracts") or {}).values())
            if len(contracts) != 2:
                continue
            if not all(any(c.get("symbol", "").startswith(p) for p in prefixes) for c in contracts):
                continue
            if not ev.get("mutuallyExclusive", True):
                continue
            out.append({"event": ev, "contracts": contracts})
        return out

    def ingest(self, snap: VenueSnapshot, sport: str, category: str, events: list[dict], quotes: dict[str, dict], states: dict[str, dict]) -> None:
        for item in events:
            ev, contracts = item["event"], item["contracts"]
            st = states.get(ev.get("id"), {})
            start = parse_iso(st.get("gameStart")) or _event_day_from_timeline(ev.get("timeline"))
            date = et_date(start)
            names = [clean_label(c.get("displayLongName") or c.get("displayShortName") or "") for c in contracts]
            progress = str(st.get("eventProgress") or "").strip()
            in_play = _in_play_from_progress(progress, st.get("eventStatus"))
            if sport == "nfl":
                codes = [nfl_team_code(c.get("displayShortName")) or nfl_team_code(c.get("displayLongName")) or nfl_team_code(c.get("symbol", "").rsplit("-", 1)[-1]) for c in contracts]
                if any(c is None for c in codes):
                    continue
                if date is None:
                    from ..matching.normalize import kalshi_ticker_date
                    date = kalshi_ticker_date(contracts[0].get("symbol", ""))
                key = nfl_event_key(codes, date)  # type: ignore[arg-type]
                tie_rule = "unknown"  # Rothera NFL rules do not spell out ties in the public blurb
            elif sport == "tennis":
                codes = person_keys(names)
                if date is None:
                    from ..matching.normalize import kalshi_ticker_date
                    date = kalshi_ticker_date(contracts[0].get("symbol", ""))
                key = tennis_event_key(names, date)
                tie_rule = "void"
            else:
                codes = [c.get("displayShortName") or c.get("symbol", "").rsplit("-", 1)[-1] for c in contracts]
                key = f"{sport}:" + "|".join(sorted(codes)) + f":{date or ''}"
                tie_rule = "unknown"
            if not key:
                continue
            slug = (ev.get("urlSlugs") or [ev.get("id")])[0]
            url = f"{WEB}/us/en/prediction-markets/{category}/events/{slug}/"
            info = EventInfo(event_key=key, sport=sport, market_type="moneyline", outcomes=sorted(codes), labels={codes[i]: names[i] for i in range(2)}, start_time=start, tie_rule=tie_rule, venues={self.venue: {"event_id": ev.get("id"), "slug": slug, "url": url, "exchange": exchange_from_symbol_or_enum(contracts[0].get("symbol"), contracts[0].get("exchange")), "progress": progress}}, in_play=in_play)
            snap.events.setdefault(key, info)
            for i, c in enumerate(contracts):
                qd = quotes.get(c["id"]) or {}
                exch = exchange_from_symbol_or_enum(c.get("symbol"), c.get("exchange"))
                q = OutcomeQuote(
                    venue=self.venue, venue_market_id=c["id"], event_key=key, outcome=codes[i], outcome_label=names[i],
                    ask=_f(qd.get("yes_ask_price")), bid=_f(qd.get("yes_bid_price")), ask_size=_f(qd.get("ask_size_fractional") or qd.get("ask_size")), bid_size=_f(qd.get("bid_size_fractional") or qd.get("bid_size")),
                    fee_params={"exchange": exch, "symbol": c.get("symbol"), "exchange_enum": c.get("exchange")}, url=url, ts=snap.fetched_at,
                    meta={"symbol": c.get("symbol"), "exchange": exch, "state": qd.get("state"), "last": _f(qd.get("last_trade_price")), "updated_at": qd.get("updated_at"), "no_ask": _f(qd.get("no_ask_price")), "no_bid": _f(qd.get("no_bid_price"))},
                    book_id="kalshi" if exch == "kalshi" else exch,
                    quote_time=_epoch(qd.get("ask_venue_timestamp") or qd.get("updated_at")),
                )
                snap.quotes.append(q)

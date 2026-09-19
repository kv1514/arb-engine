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
import os
import re
import time
from typing import Any, Iterable, Optional

from ..fees.robinhood import exchange_from_symbol_or_enum
from .kalshi import TENNIS_SETTLEMENT as KALSHI_TENNIS_SETTLEMENT
from ..models import VENUE_ROBINHOOD, EventInfo, OutcomeQuote, VenueSnapshot
from ..matching.normalize import et_date, fmt_line, nfl_event_key, parse_iso, person_keys, push_rule_for_line, split_pair, spread_event_key, spread_outcomes, strip_digits, tennis_event_key, ticker_pair, total_event_key, team_event_key
from ..matching.teams import TEAM_SPORTS, nfl_team_city, nfl_team_code, team_code, team_name
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
    "ncaaf": ("NCAAFGAME-", "KXNCAAFGAME-", "NX.F.OPT.CFB"),  # NX.F.OPT.* = CDNA-routed college games
    "tennis": ("KXATPMATCH-", "KXWTAMATCH-", "ATPMATCH-", "WTAMATCH-", "KXATPCHALLENGERMATCH-", "KXWTACHALLENGERMATCH-", "KXITFMATCH-", "KXITFWMATCH-"),
    "nba": ("NBAGAME-", "KXNBAGAME-"),
    "nhl": ("NHLGAME-", "KXNHLGAME-"),
    "mlb": ("MLBGAME-", "KXMLBGAME-"),
}


# One binary contract per line; each contract is its own two-outcome event.
LINE_SYMBOL_PREFIXES: dict[str, dict[str, str]] = {
    "nfl": {"NFLSPREAD-": "spread", "KXNFLSPREAD-": "spread", "NFLTOTAL-": "total", "KXNFLTOTAL-": "total"},
    "ncaaf": {"KXNCAAFSPREAD-": "spread", "KXNCAAFTOTAL-": "total"},
}
# CDNA (college) lines share the winner symbol family; the event type tells them apart.
CDNA_LINE_EVENT_TYPES = {"EVENT_TYPE_SPREAD": "spread", "EVENT_TYPE_TOTALS": "total"}
CDNA_PREFIX = "NX.F.OPT."


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
    if dt is None or dt.year < 2000:  # Go zero time "0001-01-01T00:00:00Z" on never-quoted contracts
        return None
    return dt.timestamp()


def extract_next_data(html: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("__NEXT_DATA__ not found (page layout changed or blocked)")
    return json.loads(m.group(1))


class RobinhoodAdapter:
    venue = VENUE_ROBINHOOD

    def __init__(self, http: Optional[HttpClient] = None, refresh_quotes: bool = True, with_lines: bool = True, catalogue_ttl: float = 1800.0, cache_dir: Optional[str] = None):
        self.http = http or HttpClient(headers={"User-Agent": BROWSER_UA, "Accept": "text/html,application/json"}, timeout=90)
        self.refresh_quotes = refresh_quotes
        self.with_lines = with_lines
        # The category page (~30 MB of HTML) only changes when contracts are listed/delisted,
        # so it is cached on disk; quotes are always refreshed from the API.
        # Caching is only on for the real HTTP client; injected (test) clients never touch disk.
        self.catalogue_ttl = catalogue_ttl if http is None else 0.0
        self.cache_dir = cache_dir if cache_dir is not None else os.environ.get("ARB_CACHE_DIR", os.path.join("out", "cache"))

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

    def category_page(self, category: str, use_cache: bool = True) -> dict:
        path = os.path.join(self.cache_dir, f"robinhood_{category}_catalogue.json") if self.cache_dir else None
        if use_cache and path and self.catalogue_ttl > 0 and os.path.exists(path) and time.time() - os.path.getmtime(path) < self.catalogue_ttl:
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except (OSError, ValueError):
                pass
        pp = self._page_props(f"{WEB}/us/en/prediction-markets/{category}/")
        if path and self.catalogue_ttl > 0:
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                slim = {"events": pp.get("events"), "eventStates": pp.get("eventStates"), "quotes": pp.get("quotes"), "cached_at": time.time()}
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(slim, f)
            except (OSError, TypeError):
                pass
        return pp

    def event_page(self, category: str, slug: str) -> dict:
        return self._page_props(f"{WEB}/us/en/prediction-markets/{category}/events/{slug}/")

    def quotes(self, contract_ids: Iterable[str], workers: int = 8) -> dict[str, dict]:
        """Batched (20 ids/call) and parallel — a full NFL category is ~1,500 contracts."""
        ids = list(dict.fromkeys(contract_ids))
        chunks = [ids[i : i + 20] for i in range(0, len(ids), 20)]
        out: dict[str, dict] = {}

        def one(chunk: list[str]) -> list[dict]:
            data = self.http.get(f"{API}/marketdata/event/contract/quotes/v1/", {"ids": ",".join(chunk)}, headers={"Accept": "application/json"})
            return [item.get("data") or {} for item in data.get("data", [])]

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(chunks) or 1))) as pool:
            for items in pool.map(one, chunks):
                for d in items:
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
        lines = self.select_line_contracts(sport, pp.get("events") or []) if self.with_lines else []
        if self.refresh_quotes:
            ids = [c["id"] for ev in events for c in ev["contracts"]] + [x["contract"]["id"] for x in lines]
            try:
                quotes.update(self.quotes(ids))
            except Exception as e:
                snap.errors.append(f"quotes refresh: {e}")
        self.ingest(snap, sport, category, events, quotes, states)
        if lines:
            self.ingest_lines(snap, sport, category, lines, quotes, states)
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
            # CDNA lists the two sides as separate instruments (mutuallyExclusive=false) but a
            # two-contract EVENT_TYPE_WINNER is still one game.
            if not ev.get("mutuallyExclusive", True) and ev.get("eventType") != "EVENT_TYPE_WINNER":
                continue
            out.append({"event": ev, "contracts": contracts})
        return out

    @staticmethod
    def select_line_contracts(sport: str, events: list[dict]) -> list[dict]:
        """Spread/total contracts: [{event, contract, market_type}] — one per line."""
        fams = LINE_SYMBOL_PREFIXES.get(sport, {})
        out: list[dict] = []
        for ev in events:
            cdna_type = CDNA_LINE_EVENT_TYPES.get(str(ev.get("eventType") or ""))
            for c in (ev.get("eventContracts") or {}).values():
                sym = c.get("symbol", "")
                if cdna_type and sym.startswith(CDNA_PREFIX):
                    out.append({"event": ev, "contract": c, "market_type": cdna_type})
                    continue
                for prefix, mtype in fams.items():
                    if sym.startswith(prefix):
                        out.append({"event": ev, "contract": c, "market_type": mtype})
                        break
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
            if sport in TEAM_SPORTS:
                codes = [team_code(sport, c.get("displayShortName")) or team_code(sport, c.get("displayLongName")) or team_code(sport, c.get("symbol", "").rsplit("-", 1)[-1]) for c in contracts]
                if any(c is None for c in codes) or codes[0] == codes[1]:
                    continue
                if date is None:
                    from ..matching.normalize import kalshi_ticker_date
                    date = kalshi_ticker_date(contracts[0].get("symbol", "")) or _cdna_symbol_date(contracts[0].get("symbol", ""))
                key = team_event_key(sport, codes, date)  # type: ignore[arg-type]
                tie_rule = "unknown"  # Rothera/CDNA rules do not spell out ties in the public blurb
            elif sport == "tennis":
                codes = person_keys(names)
                if date is None:
                    from ..matching.normalize import kalshi_ticker_date
                    date = kalshi_ticker_date(contracts[0].get("symbol", ""))
                key = tennis_event_key(names, date)
                tie_rule = "void"
                settlement = dict(KALSHI_TENNIS_SETTLEMENT)  # Robinhood tennis is Kalshi's book and rules
            else:
                codes = [c.get("displayShortName") or c.get("symbol", "").rsplit("-", 1)[-1] for c in contracts]
                key = f"{sport}:" + "|".join(sorted(codes)) + f":{date or ''}"
                tie_rule = "unknown"
            if not key:
                continue
            slug = (ev.get("urlSlugs") or [ev.get("id")])[0]
            url = f"{WEB}/us/en/prediction-markets/{category}/events/{slug}/"
            venue_meta: dict[str, Any] = {"event_id": ev.get("id"), "slug": slug, "url": url, "exchange": exchange_from_symbol_or_enum(contracts[0].get("symbol"), contracts[0].get("exchange")), "progress": progress}
            if sport == "tennis":
                venue_meta["settlement"] = settlement
            info = EventInfo(event_key=key, sport=sport, market_type="moneyline", outcomes=sorted(codes), labels={codes[i]: names[i] for i in range(2)}, start_time=start, tie_rule=tie_rule, venues={self.venue: venue_meta}, in_play=in_play)
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


    def ingest_lines(self, snap: VenueSnapshot, sport: str, category: str, items: list[dict], quotes: dict[str, dict], states: dict[str, dict]) -> None:
        """Spread/total contracts (Rothera ``NFLSPREAD-…``/``NFLTOTAL-…`` or Kalshi-routed)."""
        team_sport = sport in TEAM_SPORTS
        for item in items:
            ev, c, mtype = item["event"], item["contract"], item["market_type"]
            sym = c.get("symbol", "")
            line = _f(c.get("floorStrikeValue"))
            is_cdna = sym.startswith(CDNA_PREFIX)
            pair = None if is_cdna else ticker_pair(sym.rsplit("-", 1)[0])
            if line is None or (not pair and not is_cdna):
                continue
            st = states.get(ev.get("id"), {})
            start = parse_iso(st.get("gameStart")) or _event_day_from_timeline(ev.get("timeline"))
            date = et_date(start)
            if date is None:
                from ..matching.normalize import kalshi_ticker_date
                date = kalshi_ticker_date(sym) or _cdna_symbol_date(sym)
            exch = exchange_from_symbol_or_enum(sym, c.get("exchange"))
            slug = (ev.get("urlSlugs") or [ev.get("id")])[0]
            url = f"{WEB}/us/en/prediction-markets/{category}/events/{slug}/"
            if is_cdna:
                # "Portland State vs Oregon: Spread" + contract "Oregon -93.5 points" / "Over 44.5 points"
                game_codes = _cdna_game_codes(sport, ev.get("name"))
                if not game_codes:
                    continue
                if mtype == "spread":
                    fav = _cdna_spread_team(sport, c.get("displayShortName") or c.get("displayLongName") or "")
                    dog = next((x for x in game_codes if x != fav), None) if fav in game_codes else None
                    if not fav or not dog:
                        continue
                    key = spread_event_key(sport, [fav, dog], date, fav, line)
                    yes_key, no_key = spread_outcomes(fav, dog, line)
                    labels = {yes_key: f"{team_name(sport, fav)} -{fmt_line(line)}", no_key: f"{team_name(sport, dog)} +{fmt_line(line)}"}
                    outcomes = [yes_key, no_key]
                else:
                    if not re.match(r"^\s*over\b", str(c.get("displayShortName") or c.get("displayLongName") or ""), re.I):
                        continue  # each line is listed once as "Over X"; the NO side is the under
                    key = total_event_key(sport, list(game_codes), date, line)
                    yes_key, no_key = "over", "under"
                    labels = {"over": f"Over {fmt_line(line)}", "under": f"Under {fmt_line(line)}"}
                    outcomes = ["over", "under"]
                pair = "".join(game_codes)
            elif mtype == "spread":
                team_raw = strip_digits(sym.rsplit("-", 1)[-1])
                other_raw = split_pair(pair, team_raw)
                fav = team_code(sport, team_raw) if team_sport else team_raw
                dog = (team_code(sport, other_raw) if team_sport else other_raw) if other_raw else None
                if not fav or not dog:
                    continue
                key = spread_event_key(sport, [fav, dog], date, fav, line)
                yes_key, no_key = spread_outcomes(fav, dog, line)
                labels = {yes_key: f"{team_name(sport, fav) if team_sport else fav} -{fmt_line(line)}", no_key: f"{team_name(sport, dog) if team_sport else dog} +{fmt_line(line)}"}
                outcomes = [yes_key, no_key]
            else:
                codes: list[str] = []
                for cut in range(2, len(pair) - 1):
                    a, b = pair[:cut], pair[cut:]
                    if team_sport and team_code(sport, a) and team_code(sport, b):
                        codes = [team_code(sport, a), team_code(sport, b)]  # type: ignore[list-item]
                        break
                if not codes:
                    continue
                key = total_event_key(sport, codes, date, line)
                yes_key, no_key = "over", "under"
                labels = {"over": f"Over {fmt_line(line)}", "under": f"Under {fmt_line(line)}"}
                outcomes = ["over", "under"]
            progress = str(st.get("eventProgress") or "").strip()
            info = EventInfo(event_key=key, sport=sport, market_type=mtype, outcomes=outcomes, labels=labels, start_time=start, line=line, tie_rule=push_rule_for_line(line), venues={self.venue: {"event_id": ev.get("id"), "contract_id": c["id"], "slug": slug, "url": url, "exchange": exch}, "_teams": {"title": _pair_title(pair, sport)}}, in_play=_in_play_from_progress(progress, st.get("eventStatus")))
            snap.events.setdefault(key, info)
            qd = quotes.get(c["id"]) or {}
            common = dict(venue=self.venue, event_key=key, fee_params={"exchange": exch, "symbol": sym}, url=url, ts=snap.fetched_at, book_id="kalshi" if exch == "kalshi" else exch, quote_time=_epoch(qd.get("ask_venue_timestamp") or qd.get("updated_at")))
            snap.quotes.append(OutcomeQuote(venue_market_id=c["id"], outcome=yes_key, outcome_label=labels[yes_key], ask=_f(qd.get("yes_ask_price")), bid=_f(qd.get("yes_bid_price")), ask_size=_f(qd.get("ask_size_fractional") or qd.get("ask_size")), bid_size=_f(qd.get("bid_size_fractional") or qd.get("bid_size")), meta={"symbol": sym, "exchange": exch, "side": "yes", "line": line, "contract_id": c["id"]}, **common))
            snap.quotes.append(OutcomeQuote(venue_market_id=c["id"] + "#no", outcome=no_key, outcome_label=labels[no_key], ask=_f(qd.get("no_ask_price")), bid=_f(qd.get("no_bid_price")), ask_size=_f(qd.get("bid_size_fractional") or qd.get("bid_size")), bid_size=_f(qd.get("ask_size_fractional") or qd.get("ask_size")), meta={"symbol": sym, "exchange": exch, "side": "no", "line": line, "contract_id": c["id"]}, **common))


def _pair_title(pair: str, sport: str) -> str:
    """'DETBUF' -> 'DET @ BUF' (Kalshi/Rothera pairs are away then home)."""
    for cut in range(2, len(pair) - 1):
        a, b = pair[:cut], pair[cut:]
        if sport not in TEAM_SPORTS or (team_code(sport, a) and team_code(sport, b)):
            return f"{a} @ {b}"
    return pair


def _cdna_game_codes(sport: str, event_name: Any) -> Optional[tuple[str, str]]:
    """'Portland State vs Oregon: Spread' -> ('PRST', 'ORE') (away, home)."""
    name = re.sub(r":.*$", "", str(event_name or "")).strip()
    parts = [x.strip() for x in re.split(r"\s+vs\.?\s+", name, maxsplit=1)]
    if len(parts) != 2:
        return None
    a, b = team_code(sport, parts[0]), team_code(sport, parts[1])
    return (a, b) if a and b and a != b else None


def _cdna_spread_team(sport: str, contract_name: str) -> Optional[str]:
    """'Oregon -93.5 points' -> 'ORE' (the favourite named on the contract)."""
    m = re.match(r"^\s*(.+?)\s+[-+]\d", contract_name or "")
    return team_code(sport, m.group(1)) if m else None


def _cdna_symbol_date(symbol: str) -> Optional[str]:
    """'NX.F.OPT.CFB-00002-260918-M.O.1.1.20270228' -> '2026-09-18' (game date, YYMMDD)."""
    m = re.search(r"-(\d{2})(\d{2})(\d{2})-", symbol or "")
    return f"20{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None

"""Analyse one Robinhood event page URL across venues (what the overlay shows).

Robinhood re-uses Kalshi's ticker scheme, so a Rothera contract ``NFLGAME-26SEP20PHITEN-PHI``
maps to Kalshi ``KXNFLGAME-26SEP20PHITEN-PHI`` and a Kalshi-routed contract keeps its ticker.
Polymarket NFL moneylines live at ``nfl-{away}-{home}-{utc-date}``; tennis uses the public
search endpoint and matches on the players' surnames.

Polling discipline (the overlay asks every second per open tab, the bridge is threaded):

* every raw venue payload goes through a :class:`TtlCache` — Robinhood quotes, the Kalshi
  market batch, the Polymarket market and CLOB book at ``quotes_ttl`` (1 s), the 1-3 MB
  Robinhood event page and the resolved Polymarket slug at ``page_ttl`` (60 s) — with
  single-flight per key, so two tabs on one game cost one fetch and a poll never serves a
  quote older than ``quotes_max_age`` (2 s; a failed or timed-out refresh may fall back to
  a cached payload that young, never older). A reader waits at most ``venue_timeout`` for
  the fetch in flight on its key (:class:`FetchInFlight` otherwise), so a hung venue never
  parks one thread per poll per tab behind it. ``fresh=True`` bypasses every cache;
* the three venues are fetched concurrently (:meth:`EventAnalyzer._run_venues`) with a
  per-venue ``venue_timeout``: a slow venue is reported in ``errors`` / ``venue_status`` and
  the other venues' rows are still returned in their fixed order, so one stalled API cannot
  freeze the strip. The result carries ``timings`` (seconds per venue and total) and
  ``venue_status`` (ok / cache hit / error per venue) for the bridge's ``/health``;
* ``clock`` is the one time source for cache ages and quote timestamps (tests mock it).
"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional

from .fees.registry import fee_model_for
from .fees.robinhood import exchange_from_symbol_or_enum
from .matching.normalize import fmt_line, kalshi_ticker_date, person_key, person_keys, push_rule_for_line, split_pair, spread_event_key, spread_outcomes, strip_digits, ticker_pair, total_event_key
from .matching.teams import nfl_team_city, nfl_team_code, team_code, team_name
from .models import EventInfo, OutcomeQuote
from .quant.arbitrage import Leg, best_leg_per_outcome, evaluate, max_price_for_leg
from .quant.fairvalue import consensus_fair_value
from .venues.kalshi import KalshiClient
from .venues.polymarket import GAMMA, PolymarketAdapter, _f, _jl
from .matching.normalize import parse_iso
from .venues.robinhood import LINE_SYMBOL_PREFIXES, RobinhoodAdapter, _event_day_from_timeline, _in_play_from_progress, clean_label, tie_payout_for
from .matching.teams import TEAM_SPORTS


def polymarket_order_meta(m: dict) -> dict[str, Any]:
    """Order-grid facts the arbitrage math needs from a Gamma market, the same keys the scan
    adapter sets: ``tick`` (``orderPriceMinTickSize``, 0.001 on tails), ``min_size``
    (``orderMinSize``, 5 shares) and ``restricted`` (Gamma's US flag -> signal-only row).

    Parity note: every recorded Gamma sports market is ``restricted``, so bridge-mode
    ``/analyze`` keeps the Polymarket row for the fair value but never uses it as a leg
    (``signal-only:polymarket``), exactly as ``scan`` does. The overlay's direct mode
    (``extension/arb-core.js``) does not read this flag yet and can still show a Polymarket
    leg for the same event until the JS twin consumes ``tests/fixtures/arb_vectors.json``
    and the compliance table lands (later items)."""
    out: dict[str, Any] = {"tick": _f(m.get("orderPriceMinTickSize")), "min_size": _f(m.get("orderMinSize"))}
    if m.get("restricted") is True:
        out["restricted"] = True
    return out

_SYM = re.compile(r"^(KX)?([A-Z0-9]+)-(\d{2})([A-Z]{3})(\d{2})([A-Z0-9]+)-([A-Z0-9]+)$")


def parse_symbol(symbol: str) -> Optional[dict[str, Any]]:
    m = _SYM.match(symbol or "")
    if not m:
        return None
    pair, side = m.group(6), m.group(7)
    teams = None
    if pair.endswith(side):
        teams = [pair[: -len(side)], side]
    elif pair.startswith(side):
        teams = [side, pair[len(side):]]
    return {"family": m.group(2), "date": kalshi_ticker_date(symbol), "pair": pair, "side": side, "teams": teams, "kalshi_ticker": ("" if m.group(1) else "KX") + symbol, "routed": "kalshi" if m.group(1) else "other"}


def polymarket_nfl_slugs(teams: list[str], date: str) -> list[str]:
    from datetime import datetime, timedelta

    d0 = datetime.strptime(date, "%Y-%m-%d")
    out = []
    for d in (d0, d0 + timedelta(days=1)):
        ds = d.strftime("%Y-%m-%d")
        a, b = teams[0].lower(), teams[1].lower()
        out += [f"nfl-{a}-{b}-{ds}", f"nfl-{b}-{a}-{ds}"]
    return out


@dataclass
class Cached:
    """One cache read: ``value`` fetched at ``at`` (clock seconds), ``age`` seconds old at
    the read, ``status`` one of ``hit`` (inside the TTL), ``miss`` (fetched now) or ``stale``
    (the refresh failed and a payload no older than ``stale_ok`` was served instead)."""

    value: Any
    at: float
    age: float
    status: str
    error: Optional[str] = None


class CacheMiss(Exception):
    """``TtlCache.peek`` found nothing young enough (a stale-only read after a timeout)."""


class FetchInFlight(Exception):
    """``TtlCache.get`` gave up waiting for the fetch another thread has in flight on the
    same key (``lock_timeout``) and had no payload young enough to serve instead."""


class _Failed:
    """A fetch that raised inside the TTL: re-raised to every reader until it expires, so a
    venue that is down is asked once per TTL, not once per open tab per poll."""

    __slots__ = ("exc",)

    def __init__(self, exc: BaseException):
        self.exc = exc


class TtlCache:
    """Thread-safe read-through cache with single-flight per key.

    ``get`` returns the cached value while it is younger than ``ttl``; otherwise one caller
    fetches under a per-key lock while the others wait for its result (a stalled fetch
    therefore stalls only its own key, and a late result still lands for the next poll).
    A failed fetch may fall back to a payload at most ``stale_ok`` seconds old — that is
    the "never older than ~2 s" bound for quotes; with nothing that young the failure itself
    is remembered for ``ttl`` and re-raised (``errors``), so a down venue is retried once per
    TTL rather than hammered. ``fresh=True`` refetches regardless. A reader waits at most
    ``lock_timeout`` seconds for the fetch in flight on its key: past it the ``stale_ok``
    fallback applies or :class:`FetchInFlight` is raised, so a fetch hung in a TCP connect
    parks only its own thread, not one per poll per tab. ``hits`` / ``misses`` / ``stale`` /
    ``errors`` feed the bridge's cache hit rates."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._entries: dict[Any, tuple[float, Any]] = {}
        self._locks: dict[Any, threading.Lock] = {}
        self._mu = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.stale = 0
        self.errors = 0

    def _key_lock(self, key: Any) -> threading.Lock:
        with self._mu:
            lk = self._locks.get(key)
            if lk is None:
                lk = self._locks[key] = threading.Lock()
            return lk

    def _entry(self, key: Any) -> Optional[tuple[float, Any]]:
        with self._mu:
            return self._entries.get(key)

    def peek(self, key: Any, max_age: float) -> Optional[Cached]:
        """The cached value if it is at most ``max_age`` seconds old, else ``None``; never
        fetches and never reports a remembered failure."""
        ent = self._entry(key)
        if ent is None or isinstance(ent[1], _Failed):
            return None
        age = self.clock() - ent[0]
        if age > max_age:
            return None
        return Cached(ent[1], ent[0], max(0.0, age), "hit")

    def _young(self, key: Any, ttl: float) -> Optional[Cached]:
        """A hit strictly inside the TTL (counted), a remembered failure inside the TTL
        (re-raised), or ``None`` (fetch)."""
        ent = self._entry(key)
        if ent is None:
            return None
        age = self.clock() - ent[0]
        if age >= ttl:
            return None
        if isinstance(ent[1], _Failed):
            raise ent[1].exc
        with self._mu:
            self.hits += 1
        return Cached(ent[1], ent[0], max(0.0, age), "hit")

    def put(self, key: Any, value: Any) -> None:
        with self._mu:
            self._entries[key] = (self.clock(), value)

    def _stale_or(self, key: Any, stale_ok: float, err: str, exc: Optional[BaseException] = None) -> Cached:
        """The ``stale_ok`` fallback for a refresh that failed (``exc`` is remembered for the
        TTL) or could not be waited for (nothing remembered: the fetch in flight will land)."""
        old = self.peek(key, stale_ok) if stale_ok > 0 else None
        if old is not None:
            with self._mu:
                self.stale += 1
            return Cached(old.value, old.at, old.age, "stale", error=err)
        if exc is not None:
            with self._mu:
                self._entries[key] = (self.clock(), _Failed(exc))
                self.errors += 1
            raise exc
        raise FetchInFlight(err)

    def get(self, key: Any, ttl: float, fetch: Callable[[], Any], fresh: bool = False, stale_ok: float = 0.0, lock_timeout: Optional[float] = None) -> Cached:
        if not fresh:
            hit = self._young(key, ttl)
            if hit is not None:
                return hit
        lk = self._key_lock(key)
        if not lk.acquire(timeout=-1 if lock_timeout is None else max(0.0, lock_timeout)):
            return self._stale_or(key, stale_ok, f"a fetch already in flight has not answered within {lock_timeout:g}s")
        try:
            if not fresh:  # another thread may have refreshed it while we waited
                hit = self._young(key, ttl)
                if hit is not None:
                    return hit
            try:
                value = fetch()
            except Exception as e:
                return self._stale_or(key, stale_ok, str(e), exc=e)
            at = self.clock()
            with self._mu:
                self._entries[key] = (at, value)
                self.misses += 1
            return Cached(value, at, 0.0, "miss")
        finally:
            lk.release()

    def stats(self) -> dict[str, Any]:
        with self._mu:
            h, m, st, er = self.hits, self.misses, self.stale, self.errors
            n_ent = len(self._entries)
        n = h + m + st
        return {"hits": h, "misses": m, "stale": st, "errors": er, "hit_rate": (round(h / n, 3) if n else None), "entries": n_ent}


@dataclass
class VenueResult:
    """What one venue task produced: ``value`` (or ``None``), the error string that goes to
    ``analysis.errors``, seconds spent, and the cache status of its quote payload."""

    value: Any = None
    error: Optional[str] = None
    seconds: float = 0.0
    cache: Optional[str] = None
    ok: bool = False

    def as_status(self) -> dict[str, Any]:
        return {"ok": self.ok, "seconds": round(self.seconds, 4), "cache": self.cache, "error": self.error}


def _apply_books(quotes: list[OutcomeQuote], books: dict[str, tuple[Any, dict[str, Any]]]) -> None:
    """Top-of-book prices/sizes and tick/min-size meta onto the Polymarket quotes — the same
    mapping as ``PolymarketAdapter.attach_books_for``, applied here so the CLOB payload can
    sit in the 1 s cache instead of being re-fetched every poll."""
    for q in quotes:
        got = books.get(q.venue_market_id)
        if got is None:
            continue
        b, bmeta = got
        q.book = b
        q.meta.update(bmeta)
        if "tick_size" in bmeta:
            q.meta["tick"] = bmeta["tick_size"]
        if "min_order_size" in bmeta:
            q.meta["min_size"] = bmeta["min_order_size"]
        if b.asks:
            q.ask, q.ask_size = b.asks[0].price, b.asks[0].size
        if b.bids:
            q.bid, q.bid_size = b.bids[0].price, b.bids[0].size


class EventAnalyzer:
    #: raw-payload TTLs (seconds); ``quotes_max_age`` bounds what a failed refresh may serve.
    QUOTES_TTL = 1.0
    QUOTES_MAX_AGE = 2.0
    PAGE_TTL = 60.0
    VENUE_TIMEOUT = 2.0

    def __init__(self, robinhood: Optional[RobinhoodAdapter] = None, kalshi: Optional[KalshiClient] = None, polymarket: Optional[PolymarketAdapter] = None):
        self.rh = robinhood or RobinhoodAdapter()
        self.kalshi = kalshi or KalshiClient(env="prod")
        self.pm = polymarket or PolymarketAdapter()
        self.clock: Callable[[], float] = time.time   # one time source for cache ages and quote ts (tests mock it)
        self._series_cache: dict[str, dict] = {}
        self.page_ttl = self.PAGE_TTL         # the event page only changes when contracts are (de)listed; quotes are refreshed live
        self.quotes_ttl = self.QUOTES_TTL     # Robinhood quotes / Kalshi markets / Polymarket market + book
        self.quotes_max_age = self.QUOTES_MAX_AGE  # a failed or timed-out refresh may serve a payload this old, never older
        self.venue_timeout = self.VENUE_TIMEOUT    # per-venue wall clock inside one analyze_url
        self.pm_miss_ttl = self.PM_MISS_TTL        # a Polymarket lookup that found no market is not repeated for this long
        clk = lambda: self.clock()  # noqa: E731  (late-bound so reassigning .clock reaches every cache)
        self.page_cache = TtlCache(clk)       # slug -> (category, page props)
        self.quote_cache = TtlCache(clk)      # ("rh"|"kalshi"|"pm-market"|"pm-books"|..., key) -> raw payload
        self.slug_cache = TtlCache(clk)       # Polymarket lookup key -> resolved slug (catalogue, page_ttl)
        self.last_event = None            # MergedEvent from the last game-winner analyze_url
        self.last_url: Optional[str] = None
        self.last_analyzed_at: float = 0.0
        self.recent_events: dict[str, tuple[Any, float]] = {}  # url -> (MergedEvent, analyzed_at); one slot per open tab
        self._recent_mu = threading.Lock()

    PUBLIC_CATEGORIES = ("nfl", "tennis", "college-football", "nba", "nhl", "baseball", "soccer", "mma", "golf", "pro-football", "esports", "cricket")

    def resolve_public_event(self, slug: str) -> tuple[Optional[str], Optional[dict]]:
        """Find the public category page that serves this event slug (cached per slug)."""
        cache = getattr(self, "_slug_category", None)
        if cache is None:
            cache = self._slug_category = {}
        cats = ([cache[slug]] if slug in cache else []) + [c for c in self.PUBLIC_CATEGORIES if c != cache.get(slug)]
        for cat in cats:
            try:
                pp = self.rh.event_page(cat, slug)
            except Exception:
                continue
            if pp.get("event"):
                cache[slug] = cat
                return cat, pp
        return None, None

    # ---- lookups ------------------------------------------------------------------------
    def _page(self, slug: str, category: Optional[str], fresh: bool = False):
        """Event page props for a slug, cached ``page_ttl`` seconds (the overlay polls every
        second; the 1-3 MB page only changes when contracts are listed or delisted, and quotes
        come from the quotes API on every call anyway). Single-flight: two tabs on one game
        fetch it once. Returns ``(category, pp)``; with a category given returns ``pp`` only."""

        def fetch() -> tuple[str, Optional[dict]]:
            if category:
                return category, self.rh.event_page(category, slug)
            cat, pp = self.resolve_public_event(slug)
            if pp is None:
                raise CacheMiss(f"no public event page for slug {slug!r}")  # not cached: retried next poll
            return cat or "", pp

        try:
            cat, pp = self.page_cache.get(slug, self.page_ttl, fetch, fresh=fresh).value
        except CacheMiss:
            return None, None
        return pp if category else (cat, pp)

    def _page_at(self, slug: str, default: float) -> float:
        """When the cached page for ``slug`` was fetched — the timestamp of its server-rendered
        quotes when they are the fallback for a venue that failed or timed out."""
        hit = self.page_cache.peek(slug, float("inf"))
        return hit.at if hit is not None else default

    def _series(self, ticker: str) -> dict:
        s = ticker.split("-")[0]
        if s not in self._series_cache:  # a duplicate fetch from two threads is harmless (same immutable answer)
            try:
                self._series_cache[s] = self.kalshi.series(s)
            except Exception:
                self._series_cache[s] = {"fee_type": "quadratic", "fee_multiplier": 1}
        return self._series_cache[s]

    # ---- Polymarket lookups: slug resolved once per page_ttl, market re-read per quotes_ttl ----
    def _pm_markets_by_slug(self, slug: str, fresh: bool = False) -> Cached:
        """Gamma ``/markets?slug=`` through the 1 s cache (its bestBid/bestAsk are the price
        when the CLOB book is unavailable, so they refresh with the quotes)."""
        return self.quote_cache.get(("pm-markets", slug), self.quotes_ttl, lambda: self.pm.http.get(f"{GAMMA}/markets", {"slug": slug}), fresh=fresh, stale_ok=self.quotes_max_age, lock_timeout=self.venue_timeout)

    #: a Polymarket lookup that found no market is not repeated for this long (a late listing
    #: shows up within it; without it an unlisted game costs several Gamma calls per poll per tab)
    PM_MISS_TTL = 15.0

    def _pm_resolved(self, lookup_key: tuple, pick: Callable[[list], Optional[dict]], resolve: Callable[[], Optional[tuple[str, dict, str]]], fresh: bool = False, stale_only: bool = False) -> Cached:
        """The moneyline market for ``lookup_key`` as a :class:`Cached` (``value`` is the market
        or ``None``; ``status`` is that of the payload it came from): re-read the slug resolved
        within ``page_ttl`` (one Gamma call, cached ``quotes_ttl``) and only run the full
        ``resolve`` (slug candidates / public search, several calls) when no slug is known or
        the market vanished from it. ``pick`` chooses the market out of a ``/markets?slug=``
        list; ``resolve`` returns ``(slug, market, cache_status)`` or raises when the lookups
        failed. Only a lookup that *answered* with no market is remembered ``PM_MISS_TTL``
        seconds: a Gamma error (on the known slug or inside ``resolve``) propagates to the
        venue's ``errors`` and keeps the known slug, so an outage is never reported as "not
        listed" and the row returns on the first poll after Gamma recovers. ``stale_only``
        (after a venue timeout) never fetches: the market payload cached within
        ``quotes_max_age`` or :class:`CacheMiss`."""
        known = self.slug_cache.peek(lookup_key, self.page_ttl)
        if known is not None and known.value is None and known.age >= self.pm_miss_ttl:
            known = None  # the "not listed" answer has expired
        if stale_only:
            if known is not None and known.value is None:
                return Cached(None, known.at, known.age, "stale")
            ent = self.quote_cache.peek(("pm-markets", known.value), self.quotes_max_age) if known is not None else None
            if ent is None:
                raise CacheMiss("no polymarket market payload younger than quotes_max_age")
            return Cached(pick(ent.value or []), ent.at, ent.age, "stale")
        if known is not None and not fresh:
            if known.value is None:
                return Cached(None, known.at, known.age, "hit")
            got = self._pm_markets_by_slug(known.value)   # a Gamma error propagates: slug kept, venue reports it
            m = pick(got.value or [])
            if m is not None:
                return Cached(m, got.at, got.age, got.status, error=got.error)
        found = resolve()   # raises when a lookup failed: nothing is negative-cached then
        now = self.clock()
        if found is None:
            self.slug_cache.put(lookup_key, None)
            return Cached(None, now, 0.0, "miss")
        slug, m, status = found
        self.slug_cache.put(lookup_key, slug)
        return Cached(m, now, 0.0, status)

    @staticmethod
    def _pick_moneyline(ms: list) -> Optional[dict]:
        for m in ms or []:
            if m.get("sportsMarketType") == "moneyline":
                return m
        return None

    def polymarket_nfl(self, teams: list[str], date: str, fresh: bool = False, stale_only: bool = False) -> Optional[dict]:
        return self._pm_nfl(teams, date, fresh=fresh, stale_only=stale_only).value

    def _pm_nfl(self, teams: list[str], date: str, fresh: bool = False, stale_only: bool = False) -> Cached:
        def resolve() -> Optional[tuple[str, dict, str]]:
            failed: Optional[Exception] = None
            for slug in polymarket_nfl_slugs(teams, date):
                try:
                    got = self._pm_markets_by_slug(slug, fresh=fresh)
                except Exception as e:
                    failed = e
                    continue
                m = self._pick_moneyline(got.value)
                if m is not None:
                    return slug, m, got.status
            if failed is not None:   # not "not listed": a candidate could not be read
                raise failed
            return None

        return self._pm_resolved(("nfl", tuple(teams), date), self._pick_moneyline, resolve, fresh=fresh, stale_only=stale_only)

    def _parse_cdna_college(self, contracts_raw: list[dict], names: list[str], ev: dict) -> list[Optional[dict[str, Any]]]:
        """CDNA symbols (NX.F.OPT.CFB-00027-260919-M.O.1.1.20270228) carry no team codes, so
        build the same dict parse_symbol() returns from the contract names: canonical codes,
        the game date from the symbol, and the Kalshi ticker KXNCAAFGAME-{YYMONDD}{AWAY}{HOME}-{TEAM}
        (Robinhood names the event "Away vs Home", matching Kalshi's pair order)."""
        from .matching.teams import team_table
        from .venues.robinhood import _cdna_symbol_date

        codes = [team_code("ncaaf", c.get("displayShortName")) or team_code("ncaaf", n) for c, n in zip(contracts_raw, names)]
        date = _cdna_symbol_date(contracts_raw[0].get("symbol", ""))
        if any(c is None for c in codes) or codes[0] == codes[1] or not date:
            return [None, None]
        table = team_table("ncaaf")
        kcodes = [(table.get(c) or {}).get("kalshi") or c for c in codes]
        y, m, d = date.split("-")
        mon = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"][int(m) - 1]
        # Name order "Purdue vs UCLA" = away, home -> KXNCAAFGAME-26SEP19PURUCLA
        order = list(range(len(contracts_raw)))
        name_parts = [x.strip() for x in re.split(r"\s+vs\.?\s+", str(ev.get("name") or ""), maxsplit=1)]
        if len(name_parts) == 2:
            first = team_code("ncaaf", name_parts[0])
            if first == codes[1]:
                order = [1, 0]
        pair = "".join(kcodes[i] for i in order)
        return [{"family": "CFBGAME", "date": date, "pair": pair, "side": kcodes[i], "teams": [kcodes[j] for j in order], "kalshi_ticker": f"KXNCAAFGAME-{y[2:]}{mon}{d}{pair}-{kcodes[i]}", "routed": "cdna"} for i in range(len(contracts_raw))]

    POLYMARKET_SLUG_PREFIX = {"ncaaf": "cfb", "nba": "nba", "nhl": "nhl"}

    def polymarket_cfb(self, names: list[str], outcomes: list[str], date: str) -> Optional[dict]:
        return self.polymarket_team_game("ncaaf", names, outcomes, date)

    def polymarket_team_game(self, sport: str, names: list[str], outcomes: list[str], date: str, fresh: bool = False, stale_only: bool = False) -> Optional[dict]:
        return self._pm_team_game(sport, names, outcomes, date, fresh=fresh, stale_only=stale_only).value

    def _pm_team_game(self, sport: str, names: list[str], outcomes: list[str], date: str, fresh: bool = False, stale_only: bool = False) -> Cached:
        """Moneyline on Polymarket for a table sport: search by the two team names, keep the
        event whose slug is {prefix}-…-{date} (UTC date may be the ET date or the day after),
        then read the moneyline market by slug. The slug found is remembered for ``page_ttl``
        so a poll costs one Gamma call, not a search plus one."""
        from datetime import datetime, timedelta

        prefix = self.POLYMARKET_SLUG_PREFIX.get(sport, sport)
        d0 = datetime.strptime(date, "%Y-%m-%d")
        dates = {(d0 + timedelta(days=k)).strftime("%Y-%m-%d") for k in (0, 1)}
        want = set(outcomes)

        def pick(ms: list) -> Optional[dict]:
            for mk in ms or []:
                outs = _jl(mk.get("outcomes"))
                if mk.get("sportsMarketType") == "moneyline" and len(outs) == 2 and {team_code(sport, o) for o in outs} == want:
                    return mk
            return None

        def resolve() -> Optional[tuple[str, dict, str]]:
            q = " ".join(team_name(sport, o) for o in outcomes)
            found = self.pm.http.get(f"{GAMMA}/public-search", {"q": q, "limit_per_type": 10})   # a failed search propagates
            failed: Optional[Exception] = None
            for cand in (found or {}).get("events") or []:
                slug = str(cand.get("slug") or "")
                m = re.match("^" + re.escape(prefix) + r"-[a-z0-9]+-[a-z0-9]+-(\d{4}-\d{2}-\d{2})$", slug)
                if not m or m.group(1) not in dates:
                    continue
                try:
                    got = self._pm_markets_by_slug(slug, fresh=fresh)
                    ms, status = got.value, got.status
                except Exception as e:
                    failed, ms, status = e, None, "miss"
                mk = pick(ms or [])
                if mk is not None:
                    return slug, mk, status
                if not ms:  # closed/odd markets: read the event itself
                    try:
                        evs = self.pm.http.get(f"{GAMMA}/events", {"slug": slug})
                    except Exception as e:
                        failed, evs = e, None
                    mk = pick((evs or [{}])[0].get("markets") or [])
                    if mk is not None:
                        return slug, mk, "miss"
            if failed is not None:   # a candidate could not be read: not "not listed"
                raise failed
            return None

        return self._pm_resolved((sport, tuple(sorted(outcomes)), date), pick, resolve, fresh=fresh, stale_only=stale_only)

    def polymarket_tennis(self, names: list[str]) -> Optional[dict]:
        q = " ".join(person_key(n) for n in names)
        keys = sorted(person_keys(names))
        try:
            d = self.pm.http.get(f"{GAMMA}/public-search", {"q": q, "limit_per_type": 3})
        except Exception:
            return None
        for ev in d.get("events", []):
            for m in ev.get("markets", []):
                if m.get("sportsMarketType") != "moneyline" or m.get("closed"):
                    continue
                outs = _jl(m.get("outcomes"))
                if len(outs) == 2 and sorted(person_keys(outs)) == keys:
                    m.setdefault("events", [{"slug": ev.get("slug")}])
                    return m
        return None

    # ---- cached raw payloads and the concurrent venue runner ------------------------------
    def _raw(self, key: tuple, fetch: Callable[[], Any], fresh: bool, stale_only: bool) -> Cached:
        """One raw venue payload through ``quote_cache``: ``stale_only`` (the venue task is
        being re-run after its timeout) never fetches and only accepts a payload at most
        ``quotes_max_age`` old, raising :class:`CacheMiss` otherwise. A live read waits at
        most ``venue_timeout`` for a fetch another poll has in flight on the same key."""
        if stale_only:
            hit = self.quote_cache.peek(key, self.quotes_max_age)
            if hit is None:
                raise CacheMiss(f"no {key[0]} payload younger than {self.quotes_max_age:g}s")
            hit.status = "stale"
            return hit
        return self.quote_cache.get(key, self.quotes_ttl, fetch, fresh=fresh, stale_ok=self.quotes_max_age, lock_timeout=self.venue_timeout)

    def _rh_quotes(self, ids: list[str], fresh: bool = False, stale_only: bool = False) -> Cached:
        return self._raw(("rh", tuple(ids)), lambda: self.rh.quotes(ids), fresh, stale_only)

    def _kalshi_markets(self, tickers: list[str], fresh: bool = False, stale_only: bool = False) -> Cached:
        """``{ticker: market}`` for the batch plus the per-ticker error strings, one cache
        entry (the two sides of a game expire together, so a poll is all-or-nothing)."""

        def fetch() -> tuple[dict[str, dict], list[str]]:
            got: dict[str, dict] = {}
            errs: list[str] = []
            with ThreadPoolExecutor(max_workers=max(1, len(tickers))) as pool:
                for t, res in zip(tickers, pool.map(lambda t: self._try(lambda: self.kalshi.market(t)), tickers)):
                    if isinstance(res, Exception):
                        errs.append(f"kalshi {t}: {res}")
                    else:
                        got[t] = res
            if tickers and not got:  # the venue is down, not the markets: let the stale fallback apply
                raise RuntimeError("; ".join(errs))
            return got, errs

        return self._raw(("kalshi", tuple(tickers)), fetch, fresh, stale_only)

    def _kalshi_event_markets(self, event_ticker: str, fresh: bool = False, stale_only: bool = False) -> Cached:
        return self._raw(("kalshi-event", event_ticker), lambda: self.kalshi.get("/markets", {"event_ticker": event_ticker, "limit": 200}).get("markets", []), fresh, stale_only)

    def _pm_books(self, tokens: list[str], fresh: bool = False, stale_only: bool = False) -> Cached:
        return self._raw(("pm-books", tuple(tokens)), lambda: self.pm.books_with_meta(tokens), fresh, stale_only)

    @staticmethod
    def _stale_note(venue: str, got: Cached, errs: list[str]) -> None:
        """A refresh that failed and fell back to a cached payload is reported, with the age
        (once: the Polymarket market and book payloads may fail with the same message)."""
        if got.status == "stale" and got.error:
            msg = f"{venue}: refresh failed ({got.error}); serving {got.age:.1f}s-old quotes"
            if msg not in errs:
                errs.append(msg)

    @staticmethod
    def _try(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as e:  # returned, not raised: pool.map must keep the ticker order
            return e

    def _run_venues(self, tasks: dict[str, Callable[[bool], tuple[Any, list[str], Optional[str]]]], timeout: Optional[float] = None) -> dict[str, VenueResult]:
        """Run one task per venue concurrently and collect them in the order given (so the
        quotes_by_venue order and each venue's quote order never depend on which API answered
        first). A task takes ``stale_only`` and returns ``(value, errors, cache_status)``.

        Each venue gets ``timeout`` seconds of wall clock from the start; a task past it is
        re-run in stale-only mode (cached payload at most ``quotes_max_age`` old) and its
        venue is reported ``timed out`` in ``errors`` — the other venues' rows are kept. The
        timed-out thread keeps running and its late result still lands in the cache for the
        next poll; the pool is not joined so the request returns on time."""
        timeout = self.venue_timeout if timeout is None else timeout
        t_start = time.perf_counter()
        pool = ThreadPoolExecutor(max_workers=max(1, len(tasks)), thread_name_prefix="venue")

        def timed(fn: Callable[[bool], Any]) -> Callable[[], tuple[Any, float]]:
            def run() -> tuple[Any, float]:
                t0 = time.perf_counter()
                return fn(False), time.perf_counter() - t0
            return run

        futs = {v: pool.submit(timed(fn)) for v, fn in tasks.items()}
        out: dict[str, VenueResult] = {}
        for v, fut in futs.items():
            try:
                (value, errs, status), secs = fut.result(timeout=max(0.0, t_start + timeout - time.perf_counter()))
                out[v] = VenueResult(value=value, error="; ".join(errs) if errs else None, seconds=secs, cache=status, ok=status != "stale")
            except FutureTimeout:
                msg = f"{v}: timed out after {timeout:g}s"
                try:
                    value, errs, status = tasks[v](True)
                    served = {"stale": "serving cached quotes", "page": "serving the event page's quotes"}.get(status or "", "serving cached data")
                    out[v] = VenueResult(value=value, error="; ".join([msg + f" ({served})"] + list(errs)), seconds=timeout, cache=status, ok=False)
                except Exception:
                    out[v] = VenueResult(value=None, error=msg, seconds=timeout, cache=None, ok=False)
            except Exception as e:
                out[v] = VenueResult(value=None, error=f"{v}: {e}", seconds=time.perf_counter() - t_start, cache=None, ok=False)
        pool.shutdown(wait=False)
        return out

    @staticmethod
    def _timings(results: dict[str, VenueResult], t0: float) -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
        timings = {v: round(r.seconds, 4) for v, r in results.items()}
        timings["total"] = round(time.perf_counter() - t0, 4)
        return timings, {v: r.as_status() for v, r in results.items()}

    RECENT_KEEP_S = 120.0  # a tab's last MergedEvent is dropped this long after its scan

    def _remember(self, url: str, me: Any, now: float) -> None:
        """One ``recent_events`` slot per URL (open tab) for the bridge's ``/inplay`` reuse;
        slots older than ``RECENT_KEEP_S`` are dropped so closed tabs do not accumulate."""
        with self._recent_mu:
            self.recent_events[url] = (me, now)
            for u in [u for u, (_, at) in self.recent_events.items() if now - at > self.RECENT_KEEP_S]:
                del self.recent_events[u]

    # ---- analysis -----------------------------------------------------------------------
    def analyze_url(self, url: str, settings: Optional[dict[str, Any]] = None, contracts: float = 100, target_margin: float = 0.0, emit_no_side: bool = False, executable_venues: Optional[set[str]] = None, fresh: bool = False) -> dict[str, Any]:
        """``emit_no_side`` adds the NO side of each Robinhood game contract as its own leg
        (off: the overlay keeps today's row counts); ``executable_venues`` marks the other
        venues signal-only (default: ``scanner.resolve_executable_venues(settings)``);
        ``fresh`` bypasses every cache (the bridge's ``?fresh=1``). The result carries
        ``timings`` and ``venue_status`` beside ``analysis`` (module docstring)."""
        settings = settings or {}
        t0 = time.perf_counter()
        m = re.search(r"/prediction-markets/([^/]+)/events/([^/?#]+)", url)
        if m:
            category, slug = m.group(1), m.group(2)
            pp = self._page(slug, category, fresh=fresh)
        else:
            # Logged-in trading route: robinhood.com/events/<slug>?contract=<id> (client-rendered,
            # no category in the URL). The public page for the same slug carries the event data.
            m = re.search(r"robinhood\.com/events/([^/?#]+)", url)
            if not m:
                return {"ok": False, "error": "not a Robinhood prediction-market event URL"}
            slug = m.group(1)
            category, pp = self._page(slug, None, fresh=fresh)
            if pp is None:
                return {"ok": False, "error": f"could not find a public event page for slug {slug!r}"}
        ev = pp.get("event") or {}
        contracts_raw = list((ev.get("eventContracts") or {}).values())
        line_types = {mt for c in contracts_raw for pfx, mt in LINE_SYMBOL_PREFIXES.get("nfl", {}).items() if str(c.get("symbol", "")).startswith(pfx)}
        if contracts_raw and len(line_types) == 1:
            return self.analyze_lines(url, pp, ev, contracts_raw, line_types.pop(), settings=settings, contracts=contracts, target_margin=target_margin, executable_venues=executable_venues, fresh=fresh, t0=t0)
        if len(contracts_raw) != 2:
            return {"ok": True, "event": {"id": ev.get("id"), "name": ev.get("name")}, "analysis": None, "note": f"{len(contracts_raw)} contracts — only two-outcome game/match markets are analysed"}
        names = [clean_label(c.get("displayLongName") or c.get("displayShortName") or "") for c in contracts_raw]
        parsed = [parse_symbol(c.get("symbol", "")) for c in contracts_raw]
        is_cdna_college = all(str(c.get("symbol", "")).startswith("NX.F.OPT.CFB") for c in contracts_raw)
        if is_cdna_college:
            parsed = self._parse_cdna_college(contracts_raw, names, ev)
        if any(p is None for p in parsed):
            return {"ok": True, "event": {"id": ev.get("id"), "name": ev.get("name")}, "analysis": None, "note": "unrecognised contract symbols"}
        family = parsed[0]["family"]  # type: ignore[index]
        is_nfl = family == "NFLGAME"
        is_ncaaf = family in ("NCAAFGAME", "CFBGAME")
        table_sport = {"NCAAFGAME": "ncaaf", "CFBGAME": "ncaaf", "NBAGAME": "nba", "NHLGAME": "nhl"}.get(family)
        is_tennis = bool(re.search(r"(ATP|WTA|ITF)", family) and "MATCH" in family)
        sport = "nfl" if is_nfl else table_sport if table_sport else "tennis" if is_tennis else "other"
        if is_nfl:
            outcomes = [nfl_team_code(c.get("displayShortName")) or nfl_team_code(n) or p["side"] for c, n, p in zip(contracts_raw, names, parsed)]  # type: ignore[index]
        elif table_sport:
            outcomes = [team_code(table_sport, c.get("displayShortName")) or team_code(table_sport, n) or team_code(table_sport, p["side"]) or p["side"] for c, n, p in zip(contracts_raw, names, parsed)]  # type: ignore[index]
        else:
            outcomes = person_keys(names)
        labels = dict(zip(outcomes, names))
        event_key = f"{sport}:" + "|".join(sorted(outcomes)) + f":{parsed[0]['date'] or ''}"  # type: ignore[index]
        now = self.clock()
        ssr = pp.get("quotes") or {}
        ids = [c["id"] for c in contracts_raw]

        def robinhood(stale_only: bool) -> tuple[list[OutcomeQuote], list[str], Optional[str]]:
            errs: list[str] = []
            try:
                got = self._rh_quotes(ids, fresh=fresh, stale_only=stale_only)
                live, ts, status = got.value, got.at, got.status
                self._stale_note("robinhood", got, errs)
            except Exception as e:
                # No quote younger than quotes_max_age: the page's server-rendered quotes still
                # price the rows, timestamped at the page fetch so the quote-old gate sees their age.
                live, ts, status = {}, self._page_at(slug, now), "page"
                if not isinstance(e, CacheMiss):   # a stale-only miss is expected; a failure / in-flight wait is reported
                    errs.append(f"robinhood quotes: {e}")
            rows: list[OutcomeQuote] = []
            for i, (c, o, p) in enumerate(zip(contracts_raw, outcomes, parsed)):
                qd = live.get(c["id"]) or ssr.get(c["id"]) or {}
                exch = exchange_from_symbol_or_enum(c.get("symbol"), c.get("exchange"))
                common = dict(venue="robinhood", event_key=event_key, fee_params={"exchange": exch, "symbol": c.get("symbol")}, url=url, ts=ts, book_id="kalshi" if exch == "kalshi" else exch)
                meta: dict[str, Any] = {"exchange": exch, "symbol": c.get("symbol"), "side": "yes", "contract_id": c["id"]}
                tie_yes = tie_payout_for(exch, "yes") if sport in TEAM_SPORTS else None
                if tie_yes is not None:
                    meta["tie_payout"] = tie_yes
                rows.append(OutcomeQuote(venue_market_id=c["id"], outcome=o, outcome_label=labels[o], ask=_f(qd.get("yes_ask_price")), bid=_f(qd.get("yes_bid_price")), ask_size=_f(qd.get("ask_size")), meta=meta, **common))
                if emit_no_side and len(outcomes) == 2:
                    # NO on this team is a bet on the other team (Rothera: pays $1 on a tie).
                    other = outcomes[1 - i]
                    no_meta: dict[str, Any] = {"exchange": exch, "symbol": c.get("symbol"), "side": "no", "contract_id": c["id"], "no_of": o}
                    tie_no = tie_payout_for(exch, "no") if sport in TEAM_SPORTS else None
                    if tie_no is not None:
                        no_meta["tie_payout"] = tie_no
                    rows.append(OutcomeQuote(venue_market_id=c["id"] + "#no", outcome=other, outcome_label=f"NO {labels[o]}", ask=_f(qd.get("no_ask_price")), bid=_f(qd.get("no_bid_price")), ask_size=_f(qd.get("bid_size")), meta=no_meta, **common))
            return rows, errs, status

        tickers = [p["kalshi_ticker"] for p in parsed]  # type: ignore[index]

        def kalshi(stale_only: bool) -> tuple[list[OutcomeQuote], list[str], Optional[str]]:
            got = self._kalshi_markets(tickers, fresh=fresh, stale_only=stale_only)
            markets, errs = got.value
            errs = list(errs)
            self._stale_note("kalshi", got, errs)
            rows: list[OutcomeQuote] = []
            for o, ticker in zip(outcomes, tickers):
                km = markets.get(ticker)
                if km is None:
                    continue
                series = self._series(ticker)
                ask, bid = _f(km.get("yes_ask_dollars")), _f(km.get("yes_bid_dollars"))
                rows.append(OutcomeQuote(venue="kalshi", venue_market_id=ticker, event_key=event_key, outcome=o, outcome_label=labels[o], ask=ask if ask and ask < 1 else None, bid=bid if bid and bid > 0 else None, ask_size=_f(km.get("yes_ask_size_fp")), fee_params={"fee_type": series.get("fee_type"), "fee_multiplier": series.get("fee_multiplier", 1)}, url=f"https://kalshi.com/markets/{ticker.split('-')[0].lower()}/{str(km.get('event_ticker', '')).lower()}", ts=got.at, meta={"status": km.get("status")}))
            return rows, list(errs), got.status

        def polymarket(stale_only: bool) -> tuple[list[OutcomeQuote], list[str], Optional[str]]:
            errs: list[str] = []
            pm_market = None
            market_status: Optional[str] = "miss"   # the Gamma payload the prices come from
            if is_nfl and parsed[0]["teams"] and parsed[0]["date"]:  # type: ignore[index]
                got = self._pm_nfl(parsed[0]["teams"], parsed[0]["date"], fresh=fresh, stale_only=stale_only)  # type: ignore[index]
                pm_market, market_status = got.value, got.status
                self._stale_note("polymarket", got, errs)
            elif table_sport and parsed[0]["date"]:  # type: ignore[index]
                got = self._pm_team_game(table_sport, names, outcomes, parsed[0]["date"], fresh=fresh, stale_only=stale_only)  # type: ignore[index]
                pm_market, market_status = got.value, got.status
                self._stale_note("polymarket", got, errs)
            elif is_tennis:
                if stale_only:
                    raise CacheMiss("tennis search results are not cached")
                pm_market = self.polymarket_tennis(names)
            if not pm_market:
                errs.append("polymarket: no matching market found")
                return [], errs, market_status
            outs = _jl(pm_market.get("outcomes"))
            bb, ba = _f(pm_market.get("bestBid")), _f(pm_market.get("bestAsk"))
            sides = [(bb, ba), ((1 - ba) if ba is not None else None, (1 - bb) if bb is not None else None)]
            pq: list[OutcomeQuote] = []
            tokens = _jl(pm_market.get("clobTokenIds"))
            cached_market = self.quote_cache.peek(("pm-markets", pm_market.get("slug")), float("inf"))
            pm_ts = cached_market.at if cached_market is not None else now
            for i, label in enumerate(outs):
                key = (nfl_team_code(label) if is_nfl else team_code(table_sport, label) if table_sport else person_key(label)) or ""
                if key not in outcomes:
                    continue
                bid, ask = sides[i]
                slug_ev = (pm_market.get("events") or [{}])[0].get("slug") or pm_market.get("slug")
                pq.append(OutcomeQuote(venue="polymarket", venue_market_id=str(tokens[i]) if i < len(tokens) else "", event_key=event_key, outcome=key, outcome_label=label, ask=round(ask, 4) if ask and 0 < ask < 1 else None, bid=round(bid, 4) if bid and 0 < bid < 1 else None, fee_params={"feeSchedule": pm_market.get("feeSchedule"), "feesEnabled": pm_market.get("feesEnabled", True)}, url=f"https://polymarket.com/event/{slug_ev}", ts=pm_ts, meta=dict(polymarket_order_meta(pm_market), outcome_index=i)))
            if len(pq) != 2:
                errs.append("polymarket: outcome names did not match")
                return [], errs, None
            # Top-of-book sizes: Gamma's bestAsk carries no size, the CLOB book does (needed to
            # size a STEAL / arb on Polymarket; one batched call, cached quotes_ttl). Without a
            # book the Gamma prices stand and the venue's cache status is that payload's; a
            # market payload that had to be served stale keeps the venue "stale" (not ok) even
            # when the book refreshed, so /health sees the Gamma failure.
            status: Optional[str] = "stale" if stale_only else market_status
            toks = list(dict.fromkeys(q.venue_market_id for q in pq if q.venue_market_id))
            try:
                books = self._pm_books(toks, fresh=fresh, stale_only=stale_only)
                _apply_books(pq, books.value)
                for q in pq:
                    q.ts = books.at
                status = "stale" if market_status == "stale" else books.status
                self._stale_note("polymarket", books, errs)
            except CacheMiss:
                pass  # the cached Gamma prices stand; sizes are missing until the next poll
            except Exception:
                errs.append("polymarket: book sizes unavailable (prices still live)")
            return pq, errs, status

        results = self._run_venues({"robinhood": robinhood, "kalshi": kalshi, "polymarket": polymarket})
        quotes_by_venue: dict[str, list[OutcomeQuote]] = {"robinhood": results["robinhood"].value or []}
        errors: list[str] = []
        for v in ("robinhood", "kalshi", "polymarket"):
            r = results[v]
            if v != "robinhood" and r.value:
                quotes_by_venue[v] = r.value
            if r.error:
                errors.append(r.error)
        timings, venue_status = self._timings(results, t0)

        info = EventInfo(event_key=event_key, sport=sport, market_type="moneyline", outcomes=sorted(outcomes), labels=labels, in_play=_in_play_from_progress(str((pp.get("eventStates") or {}).get(ev.get("id"), {}).get("eventProgress") or "").strip(), None) if isinstance(pp.get("eventStates"), dict) else None)
        from .matching.matcher import MergedEvent
        from .scanner import analyze_event, resolve_executable_venues

        me = MergedEvent(event_key=event_key, info=info, quotes_by_venue=quotes_by_venue)
        self.last_event = me  # reused by the in-play watcher and the bridge's /inplay
        self.last_url, self.last_analyzed_at = url, now
        self._remember(url, me, now)
        report = analyze_event(me, settings, contracts=contracts, target_margin=target_margin, now=now, executable_venues=resolve_executable_venues(settings, executable_venues))
        out = asdict(report)
        out["errors"] = errors
        timings["total"] = round(time.perf_counter() - t0, 4)
        return {"ok": True, "event": {"id": ev.get("id"), "name": ev.get("name"), "sport": sport, "url": url, "key": event_key}, "analysis": out, "timings": timings, "venue_status": venue_status}

    # ---- spread / total pages: one binary contract per line -------------------------------
    def _pm_event_by_slug(self, slug: str, fresh: bool = False, stale_only: bool = False) -> Cached:
        return self._raw(("pm-event", slug), lambda: self.pm.http.get(f"{GAMMA}/events", {"slug": slug}), fresh, stale_only)

    def polymarket_game(self, teams: list[str], date: str, fresh: bool = False, stale_only: bool = False) -> Optional[dict]:
        return self._pm_game(teams, date, fresh=fresh, stale_only=stale_only).value

    def _pm_game(self, teams: list[str], date: str, fresh: bool = False, stale_only: bool = False) -> Cached:
        """Full Polymarket event (all lines) for an NFL game as a :class:`Cached` (``value`` is
        the event or ``None``), trying both team orders and the ET date / next UTC date; the
        slug that answered is kept ``page_ttl``, a game not found ``pm_miss_ttl``, and the
        event payload ``quotes_ttl`` (lines pages poll every second too)."""
        lookup_key = ("nfl-event", tuple(teams), date)
        known = self.slug_cache.peek(lookup_key, self.page_ttl)
        if known is not None and known.value is None and known.age >= self.pm_miss_ttl:
            known = None
        if stale_only:
            if known is not None and known.value is None:
                return Cached(None, known.at, known.age, "stale")
            ent = self.quote_cache.peek(("pm-event", known.value), self.quotes_max_age) if known is not None else None
            if ent is None:
                raise CacheMiss("no polymarket event payload younger than quotes_max_age")
            return Cached((ent.value or [None])[0], ent.at, ent.age, "stale")
        if known is not None and not fresh:
            if known.value is None:
                return Cached(None, known.at, known.age, "hit")
            got = self._pm_event_by_slug(known.value)   # a Gamma error propagates: slug kept, venue reports it
            if got.value:
                return Cached(got.value[0], got.at, got.age, got.status, error=got.error)
        failed: Optional[Exception] = None
        for slug in polymarket_nfl_slugs(teams, date):
            if known is not None and slug == known.value and not fresh:
                continue   # just read above: the event is no longer at that slug
            try:
                got = self._pm_event_by_slug(slug, fresh=fresh)
            except Exception as e:
                failed = e
                continue
            if got.value:
                self.slug_cache.put(lookup_key, slug)
                return Cached(got.value[0], got.at, got.age, got.status)
        if failed is not None:   # a candidate could not be read: not "not listed"
            raise failed
        self.slug_cache.put(lookup_key, None)
        return Cached(None, self.clock(), 0.0, "miss")

    def analyze_lines(self, url: str, pp: dict, ev: dict, contracts_raw: list[dict], mtype: str, settings: Optional[dict[str, Any]] = None, contracts: float = 100, target_margin: float = 0.0, executable_venues: Optional[set[str]] = None, fresh: bool = False, t0: Optional[float] = None) -> dict[str, Any]:
        from .matching.matcher import MergedEvent
        from .scanner import analyze_event, resolve_executable_venues

        settings = settings or {}
        t0 = time.perf_counter() if t0 is None else t0
        exec_venues = resolve_executable_venues(settings, executable_venues)
        now = self.clock()
        sym0 = contracts_raw[0].get("symbol", "")
        p0 = parse_symbol(sym0)
        if not p0:
            return {"ok": True, "event": {"id": ev.get("id"), "name": ev.get("name")}, "analysis": None, "note": "unrecognised contract symbols"}
        pair = p0["pair"]
        codes: list[str] = []
        for cut in range(2, len(pair) - 1):
            a, b = pair[:cut], pair[cut:]
            if nfl_team_code(a) and nfl_team_code(b):
                codes = [nfl_team_code(a), nfl_team_code(b)]  # type: ignore[list-item]
                break
        if not codes:
            return {"ok": True, "event": {"id": ev.get("id"), "name": ev.get("name")}, "analysis": None, "note": f"could not split team pair {pair}"}
        away, home = codes
        date = p0["date"] or ""
        ssr = pp.get("quotes") or {}
        ids = [c["id"] for c in contracts_raw]
        slug = str(url.rstrip("/").rsplit("/", 1)[-1])
        family_kx = "KX" + p0["family"]
        # All of Kalshi's lines for this game in one call, indexed by (team, line) / line.
        kalshi_event = f"{family_kx}-{sym0.split('-')[1]}"

        def robinhood(stale_only: bool) -> tuple[tuple[dict, float], list[str], Optional[str]]:
            errs: list[str] = []
            try:
                got = self._rh_quotes(ids, fresh=fresh, stale_only=stale_only)
                self._stale_note("robinhood", got, errs)
                return (got.value, got.at), errs, got.status
            except Exception as e:
                return ({}, self._page_at(slug, now)), ([] if isinstance(e, CacheMiss) else [f"robinhood quotes: {e}"]), "page"

        def kalshi(stale_only: bool) -> tuple[tuple[dict, float], list[str], Optional[str]]:
            got = self._kalshi_event_markets(kalshi_event, fresh=fresh, stale_only=stale_only)
            errs: list[str] = []
            self._stale_note("kalshi", got, errs)
            k_index: dict[tuple[str, float], dict] = {}
            for km in got.value:
                if km.get("status") not in (None, "active", "open"):
                    continue
                kl = _f(km.get("floor_strike"))
                if kl is None:
                    continue
                k_index[(strip_digits(km["ticker"].rsplit("-", 1)[-1]) if mtype == "spread" else "", kl)] = km
            return (k_index, got.at), errs, got.status

        def polymarket(stale_only: bool) -> tuple[tuple[Optional[dict], float], list[str], Optional[str]]:
            got = self._pm_game([away, home], date, fresh=fresh, stale_only=stale_only)
            pm_event = got.value
            errs = [] if pm_event is not None else ["polymarket: game not found"]
            self._stale_note("polymarket", got, errs)
            return (pm_event, got.at if pm_event is not None else now), errs, got.status

        results = self._run_venues({"robinhood": robinhood, "kalshi": kalshi, "polymarket": polymarket})
        errors: list[str] = []
        live, rh_ts = results["robinhood"].value or ({}, now)
        k_index, k_ts = results["kalshi"].value or ({}, now)
        pm_event, pm_ts = results["polymarket"].value or (None, now)
        errors.extend(results[v].error for v in ("robinhood", "kalshi", "polymarket") if results[v].error)
        timings, venue_status = self._timings(results, t0)
        states = pp.get("eventStates") or {}
        st = states.get(ev.get("id"), {}) if isinstance(states, dict) else {}
        in_play = _in_play_from_progress(str(st.get("eventProgress") or "").strip(), st.get("eventStatus"))
        pm_index: dict[tuple[str, str], dict] = {}
        for m in (pm_event or {}).get("markets", []):
            smt = m.get("sportsMarketType")
            ln = _f(m.get("line"))
            if ln is None:
                continue
            if smt == "spreads":
                outs = _jl(m.get("outcomes"))
                oc = [nfl_team_code(o) for o in outs]
                if len(oc) == 2 and None not in oc:
                    fav = oc[0] if ln < 0 else oc[1]
                    pm_index[("spread", f"{fav}-{fmt_line(abs(ln))}")] = m
            elif smt == "totals":
                pm_index[("total", fmt_line(ln))] = m
        k_series = self._series(kalshi_event)
        k_fee = {"fee_type": k_series.get("fee_type"), "fee_multiplier": k_series.get("fee_multiplier", 1)}
        start_time = parse_iso(st.get("gameStart")) or _event_day_from_timeline(ev.get("timeline"))
        missing_k: list[str] = []
        lines_out: list[dict[str, Any]] = []
        for c in contracts_raw:
            sym = c.get("symbol", "")
            line = _f(c.get("floorStrikeValue"))
            if line is None:
                continue
            exch = exchange_from_symbol_or_enum(sym, c.get("exchange"))
            if mtype == "spread":
                team_raw = strip_digits(sym.rsplit("-", 1)[-1])
                fav = nfl_team_code(team_raw)
                dog = nfl_team_code(split_pair(pair, team_raw) or "")
                if not fav or not dog:
                    continue
                key = spread_event_key("nfl", [fav, dog], date, fav, line)
                yes_key, no_key = spread_outcomes(fav, dog, line)
                labels = {yes_key: f"{nfl_team_city(fav)} -{fmt_line(line)}", no_key: f"{nfl_team_city(dog)} +{fmt_line(line)}"}
                km = k_index.get((team_raw, line))
                pm_m = pm_index.get(("spread", f"{fav}-{fmt_line(line)}"))
            else:
                key = total_event_key("nfl", codes, date, line)
                yes_key, no_key = "over", "under"
                labels = {"over": f"Over {fmt_line(line)}", "under": f"Under {fmt_line(line)}"}
                km = k_index.get(("", line))
                pm_m = pm_index.get(("total", fmt_line(line)))
            info = EventInfo(event_key=key, sport="nfl", market_type=mtype, outcomes=[yes_key, no_key], labels=labels, line=line, tie_rule=push_rule_for_line(line), venues={"_teams": {"title": f"{away} @ {home}"}}, in_play=in_play, start_time=start_time)
            qd = live.get(c["id"]) or ssr.get(c["id"]) or {}
            rh_common = dict(venue="robinhood", event_key=key, fee_params={"exchange": exch, "symbol": sym}, url=url, ts=rh_ts, book_id="kalshi" if exch == "kalshi" else exch)
            qbv: dict[str, list[OutcomeQuote]] = {"robinhood": [
                OutcomeQuote(venue_market_id=c["id"], outcome=yes_key, outcome_label=labels[yes_key], ask=_f(qd.get("yes_ask_price")), bid=_f(qd.get("yes_bid_price")), ask_size=_f(qd.get("ask_size")), meta={"symbol": sym, "exchange": exch, "side": "yes"}, **rh_common),
                OutcomeQuote(venue_market_id=c["id"] + "#no", outcome=no_key, outcome_label=labels[no_key], ask=_f(qd.get("no_ask_price")), bid=_f(qd.get("no_bid_price")), ask_size=_f(qd.get("bid_size")), meta={"symbol": sym, "exchange": exch, "side": "no"}, **rh_common),
            ]}
            if km is not None:
                kalshi_ticker = km["ticker"]
                ya, yb, na, nb = _f(km.get("yes_ask_dollars")), _f(km.get("yes_bid_dollars")), _f(km.get("no_ask_dollars")), _f(km.get("no_bid_dollars"))
                kurl = f"https://kalshi.com/markets/{family_kx.lower()}/{kalshi_event.lower()}"
                qbv["kalshi"] = [
                    OutcomeQuote(venue="kalshi", venue_market_id=kalshi_ticker, event_key=key, outcome=yes_key, outcome_label=labels[yes_key], ask=ya if ya and ya < 1 else None, bid=yb if yb and yb > 0 else None, ask_size=_f(km.get("yes_ask_size_fp")), fee_params=k_fee, url=kurl, ts=k_ts, meta={"ticker": kalshi_ticker, "side": "yes"}),
                    OutcomeQuote(venue="kalshi", venue_market_id=kalshi_ticker + "#no", event_key=key, outcome=no_key, outcome_label=labels[no_key], ask=na if na and na < 1 else None, bid=nb if nb and nb > 0 else None, ask_size=_f(km.get("yes_bid_size_fp")), fee_params=k_fee, url=kurl, ts=k_ts, meta={"ticker": kalshi_ticker, "side": "no"}),
                ]
            else:
                missing_k.append(fmt_line(line))
            if pm_m:
                outs = _jl(pm_m.get("outcomes"))
                bb, ba = _f(pm_m.get("bestBid")), _f(pm_m.get("bestAsk"))
                sides = [(bb, ba), ((1 - ba) if ba is not None else None, (1 - bb) if bb is not None else None)]
                if mtype == "spread":
                    ln = _f(pm_m.get("line")) or 0.0
                    keys = [yes_key, no_key] if ln < 0 else [no_key, yes_key]
                else:
                    keys = ["over", "under"] if str(outs[0]).lower().startswith("over") else ["under", "over"]
                tokens = _jl(pm_m.get("clobTokenIds"))
                purl = f"https://polymarket.com/event/{(pm_event or {}).get('slug')}"
                qbv["polymarket"] = [
                    OutcomeQuote(venue="polymarket", venue_market_id=str(tokens[i]) if i < len(tokens) else "", event_key=key, outcome=keys[i], outcome_label=labels[keys[i]], ask=round(sides[i][1], 4) if sides[i][1] and 0 < sides[i][1] < 1 else None, bid=round(sides[i][0], 4) if sides[i][0] and 0 < sides[i][0] < 1 else None, fee_params={"feeSchedule": pm_m.get("feeSchedule"), "feesEnabled": pm_m.get("feesEnabled", True)}, url=purl, ts=pm_ts, meta=dict(polymarket_order_meta(pm_m), slug=pm_m.get("slug"), outcome_index=i))
                    for i in range(2)
                ]
            me = MergedEvent(event_key=key, info=info, quotes_by_venue=qbv)
            self.last_lines = getattr(self, "last_lines", {})
            self.last_lines[key] = me
            rep = analyze_event(me, settings, contracts=contracts, target_margin=target_margin, now=now, executable_venues=exec_venues)
            d = asdict(rep)
            d["contract_id"] = c["id"]
            d["symbol"] = sym
            lines_out.append(d)
        if missing_k:
            errors.append(f"kalshi lists no market for lines: {', '.join(dict.fromkeys(missing_k))}")
        lines_out.sort(key=lambda x: (not x["fillable"], -(x["margin"] if x["margin"] is not None else -9), x["line"] or 0))
        timings["total"] = round(time.perf_counter() - t0, 4)
        return {"ok": True, "event": {"id": ev.get("id"), "name": ev.get("name"), "sport": "nfl", "url": url, "market_type": mtype, "game": f"{away} @ {home}"}, "analysis": {"market_type": mtype, "lines": lines_out, "errors": errors, "venues": ["robinhood", "kalshi", "polymarket"], "contracts": contracts, "target_margin": target_margin}, "timings": timings, "venue_status": venue_status}

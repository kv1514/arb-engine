"""Local HTTP bridge for the browser overlay: ``python -m arb_engine bridge``.

    GET /health                          ok, executable_venues, per-venue reachability (age of the
                                          last successful fetch, last error), requests/s, cache
                                          hit rates and the last N requests (popup / preflight)
    GET /analyze?url=<robinhood event url>[&contracts=100&target_margin=0&gold=0&fresh=1]
    GET /inplay?url=…[&position=venue:outcome:price:count&bankroll=1000&kelly=0.25&steal_edge=0.03]
    GET /inplay?url=<robinhood event url>[&position=venue:outcome:price:count]...
                [&target_margin=0&max_age=2&interval=10&fresh=1]  in-play view: lock/steal actions,
                                          game state, blended fair, execution gates (reuses the
                                          venue scan /analyze just did for the same url when it
                                          is at most max_age seconds old; ESPN cached 2 s)
    GET /kalshi/market/<ticker>          proxies for the extension (Kalshi's API refuses
    GET /kalshi/markets?event_ticker=…    browser Origins other than kalshi.com)

Binds to 127.0.0.1 only and adds permissive CORS headers so the extension (and the page)
can call it. It never places orders.

Under 1 s polling per open tab the rules are: every response carries ``timings`` (seconds per
venue, ``espn`` on /inplay, ``total`` for the request) and ``venue_status``; the analyzer's
1 s quote caches and concurrent venue fetch (``eventlookup`` module docstring) keep a poll
under ~0.1 s warm; ``?fresh=1`` bypasses every cache; a handler never answers 500 — any
exception becomes ``{ok: false, error, where}`` with a 200 and one stderr line, so the
overlay renders the message instead of falling back to direct mode on a transient fault.

What every analyzer call carries (parity with ``scan`` / ``live``): the settings resolved
from the environment (``config.load_settings``), the *executable* venue set from the
compliance table (``scanner.resolve_executable_venues``: Polymarket is signal-only unless
``EXECUTABLE_VENUES`` opts in) and ``emit_no_side=True`` so the Rothera NO leg of each
Robinhood game contract is priced as its own row (``<contract id>#no``, ``meta.side="no"``).

``/inplay`` keeps one ``FeedFreshness`` per event key across polls (``Handler.freshness``),
because the execution gates (``feed-stale`` / ``clock-frozen`` / ``score-pending``) are
defined on the *change* between successive polls: a fresh object per request would never
see one. The JSON is ``dataclasses.asdict`` of ``EventReport`` / ``InplayView``; the keys
the overlay reads (``tie_margin``, ``tie_payout_total``, ``venues[].ineligible``,
``gated_reasons``, ``steal_gated``, ``lock_gated``, ``steal_threshold``) are pinned by
:func:`with_overlay_fields` so a dataclass edit cannot silently drop them.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from .config import settings_from_env
from .eventlookup import EventAnalyzer, TtlCache
from .scanner import resolve_executable_venues
from .venues.kalshi import KalshiClient


#: /inplay reuses the venue scan /analyze made for the same url within this many seconds:
#: the overlay calls /inplay right after /analyze, and in play a quote must never be served
#: older than ~2 s (the analyzer's ``quotes_max_age``), so this is that bound, not 10 s.
DEFAULT_INPLAY_MAX_AGE = 2.0
DEFAULT_INPLAY_INTERVAL = 10.0  # seconds; the quote-old gate's poll interval when the overlay sends none
ESPN_TTL = 2.0                  # seconds; ESPN scoreboard/summary state per event
HEALTH_WINDOW_S = 10.0          # requests/s on /health is measured over this window
HEALTH_LAST_N = 20              # requests echoed on /health
VENUES = ("robinhood", "kalshi", "polymarket", "espn")

#: EventReport keys the overlay reads beside ``outcomes`` / ``arb`` (kept even when None).
REPORT_KEYS: tuple[str, ...] = ("tie_margin", "tie_payout_total", "flags", "fillable")
#: VenuePrice keys per ``outcomes[].venues[]`` row (``ineligible`` is what background.js reads).
VENUE_ROW_KEYS: tuple[str, ...] = ("ineligible", "side", "tie_payout", "mirror_of")
#: SideView / InplayView gate fields, with the value an older dataclass would have meant.
SIDE_GATE_DEFAULTS: dict[str, Any] = {"gated_reasons": [], "steal_gated": False, "lock_gated": False, "steal_threshold": None}
VIEW_GATE_DEFAULTS: dict[str, Any] = {"gated_reasons": [], "freshness": None, "spread_source": None}


def recent_event(analyzer, url: str, max_age: float = DEFAULT_INPLAY_MAX_AGE, now: float | None = None):
    """The MergedEvent ``analyzer.analyze_url`` produced for ``url`` within ``max_age``
    seconds, else ``None`` (caller re-scans). Keeps bridge-mode overlay refreshes at one
    venue scan per tick instead of two."""
    slot = (getattr(analyzer, "recent_events", None) or {}).get(url)  # one slot per open tab
    if slot is not None:
        me, at = slot
    else:
        me = getattr(analyzer, "last_event", None)
        if me is None or getattr(analyzer, "last_url", None) != url:
            return None
        at = float(getattr(analyzer, "last_analyzed_at", 0.0) or 0.0)
    if me is None or (time.time() if now is None else now) - float(at or 0.0) > max_age:
        return None
    return me


class BridgeStats:
    """Rolling request log and per-venue reachability for ``/health`` (thread-safe).

    ``record`` takes what one request learned: its route, outcome and duration, the
    analyzer's ``venue_status`` (ok / cache / error per venue) and the ESPN fetch result.
    ``snapshot`` answers the popup/preflight questions: is each venue reachable (its last
    fetch succeeded, and how long ago), what was its last error, how many requests per
    second the bridge is serving and how often the caches absorb them."""

    def __init__(self, clock: Callable[[], float] = time.time, keep: int = 200):
        self.clock = clock
        self.started = clock()
        self._mu = threading.Lock()
        self.log: deque[dict[str, Any]] = deque(maxlen=keep)
        self.total = 0
        self.errors = 0
        self.venues: dict[str, dict[str, Any]] = {v: {"last_ok_at": None, "last_error": None, "last_error_at": None, "hits": 0, "misses": 0, "stale": 0} for v in VENUES}

    def note_venue(self, venue: str, ok: bool, error: Optional[str] = None, cache: Optional[str] = None, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        with self._mu:
            st = self.venues.setdefault(venue, {"last_ok_at": None, "last_error": None, "last_error_at": None, "hits": 0, "misses": 0, "stale": 0})
            if ok:
                st["last_ok_at"] = now
            if error:
                st["last_error"], st["last_error_at"] = error, now
            if cache == "hit":
                st["hits"] += 1
            elif cache == "miss":
                st["misses"] += 1
            elif cache == "stale":
                st["stale"] += 1

    def record(self, route: str, ok: bool, seconds: float, error: Optional[str] = None, venue_status: Optional[dict[str, dict[str, Any]]] = None, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        with self._mu:
            self.total += 1
            if not ok:
                self.errors += 1
            self.log.append({"t": round(now, 3), "route": route, "ok": ok, "seconds": round(seconds, 4), "error": error})
        for v, st in (venue_status or {}).items():
            # ``ok`` = the venue answered this request (a cache hit says nothing about it; a
            # stale/timed-out venue did not); ``error`` = what it said or why it did not.
            cache = st.get("cache")
            self.note_venue(v, ok=bool(st.get("ok")) and cache in (None, "miss"), error=st.get("error") if (not st.get("ok") or st.get("error")) else None, cache=cache, now=now)

    def snapshot(self, now: Optional[float] = None) -> dict[str, Any]:
        now = self.clock() if now is None else now
        with self._mu:
            recent = [r for r in self.log if now - r["t"] <= HEALTH_WINDOW_S]
            window = min(HEALTH_WINDOW_S, max(1.0, now - self.started))  # over at least 1 s: a probe right after start is not "infinite" load
            venues = {}
            for v, st in self.venues.items():
                n = st["hits"] + st["misses"] + st["stale"]
                ok_at, err_at = st["last_ok_at"], st["last_error_at"]
                venues[v] = {
                    "reachable": ok_at is not None and (err_at is None or err_at <= ok_at),
                    "last_ok_age_s": round(now - ok_at, 3) if ok_at is not None else None,
                    "last_error": st["last_error"],
                    "last_error_age_s": round(now - err_at, 3) if err_at is not None else None,
                    "cache": {"hits": st["hits"], "misses": st["misses"], "stale": st["stale"], "hit_rate": round(st["hits"] / n, 3) if n else None},
                }
            return {
                "uptime_s": round(now - self.started, 3),
                "requests": {"total": self.total, "errors": self.errors, "per_s": round(len(recent) / window, 3), "window_s": HEALTH_WINDOW_S, "last": list(self.log)[-HEALTH_LAST_N:]},
                "venues": venues,
            }


def _pin_report(rep: Any) -> None:
    if not isinstance(rep, dict):
        return
    for k in REPORT_KEYS:
        rep.setdefault(k, [] if k == "flags" else False if k == "fillable" else None)
    for o in rep.get("outcomes") or []:
        for v in (o.get("venues") or []) if isinstance(o, dict) else []:
            if isinstance(v, dict):
                for k in VENUE_ROW_KEYS:
                    v.setdefault(k, None)


def with_overlay_fields(res: dict[str, Any]) -> dict[str, Any]:
    """Pin the ``/analyze`` keys the overlay consumes (in place, returned for chaining).

    ``asdict(EventReport)`` already emits them today; ``setdefault`` is the backward- and
    forward-compatibility contract: nothing the extension reads is renamed, and a report
    produced without a field (an older dataclass, a ``lines`` page) still carries the key so
    ``fromBridge`` / ``rowsFromReport`` never see ``undefined``."""
    a = res.get("analysis") if isinstance(res, dict) else None
    if isinstance(a, dict):
        _pin_report(a)
        for line in a.get("lines") or []:
            _pin_report(line)
    return res


def with_gate_fields(view: dict[str, Any], executable_venues: Optional[set[str]] = None) -> dict[str, Any]:
    """Pin the ``/inplay`` gate keys: ``gated_reasons`` is always a list (never null) at the
    event level and on every side; ``steal_gated`` / ``lock_gated`` / ``steal_threshold``
    default to what an ungated SideView means. ``evaluate_inplay`` prices the best ask on
    *every* venue (the in-play fair needs the signal), so the view also says which venues
    are executable and marks a side whose best venue is not (``best_ineligible``) — the
    overlay must not render a STEAL on Polymarket for an account that cannot trade there."""
    for k, dflt in VIEW_GATE_DEFAULTS.items():
        view.setdefault(k, dflt)
    view["gated_reasons"] = list(view.get("gated_reasons") or [])
    view["executable_venues"] = sorted(executable_venues) if executable_venues is not None else None
    for sv in view.get("sides") or []:
        if not isinstance(sv, dict):
            continue
        for k, dflt in SIDE_GATE_DEFAULTS.items():
            sv.setdefault(k, dflt)
        sv["gated_reasons"] = list(sv.get("gated_reasons") or [])
        bv = sv.get("best_venue")
        sv["best_ineligible"] = "not executable" if (executable_venues is not None and bv is not None and bv not in executable_venues) else None
    return view


def _truthy(v: Optional[str]) -> bool:
    return v in ("1", "true", "yes", "on")


def executable_venues_for(settings: dict[str, Any]) -> Optional[set[str]]:
    """The venue set an order can actually go to, for ``analyze_url(executable_venues=...)``:
    the ``EXECUTABLE_VENUES`` override when set (``all`` lifts every restriction), else the
    compliance table (Polymarket signal-only). ``config.load_settings`` emits every declared
    key, so the key is present-but-None when unset; ``resolve_executable_venues`` treats that
    as unset, and the bridge drops it too so the intent is explicit here."""
    return resolve_executable_venues({k: v for k, v in settings.items() if not (k == "executable_venues" and v is None)})


class Handler(BaseHTTPRequestHandler):
    analyzer: EventAnalyzer
    kalshi: KalshiClient
    #: one time source for the poll memory, the ESPN cache and the request log; tests replace
    #: it with a callable object (read through the class so a plain function is never bound)
    clock: Callable[[], float] = time.time
    espn_fetchers: dict = {}
    #: event key -> FeedFreshness, shared by every /inplay request (the gates are per event, not per request)
    freshness: dict = {}
    #: event key -> GameState, ESPN_TTL seconds, single-flight (two tabs on one game poll ESPN once)
    espn_cache: TtlCache = TtlCache()
    stats: BridgeStats = BridgeStats()
    _lock = threading.Lock()  # ThreadingHTTPServer: two overlay tabs on one game must not race the poll memory

    @classmethod
    def reset_state(cls) -> None:
        """Fresh per-process state (``serve`` and the tests): poll memory, ESPN cache, request log."""
        cls.espn_fetchers, cls.freshness = {}, {}
        cls.espn_cache = TtlCache(lambda: cls.clock())
        cls.stats = BridgeStats(lambda: cls.clock())

    def _now(self) -> float:
        return type(self).clock()

    def _espn(self, event_key: str, fresh: bool = False):
        """ESPN GameState for the event through the 2 s cache; the fetcher itself keeps the
        scoreboard-every-call / summary-every-30-s cadence."""
        from .cli import _espn_state_fetcher

        with self._lock:
            f = self.espn_fetchers.get(event_key)
            if f is None:
                f = self.espn_fetchers[event_key] = _espn_state_fetcher(event_key)
        return self.espn_cache.get(event_key, ESPN_TTL, f, fresh=fresh)

    def _settings(self, qs: dict[str, str]) -> dict[str, Any]:
        """Every declared key from the environment, plus the overlay's per-request Gold flag."""
        settings = settings_from_env()
        if _truthy(qs.get("gold")):
            settings["robinhood_gold"] = True
        return settings

    def _analyze(self, url: str, settings: dict[str, Any], qs: dict[str, str]) -> dict[str, Any]:
        """One venue scan with the scan/live-parity arguments (module docstring)."""
        # bankroll / kelly ride along in the settings so the LAG signals can size (0 = no sizing).
        st = dict(settings)
        if qs.get("bankroll"):
            try:
                st["bankroll"] = float(qs["bankroll"]) or None
                st["kelly_fraction"] = float(qs.get("kelly", 0.25) or 0.25)
            except ValueError:
                pass
        res = self.analyzer.analyze_url(url, settings=st, contracts=float(qs.get("contracts", 100)), target_margin=float(qs.get("target_margin", 0)), emit_no_side=True, executable_venues=executable_venues_for(settings), fresh=_truthy(qs.get("fresh")))
        return with_overlay_fields(res)

    def _freshness(self, event_key: str, settings: dict[str, Any], interval_s: float):
        from .strategy.inplay import FeedFreshness

        f = self.freshness.get(event_key)
        if f is None:
            f = self.freshness[event_key] = FeedFreshness.from_settings(settings, interval_s=interval_s)
        else:
            f.interval_s = interval_s  # the overlay's refresh period drives the quote-old gate
        return f

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, {})

    # ---- routes: each returns (status, payload); do_GET adds timings and never lets one raise ----
    def _route_health(self, qs: dict[str, str]) -> tuple[int, dict]:
        snap = self.stats.snapshot(self._now())
        an = self.analyzer
        return 200, {
            "ok": True,
            "service": "arb-engine bridge",
            "executable_venues": sorted(executable_venues_for(settings_from_env()) or []),
            "caches": {"quotes_ttl_s": an.quotes_ttl, "quotes_max_age_s": an.quotes_max_age, "page_ttl_s": an.page_ttl, "espn_ttl_s": ESPN_TTL, "venue_timeout_s": an.venue_timeout, "quotes": an.quote_cache.stats(), "pages": an.page_cache.stats(), "espn": self.espn_cache.stats()},
            **snap,
        }

    def _route_analyze(self, qs: dict[str, str]) -> tuple[int, dict]:
        res = self._analyze(qs.get("url", ""), self._settings(qs), qs)
        self.stats.record("/analyze", bool(res.get("ok")), float((res.get("timings") or {}).get("total") or 0.0), error=res.get("error"), venue_status=res.get("venue_status"), now=self._now())
        return 200, res

    def _route_inplay(self, qs: dict[str, str], query: str) -> tuple[int, dict]:
        from dataclasses import asdict

        from .strategy.inplay import Lot, evaluate_inplay

        url = qs.get("url", "")
        settings = self._settings(qs)
        fresh = _truthy(qs.get("fresh"))
        now = self._now()
        t_route = time.perf_counter()
        timings: dict[str, float] = {}
        venue_status: Optional[dict] = None
        me = None if fresh else recent_event(self.analyzer, url, max_age=float(qs.get("max_age", DEFAULT_INPLAY_MAX_AGE)), now=now)
        if me is None:
            res = self._analyze(url, settings, qs)
            timings.update({k: v for k, v in (res.get("timings") or {}).items() if k != "total"})
            venue_status = res.get("venue_status")
            me = recent_event(self.analyzer, url, max_age=float("inf"), now=now) if res.get("ok") else None
            if not res.get("ok") or "lines" in (res.get("analysis") or {}) or me is None:
                self.stats.record("/inplay", False, float((res.get("timings") or {}).get("total") or 0.0), error=res.get("error") or "not a game-winner page", venue_status=venue_status, now=now)
                return 200, {"ok": False, "error": res.get("error") or "not a game-winner page", "where": "/inplay analyze", "timings": timings}
        lots = [Lot.parse(x) for x in parse_qs(query).get("position", []) if x.strip()]
        gs = None
        espn_error: Optional[str] = None
        if qs.get("espn", "1") not in ("0", "false"):
            t0 = time.perf_counter()
            try:
                got = self._espn(me.event_key, fresh=fresh)
                gs = got.value
                self.stats.note_venue("espn", ok=got.status == "miss", cache=got.status, now=now)
            except Exception as e:
                espn_error = f"espn: {e}"
                self.stats.note_venue("espn", ok=False, error=str(e), now=now)
            timings["espn"] = round(time.perf_counter() - t0, 4)
        bankroll = float(qs.get("bankroll", 0) or 0) or None
        interval_s = float(qs.get("interval", DEFAULT_INPLAY_INTERVAL) or DEFAULT_INPLAY_INTERVAL)
        with self._lock:
            fresh_obj = self._freshness(me.event_key, settings, interval_s)
            view = evaluate_inplay(me, lots, settings, steal_edge=float(qs.get("steal_edge", 0.03)), target_margin=float(qs.get("target_margin", 0)), game_state=gs, bankroll=bankroll, kelly_fraction=float(qs.get("kelly", 0.25) or 0.25), freshness=fresh_obj, now=now)
        self.stats.record("/inplay", True, time.perf_counter() - t_route, venue_status=venue_status, now=now)
        return 200, {"ok": True, "view": with_gate_fields(asdict(view), executable_venues_for(settings)), "timings": timings, "espn_error": espn_error, "venue_status": venue_status}

    def _route_kalshi_markets(self, qs: dict[str, str]) -> tuple[int, dict]:
        return 200, self.kalshi.get("/markets", {k: v for k, v in qs.items() if k in ("event_ticker", "series_ticker", "status", "limit")})

    def _route_kalshi_market(self, ticker: str) -> tuple[int, dict]:
        return 200, {"market": self.kalshi.market(ticker), "series": self.analyzer._series(ticker)}

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        t0 = time.perf_counter()
        where = u.path
        try:
            if u.path == "/health":
                code, payload = self._route_health(qs)
            elif u.path == "/analyze":
                code, payload = self._route_analyze(qs)
            elif u.path == "/inplay":
                code, payload = self._route_inplay(qs, u.query)
            elif u.path == "/kalshi/markets":
                code, payload = self._route_kalshi_markets(qs)
            elif u.path.startswith("/kalshi/market/"):
                code, payload = self._route_kalshi_market(u.path.rsplit("/", 1)[-1])
            else:
                code, payload = 404, {"ok": False, "error": "unknown route", "where": where}
        except Exception as e:  # never 500: the overlay renders the message and keeps polling
            tb = traceback.extract_tb(e.__traceback__)
            frame = tb[-1] if tb else None
            where = f"{u.path} {type(e).__name__} at {frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}" if frame else u.path
            sys.stderr.write(f"bridge: error {where}: {e}\n")
            code, payload = 200, {"ok": False, "error": str(e), "where": where}
            self.stats.record(u.path, False, time.perf_counter() - t0, error=f"{type(e).__name__}: {e}", now=self._now())
        if isinstance(payload, dict):
            t = payload.get("timings")
            payload["timings"] = dict(t if isinstance(t, dict) else {}, total=round(time.perf_counter() - t0, 4))
        try:
            self._send(code, payload)
        except (BrokenPipeError, ConnectionResetError) as e:  # the tab closed mid-response
            sys.stderr.write(f"bridge: client went away on {u.path}: {e}\n")

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        """No access line for a 200: at one poll per second per tab the access log would bury
        the ``bridge: error …`` lines; 404s and the like are still logged."""
        if str(code) != "200":
            super().log_request(code, size)

    def log_message(self, fmt: str, *args) -> None:  # quieter
        sys.stderr.write("bridge: " + fmt % args + "\n")


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    Handler.analyzer = EventAnalyzer()
    Handler.kalshi = Handler.analyzer.kalshi
    Handler.reset_state()
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"arb-engine bridge listening on http://{host}:{port}  (GET /analyze?url=... | /inplay?url=... | /kalshi/market/<ticker> | /health)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

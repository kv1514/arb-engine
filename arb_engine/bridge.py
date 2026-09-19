"""Local HTTP bridge for the browser overlay: ``python -m arb_engine bridge``.

    GET /health
    GET /analyze?url=<robinhood event url>[&contracts=100&target_margin=0&gold=0]
    GET /inplay?url=…[&position=venue:outcome:price:count&bankroll=1000&kelly=0.25&steal_edge=0.03]
    GET /inplay?url=<robinhood event url>[&position=venue:outcome:price:count]...
                [&target_margin=0&max_age=10&interval=10]  in-play view: lock/steal actions, game
                                          state, blended fair, execution gates (reuses the venue
                                          scan /analyze just did for the same url when it is at
                                          most max_age seconds old)
    GET /kalshi/market/<ticker>          proxies for the extension (Kalshi's API refuses
    GET /kalshi/markets?event_ticker=…    browser Origins other than kalshi.com)

Binds to 127.0.0.1 only and adds permissive CORS headers so the extension (and the page)
can call it. It never places orders.

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .config import settings_from_env
from .eventlookup import EventAnalyzer
from .scanner import resolve_executable_venues
from .venues.kalshi import KalshiClient


DEFAULT_INPLAY_MAX_AGE = 10.0  # seconds; the overlay calls /inplay right after /analyze

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
    me = getattr(analyzer, "last_event", None)
    if me is None or getattr(analyzer, "last_url", None) != url:
        return None
    at = float(getattr(analyzer, "last_analyzed_at", 0.0) or 0.0)
    if (time.time() if now is None else now) - at > max_age:
        return None
    return me


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
    espn_fetchers: dict = {}
    #: event key -> FeedFreshness, shared by every /inplay request (the gates are per event, not per request)
    freshness: dict = {}
    _lock = threading.Lock()  # ThreadingHTTPServer: two overlay tabs on one game must not race the poll memory

    def _espn(self, event_key: str):
        from .cli import _espn_state_fetcher

        f = self.espn_fetchers.get(event_key)
        if f is None:
            f = self.espn_fetchers[event_key] = _espn_state_fetcher(event_key)
        return f()

    def _settings(self, qs: dict[str, str]) -> dict[str, Any]:
        """Every declared key from the environment, plus the overlay's per-request Gold flag."""
        settings = settings_from_env()
        if _truthy(qs.get("gold")):
            settings["robinhood_gold"] = True
        return settings

    def _analyze(self, url: str, settings: dict[str, Any], qs: dict[str, str]) -> dict[str, Any]:
        """One venue scan with the scan/live-parity arguments (module docstring)."""
        res = self.analyzer.analyze_url(url, settings=settings, contracts=float(qs.get("contracts", 100)), target_margin=float(qs.get("target_margin", 0)), emit_no_side=True, executable_venues=executable_venues_for(settings))
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

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/health":
                self._send(200, {"ok": True, "service": "arb-engine bridge"})
            elif u.path == "/analyze":
                self._send(200, self._analyze(qs.get("url", ""), self._settings(qs), qs))
            elif u.path == "/inplay":
                from dataclasses import asdict

                from .strategy.inplay import Lot, evaluate_inplay

                url = qs.get("url", "")
                settings = self._settings(qs)
                now = time.time()
                me = recent_event(self.analyzer, url, max_age=float(qs.get("max_age", DEFAULT_INPLAY_MAX_AGE)), now=now)
                if me is None:
                    res = self._analyze(url, settings, qs)
                    if not res.get("ok") or "lines" in (res.get("analysis") or {}) or getattr(self.analyzer, "last_event", None) is None:
                        self._send(200, {"ok": False, "error": res.get("error") or "not a game-winner page"})
                        return
                    me = self.analyzer.last_event
                lots = [Lot.parse(x) for x in parse_qs(u.query).get("position", []) if x.strip()]
                gs = None
                if qs.get("espn", "1") not in ("0", "false"):
                    try:
                        gs = self._espn(me.event_key)
                    except Exception:
                        gs = None
                bankroll = float(qs.get("bankroll", 0) or 0) or None
                interval_s = float(qs.get("interval", DEFAULT_INPLAY_MAX_AGE) or DEFAULT_INPLAY_MAX_AGE)
                with self._lock:
                    fresh = self._freshness(me.event_key, settings, interval_s)
                    view = evaluate_inplay(me, lots, settings, steal_edge=float(qs.get("steal_edge", 0.03)), target_margin=float(qs.get("target_margin", 0)), game_state=gs, bankroll=bankroll, kelly_fraction=float(qs.get("kelly", 0.25) or 0.25), freshness=fresh, now=now)
                self._send(200, {"ok": True, "view": with_gate_fields(asdict(view), executable_venues_for(settings))})
            elif u.path == "/kalshi/markets":
                data = self.kalshi.get("/markets", {k: v for k, v in qs.items() if k in ("event_ticker", "series_ticker", "status", "limit")})
                self._send(200, data)
            elif u.path.startswith("/kalshi/market/"):
                ticker = u.path.rsplit("/", 1)[-1]
                self._send(200, {"market": self.kalshi.market(ticker), "series": self.analyzer._series(ticker)})
            else:
                self._send(404, {"ok": False, "error": "unknown route"})
        except Exception as e:  # keep the bridge alive on any error
            self._send(500, {"ok": False, "error": str(e)})

    def log_message(self, fmt: str, *args) -> None:  # quieter
        sys.stderr.write("bridge: " + fmt % args + "\n")


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    Handler.analyzer = EventAnalyzer()
    Handler.kalshi = Handler.analyzer.kalshi
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"arb-engine bridge listening on http://{host}:{port}  (GET /analyze?url=... | /inplay?url=... | /kalshi/market/<ticker> | /health)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

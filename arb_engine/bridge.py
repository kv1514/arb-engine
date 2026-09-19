"""Local HTTP bridge for the browser overlay: ``python -m arb_engine bridge``.

    GET /health
    GET /analyze?url=<robinhood event url>[&contracts=100&target_margin=0&gold=0]
    GET /inplay?url=…[&position=venue:outcome:price:count&bankroll=1000&kelly=0.25&steal_edge=0.03]
    GET /inplay?url=<robinhood event url>[&position=venue:outcome:price:count]...
                [&target_margin=0&max_age=10]  in-play view: lock/steal actions, game state, blended fair
                                          (reuses the venue scan /analyze just did for the same
                                          url when it is at most max_age seconds old)
    GET /kalshi/market/<ticker>          proxies for the extension (Kalshi's API refuses
    GET /kalshi/markets?event_ticker=…    browser Origins other than kalshi.com)

Binds to 127.0.0.1 only and adds permissive CORS headers so the extension (and the page)
can call it. It never places orders.
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import settings_from_env
from .eventlookup import EventAnalyzer
from .venues.kalshi import KalshiClient


DEFAULT_INPLAY_MAX_AGE = 10.0  # seconds; the overlay calls /inplay right after /analyze


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


class Handler(BaseHTTPRequestHandler):
    analyzer: EventAnalyzer
    kalshi: KalshiClient
    espn_fetchers: dict = {}

    def _espn(self, event_key: str):
        from .cli import _espn_state_fetcher

        f = self.espn_fetchers.get(event_key)
        if f is None:
            f = self.espn_fetchers[event_key] = _espn_state_fetcher(event_key)
        return f()

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
                url = qs.get("url", "")
                settings = settings_from_env()
                if qs.get("gold") in ("1", "true"):
                    settings["robinhood_gold"] = True
                res = self.analyzer.analyze_url(url, settings=settings, contracts=float(qs.get("contracts", 100)), target_margin=float(qs.get("target_margin", 0)))
                self._send(200, res)
            elif u.path == "/inplay":
                from dataclasses import asdict

                from .strategy.inplay import Lot, evaluate_inplay

                url = qs.get("url", "")
                settings = settings_from_env()
                if qs.get("gold") in ("1", "true"):
                    settings["robinhood_gold"] = True
                me = recent_event(self.analyzer, url, max_age=float(qs.get("max_age", DEFAULT_INPLAY_MAX_AGE)))
                if me is None:
                    res = self.analyzer.analyze_url(url, settings=settings, contracts=float(qs.get("contracts", 100)))
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
                view = evaluate_inplay(me, lots, settings, steal_edge=float(qs.get("steal_edge", 0.03)), target_margin=float(qs.get("target_margin", 0)), game_state=gs, bankroll=bankroll, kelly_fraction=float(qs.get("kelly", 0.25) or 0.25))
                self._send(200, {"ok": True, "view": asdict(view)})
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
    print(f"arb-engine bridge listening on http://{host}:{port}  (GET /analyze?url=... | /kalshi/market/<ticker> | /health)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

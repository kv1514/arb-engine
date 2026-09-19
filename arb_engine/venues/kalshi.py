"""Kalshi Trade API v2 — public market data plus (optional) signed portfolio/order calls.

Public endpoints need no key. Authenticated ones sign ``timestamp_ms + METHOD + path``
with RSA-PSS (SHA-256, MGF1, **digest-length salt** — see ``KalshiClient._sign``) and send
``KALSHI-ACCESS-KEY`` / ``KALSHI-ACCESS-SIGNATURE`` / ``KALSHI-ACCESS-TIMESTAMP`` headers.
Requires ``cryptography``.

Hosts: Kalshi moved the REST API to ``external-api.kalshi.com`` (prod) and
``external-api.demo.kalshi.co`` (demo); the older ``api.elections.kalshi.com`` /
``demo-api.kalshi.co`` still answer and are kept as ``LEGACY_REST_BASE`` — the client falls
back to them once on a *connection* error (DNS, refused, timeout), never on an HTTP status.
``KALSHI_BASE_URL`` overrides both and disables the fallback.

Environment: ``KALSHI_ENV`` (``demo`` default | ``prod``), ``KALSHI_API_KEY``,
``KALSHI_PRIVATE_KEY_PATH``, ``KALSHI_BASE_URL``, ``KALSHI_RATE_LIMIT`` (public reads/s,
default 15). Same key variables the 9crusher/mcp-server-kalshi MCP server uses, so one
``.env`` serves both.
"""

from __future__ import annotations

import base64
import http.client
import os
import re
import socket
import time
import urllib.error
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

from ..models import VENUE_KALSHI, Book, EventInfo, Level, OutcomeQuote, VenueSnapshot
from ..matching.normalize import (
    et_date,
    fmt_line,
    kalshi_ticker_date,
    nfl_event_key,
    parse_iso,
    person_keys,
    push_rule_for_line,
    split_pair,
    spread_event_key,
    spread_outcomes,
    strip_digits,
    tennis_event_key,
    ticker_pair,
    total_event_key,
)
from ..matching.teams import TEAM_SPORTS, nfl_team_city, nfl_team_code, team_code, team_name
from .http import HttpClient, HttpError

ENV_REST_BASE = {
    "prod": "https://external-api.kalshi.com/trade-api/v2",
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
}
# Pre-2026 hosts. Still served (both answered 200 on /exchange/status, 2026-09-19); the
# extension's DNR header rule and history.py still target api.elections.kalshi.com.
LEGACY_REST_BASE = {
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}
DEFAULT_RATE_LIMIT = 15.0  # public reads/s; Kalshi's documented read budget is well above this and 429s still back off
BATCH_CANCEL_MAX = 20      # orders per DELETE /portfolio/events/orders/batched call (the cap scales with the account's write tier)

# Errors that mean "we never reached the host" — the only trigger for the legacy-host fallback.
_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (urllib.error.URLError, socket.gaierror, ConnectionError, TimeoutError, http.client.HTTPException)


# Kalshi tennis rule text (rules_primary/secondary on every KX{ATP,WTA}MATCH market, 2026-09-18):
# "If X wins the match after a ball has been played, then Yes"; no ball played (injury, walkover,
# forfeiture, cancellation) -> "the market will resolve to a fair price in accordance with the
# rules"; postponed -> stays open up to two weeks. Robinhood's tennis contracts are this book.
# The settlement registry (matching/settlement_rules, P08) is the source of truth when present;
# this literal is the fallback so the adapter imports without it.
_TENNIS_SETTLEMENT_FALLBACK = {"retirement": "advancer", "walkover": "fair_price", "cancelled": "fair_price", "postponed": "open_2w"}


def _registry_tennis_settlement() -> tuple[dict[str, str], str]:
    """(rules, source): the registry's Kalshi entry when P08's module exists (either a flat
    rule dict or one keyed by venue), else the literal above. The registry exports ``{}``
    when its JSON is missing or corrupt and documents that as "use the adapters' literals",
    so an empty or missing Kalshi entry is the fallback too — otherwise every tennis
    EventInfo would carry an empty settlement dict and the scanner's mismatch flags would
    silently stop firing."""
    try:
        from ..matching.settlement_rules import TENNIS_SETTLEMENT as reg  # type: ignore[import-not-found]
    except ImportError:
        return dict(_TENNIS_SETTLEMENT_FALLBACK), "fallback"
    if not isinstance(reg, dict):
        return dict(_TENNIS_SETTLEMENT_FALLBACK), "fallback"
    venue_keyed = any(isinstance(v, dict) for v in reg.values())  # {"kalshi": {...}, "polymarket": {...}}
    rules = reg.get("kalshi") if venue_keyed else reg
    if not isinstance(rules, dict) or not rules or not all(isinstance(v, str) for v in rules.values()):
        return dict(_TENNIS_SETTLEMENT_FALLBACK), "fallback"
    return dict(rules), "registry"


TENNIS_SETTLEMENT, TENNIS_SETTLEMENT_SOURCE = _registry_tennis_settlement()

# Series we scan per sport. Game-level series carry maker fees (quadratic_with_maker_fees).
SPORT_SERIES: dict[str, list[dict[str, Any]]] = {
    "nfl": [
        {"series": "KXNFLGAME", "market_type": "moneyline"},
        {"series": "KXNFLSPREAD", "market_type": "spread"},
        {"series": "KXNFLTOTAL", "market_type": "total"},
    ],
    "ncaaf": [
        {"series": "KXNCAAFGAME", "market_type": "moneyline"},
        {"series": "KXNCAAFSPREAD", "market_type": "spread"},
        {"series": "KXNCAAFTOTAL", "market_type": "total"},
    ],
    "tennis": [
        {"series": "KXATPMATCH", "market_type": "moneyline"},
        {"series": "KXWTAMATCH", "market_type": "moneyline"},
        {"series": "KXATPCHALLENGERMATCH", "market_type": "moneyline"},
        {"series": "KXWTACHALLENGERMATCH", "market_type": "moneyline"},
    ],
    "nba": [{"series": "KXNBAGAME", "market_type": "moneyline"}],
    "nhl": [{"series": "KXNHLGAME", "market_type": "moneyline"}],
    "mlb": [{"series": "KXMLBGAME", "market_type": "moneyline"}],
}


def _f(x: Any) -> Optional[float]:
    try:
        return float(x) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None


class KalshiClient:
    def __init__(self, env: Optional[str] = None, api_key: Optional[str] = None, private_key_path: Optional[str] = None, base_url: Optional[str] = None, http: Optional[HttpClient] = None):
        self.env = (env or os.environ.get("KALSHI_ENV") or "demo").lower()
        explicit = base_url or os.environ.get("KALSHI_BASE_URL")
        self.base_url = (explicit or ENV_REST_BASE.get(self.env, ENV_REST_BASE["demo"])).rstrip("/")
        # Only the default host gets a legacy fallback; an explicit URL is the operator's choice.
        self.legacy_base_url: Optional[str] = None if explicit else LEGACY_REST_BASE.get(self.env, LEGACY_REST_BASE["demo"]).rstrip("/")
        self.fell_back = False
        self.api_key = api_key or os.environ.get("KALSHI_API_KEY")
        self.private_key_path = private_key_path or os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        self.http = http or HttpClient(rate_limit=float(os.environ.get("KALSHI_RATE_LIMIT", str(DEFAULT_RATE_LIMIT))), retries=3)
        self._private_key = None

    # ---- auth -----------------------------------------------------------------------
    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.private_key_path)

    def _load_key(self):
        if self._private_key is None:
            try:
                from cryptography.hazmat.primitives import serialization
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("pip install cryptography  (needed for Kalshi request signing)") from e
            with open(os.path.expanduser(self.private_key_path), "rb") as f:  # type: ignore[arg-type]
                self._private_key = serialization.load_pem_private_key(f.read(), password=None)
        return self._private_key

    def _sign(self, method: str, path: str) -> dict[str, str]:
        """RSA-PSS over ``timestamp_ms + METHOD + path`` with SHA-256 / MGF1-SHA256 and
        ``salt_length = DIGEST_LENGTH`` (32 bytes). Kalshi's docs and starter client
        (docs.kalshi.com/getting_started/api_keys; ``clients.py`` uses
        ``padding.PSS.DIGEST_LENGTH``) sign this way and the gateway verifies with that salt,
        so a ``MAX_LENGTH`` salt (222 bytes on a 2048-bit key) is rejected as a bad signature."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + path).encode()
        sig = self._load_key().sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        return {
            "KALSHI-ACCESS-KEY": self.api_key or "",
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        if not self.has_credentials:
            raise RuntimeError("Kalshi credentials missing: set KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH")
        # Signed path includes the /trade-api/v2 prefix but not the query string.
        prefix = self.base_url[self.base_url.index("/trade-api"):]
        return self._sign(method, prefix + path)

    # ---- request layer (host fallback) ----------------------------------------------
    @staticmethod
    def _is_connection_error(e: BaseException) -> bool:
        """True for errors raised before any HTTP status came back (curl exit codes surface as
        ``HttpError(status=0)``). A 4xx/5xx is the host talking to us and is never a reason
        to change hosts."""
        return isinstance(e, _CONNECTION_ERRORS) or (isinstance(e, HttpError) and e.status == 0)

    def _request(self, method: str, path: str, params: Optional[dict] = None, body: Any = None, auth: bool = False) -> Any:
        """Issue ``method path`` on ``base_url``; on a connection error, switch once to the
        legacy host for the rest of the session and retry. Signed headers are rebuilt per
        attempt (fresh timestamp) and cover the path prefix, which is the same on both hosts.
        A re-sent POST is safe for orders because every payload carries a ``client_order_id``
        the exchange de-duplicates (``HttpClient`` already re-sends on timeouts)."""
        for attempt in (0, 1):
            headers = self._auth_headers(method, path) if (auth or method != "GET") else None
            try:
                if method == "GET":
                    return self.http.get(self.base_url + path, params=params, headers=headers)
                if method == "POST":
                    return self.http.post(self.base_url + path, json_body=body, headers=headers)
                if body is None:
                    return self.http.delete(self.base_url + path, headers=headers)
                return self.http.request("DELETE", self.base_url + path, json_body=body, headers=headers)
            except Exception as e:
                if attempt == 0 and self.legacy_base_url and not self.fell_back and self._is_connection_error(e):
                    self.base_url, self.legacy_base_url, self.fell_back = self.legacy_base_url, None, True
                    continue
                raise
        raise AssertionError("unreachable")  # pragma: no cover

    # ---- public market data ---------------------------------------------------------
    def get(self, path: str, params: Optional[dict] = None, auth: bool = False) -> Any:
        return self._request("GET", path, params=params, auth=auth)

    def post(self, path: str, body: dict) -> Any:
        return self._request("POST", path, body=body, auth=True)

    def delete(self, path: str, body: Optional[dict] = None) -> Any:
        return self._request("DELETE", path, body=body, auth=True)

    def exchange_status(self) -> dict:
        return self.get("/exchange/status")

    def series(self, ticker: str) -> dict:
        return self.get(f"/series/{ticker}").get("series", {})

    def markets(self, series_ticker: str, status: str = "open", limit: int = 200, max_pages: int = 20) -> list[dict]:
        out: list[dict] = []
        cursor: Optional[str] = None
        for _ in range(max_pages):
            data = self.get("/markets", {"series_ticker": series_ticker, "status": status, "limit": limit, "cursor": cursor})
            out.extend(data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    def market(self, ticker: str) -> dict:
        return self.get(f"/markets/{ticker}").get("market", {})

    def orderbook(self, ticker: str, depth: int = 10) -> dict:
        return self.get(f"/markets/{ticker}/orderbook", {"depth": depth})

    def event(self, event_ticker: str) -> dict:
        return self.get(f"/events/{event_ticker}")

    # ---- portfolio / orders (signed) ------------------------------------------------
    def balance(self) -> dict:
        return self.get("/portfolio/balance", auth=True)

    def positions(self, **params: Any) -> dict:
        return self.get("/portfolio/positions", params or None, auth=True)

    # Reads have one path family. Kalshi's "V2" migration only moved the *writes* to
    # /portfolio/events/...; GET /portfolio/orders, /portfolio/orders/{id} and /portfolio/fills
    # stay the documented readers (api-reference/orders/get-orders, get-order,
    # portfolio/get-fills, 2026-09-19) and their rows carry both the legacy ``side``/``action``
    # and the canonical ``outcome_side``/``book_side`` + ``yes_price_dollars`` fields. There is
    # no GET under /portfolio/events/.
    def orders(self, **params: Any) -> dict:
        """One raw page of ``GET /portfolio/orders`` (``{"orders": [...], "cursor": ...}``);
        the ``kalshi orders`` CLI prints it. :meth:`orders_v2` follows the cursor."""
        return self.get("/portfolio/orders", params or None, auth=True)

    def order(self, order_id: str) -> dict:
        """``GET /portfolio/orders/{id}`` unwrapped to the ``Order`` row (what
        ``KalshiBroker.poll`` reads)."""
        return self.get(f"/portfolio/orders/{order_id}", auth=True).get("order", {})

    order_v2 = order  # same documented path; kept so callers written against the V2 name work

    def fills(self, **params: Any) -> dict:
        """One raw page of ``GET /portfolio/fills``; :meth:`fills_v2` follows the cursor."""
        return self.get("/portfolio/fills", params or None, auth=True)

    def orders_v2(self, **params: Any) -> list[dict]:
        """Every ``Order`` row of ``GET /portfolio/orders`` matching ``params`` (``status``
        resting|canceled|executed, ``ticker``, ``event_ticker``, ``limit``...), following
        ``cursor`` pages. Direction is ``outcome_side`` yes/no (``book_side`` bid/ask is the
        same bit); prices are ``yes_price_dollars`` / ``no_price_dollars`` strings; counts are
        ``*_count_fp`` strings — see :func:`order_side_price`."""
        return self._paged("/portfolio/orders", "orders", params)

    def fills_v2(self, **params: Any) -> list[dict]:
        """Every ``Fill`` row of ``GET /portfolio/fills`` (same direction/price vocabulary as
        :meth:`orders_v2`, plus ``fill_id``/``trade_id``, ``is_taker``, ``fee_cost``)."""
        return self._paged("/portfolio/fills", "fills", params)

    def _paged(self, path: str, key: str, params: Optional[dict], max_pages: int = 20) -> list[dict]:
        out: list[dict] = []
        q = dict(params or {})
        for _ in range(max_pages):
            data = self.get(path, q or None, auth=True)
            out.extend(data.get(key) or [])
            cursor = data.get("cursor")
            if not cursor:
                break
            q["cursor"] = cursor
        return out

    def trades(self, ticker: str, limit: int = 100, min_ts: Optional[int] = None) -> list[dict]:
        """Public trade prints for a market (newest first)."""
        return self.get("/markets/trades", {"ticker": ticker, "limit": limit, "min_ts": min_ts}).get("trades", [])

    def create_order(self, payload: dict) -> dict:
        """V2 order endpoint: ``side`` is ``bid`` (buy YES) / ``ask`` (sell YES), ``price``
        is the YES price in dollars as a 4-dp string, ``count`` a string."""
        return self.post("/portfolio/events/orders", payload)

    def cancel_order(self, order_id: str) -> dict:
        """``DELETE /portfolio/events/orders/{id}`` -> flat ``{order_id, client_order_id,
        reduced_by, ts_ms}`` (not an order object; ``reduced_by`` is the count taken off the
        book). 404 when the id is unknown."""
        return self.delete(f"/portfolio/events/orders/{order_id}")

    def cancel_orders_batched(self, orders: Sequence[str | dict]) -> list[dict]:
        """``DELETE /portfolio/events/orders/batched`` in chunks of :data:`BATCH_CANCEL_MAX`.

        Body per the documented ``BatchCancelOrdersV2Request``: ``{"orders": [{"order_id": ...,
        "exchange_index"?: int, "market_ticker"?: str, "subaccount"?: int}]}``; each item here
        is an id string or such a dict. ``exchange_index`` routes the cancel to the shard that
        holds the order (``market_ticker`` lets the gateway auto-route when it is unknown).
        One round trip per chunk is what makes a shutdown sweep of a full maker book fast
        enough to finish before a supervisor kills the process. Returns one response per
        chunk, each ``{"orders": [{order_id, client_order_id, reduced_by, ts_ms}]}`` where
        ``reduced_by`` is ``"0.00"`` when that cancel errored — see
        :func:`batch_cancel_reduced`. An empty list makes no request."""
        entries = [e for e in (_cancel_entry(o) for o in orders) if e]
        out: list[dict] = []
        for i in range(0, len(entries), BATCH_CANCEL_MAX):
            out.append(self.delete("/portfolio/events/orders/batched", {"orders": entries[i : i + BATCH_CANCEL_MAX]}))
        return out

    def cancel_all_orders(self, subaccount: Optional[int] = None) -> None:
        """``DELETE /portfolio/events/orders`` — the exchange cancels every resting order of
        the member on every shard (204, no body). Needs no listing, so it is the sweep that
        still works when ``GET /portfolio/orders`` does not; orders placed within the next
        minute may be cancelled too, so it belongs to shutdown paths only."""
        q = f"?subaccount={int(subaccount)}" if subaccount is not None else ""
        self.delete("/portfolio/events/orders" + q)


def _cancel_entry(o: Any) -> Optional[dict]:
    if isinstance(o, dict):
        oid = str(o.get("order_id") or o.get("id") or "")
        if not oid:
            return None
        e: dict[str, Any] = {"order_id": oid}
        for k in ("exchange_index", "market_ticker", "subaccount"):
            if o.get(k) is not None and o.get(k) != "":
                e[k] = o[k]
        return e
    return {"order_id": str(o)} if o else None


def batch_cancel_reduced(responses: Iterable[dict]) -> dict[str, float]:
    """``order_id -> reduced_by`` over batched-cancel responses; ``0.0`` means the exchange
    reported that cancel as errored (or the order had nothing left to cancel)."""
    out: dict[str, float] = {}
    for res in responses or ():
        for row in (res or {}).get("orders") or []:
            oid = str(row.get("order_id") or "")
            if oid:
                out[oid] = _f(row.get("reduced_by")) or 0.0
    return out


def order_side_price(od: dict) -> tuple[str, Optional[float]]:
    """(``yes``|``no``, price of that side) from an ``Order``/``Fill`` row. Canonical fields
    first (``outcome_side``, else ``book_side`` bid->yes / ask->no, else legacy ``side``);
    the price is ``yes_price_dollars`` for a YES order and ``no_price_dollars`` for a NO
    order, falling back to ``1 - yes`` and to the legacy cent fields."""
    side = str(od.get("outcome_side") or "").lower()
    if side not in ("yes", "no"):
        bs = str(od.get("book_side") or "").lower()
        side = "yes" if bs == "bid" else "no" if bs == "ask" else str(od.get("side") or "yes").lower()
    yes = _f(od.get("yes_price_dollars"))
    if yes is None and od.get("yes_price") is not None:
        yes = (_f(od.get("yes_price")) or 0.0) / 100.0
    no = _f(od.get("no_price_dollars"))
    if no is None and od.get("no_price") is not None:
        no = (_f(od.get("no_price")) or 0.0) / 100.0
    if side == "yes":
        price = yes if yes is not None else (round(1.0 - no, 4) if no is not None else None)
    else:
        price = no if no is not None else (round(1.0 - yes, 4) if yes is not None else None)
    return side, price


IOC_TIFS = {"immediate_or_cancel", "fill_or_kill", "ioc", "fok"}


def order_expiration(kickoff: Optional[float | datetime], now: Optional[float] = None, gtd_horizon_s: Optional[float] = None) -> Optional[int]:
    """Expiry for a resting order: the earlier of kickoff and ``now + gtd_horizon_s``, as a
    Unix timestamp in whole seconds (``CreateOrderV2Request.expiration_time`` is
    ``integer/int64``; an RFC 3339 string fails the gateway's validation), or ``None`` when
    neither bound is known. A resting maker order must not outlive the process that hedges
    it (the horizon) nor the pre-game book it was priced against (kickoff)."""
    now = time.time() if now is None else float(now)
    cands: list[float] = []
    if isinstance(kickoff, datetime):
        cands.append(kickoff.timestamp())
    elif kickoff is not None:
        cands.append(float(kickoff))
    if gtd_horizon_s is not None and gtd_horizon_s > 0:
        cands.append(now + float(gtd_horizon_s))
    if not cands:
        return None
    exp = max(min(cands), now + 1.0)  # never emit an already-expired timestamp
    return int(exp)


def build_order_payload(ticker: str, action: str, side: str, count: float, price: float, *, time_in_force: str = "good_till_canceled", post_only: bool = False, client_order_id: Optional[str] = None, exchange_index: Optional[int] = None, expiration_time: Optional[int | float | datetime] = None, cancel_order_on_pause: Optional[bool] = None, order_group_id: Optional[str] = None) -> dict:
    """Natural (buy/sell, yes/no, price of that side) -> Kalshi V2 (bid/ask on the YES leg).

    buy YES @ p  -> bid @ p          sell YES @ p -> ask @ p
    buy NO  @ p  -> ask @ 1 - p      sell NO  @ p -> bid @ 1 - p

    ``expiration_time`` (Unix seconds from :func:`order_expiration`; a datetime is converted)
    is sent as an int64 per the V2 schema, only makes sense for a resting order and is
    dropped for IOC/FOK (the gateway rejects the combination). ``cancel_order_on_pause`` asks the exchange to
    pull the order if trading pauses (a paused market re-opens on news we have not priced).
    Every new kwarg is optional so older callers and their tests are unchanged.
    """
    action = action.lower()
    side = side.lower()
    yes_price = price if side == "yes" else round(1.0 - price, 4)
    book_side = "bid" if (action, side) in {("buy", "yes"), ("sell", "no")} else "ask"
    payload: dict[str, Any] = {
        "ticker": ticker,
        "side": book_side,
        "count": str(int(count)) if float(count).is_integer() else str(count),
        "price": f"{yes_price:.4f}",
        "time_in_force": time_in_force,
        "self_trade_prevention_type": "taker_at_cross",
    }
    if post_only:
        payload["post_only"] = True
    if client_order_id:
        payload["client_order_id"] = client_order_id
    if exchange_index is not None:
        payload["exchange_index"] = exchange_index
    if expiration_time and time_in_force.lower() not in IOC_TIFS:
        payload["expiration_time"] = int(expiration_time.timestamp()) if isinstance(expiration_time, datetime) else int(expiration_time)
    if cancel_order_on_pause is not None:
        payload["cancel_order_on_pause"] = bool(cancel_order_on_pause)
    if order_group_id:
        payload["order_group_id"] = order_group_id
    return payload


def parse_orderbook(ob: dict) -> tuple[Book, Book]:
    """Kalshi returns resting bids only: ``yes_dollars``/``no_dollars`` = [[price, size]].
    YES asks are the NO bids mirrored (ask_yes = 1 - bid_no). Returns (yes_book, no_book)."""
    fp = ob.get("orderbook_fp") or ob.get("orderbook") or {}
    yes_bids = sorted(((float(p), float(s)) for p, s in (fp.get("yes_dollars") or fp.get("yes") or [])), key=lambda x: -x[0])
    no_bids = sorted(((float(p), float(s)) for p, s in (fp.get("no_dollars") or fp.get("no") or [])), key=lambda x: -x[0])
    # legacy integer-cent books
    if fp.get("yes") and not fp.get("yes_dollars"):
        yes_bids = [(p / 100.0, s) for p, s in yes_bids]
        no_bids = [(p / 100.0, s) for p, s in no_bids]
    yes_book = Book(asks=[Level(round(1.0 - p, 4), s) for p, s in no_bids], bids=[Level(p, s) for p, s in yes_bids])
    no_book = Book(asks=[Level(round(1.0 - p, 4), s) for p, s in yes_bids], bids=[Level(p, s) for p, s in no_bids])
    yes_book.asks.sort(key=lambda l: l.price)
    no_book.asks.sort(key=lambda l: l.price)
    return yes_book, no_book


class KalshiAdapter:
    venue = VENUE_KALSHI

    def __init__(self, client: Optional[KalshiClient] = None, with_books: bool = False, book_depth: int = 10):
        # Market data is identical on prod; default to prod for scanning even without keys.
        self.client = client or KalshiClient(env=os.environ.get("KALSHI_DATA_ENV", "prod"))
        self.with_books = with_books
        self.book_depth = book_depth
        self._series_cache: dict[str, dict] = {}

    def series_info(self, ticker: str) -> dict:
        if ticker not in self._series_cache:
            try:
                self._series_cache[ticker] = self.client.series(ticker)
            except Exception:
                self._series_cache[ticker] = {"ticker": ticker, "fee_type": "quadratic", "fee_multiplier": 1}
        return self._series_cache[ticker]

    def fetch(self, sport: str) -> VenueSnapshot:
        snap = VenueSnapshot(venue=self.venue, fetched_at=time.time())
        for spec in SPORT_SERIES.get(sport, []):
            try:
                markets = self.client.markets(spec["series"])
            except Exception as e:  # network / API error: record and continue
                snap.errors.append(f"{spec['series']}: {e}")
                continue
            series = self.series_info(spec["series"])
            fee_params = {"fee_type": series.get("fee_type"), "fee_multiplier": series.get("fee_multiplier", 1), "series": spec["series"]}
            self._ingest_markets(snap, sport, spec, markets, fee_params)
        if self.with_books:
            self._attach_books(snap)
        return snap

    def _attach_books(self, snap: VenueSnapshot, workers: int = 4) -> None:
        self.attach_books_for(snap.quotes, snap.errors, workers=workers)

    def attach_books_for(self, quotes: list[OutcomeQuote], errors: Optional[list[str]] = None, workers: int = 4) -> None:
        """Fetch order books (rate-limited, parallel) and attach the YES/NO side to each quote."""
        from concurrent.futures import ThreadPoolExecutor

        errors = errors if errors is not None else []
        tickers = sorted({q.meta.get("ticker") or q.venue_market_id.split("#")[0] for q in quotes})

        def one(t: str):
            try:
                return t, parse_orderbook(self.client.orderbook(t, self.book_depth))
            except Exception as e:
                return t, e

        books: dict[str, tuple[Book, Book]] = {}
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(tickers) or 1))) as pool:
            for t, res in pool.map(one, tickers):
                if isinstance(res, Exception):
                    errors.append(f"orderbook {t}: {res}")
                else:
                    books[t] = res
        for q in quotes:
            t = q.meta.get("ticker") or q.venue_market_id.split("#")[0]
            if t not in books:
                continue
            yes_book, no_book = books[t]
            q.book = no_book if q.meta.get("side") == "no" else yes_book
            if q.book.asks:
                q.ask = q.book.asks[0].price
                q.ask_size = q.book.asks[0].size
            if q.book.bids:
                q.bid = q.book.bids[0].price
                q.bid_size = q.book.bids[0].size

    def _ingest_markets(self, snap: VenueSnapshot, sport: str, spec: dict, markets: Iterable[dict], fee_params: dict) -> None:
        if spec["market_type"] in ("spread", "total"):
            self._ingest_line_markets(snap, sport, spec, markets, fee_params)
            return
        by_event: dict[str, list[dict]] = {}
        for m in markets:
            if m.get("status") not in (None, "active", "open"):
                continue
            by_event.setdefault(m.get("event_ticker", ""), []).append(m)
        for event_ticker, ms in by_event.items():
            if len(ms) < 2:
                continue  # need both sides for a mutually exclusive event
            start = None
            for m in ms:
                start = parse_iso(m.get("occurrence_datetime")) or start
            # The ticker's date is Kalshi's own ET game date; occurrence_datetime is not kickoff
            # for college games (26SEP19PURUCLA carries 2026-09-20T06:00Z for a 03:00Z kick).
            date = kalshi_ticker_date(event_ticker) or et_date(start)
            labels = {m["ticker"]: re.sub(r"\s*\((?:b\.|born)[^)]*\)", "", (m.get("yes_sub_title") or m.get("title", "")).replace(" wins", "")).strip() for m in ms}
            if sport in TEAM_SPORTS:
                codes = {m["ticker"]: (team_code(sport, m["ticker"].rsplit("-", 1)[-1]) or team_code(sport, labels[m["ticker"]])) for m in ms}
                if any(c is None for c in codes.values()):
                    continue
                key = f"{sport}:" + "|".join(sorted(codes.values())) + f":{date or ''}"  # type: ignore[arg-type]
                tie_rule = "half"
            elif sport == "tennis":
                pk = person_keys([labels[m["ticker"]] for m in ms])
                codes = {m["ticker"]: pk[i] for i, m in enumerate(ms)}
                key = tennis_event_key(list(labels.values()), date)
                tie_rule = "void"
                settlement = dict(TENNIS_SETTLEMENT)
            else:
                codes = {m["ticker"]: m["ticker"].rsplit("-", 1)[-1] for m in ms}
                key = f"{sport}:" + "|".join(sorted(codes.values())) + f":{date or ''}"
                tie_rule = "half"
            venue_meta: dict[str, Any] = {"event_ticker": event_ticker, "url": f"https://kalshi.com/markets/{spec['series'].lower()}/{event_ticker.lower()}"}
            if sport == "tennis":
                venue_meta["settlement"] = settlement
            info = EventInfo(
                event_key=key, sport=sport, market_type=spec["market_type"], outcomes=sorted(codes.values()),
                labels={codes[t]: labels[t] for t in codes}, start_time=start, tie_rule=tie_rule,
                venues={self.venue: venue_meta},
            )
            snap.events[key] = info
            for m in ms:
                yes_ask = _f(m.get("yes_ask_dollars")) or (_f(m.get("yes_ask")) / 100.0 if _f(m.get("yes_ask")) else None)
                yes_bid = _f(m.get("yes_bid_dollars")) or (_f(m.get("yes_bid")) / 100.0 if _f(m.get("yes_bid")) else None)
                book = None
                q = OutcomeQuote(
                    venue=self.venue, venue_market_id=m["ticker"], event_key=key, outcome=codes[m["ticker"]],
                    outcome_label=labels[m["ticker"]], ask=yes_ask if yes_ask and yes_ask < 1.0 else None,
                    bid=yes_bid if yes_bid and yes_bid > 0.0 else None,
                    ask_size=_f(m.get("yes_ask_size_fp")), bid_size=_f(m.get("yes_bid_size_fp")), book=book,
                    fee_params=dict(fee_params), url=f"https://kalshi.com/markets/{spec['series'].lower()}/{event_ticker.lower()}",
                    ts=snap.fetched_at,
                    meta={"ticker": m["ticker"], "side": "yes", "exchange_index": m.get("exchange_index"), "close_time": m.get("close_time"), "volume": _f(m.get("volume_fp")), "open_interest": _f(m.get("open_interest_fp")), "last": _f(m.get("last_price_dollars"))},
                )
                snap.quotes.append(q)


    # ---- spreads / totals: one binary market per line -------------------------------------
    def _ingest_line_markets(self, snap: VenueSnapshot, sport: str, spec: dict, markets: Iterable[dict], fee_params: dict) -> None:
        mtype = spec["market_type"]
        for m in markets:
            if m.get("status") not in (None, "active", "open"):
                continue
            line = _f(m.get("floor_strike"))
            if line is None:
                continue
            event_ticker = m.get("event_ticker", "")
            pair = ticker_pair(event_ticker)
            if not pair:
                continue
            start = parse_iso(m.get("occurrence_datetime"))
            date = kalshi_ticker_date(event_ticker) or et_date(start)
            url = f"https://kalshi.com/markets/{spec['series'].lower()}/{event_ticker.lower()}"
            if mtype == "spread":
                team_raw = strip_digits(m["ticker"].rsplit("-", 1)[-1])
                other_raw = split_pair(pair, team_raw)
                fav = team_code(sport, team_raw) if sport in TEAM_SPORTS else team_raw
                dog = (team_code(sport, other_raw) if sport in TEAM_SPORTS else other_raw) if other_raw else None
                if not fav or not dog:
                    continue
                key = spread_event_key(sport, [fav, dog], date, fav, line)
                yes_key, no_key = spread_outcomes(fav, dog, line)
                labels = {yes_key: f"{team_name(sport, fav) if sport in TEAM_SPORTS else fav} -{fmt_line(line)}", no_key: f"{team_name(sport, dog) if sport in TEAM_SPORTS else dog} +{fmt_line(line)}"}
                outcomes = [yes_key, no_key]
            else:
                codes: list[str] = []
                for cut in range(2, len(pair) - 1):  # split 'DETBUF' into two known codes
                    a, b = pair[:cut], pair[cut:]
                    if sport in TEAM_SPORTS and team_code(sport, a) and team_code(sport, b):
                        codes = [team_code(sport, a), team_code(sport, b)]  # type: ignore[list-item]
                        break
                if not codes:
                    codes = [pair[: len(pair) // 2], pair[len(pair) // 2:]]
                key = total_event_key(sport, codes, date, line)
                yes_key, no_key = "over", "under"
                labels = {"over": f"Over {fmt_line(line)}", "under": f"Under {fmt_line(line)}"}
                outcomes = ["over", "under"]
            info = EventInfo(
                event_key=key, sport=sport, market_type=mtype, outcomes=outcomes, labels=labels, start_time=start,
                line=line, tie_rule=push_rule_for_line(line),
                venues={self.venue: {"event_ticker": event_ticker, "ticker": m["ticker"], "url": url}, "_teams": {"title": _pair_title(pair, sport)}},
            )
            snap.events.setdefault(key, info)
            yes_ask, yes_bid = _f(m.get("yes_ask_dollars")), _f(m.get("yes_bid_dollars"))
            no_ask, no_bid = _f(m.get("no_ask_dollars")), _f(m.get("no_bid_dollars"))
            if no_ask is None and yes_bid is not None:
                no_ask = round(1.0 - yes_bid, 4)
            if no_bid is None and yes_ask is not None:
                no_bid = round(1.0 - yes_ask, 4)
            common = dict(venue=self.venue, event_key=key, fee_params=dict(fee_params), url=url, ts=snap.fetched_at)
            snap.quotes.append(OutcomeQuote(venue_market_id=m["ticker"], outcome=yes_key, outcome_label=labels[yes_key], ask=yes_ask if yes_ask and yes_ask < 1 else None, bid=yes_bid if yes_bid and yes_bid > 0 else None, ask_size=_f(m.get("yes_ask_size_fp")), bid_size=_f(m.get("yes_bid_size_fp")), meta={"ticker": m["ticker"], "side": "yes", "exchange_index": m.get("exchange_index"), "line": line}, **common))
            snap.quotes.append(OutcomeQuote(venue_market_id=m["ticker"] + "#no", outcome=no_key, outcome_label=labels[no_key], ask=no_ask if no_ask and no_ask < 1 else None, bid=no_bid if no_bid and no_bid > 0 else None, ask_size=_f(m.get("yes_bid_size_fp")), bid_size=_f(m.get("yes_ask_size_fp")), meta={"ticker": m["ticker"], "side": "no", "exchange_index": m.get("exchange_index"), "line": line}, **common))


def _pair_title(pair: str, sport: str) -> str:
    """'DETBUF' -> 'DET @ BUF' (Kalshi/Rothera pairs are away then home)."""
    for cut in range(2, len(pair) - 1):
        a, b = pair[:cut], pair[cut:]
        if sport not in TEAM_SPORTS or (team_code(sport, a) and team_code(sport, b)):
            return f"{a} @ {b}"
    return pair

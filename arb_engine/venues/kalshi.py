"""Kalshi Trade API v2 — public market data plus (optional) signed portfolio/order calls.

Public endpoints need no key. Authenticated ones sign ``timestamp_ms + METHOD + path``
with RSA-PSS (SHA-256, MGF1, max salt) and send ``KALSHI-ACCESS-KEY`` /
``KALSHI-ACCESS-SIGNATURE`` / ``KALSHI-ACCESS-TIMESTAMP`` headers. Requires ``cryptography``.

Environment: ``KALSHI_ENV`` (``demo`` default | ``prod``), ``KALSHI_API_KEY``,
``KALSHI_PRIVATE_KEY_PATH``. Same variables the 9crusher/mcp-server-kalshi MCP server uses,
so one ``.env`` serves both.
"""

from __future__ import annotations

import base64
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

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
from ..matching.teams import nfl_team_city, nfl_team_code
from .http import HttpClient

ENV_REST_BASE = {
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}

# Series we scan per sport. Game-level series carry maker fees (quadratic_with_maker_fees).
SPORT_SERIES: dict[str, list[dict[str, Any]]] = {
    "nfl": [
        {"series": "KXNFLGAME", "market_type": "moneyline"},
        {"series": "KXNFLSPREAD", "market_type": "spread"},
        {"series": "KXNFLTOTAL", "market_type": "total"},
    ],
    "ncaaf": [{"series": "KXNCAAFGAME", "market_type": "moneyline"}],
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
        self.base_url = (base_url or os.environ.get("KALSHI_BASE_URL") or ENV_REST_BASE.get(self.env, ENV_REST_BASE["demo"])).rstrip("/")
        self.api_key = api_key or os.environ.get("KALSHI_API_KEY")
        self.private_key_path = private_key_path or os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        self.http = http or HttpClient(rate_limit=float(os.environ.get("KALSHI_RATE_LIMIT", "8")), retries=3)
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
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + path).encode()
        sig = self._load_key().sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
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

    # ---- public market data ---------------------------------------------------------
    def get(self, path: str, params: Optional[dict] = None, auth: bool = False) -> Any:
        headers = self._auth_headers("GET", path) if auth else None
        return self.http.get(self.base_url + path, params=params, headers=headers)

    def post(self, path: str, body: dict) -> Any:
        return self.http.post(self.base_url + path, json_body=body, headers=self._auth_headers("POST", path))

    def delete(self, path: str) -> Any:
        return self.http.delete(self.base_url + path, headers=self._auth_headers("DELETE", path))

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

    def orders(self, **params: Any) -> dict:
        return self.get("/portfolio/orders", params or None, auth=True)

    def order(self, order_id: str) -> dict:
        return self.get(f"/portfolio/orders/{order_id}", auth=True).get("order", {})

    def fills(self, **params: Any) -> dict:
        return self.get("/portfolio/fills", params or None, auth=True)

    def trades(self, ticker: str, limit: int = 100, min_ts: Optional[int] = None) -> list[dict]:
        """Public trade prints for a market (newest first)."""
        return self.get("/markets/trades", {"ticker": ticker, "limit": limit, "min_ts": min_ts}).get("trades", [])

    def create_order(self, payload: dict) -> dict:
        """V2 order endpoint: ``side`` is ``bid`` (buy YES) / ``ask`` (sell YES), ``price``
        is the YES price in dollars as a 4-dp string, ``count`` a string."""
        return self.post("/portfolio/events/orders", payload)

    def cancel_order(self, order_id: str) -> dict:
        return self.delete(f"/portfolio/events/orders/{order_id}")


def build_order_payload(ticker: str, action: str, side: str, count: float, price: float, *, time_in_force: str = "good_till_canceled", post_only: bool = False, client_order_id: Optional[str] = None, exchange_index: Optional[int] = None) -> dict:
    """Natural (buy/sell, yes/no, price of that side) -> Kalshi V2 (bid/ask on the YES leg).

    buy YES @ p  -> bid @ p          sell YES @ p -> ask @ p
    buy NO  @ p  -> ask @ 1 - p      sell NO  @ p -> bid @ 1 - p
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
            date = et_date(start) or kalshi_ticker_date(event_ticker)
            labels = {m["ticker"]: re.sub(r"\s*\((?:b\.|born)[^)]*\)", "", (m.get("yes_sub_title") or m.get("title", "")).replace(" wins", "")).strip() for m in ms}
            if sport in ("nfl", "ncaaf"):
                if sport == "nfl":
                    codes = {m["ticker"]: (nfl_team_code(m["ticker"].rsplit("-", 1)[-1]) or nfl_team_code(labels[m["ticker"]])) for m in ms}
                else:
                    codes = {m["ticker"]: m["ticker"].rsplit("-", 1)[-1] for m in ms}
                if any(c is None for c in codes.values()):
                    continue
                key = f"{sport}:" + "|".join(sorted(codes.values())) + f":{date or ''}"  # type: ignore[arg-type]
                tie_rule = "half"
            elif sport == "tennis":
                pk = person_keys([labels[m["ticker"]] for m in ms])
                codes = {m["ticker"]: pk[i] for i, m in enumerate(ms)}
                key = tennis_event_key(list(labels.values()), date)
                tie_rule = "void"
            else:
                codes = {m["ticker"]: m["ticker"].rsplit("-", 1)[-1] for m in ms}
                key = f"{sport}:" + "|".join(sorted(codes.values())) + f":{date or ''}"
                tie_rule = "half"
            info = EventInfo(
                event_key=key, sport=sport, market_type=spec["market_type"], outcomes=sorted(codes.values()),
                labels={codes[t]: labels[t] for t in codes}, start_time=start, tie_rule=tie_rule,
                venues={self.venue: {"event_ticker": event_ticker, "url": f"https://kalshi.com/markets/{spec['series'].lower()}/{event_ticker.lower()}"}},
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
            date = et_date(start) or kalshi_ticker_date(event_ticker)
            url = f"https://kalshi.com/markets/{spec['series'].lower()}/{event_ticker.lower()}"
            if mtype == "spread":
                team_raw = strip_digits(m["ticker"].rsplit("-", 1)[-1])
                other_raw = split_pair(pair, team_raw)
                fav = nfl_team_code(team_raw) if sport == "nfl" else team_raw
                dog = (nfl_team_code(other_raw) if sport == "nfl" else other_raw) if other_raw else None
                if not fav or not dog:
                    continue
                key = spread_event_key(sport, [fav, dog], date, fav, line)
                yes_key, no_key = spread_outcomes(fav, dog, line)
                labels = {yes_key: f"{nfl_team_city(fav) if sport == 'nfl' else fav} -{fmt_line(line)}", no_key: f"{nfl_team_city(dog) if sport == 'nfl' else dog} +{fmt_line(line)}"}
                outcomes = [yes_key, no_key]
            else:
                codes: list[str] = []
                for cut in range(2, len(pair) - 1):  # split 'DETBUF' into two known codes
                    a, b = pair[:cut], pair[cut:]
                    if sport == "nfl" and nfl_team_code(a) and nfl_team_code(b):
                        codes = [nfl_team_code(a), nfl_team_code(b)]  # type: ignore[list-item]
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
        if sport != "nfl" or (nfl_team_code(a) and nfl_team_code(b)):
            return f"{a} @ {b}"
    return pair

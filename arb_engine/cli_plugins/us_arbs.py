"""Read-only NFL moneyline comparisons on Polymarket US, Kalshi and Robinhood.

``us-arbs`` never imports an executor, loads an account, sends an order or pushes an
alert. Public metadata discovers matching games; real, successfully received books
and quotes supply the displayed liquidity. Failed reads cannot fall back to catalog
prices. A positive quoted payoff is not evidence that both legs can be filled.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Mapping, Optional, TextIO

from ..models import VenueSnapshot
from ..venues.kalshi import ENV_REST_BASE, KalshiAdapter, KalshiClient
from ..venues.robinhood import RobinhoodAdapter, _epoch


class _ObservedHttp:
    """Public GET wrapper recording actual request/receipt times per source row.

    This local wrapper deliberately does not change timing semantics of the existing
    scan/live adapters. Completion of the HTTP read, not completion of a whole venue
    sweep, is the observation time. Only successful replies enter ``received``.
    """

    def __init__(self, http: Any):
        self.http = http
        self.received: dict[str, tuple[float, float]] = {}
        self.quote_rows: dict[str, dict[str, Any]] = {}

    @property
    def transport(self) -> Any:
        return self.http.transport

    @transport.setter
    def transport(self, value: Any) -> None:
        self.http.transport = value

    def __getattr__(self, name: str) -> Any:
        return getattr(self.http, name)

    def get(self, url: str, *args: Any, **kwargs: Any) -> Any:
        req_ts = time.time()
        data = self.http.get(url, *args, **kwargs)
        obs_ts = time.time()
        if "/orderbook" in url:
            self.received[url.split("/markets/", 1)[-1].split("/", 1)[0]] = (req_ts, obs_ts)
        elif "/marketdata/event/contract/quotes/" in url and isinstance(data, dict):
            for item in data.get("data") or []:
                row = item.get("data") if isinstance(item, dict) else None
                if isinstance(row, dict) and row.get("instrument_id"):
                    contract_id = str(row["instrument_id"])
                    self.received[contract_id] = (req_ts, obs_ts)
                    self.quote_rows[contract_id] = dict(row)
        return data


class _MoneylineKalshiAdapter(KalshiAdapter):
    """Use one NFL series; book-attach failures supply no liquidity."""

    def fetch(self, sport: str) -> VenueSnapshot:
        self.client.http.received.clear()
        snap = VenueSnapshot(venue=self.venue, fetched_at=time.time())
        try:
            markets = self.client.markets("KXNFLGAME")
            series = self.series_info("KXNFLGAME")
            params = {
                "series": "KXNFLGAME", "fee_type": series.get("fee_type"),
                "fee_multiplier": series.get("fee_multiplier", 1),
            }
            if series.get("fee_multiplier_assumed") or "fee_multiplier" not in series:
                params["fee_multiplier_assumed"] = True
            self._ingest_markets(snap, sport, {"series": "KXNFLGAME", "market_type": "moneyline"}, markets, params)
        except Exception as exc:
            snap.errors.append(f"KXNFLGAME: {exc}")
        snap.fetched_at = time.time()
        # Metadata prices are deliberately unavailable until an actual book arrives.
        for quote in snap.quotes:
            quote.ask = quote.bid = quote.ask_size = quote.bid_size = None
            quote.meta["refreshed"] = False
        return snap

    def attach_books_for(self, quotes: list[Any], errors: Optional[list[str]] = None, workers: int = 4) -> None:
        self.client.http.received.clear()
        super().attach_books_for(quotes, errors, workers=workers)
        for quote in quotes:
            ticker = quote.meta.get("ticker") or quote.venue_market_id.split("#", 1)[0]
            timing = self.client.http.received.get(ticker)
            if timing is None or quote.book is None:
                quote.ask = quote.bid = quote.ask_size = quote.bid_size = None
                quote.book = None
                quote.meta["refreshed"] = False
                continue
            req_ts, obs_ts = timing
            quote.ts = obs_ts
            quote.meta.update(req_ts=req_ts, obs_ts=obs_ts, refreshed=True, approx_time=False)
            quote.meta["book_received"] = True
            quote.meta["price_source"] = "public_book"
            # An empty side of a received book must not retain a catalog price/size.
            if not quote.book.asks:
                quote.ask = quote.ask_size = None
            if not quote.book.bids:
                quote.bid = quote.bid_size = None


class _ObservedRobinhoodAdapter(RobinhoodAdapter):
    def __init__(self, http: Any = None):
        super().__init__(http=http, refresh_quotes=True, with_lines=False)
        self.http = _ObservedHttp(self.http)

    def _quote_ts(self, contract_id: Any, fetched_at: float) -> float:
        timing = self.http.received.get(str(contract_id))
        return timing[1] if timing else super()._quote_ts(contract_id, fetched_at)

    def fetch(self, sport: str, emit_no_side: bool = True) -> VenueSnapshot:
        self.http.received.clear()
        self.http.quote_rows.clear()
        snap = super().fetch(sport, emit_no_side=emit_no_side)
        for quote in snap.quotes:
            timing = self.http.received.get(str(quote.meta.get("contract_id")))
            if timing is None:
                quote.ask = quote.bid = quote.ask_size = quote.bid_size = None
                quote.meta["refreshed"] = False
                continue
            req_ts, obs_ts = timing
            quote.ts = obs_ts
            quote.meta.update(req_ts=req_ts, obs_ts=obs_ts, refreshed=True, approx_time=False)
            quote.meta["price_source"] = "public_quote"
            row = self.http.quote_rows[str(quote.meta["contract_id"])]
            # A NO ask executes against the YES bid; do not assign the YES ask's
            # venue update time to that different side of the book.
            if quote.meta.get("side") == "no":
                source_time = row.get("no_ask_venue_timestamp") or row.get("bid_venue_timestamp")
            else:
                source_time = row.get("yes_ask_venue_timestamp") or row.get("ask_venue_timestamp")
            quote.quote_time = _epoch(source_time or row.get("updated_at"))
        snap.fetched_at = time.time()
        return snap


def _default_adapters(http: Any = None) -> list[Any]:
    from ..venues.polymarket_us import PolymarketUSAdapter
    from ..venues.http import HttpClient

    # Pin production PUBLIC data rather than KALSHI_BASE_URL/account environment.
    # All adapter methods invoked below use GET with auth=False. No key is loaded.
    kalshi_http = _ObservedHttp(http or HttpClient(rate_limit=15, retries=2))
    client = KalshiClient(env="prod", base_url=ENV_REST_BASE["prod"], http=kalshi_http)
    client.api_key = client.private_key_path = None
    return [
        _MoneylineKalshiAdapter(client=client, with_books=False),
        _ObservedRobinhoodAdapter(http=http),
        PolymarketUSAdapter(http=http, with_books=True),
    ]


def fetch_snapshots(*, http: Any = None, adapters: Optional[list[Any]] = None,
                    now: Optional[float] = None) -> list[VenueSnapshot]:
    """Fetch the three public feeds concurrently; preserve every venue's failures.

    ``http`` or ``adapters`` can be injected for offline tests. ``now`` controls only
    the pregame selection, never the observation timestamps. Only matching pregame
    moneylines need Kalshi book requests; props/lines and live games are not fetched.
    """
    chosen = _default_adapters(http) if adapters is None else list(adapters)
    cutoff = time.time() if now is None else float(now)

    def one(adapter: Any) -> VenueSnapshot:
        try:
            if adapter.venue == "robinhood":
                result = adapter.fetch("nfl", emit_no_side=True)
            else:
                result = adapter.fetch("nfl")
            if not isinstance(result, VenueSnapshot) or result.venue != adapter.venue:
                raise ValueError("adapter returned a wrongly scoped snapshot")
            return result
        except Exception as exc:
            return VenueSnapshot(venue=adapter.venue, fetched_at=time.time(), errors=[str(exc)])

    with ThreadPoolExecutor(max_workers=max(1, len(chosen))) as pool:
        snapshots = list(pool.map(one, chosen))
    key_venues: dict[str, set[str]] = {}
    for snap in snapshots:
        for key, info in snap.events.items():
            if info.sport != "nfl" or info.market_type != "moneyline" or info.in_play is True or info.start_time is None:
                continue
            if info.start_time.timestamp() <= cutoff:
                continue
            key_venues.setdefault(key, set()).add(snap.venue)
    shared = {key for key, venues in key_venues.items() if len(venues) >= 2}
    for adapter, snap in zip(chosen, snapshots):
        if snap.venue != "kalshi" or not hasattr(adapter, "attach_books_for"):
            continue
        quotes = [quote for quote in snap.quotes if quote.event_key in shared]
        if quotes:
            try:
                adapter.attach_books_for(quotes, snap.errors)
            except Exception as exc:
                snap.errors.append(f"orderbooks: {exc}")
                for quote in quotes:
                    quote.ask = quote.bid = quote.ask_size = quote.bid_size = None
                    quote.book = None
                    quote.meta["refreshed"] = False
        snap.fetched_at = time.time()
    return snapshots


def _number(value: str) -> float:
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("must be a finite number") from exc
    if not math.isfinite(result):
        raise argparse.ArgumentTypeError("must be a finite number")
    return result


def _positive(value: str) -> float:
    result = _number(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def _interval(value: str) -> float:
    result = _number(value)
    if result != 0 and result < 5:
        raise argparse.ArgumentTypeError("must be 0 (once) or at least 5 seconds")
    return result


def _contracts(value: str) -> int:
    try:
        result = int(value)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if not 1 <= result <= 10000:
        raise argparse.ArgumentTypeError("must be an integer from 1 through 10000")
    return result


def _margin(value: str) -> float:
    result = _number(value)
    if not 0 <= result < 1:
        raise argparse.ArgumentTypeError("must be at least 0 and less than 1")
    return result


def _print_report(report: Mapping[str, Any], stream: TextIO) -> None:
    candidates = report.get("candidates") or []
    print(f"# read-only US NFL moneyline scan: {report.get('events', 0)} events; "
          f"{len(candidates)} positive quoted pairs; no orders or alerts", file=stream)
    for venue, messages in (report.get("errors") or {}).items():
        if isinstance(messages, str):
            messages = [messages]
        for message in messages:
            print(f"  {venue}: {message}", file=stream)
    for candidate in candidates:
        legs = " + ".join(f"{leg['venue']}:{leg['outcome']}@{leg['price']} (fee bound ${leg.get('fee_bound', leg['fee'])})"
                          for leg in candidate.get("legs", []))
        print(f"  {candidate['event_key']} [{candidate.get('classification', 'conditional')}] "
              f"{candidate['contracts']} contracts; cost ${candidate['total_cost']}; "
              f"quoted profit ${candidate['profit']}: {legs}", file=stream)
        print("    gates: " + ", ".join(candidate.get("gates") or ["not an execution instruction"]), file=stream)
    stream.flush()


def run(args: argparse.Namespace, settings: Optional[dict[str, Any]] = None, *,
        fetcher: Optional[Callable[[], list[VenueSnapshot]]] = None,
        evaluator: Optional[Callable[..., dict[str, Any]]] = None,
        out: Optional[TextIO] = None, sleep: Optional[Callable[[float], Any]] = None) -> int:
    stream = sys.stdout if out is None else out
    pause = time.sleep if sleep is None else sleep
    if evaluator is None:
        from ..quant.us_arbitrage import find_candidates
        evaluate = find_candidates
    else:
        evaluate = evaluator
    if fetcher is None:
        adapters = _default_adapters()
        fetcher = lambda: fetch_snapshots(adapters=adapters)
    try:
        while True:
            started = time.monotonic()
            snapshots = fetcher()
            report = evaluate(snapshots, settings or {}, contracts=args.contracts,
                              side_cap=args.side_cap, max_age_s=args.max_quote_age,
                              min_margin=args.min_margin)
            if args.json:
                print(json.dumps(report, sort_keys=True, allow_nan=False), file=stream, flush=True)
            else:
                _print_report(report, stream)
            if not args.every:
                return 2 if not any(snap.quotes for snap in snapshots) and report.get("errors") else 0
            # Start-to-start cadence: a slow sweep is exposed through per-row age,
            # never masked by advancing the actual observation timestamps.
            pause(max(0.0, args.every - (time.monotonic() - started)))
    except KeyboardInterrupt:
        return 0


def register(subparsers: Any, existing_parsers: Any = None) -> None:
    parser = subparsers.add_parser("us-arbs", help="read-only NFL moneyline fee/depth comparisons: Polymarket US, Kalshi, Robinhood; no orders or alerts")
    parser.add_argument("--sport", choices=["nfl"], default="nfl", help="NFL whole-game moneylines only")
    parser.add_argument("--every", type=_interval, default=0.0, help="0 = one scan; otherwise at least 5 seconds between scan starts")
    parser.add_argument("--side-cap", type=_positive, default=25.0, help="maximum fee-inclusive quoted cost per leg in dollars (default 25)")
    parser.add_argument("--contracts", type=_contracts, default=100, help="maximum equal contracts per leg before depth/cost caps (1-10000)")
    parser.add_argument("--max-quote-age", type=_positive, default=6.0, help="maximum seconds since actual public quote/book receipt")
    parser.add_argument("--min-margin", type=_margin, default=0.0, help="minimum fee-adjusted quoted margin per contract")
    parser.add_argument("--json", action="store_true", help="print one JSON report per scan (JSON lines when repeated)")
    parser.set_defaults(func=run)

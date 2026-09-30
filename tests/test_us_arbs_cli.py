"""Offline regressions for the public, read-only US arbitrage command."""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import threading
import unittest
from datetime import datetime, timezone
from unittest import mock

from arb_engine.cli_plugins import us_arbs
from arb_engine.models import EventInfo, OutcomeQuote, VenueSnapshot
from arb_engine.venues.kalshi import KalshiClient
from tests.helpers import FakeHttp, FIXTURE_NOW, load


KEY = "nfl:BUF|DET:2026-09-17"


def parser():
    result = argparse.ArgumentParser()
    subparsers = result.add_subparsers(dest="command", required=True)
    us_arbs.register(subparsers, {})
    return result


def snapshot(venue="kalshi", *, key=KEY, live=False, start=FIXTURE_NOW + 100):
    info = EventInfo(key, "nfl", "moneyline", ["BUF", "DET"],
                     start_time=datetime.fromtimestamp(start, timezone.utc), in_play=live)
    quote = OutcomeQuote(venue, f"{venue}-1", key, "BUF", ask=.4, bid=.3, ask_size=20,
                         ts=FIXTURE_NOW, meta={"side": "yes"})
    return VenueSnapshot(venue, {key: info}, [quote], FIXTURE_NOW)


class Adapter:
    def __init__(self, snap=None, *, venue=None, error=None, barrier=None):
        self.venue = venue or snap.venue
        self.snap = snap
        self.error = error
        self.barrier = barrier
        self.calls = []
        self.attached = []

    def fetch(self, sport, **kwargs):
        self.calls.append((sport, kwargs))
        if self.barrier:
            self.barrier.wait(timeout=2)
        if self.error:
            raise self.error
        return self.snap

    def attach_books_for(self, quotes, errors):
        self.attached = list(quotes)


class ReadOnlyHttp(FakeHttp):
    def post(self, *args, **kwargs):
        raise AssertionError("US data scan must not POST")

    def delete(self, *args, **kwargs):
        raise AssertionError("US data scan must not DELETE")


class ParserTests(unittest.TestCase):
    def test_defaults_are_capped_read_only_once(self):
        args = parser().parse_args(["us-arbs"])
        self.assertIs(args.func, us_arbs.run)
        self.assertEqual((args.every, args.side_cap, args.contracts, args.max_quote_age, args.min_margin),
                         (0, 25, 100, 6, 0))
        self.assertFalse(args.json)

    def test_repeat_is_zero_or_at_least_five(self):
        for value in ("-1", "0.1", "4.99", "nan", "inf", "-inf"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser().parse_args(["us-arbs", "--every", value])
        self.assertEqual(parser().parse_args(["us-arbs", "--every", "5"]).every, 5)

    def test_rejects_nonfinite_or_nonpositive_caps_and_ages(self):
        for flag in ("--side-cap", "--max-quote-age"):
            for value in ("0", "-1", "nan", "inf", "not-a-number"):
                with self.subTest(flag=flag, value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    parser().parse_args(["us-arbs", flag, value])

    def test_contracts_require_positive_whole_count(self):
        for value in ("0", "-1", "0.5", "nan", "inf", "10001"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser().parse_args(["us-arbs", "--contracts", value])

    def test_margin_cannot_be_negative_nonfinite_or_one(self):
        for value in ("-0.01", "nan", "inf", "1", "2"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser().parse_args(["us-arbs", "--min-margin", value])

    def test_other_sports_and_execution_flags_not_supported(self):
        for argv in (["--sport", "ncaaf"], ["--confirm"], ["--execute"], ["--account", "live"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser().parse_args(["us-arbs"] + argv)


class FetchTests(unittest.TestCase):
    def test_partial_failure_keeps_other_venues_and_requests_rh_no_rows(self):
        kalshi = Adapter(snapshot())
        rh = Adapter(snapshot("robinhood"))
        pm = Adapter(venue="polymarket_us", error=TimeoutError("gateway unavailable"))
        snaps = us_arbs.fetch_snapshots(adapters=[kalshi, rh, pm], now=FIXTURE_NOW)
        self.assertEqual([snap.venue for snap in snaps], ["kalshi", "robinhood", "polymarket_us"])
        self.assertEqual(len(snaps[0].quotes), 1)
        self.assertEqual(snaps[2].errors, ["gateway unavailable"])
        self.assertEqual(rh.calls, [("nfl", {"emit_no_side": True})])
        self.assertEqual(kalshi.attached, snaps[0].quotes)

    def test_venue_reads_are_concurrent(self):
        barrier = threading.Barrier(3)
        adapters = [Adapter(snapshot(venue), barrier=barrier) for venue in ("kalshi", "robinhood", "polymarket_us")]
        snaps = us_arbs.fetch_snapshots(adapters=adapters, now=FIXTURE_NOW)
        self.assertTrue(all(not snap.errors for snap in snaps))

    def test_books_only_requested_for_shared_pregame_moneylines(self):
        kalshi = Adapter(snapshot())
        kalshi.snap.events["unmatched"] = EventInfo("unmatched", "nfl", "moneyline", ["A", "B"], start_time=datetime.fromtimestamp(FIXTURE_NOW + 100, timezone.utc))
        kalshi.snap.quotes.append(OutcomeQuote("kalshi", "other", "unmatched", "A", ask=.3))
        us_arbs.fetch_snapshots(adapters=[kalshi, Adapter(snapshot("polymarket_us"))], now=FIXTURE_NOW)
        self.assertEqual([quote.venue_market_id for quote in kalshi.attached], ["kalshi-1"])

    def test_live_elapsed_or_unknown_start_not_book_fetched(self):
        for live, start in ((True, FIXTURE_NOW + 10), (False, FIXTURE_NOW), (False, FIXTURE_NOW - 10)):
            with self.subTest(live=live, start=start):
                k = Adapter(snapshot(live=live, start=start))
                us_arbs.fetch_snapshots(adapters=[k, Adapter(snapshot("polymarket_us", live=live, start=start))], now=FIXTURE_NOW)
                self.assertEqual(k.attached, [])
        k = Adapter(snapshot())
        k.snap.events[KEY].start_time = None
        us_arbs.fetch_snapshots(adapters=[k, Adapter(snapshot("polymarket_us"))], now=FIXTURE_NOW)
        self.assertEqual(k.attached, [])

    def test_wrong_venue_scope_is_failure_not_arbitrage_input(self):
        a = Adapter(snapshot("robinhood"), venue="kalshi")
        snap = us_arbs.fetch_snapshots(adapters=[a], now=FIXTURE_NOW)[0]
        self.assertEqual(snap.venue, "kalshi")
        self.assertFalse(snap.quotes)
        self.assertIn("wrongly scoped", snap.errors[0])

    def test_book_attachment_exception_clears_prices(self):
        k = Adapter(snapshot())
        with mock.patch.object(k, "attach_books_for", side_effect=TimeoutError("book failure")):
            snap = us_arbs.fetch_snapshots(adapters=[k, Adapter(snapshot("polymarket_us"))], now=FIXTURE_NOW)[0]
        self.assertIsNone(snap.quotes[0].ask)
        self.assertIsNone(snap.quotes[0].ask_size)
        self.assertFalse(snap.quotes[0].meta["refreshed"])
        self.assertIn("book failure", snap.errors[0])


class ObservedQuoteTests(unittest.TestCase):
    def make_kalshi(self, book):
        http = ReadOnlyHttp({
            "/markets?": load("kalshi_markets_nfl.json"),
            "/series/": load("kalshi_series_kxnflgame.json"),
            "/orderbook": book,
        })
        client = KalshiClient(env="prod", base_url="https://external-api.kalshi.com/trade-api/v2", http=us_arbs._ObservedHttp(http))
        return us_arbs._MoneylineKalshiAdapter(client=client), http

    def test_metadata_asks_never_count_as_a_received_book(self):
        adapter, _ = self.make_kalshi({})
        snap = adapter.fetch("nfl")
        self.assertTrue(snap.quotes)
        self.assertTrue(all(quote.ask is None and not quote.meta["refreshed"] for quote in snap.quotes))

    def test_kalshi_book_times_are_receipt_not_sweep_start_and_no_auth(self):
        adapter, http = self.make_kalshi({"orderbook_fp": {"yes_dollars": [[".40", "12"]], "no_dollars": [[".55", "18"]]}})
        with mock.patch.object(adapter.client, "_sign", side_effect=AssertionError("no signing allowed")):
            snap = adapter.fetch("nfl")
            quotes = snap.quotes[:2]
            start = snap.fetched_at
            adapter.attach_books_for(quotes, snap.errors)
        self.assertTrue(all(quote.ts >= start for quote in quotes))
        self.assertTrue(all(quote.meta["req_ts"] <= quote.meta["obs_ts"] == quote.ts for quote in quotes))
        self.assertTrue(all(quote.ask == .45 and quote.ask_size == 18 for quote in quotes))
        self.assertTrue(all(quote.meta["refreshed"] for quote in quotes))
        self.assertTrue(all("/portfolio" not in url for url in http.calls))
        self.assertTrue(all("KXNFLSPREAD" not in url and "KXNFLTOTAL" not in url for url in http.calls))

    def test_kalshi_missing_book_cannot_reuse_catalog_size(self):
        def fail():
            raise TimeoutError("unavailable")
        adapter, _ = self.make_kalshi(fail)
        snap = adapter.fetch("nfl")
        quotes = snap.quotes[:1]
        adapter.attach_books_for(quotes, snap.errors)
        self.assertIsNone(quotes[0].ask)
        self.assertIsNone(quotes[0].ask_size)
        self.assertFalse(quotes[0].meta["refreshed"])
        self.assertTrue(snap.errors)

    def test_empty_received_book_is_not_liquidity(self):
        adapter, _ = self.make_kalshi({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}})
        snap = adapter.fetch("nfl")
        adapter.attach_books_for(snap.quotes[:1], snap.errors)
        quote = snap.quotes[0]
        self.assertTrue(quote.meta["refreshed"])
        self.assertIsNone(quote.ask)
        self.assertIsNone(quote.ask_size)

    def rh_http(self, received):
        props = copy.deepcopy(load("robinhood_page_props_nfl.json"))
        props["events"] = props["events"][:1]
        html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": props}}) + "</script>"
        http = ReadOnlyHttp({"/prediction-markets/nfl/": html, "/marketdata/event/contract/quotes/": {"data": [{"data": row} for row in received]}})
        return http, props

    def test_rh_omitted_response_rows_lose_catalog_prices(self):
        http, _ = self.rh_http([])
        snap = us_arbs._ObservedRobinhoodAdapter(http=http).fetch("nfl")
        self.assertEqual(len(snap.quotes), 4)
        self.assertTrue(all(quote.ask is None and quote.ask_size is None for quote in snap.quotes))
        self.assertTrue(all(not quote.meta["refreshed"] for quote in snap.quotes))
        self.assertTrue(snap.errors)

    def test_rh_yes_and_no_share_actual_quote_receipt_time(self):
        _, props = self.rh_http([])
        contract_id = props["events"][0]["eventContracts"]["0"]["id"]
        row = {"instrument_id": contract_id, "yes_ask_price": ".4", "yes_bid_price": ".3", "no_ask_price": ".7", "no_bid_price": ".6", "ask_size": "12", "bid_size": "15"}
        http, _ = self.rh_http([row])
        adapter = us_arbs._ObservedRobinhoodAdapter(http=http)
        snap = adapter.fetch("nfl")
        quotes = [quote for quote in snap.quotes if quote.meta["contract_id"] == contract_id]
        self.assertEqual({quote.meta["side"] for quote in quotes}, {"yes", "no"})
        self.assertEqual(quotes[0].ts, quotes[1].ts)
        self.assertTrue(all(quote.meta["req_ts"] <= quote.ts <= snap.fetched_at for quote in quotes))
        self.assertTrue(all(quote.meta["refreshed"] for quote in quotes))

    def test_partial_actual_quote_never_merges_catalog_ask_or_depth(self):
        _, props = self.rh_http([])
        contract_id = props["events"][0]["eventContracts"]["0"]["id"]
        # The catalog has prices and sizes, but this received API row does not.
        http, _ = self.rh_http([{"instrument_id": contract_id, "yes_bid_price": ".30"}])
        snap = us_arbs._ObservedRobinhoodAdapter(http=http).fetch("nfl")
        quotes = [quote for quote in snap.quotes if quote.meta["contract_id"] == contract_id]
        self.assertTrue(all(quote.meta["refreshed"] for quote in quotes))
        self.assertTrue(all(quote.ask is None and quote.ask_size is None for quote in quotes))

    def test_robinhood_no_side_uses_its_own_venue_update_time(self):
        _, props = self.rh_http([])
        contract_id = props["events"][0]["eventContracts"]["0"]["id"]
        row = {"instrument_id": contract_id, "yes_ask_price": ".4", "yes_bid_price": ".3", "no_ask_price": ".7", "no_bid_price": ".6", "ask_size": "12", "bid_size": "15", "ask_venue_timestamp": "2026-09-30T00:00:00Z", "bid_venue_timestamp": "2026-09-30T00:00:07Z"}
        http, _ = self.rh_http([row])
        snap = us_arbs._ObservedRobinhoodAdapter(http=http).fetch("nfl")
        quotes = {quote.meta["side"]: quote for quote in snap.quotes if quote.meta["contract_id"] == contract_id}
        self.assertEqual(quotes["no"].quote_time - quotes["yes"].quote_time, 7)

    def test_prior_receipt_cannot_survive_failed_restart_refresh(self):
        _, props = self.rh_http([])
        contract_id = props["events"][0]["eventContracts"]["0"]["id"]
        row = {"instrument_id": contract_id, "yes_ask_price": ".4", "yes_bid_price": ".3", "ask_size": "12", "bid_size": "15"}
        http, _ = self.rh_http([row])
        adapter = us_arbs._ObservedRobinhoodAdapter(http=http)
        first = adapter.fetch("nfl")
        self.assertTrue(any(quote.ask is not None for quote in first.quotes))
        http.routes["/marketdata/event/contract/quotes/"] = {"data": []}
        second = adapter.fetch("nfl")
        self.assertTrue(all(quote.ask is None for quote in second.quotes))
        self.assertTrue(all(not quote.meta["refreshed"] for quote in second.quotes))

    def test_transport_switch_delegates_for_catalog_retry(self):
        http = ReadOnlyHttp({})
        wrapped = us_arbs._ObservedHttp(http)
        wrapped.transport = "curl"
        self.assertEqual(http.transport, "curl")


class RunTests(unittest.TestCase):
    def report(self, errors=None):
        return {"candidates": [], "errors": errors or {}, "fetched_at": FIXTURE_NOW, "venues": ["kalshi"], "events": 1}

    def test_once_json_passes_exact_cost_depth_options(self):
        args = parser().parse_args(["us-arbs", "--json", "--side-cap", "12.5", "--contracts", "20", "--max-quote-age", "2", "--min-margin", ".01"])
        fetcher = mock.Mock(return_value=[snapshot()])
        evaluator = mock.Mock(return_value=self.report())
        out = io.StringIO()
        result = us_arbs.run(args, {"robinhood_gold": True}, fetcher=fetcher, evaluator=evaluator, out=out,
                             sleep=lambda _: self.fail("one scan must not sleep"))
        self.assertEqual(result, 0)
        fetcher.assert_called_once_with()
        evaluator.assert_called_once_with(fetcher.return_value, {"robinhood_gold": True}, contracts=20, side_cap=12.5, max_age_s=2, min_margin=.01)
        self.assertEqual(json.loads(out.getvalue()), self.report())

    def test_all_failed_feeds_return_failure_without_orders(self):
        args = parser().parse_args(["us-arbs", "--json"])
        result = us_arbs.run(args, fetcher=lambda: [VenueSnapshot("kalshi", errors=["down"])],
                             evaluator=lambda *a, **kw: self.report({"kalshi": ["down"]}), out=io.StringIO())
        self.assertEqual(result, 2)

    def test_partial_failure_is_visible_with_healthy_data(self):
        args = parser().parse_args(["us-arbs"])
        out = io.StringIO()
        result = us_arbs.run(args, fetcher=lambda: [snapshot(), VenueSnapshot("polymarket_us", errors=["down"])],
                             evaluator=lambda *a, **kw: self.report({"polymarket_us": ["down"]}), out=out)
        self.assertEqual(result, 0)
        self.assertIn("polymarket_us: down", out.getvalue())
        self.assertIn("no orders or alerts", out.getvalue())

    def test_printed_pairs_are_qualified_and_gate_reasons_shown(self):
        report = self.report()
        report["candidates"] = [{"event_key": KEY, "classification": "conditional", "contracts": 20, "total_cost": "19", "profit": "1", "gates": ["settlement-unverified"], "legs": [{"venue": "polymarket_us", "outcome": "BUF", "price": ".45", "fee": ".35"}]}]
        out = io.StringIO()
        us_arbs._print_report(report, out)
        self.assertIn("[conditional]", out.getvalue())
        self.assertIn("settlement-unverified", out.getvalue())
        self.assertNotIn("guaranteed", out.getvalue().lower())

    def test_repeat_outputs_json_lines_and_clean_interrupt(self):
        args = parser().parse_args(["us-arbs", "--json", "--every", "5"])
        fetcher = mock.Mock(return_value=[snapshot()])
        out = io.StringIO()
        pause = mock.Mock(side_effect=[None, KeyboardInterrupt()])
        with mock.patch.object(us_arbs.time, "monotonic", side_effect=[0, 2, 5, 6]):
            result = us_arbs.run(args, fetcher=fetcher, evaluator=lambda *a, **kw: self.report(), out=out, sleep=pause)
        self.assertEqual(result, 0)
        self.assertEqual(fetcher.call_count, 2)
        self.assertEqual([json.loads(line) for line in out.getvalue().splitlines()], [self.report(), self.report()])
        self.assertEqual([call.args[0] for call in pause.call_args_list], [3, 4])

    def test_slow_sweep_does_not_backdate_rows_or_sleep_negative(self):
        args = parser().parse_args(["us-arbs", "--json", "--every", "5"])
        pause = mock.Mock(side_effect=KeyboardInterrupt())
        with mock.patch.object(us_arbs.time, "monotonic", side_effect=[0, 10]):
            us_arbs.run(args, fetcher=lambda: [snapshot()], evaluator=lambda *a, **kw: self.report(), out=io.StringIO(), sleep=pause)
        pause.assert_called_once_with(0)

    def test_default_three_venue_path_is_public_and_reports_conditional_fee_pairs(self):
        observed = datetime(2026, 9, 30, 17, tzinfo=timezone.utc).timestamp()
        kickoff = "2026-10-02T00:15:00Z"
        markets = copy.deepcopy(load("kalshi_markets_nfl.json"))
        markets["markets"] = markets["markets"][:2]
        for market, code, label in zip(markets["markets"], ("CLE", "PIT"), ("Cleveland", "Pittsburgh")):
            market.update(ticker=f"KXNFLGAME-26OCT01CLEPIT-{code}", event_ticker="KXNFLGAME-26OCT01CLEPIT", occurrence_datetime=kickoff, yes_sub_title=label)
        props = copy.deepcopy(load("robinhood_page_props_nfl.json"))
        props["events"] = props["events"][:1]
        event = props["events"][0]
        rows = []
        for contract, code, label in zip(event["eventContracts"].values(), ("CLE", "PIT"), ("Cleveland", "Pittsburgh")):
            contract.update(symbol=f"NFLGAME-26OCT01CLEPIT-{code}", displayShortName=code, displayLongName=label)
            rows.append({"instrument_id": contract["id"], "yes_ask_price": ".5", "yes_bid_price": ".4", "no_ask_price": ".6", "no_bid_price": ".5", "ask_size": "20", "bid_size": "20", "updated_at": "2026-09-30T17:00:00Z"})
        props["eventStates"] = {event["id"]: {"gameStart": kickoff, "eventProgress": "Oct 1", "eventStatus": "scheduled"}}
        html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": props}}) + "</script>"
        http = ReadOnlyHttp({
            "/markets?": markets, "/series/": load("kalshi_series_kxnflgame.json"),
            "/orderbook": {"orderbook_fp": {"yes_dollars": [[".25", "50"]], "no_dollars": [[".70", "50"]]}},
            "/prediction-markets/nfl/": html,
            "/marketdata/event/contract/quotes/": {"data": [{"data": row} for row in rows]},
            "/v2/leagues/nfl/events": load("polymarket_us/events_nfl.json"),
            "/v1/markets/": load("polymarket_us/book_moneyline.json"),
        })
        args = parser().parse_args(["us-arbs", "--json"])
        out = io.StringIO()
        with mock.patch.object(us_arbs.time, "time", return_value=observed), \
             mock.patch.object(KalshiClient, "_sign", side_effect=AssertionError("no account authentication")):
            result = us_arbs.run(args, fetcher=lambda: us_arbs.fetch_snapshots(http=http, now=observed), out=out)
        report = json.loads(out.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(report["venues"], ["kalshi", "polymarket_us", "robinhood"])
        self.assertTrue(report["candidates"])
        self.assertTrue(any("polymarket_us" in {leg["venue"] for leg in candidate["legs"]} for candidate in report["candidates"]))
        for candidate in report["candidates"]:
            self.assertEqual(candidate["classification"], "conditional")
            self.assertTrue(all(leg["cost"] <= 25 and leg["fee"] > 0 for leg in candidate["legs"]))
        self.assertFalse(any("/portfolio" in url for url in http.calls))
        self.assertFalse(report["errors"])

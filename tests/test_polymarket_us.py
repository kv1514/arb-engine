"""Polymarket US public-book adapter: offline, deliberately adversarial fixtures."""

import copy
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from arb_engine.store import Store
from arb_engine.venues.polymarket_us import GATEWAY, PolymarketUSAdapter, complement_book, parse_us_book

from .helpers import FakeHttp, SequencedFakeHttp, load


OBSERVED = datetime(2026, 9, 30, 17, tzinfo=timezone.utc).timestamp()
KEY = "nfl:CLE|PIT:2026-10-01"
SLUG = "tec-nfl-cle-pit-2026-10-01-moneyline"


class PolymarketUSAdapterTests(unittest.TestCase):
    def setUp(self):
        self.events = load("polymarket_us/events_nfl.json")
        self.book = load("polymarket_us/book_moneyline.json")

    def fetch(self, events=None, book=None, **kwargs):
        http = FakeHttp({"/v2/leagues/nfl/events": self.events if events is None else events,
                         "/book": self.book if book is None else book})
        adapter = PolymarketUSAdapter(http=http, clock=lambda: OBSERVED, **kwargs)
        return adapter.fetch("nfl"), http

    def test_exact_et_game_key_not_listing_or_utc_date(self):
        snap, _ = self.fetch()
        self.assertEqual(snap.errors, [])
        self.assertEqual(list(snap.events), [KEY])
        self.assertEqual(snap.events[KEY].start_time.isoformat(), "2026-10-02T00:15:00+00:00")
        self.assertEqual(snap.events[KEY].tie_rule, "unknown")
        self.assertFalse(snap.events[KEY].in_play)

    def test_long_flag_not_side_array_or_metadata_prices(self):
        snap, http = self.fetch(with_books=True)
        quotes = {quote.outcome: quote for quote in snap.quotes}
        long, short = quotes["CLE"], quotes["PIT"]
        self.assertEqual((long.ask, long.bid, long.ask_size, long.bid_size), (0.45, 0.43, 8.25, 10.5))
        self.assertEqual((short.ask, short.bid, short.ask_size, short.bid_size), (0.57, 0.55, 10.5, 8.25))
        self.assertEqual(short.meta["side"], "no")
        self.assertEqual(short.meta["no_of"], "CLE")
        self.assertTrue(short.venue_market_id.endswith("#no"))
        self.assertEqual(short.book_id, "polymarket_us")
        self.assertEqual(short.book.asks[1].price, 0.6)
        self.assertEqual(short.book.asks[1].size, 20.25)
        self.assertEqual(len([url for url in http.calls if url.endswith("/book")]), 1)
        self.assertTrue(all(url.startswith(GATEWAY) for url in http.calls))

    def test_l1_mode_still_reads_book_not_outcome_prices(self):
        snap, http = self.fetch()
        self.assertEqual(next(quote for quote in snap.quotes if quote.outcome == "CLE").ask, 0.45)
        self.assertTrue(all(quote.book is None for quote in snap.quotes))
        self.assertEqual(len(http.calls), 2)

    def test_exact_request_completion_timestamps_survive_recorder(self):
        clock = iter([OBSERVED - 4, OBSERVED - 3, OBSERVED - 2, OBSERVED, OBSERVED + 1])
        http = FakeHttp({"/events": self.events, "/book": self.book})
        adapter = PolymarketUSAdapter(http=http, clock=lambda: next(clock))
        snap = adapter.fetch("nfl")
        self.assertEqual(snap.fetched_at, OBSERVED + 1)
        for quote in snap.quotes:
            self.assertEqual(quote.ts, OBSERVED)
            self.assertEqual(quote.meta["req_ts"], OBSERVED - 2)
            self.assertEqual(quote.meta["obs_ts"], OBSERVED)
            self.assertTrue(quote.meta["refreshed"])
            self.assertFalse(quote.meta["approx_time"])
            self.assertEqual(quote.quote_time, OBSERVED - 1)
        rows = Store.l1_from_quotes({"polymarket_us": snap.quotes})["rows"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["obs_ts"] <= snap.fetched_at and not row["approx_time"] for row in rows))

    def test_unknown_settlement_never_inherits_global_rule(self):
        snap, _ = self.fetch()
        for quote in snap.quotes:
            self.assertIsNone(quote.meta["tie_payout"])
            self.assertEqual(quote.meta["settlement"], {})
            self.assertFalse(quote.meta["settlement_verified"])
            self.assertIn("Schema-derived", quote.meta["description"])
            self.assertEqual(len(quote.meta["settlement_rules_hash"]), 64)
            self.assertIn(GATEWAY, quote.meta["settlement_source"])

    def test_nfl_current_fee_keeps_unknown_coefficient_without_guessing_zero(self):
        snap, _ = self.fetch()
        for quote in snap.quotes:
            self.assertEqual(quote.fee_params["taker_theta"], "0.0695")
            self.assertEqual(quote.fee_params["reported_fee_coefficient"], 0)
            self.assertEqual(quote.fee_params["volume_rebate"], "0")
            self.assertEqual(quote.fee_params["fee_date_et"], "2026-09-30")

    def test_fee_boundary_uses_eastern_not_utc_date(self):
        before = datetime(2026, 9, 25, 3, 59, 59, tzinfo=timezone.utc).timestamp()
        at = datetime(2026, 9, 25, 4, tzinfo=timezone.utc).timestamp()
        adapter = PolymarketUSAdapter(http=FakeHttp({}))
        self.assertEqual(adapter._fee_params(before, None)["taker_theta"], "0.06")
        self.assertEqual(adapter._fee_params(at, None)["taker_theta"], "0.0695")

    def test_fractional_size_minimum_and_tick_preserved(self):
        snap, _ = self.fetch(with_books=True)
        quote = snap.quotes[0]
        self.assertEqual(quote.meta["min_size"], 0.01)
        self.assertEqual(quote.meta["tick"], 0.005)
        self.assertEqual(quote.ask_size, 10.5)

    def test_unsupported_sport_never_calls_gateway(self):
        http = FakeHttp({})
        snap = PolymarketUSAdapter(http=http, clock=lambda: OBSERVED).fetch("ncaaf")
        self.assertEqual(http.calls, [])
        self.assertEqual(snap.quotes, [])
        self.assertIn("unsupported sport", snap.errors[0])

    def test_missing_events_is_failed_not_empty(self):
        snap, _ = self.fetch(events={})
        self.assertEqual(snap.quotes, [])
        self.assertTrue(snap.errors)
        snap, _ = self.fetch(events={"events": []})
        self.assertEqual(snap.errors, [])

    def test_wrong_league_is_rejected(self):
        self.events["league"]["slug"] = "nba"
        snap, _ = self.fetch()
        self.assertEqual(snap.quotes, [])
        self.assertIn("not NFL", snap.errors[0])

    def test_closed_hidden_or_untradable_metadata_is_excluded(self):
        for field in ("closed", "hidden", "archived"):
            with self.subTest(field=field):
                data = copy.deepcopy(self.events)
                data["events"][0]["markets"][0][field] = True
                snap, http = self.fetch(events=data)
                self.assertEqual(snap.quotes, [])
                self.assertEqual(len(http.calls), 1)
        self.events["events"][0]["markets"][0]["marketSides"][0]["tradable"] = False
        snap, _ = self.fetch()
        self.assertEqual(snap.quotes, [])

    def test_period_moneyline_rejected_even_when_enum_says_moneyline(self):
        self.events["events"][0]["markets"][0]["question"] += " - First Half Winner"
        snap, http = self.fetch()
        self.assertEqual(snap.quotes, [])
        self.assertEqual(len(http.calls), 1)
        self.assertIn("period-specific", snap.errors[0])

    def test_live_gateway_full_game_type_and_lowercase_abbreviations(self):
        market = self.events["events"][0]["markets"][0]
        # Public gateway observed 2026-09-30 uses a more specific legacy string
        # than the API schema's generic moneyline example.
        market["sportsMarketType"] = "football_team_full_game_winner"
        for side in market["marketSides"]:
            side["team"]["abbreviation"] = side["team"]["abbreviation"].lower()
        snap, _ = self.fetch()
        self.assertEqual(snap.errors, [])
        self.assertEqual({quote.outcome for quote in snap.quotes}, {"PIT", "CLE"})

    def test_trimmed_actual_gateway_event_and_book_parse_without_global_defaults(self):
        events = load("polymarket_us/events_nfl_live_trimmed.json")
        book = load("polymarket_us/book_live_trimmed.json")
        completed = datetime(2026, 9, 30, 19, 27, 1, tzinfo=timezone.utc).timestamp()
        http = FakeHttp({"/events": events, "/book": book})
        snap = PolymarketUSAdapter(http=http, with_books=True, clock=lambda: completed).fetch("nfl")
        self.assertEqual(snap.errors, [])
        self.assertEqual(list(snap.events), [KEY])
        quotes = {quote.outcome: quote for quote in snap.quotes}
        self.assertEqual((quotes["PIT"].ask, quotes["PIT"].bid), (.58, .575))
        self.assertEqual((quotes["CLE"].ask, quotes["CLE"].bid), (.425, .42))
        self.assertEqual(quotes["CLE"].ask_size, 1903613.05)
        self.assertEqual(quotes["PIT"].fee_params["taker_theta"], "0.0695")
        self.assertIsNone(quotes["PIT"].meta["tie_payout"])
        self.assertFalse(quotes["PIT"].meta["settlement_verified"])

    def test_missing_long_flag_or_conflicting_directions_rejected(self):
        for bad in (None, "false", True):
            with self.subTest(bad=bad):
                data = copy.deepcopy(self.events)
                data["events"][0]["markets"][0]["marketSides"][0]["long"] = bad
                snap, _ = self.fetch(events=data)
                self.assertEqual(snap.quotes, [])
                self.assertTrue(snap.errors)

    def test_wrong_team_or_event_participant_not_matched(self):
        self.events["events"][0]["markets"][0]["marketSides"][0]["team"]["abbreviation"] = "BUF"
        snap, _ = self.fetch()
        self.assertEqual(snap.quotes, [])
        self.assertIn("team identity", snap.errors[0])

    def test_missing_kickoff_never_falls_back_to_listing_date(self):
        event = self.events["events"][0]
        del event["startTime"]
        del event["markets"][0]["gameStartTime"]
        snap, _ = self.fetch()
        self.assertEqual(snap.quotes, [])
        self.assertIn("missing game kickoff", snap.errors[0])

    def test_different_kickoffs_rejected(self):
        self.events["events"][0]["markets"][0]["gameStartTime"] = "2026-10-03T00:15:00Z"
        snap, _ = self.fetch()
        self.assertEqual(snap.quotes, [])
        self.assertIn("kickoff disagree", snap.errors[0])

    def test_live_status_cannot_be_masked_by_false_top_level(self):
        self.events["events"][0]["eventState"] = {"live": True}
        snap, _ = self.fetch()
        self.assertTrue(snap.events[KEY].in_play)

    def test_duplicate_event_does_not_duplicate_depth(self):
        self.events["events"].append(copy.deepcopy(self.events["events"][0]))
        snap, http = self.fetch()
        self.assertEqual(len(snap.quotes), 2)
        self.assertEqual(len(http.calls), 2)

    def test_conflicting_duplicate_event_is_removed_not_first_wins(self):
        other = copy.deepcopy(self.events["events"][0])
        other["markets"][0]["marketSides"][0]["team"]["abbreviation"] = "BUF"
        self.events["events"].append(other)
        snap, http = self.fetch()
        self.assertEqual(snap.quotes, [])
        self.assertEqual(snap.events, {})
        self.assertEqual(len(http.calls), 1)

    def test_invalid_book_identity_state_currency_or_nonfinite_rows_fail_closed(self):
        mutations = [("marketSlug", "wrong"), ("state", "MARKET_STATE_SUSPENDED")]
        for field, value in mutations:
            with self.subTest(field=field):
                book = copy.deepcopy(self.book)
                book["marketData"][field] = value
                snap, _ = self.fetch(book=book)
                self.assertTrue(all(quote.ask is None for quote in snap.quotes))
                self.assertTrue(snap.errors)
        for bad in ("NaN", "Infinity", "-1", "0", "100"):
            with self.subTest(price=bad):
                book = copy.deepcopy(self.book)
                book["marketData"]["offers"][0]["px"]["value"] = bad
                snap, _ = self.fetch(book=book)
                self.assertTrue(all(quote.ask is None for quote in snap.quotes))
        self.book["marketData"]["bids"][0]["px"]["currency"] = "USDC"
        snap, _ = self.fetch()
        self.assertTrue(all(quote.ask is None for quote in snap.quotes))

    def test_duplicate_price_depth_is_not_added_twice(self):
        self.book["marketData"]["bids"].append(copy.deepcopy(self.book["marketData"]["bids"][0]))
        snap, _ = self.fetch()
        self.assertTrue(all(quote.ask is None for quote in snap.quotes))
        self.assertIn("duplicate displayed", snap.errors[0])

    def test_future_quote_timestamp_rejected(self):
        self.book["marketData"]["transactTime"] = "2026-10-01T00:00:00Z"
        snap, _ = self.fetch()
        self.assertTrue(all(quote.ask is None for quote in snap.quotes))
        self.assertIn("later than response", snap.errors[0])

    def test_missing_timestamp_is_unknown_not_last_trade_time(self):
        self.book["marketData"]["transactTime"] = None
        snap, _ = self.fetch()
        self.assertTrue(all(quote.quote_time is None for quote in snap.quotes))
        self.assertTrue(all(quote.meta["obs_ts"] == OBSERVED and quote.meta["refreshed"] for quote in snap.quotes))

    def test_one_sided_book_does_not_invent_complement_liquidity(self):
        self.book["marketData"]["bids"] = []
        snap, _ = self.fetch(with_books=True)
        short = next(quote for quote in snap.quotes if quote.outcome == "PIT")
        long = next(quote for quote in snap.quotes if quote.outcome == "CLE")
        self.assertIsNone(short.ask)
        self.assertEqual(short.book.asks, [])
        self.assertEqual(long.ask, 0.45)
        self.assertEqual(short.bid, 0.55)

    def test_failed_refresh_clears_prices_and_retains_original_timestamps(self):
        snap, _ = self.fetch(with_books=True)
        quote = snap.quotes[0]
        old_times = (quote.meta["req_ts"], quote.meta["obs_ts"], quote.ts)
        adapter = PolymarketUSAdapter(http=SequencedFakeHttp([RuntimeError("timeout")]), clock=lambda: OBSERVED + 10)
        errors = []
        adapter.attach_books_for(snap.quotes, errors)
        self.assertTrue(errors)
        self.assertEqual((quote.meta["req_ts"], quote.meta["obs_ts"], quote.ts), old_times)
        self.assertFalse(quote.meta["refreshed"])
        self.assertEqual(quote.meta["arb_ineligible"], "book-unavailable")
        self.assertIsNone(quote.ask)
        self.assertIsNone(quote.book)

    def test_failed_book_does_not_block_other_market_response(self):
        event = copy.deepcopy(self.events["events"][0])
        event["id"] = "second-event"
        event["markets"][0]["id"] = "second-market"
        event["markets"][0]["slug"] = SLUG + "-other"
        self.events["events"].append(event)
        second_book = copy.deepcopy(self.book)
        second_book["marketData"]["marketSlug"] += "-other"
        http = SequencedFakeHttp([self.events, RuntimeError("timeout"), second_book])
        snap = PolymarketUSAdapter(http=http, clock=lambda: OBSERVED).fetch("nfl")
        self.assertEqual(len(snap.errors), 1)
        self.assertEqual(sum(quote.ask is not None for quote in snap.quotes), 2)

    def test_complement_decimal_roundtrip_prices_and_sizes(self):
        book, _ = parse_us_book(self.book, SLUG)
        self.assertEqual(complement_book(complement_book(book)), book)
        short = complement_book(book)
        self.assertEqual(Decimal(str(short.asks[0].price)) + Decimal(str(book.bids[0].price)), Decimal("1"))


if __name__ == "__main__":
    unittest.main()

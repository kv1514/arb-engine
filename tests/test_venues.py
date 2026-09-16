"""Adapters parse recorded venue payloads offline."""

import json
import unittest

from arb_engine.models import Book
from arb_engine.venues.kalshi import KalshiAdapter, KalshiClient, build_order_payload, parse_orderbook
from arb_engine.venues.polymarket import PolymarketAdapter, parse_clob_book
from arb_engine.venues.robinhood import RobinhoodAdapter, _in_play_from_progress, clean_label, extract_next_data

from .helpers import FakeHttp, load


class KalshiAdapterTests(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp({
            "/markets/KXNFLGAME-26SEP17DETBUF-DET/orderbook": load("kalshi_orderbook.json"),
            "/markets/KXNFLGAME-26SEP17DETBUF-BUF/orderbook": load("kalshi_orderbook.json"),
            "/markets?": load("kalshi_markets_nfl.json"),
            "/series/KXNFLGAME": load("kalshi_series_kxnflgame.json"),
        })
        self.adapter = KalshiAdapter(client=KalshiClient(env="prod", http=self.http))

    def test_fetch_nfl(self):
        snap = self.adapter.fetch("nfl")
        self.assertEqual(snap.errors, [])
        self.assertEqual(list(snap.events), ["nfl:BUF|DET:2026-09-17"])
        info = snap.events["nfl:BUF|DET:2026-09-17"]
        self.assertEqual(info.outcomes, ["BUF", "DET"])
        self.assertEqual(info.labels["DET"], "Detroit")
        self.assertEqual(info.tie_rule, "half")
        self.assertEqual(len(snap.quotes), 2)
        det = next(q for q in snap.quotes if q.outcome == "DET")
        self.assertEqual(det.ask, 0.33)
        self.assertEqual(det.bid, 0.32)
        self.assertEqual(det.fee_params["fee_type"], "quadratic_with_maker_fees")
        self.assertEqual(det.book_id, "kalshi")

    def test_fetch_with_books(self):
        adapter = KalshiAdapter(client=KalshiClient(env="prod", http=self.http), with_books=True)
        snap = adapter.fetch("nfl")
        q = snap.quotes[0]
        self.assertIsInstance(q.book, Book)
        self.assertTrue(q.book.asks)
        self.assertEqual(q.book.asks, sorted(q.book.asks, key=lambda l: l.price))

    def test_parse_orderbook_mirrors_no_bids_into_yes_asks(self):
        yes, no = parse_orderbook({"orderbook_fp": {"yes_dollars": [["0.54", "10"], ["0.55", "5"]], "no_dollars": [["0.40", "7"], ["0.41", "3"]]}})
        self.assertEqual([l.price for l in yes.asks], [0.59, 0.60])
        self.assertEqual([l.size for l in yes.asks], [3.0, 7.0])
        self.assertEqual([l.price for l in yes.bids], [0.55, 0.54])
        self.assertEqual([l.price for l in no.asks], [0.45, 0.46])


class KalshiOrderPayloadTests(unittest.TestCase):
    def test_translation_to_yes_leg(self):
        self.assertEqual(build_order_payload("T", "buy", "yes", 10, 0.52)["side"], "bid")
        self.assertEqual(build_order_payload("T", "buy", "yes", 10, 0.52)["price"], "0.5200")
        p = build_order_payload("T", "buy", "no", 10, 0.30)
        self.assertEqual((p["side"], p["price"]), ("ask", "0.7000"))
        p = build_order_payload("T", "sell", "no", 2.5, 0.30, post_only=True, exchange_index=3)
        self.assertEqual((p["side"], p["price"], p["count"], p["post_only"], p["exchange_index"]), ("bid", "0.7000", "2.5", True, 3))
        self.assertEqual(build_order_payload("T", "sell", "yes", 1, 0.52)["side"], "ask")


class PolymarketAdapterTests(unittest.TestCase):
    def setUp(self):
        events = load("polymarket_events_nfl.json")
        self.http = FakeHttp({"gamma-api.polymarket.com/events": lambda: list(events), "clob.polymarket.com/books": [dict(load("polymarket_book.json"), asset_id=json.loads(events[0]["markets"][0]["clobTokenIds"])[0])]})
        self.adapter = PolymarketAdapter(http=self.http)

    def test_fetch_nfl_moneyline_only(self):
        snap = self.adapter.fetch("nfl")
        self.assertEqual(snap.errors, [])
        self.assertEqual(list(snap.events), ["nfl:BUF|DET:2026-09-17"])
        self.assertEqual(len(snap.quotes), 2)  # spreads market skipped
        det = next(q for q in snap.quotes if q.outcome == "DET")
        buf = next(q for q in snap.quotes if q.outcome == "BUF")
        self.assertEqual((det.bid, det.ask), (0.33, 0.34))
        self.assertEqual((buf.bid, buf.ask), (0.66, 0.67))  # complement of the Lions token
        self.assertEqual(det.fee_params["feeSchedule"]["rate"], 0.05)
        self.assertEqual(det.outcome_label, "Lions")
        self.assertEqual(snap.events["nfl:BUF|DET:2026-09-17"].start_time.hour, 0)

    def test_fetch_with_books(self):
        snap = PolymarketAdapter(http=self.http, with_books=True).fetch("nfl")
        det = next(q for q in snap.quotes if q.outcome == "DET")
        self.assertIsNotNone(det.book)
        self.assertTrue(det.book.bids)

    def test_parse_clob_book_sorted(self):
        b = parse_clob_book({"bids": [{"price": "0.30", "size": "1"}, {"price": "0.33", "size": "2"}], "asks": [{"price": "0.36", "size": "1"}, {"price": "0.34", "size": "2"}]})
        self.assertEqual([l.price for l in b.bids], [0.33, 0.30])
        self.assertEqual([l.price for l in b.asks], [0.34, 0.36])


class RobinhoodAdapterTests(unittest.TestCase):
    def setUp(self):
        self.pp = load("robinhood_page_props_nfl.json")
        page = {"props": {"pageProps": self.pp}}
        html = '<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(page) + "</script></html>"
        quotes = {"status": "SUCCESS", "data": [{"status": "SUCCESS", "data": q} for q in self.pp["quotes"].values()]}
        self.http = FakeHttp({"/us/en/prediction-markets/nfl/": html, "/marketdata/event/contract/quotes/v1/": quotes})
        self.adapter = RobinhoodAdapter(http=self.http)

    def test_extract_next_data(self):
        self.assertIn("events", extract_next_data('<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"events":[]}}}</script>')["props"]["pageProps"])
        with self.assertRaises(ValueError):
            extract_next_data("<html></html>")

    def test_fetch_nfl(self):
        snap = self.adapter.fetch("nfl")
        self.assertEqual(snap.errors, [])
        self.assertIn("nfl:BUF|DET:2026-09-17", snap.events)
        self.assertIn("nfl:PHI|TEN:2026-09-20", snap.events)
        det = next(q for q in snap.quotes if q.outcome == "DET")
        self.assertEqual((det.bid, det.ask), (0.32, 0.34))
        self.assertEqual(det.fee_params["exchange"], "rothera")
        self.assertEqual(det.book_id, "rothera")
        self.assertIsNotNone(det.quote_time)
        info = snap.events["nfl:BUF|DET:2026-09-17"]
        self.assertEqual(info.venues["robinhood"]["exchange"], "rothera")
        self.assertFalse(info.in_play)
        self.assertEqual(info.start_time.isoformat(), "2026-09-18T00:15:00+00:00")
        self.assertTrue(any("quotes/v1" in c for c in self.http.calls))

    def test_kalshi_routed_contract_shares_kalshi_book(self):
        ev = json.loads(json.dumps(self.pp["events"][0]))
        for c in ev["eventContracts"].values():
            c["symbol"] = "KX" + c["symbol"]
            c["exchange"] = "EXCHANGE_SOURCE_KALSHI"
        snap = RobinhoodAdapter(http=self.http, refresh_quotes=False).fetch("nfl")  # baseline
        from arb_engine.models import VenueSnapshot
        snap2 = VenueSnapshot(venue="robinhood")
        RobinhoodAdapter(http=self.http, refresh_quotes=False).ingest(snap2, "nfl", "nfl", [{"event": ev, "contracts": list(ev["eventContracts"].values())}], self.pp["quotes"], self.pp["eventStates"])
        self.assertEqual({q.book_id for q in snap2.quotes}, {"kalshi"})
        self.assertEqual({q.fee_params["exchange"] for q in snap2.quotes}, {"kalshi"})
        self.assertEqual({q.book_id for q in snap.quotes}, {"rothera"})

    def test_progress_parsing(self):
        self.assertFalse(_in_play_from_progress("Sep 16", "EVENT_STATUS_UPCOMING"))
        self.assertFalse(_in_play_from_progress("", "EVENT_STATUS_UPCOMING"))
        self.assertTrue(_in_play_from_progress("Live", "EVENT_STATUS_UPCOMING"))
        self.assertTrue(_in_play_from_progress("Interrupted", None))
        self.assertTrue(_in_play_from_progress("Q3 4:12", None))
        self.assertFalse(_in_play_from_progress("Final", None))
        self.assertEqual(clean_label("K. Miyoshi (b. 2004)"), "K. Miyoshi")


if __name__ == "__main__":
    unittest.main()

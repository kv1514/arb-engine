"""Adapters parse recorded venue payloads offline."""

import json
import unittest

from arb_engine.models import Book
from arb_engine.venues.kalshi import KalshiAdapter, KalshiClient, build_order_payload, parse_orderbook
from arb_engine.venues.polymarket import PolymarketAdapter, parse_clob_book
from arb_engine.venues.robinhood import RobinhoodAdapter, _in_play_from_progress, clean_label, extract_next_data, tie_payout_for

from .helpers import FakeHttp, load


class KalshiAdapterTests(unittest.TestCase):
    def setUp(self):
        lines = load("kalshi_markets_nfl_lines.json")
        self.http = FakeHttp({
            "/orderbook": load("kalshi_orderbook.json"),
            "series_ticker=KXNFLGAME&": load("kalshi_markets_nfl.json"),
            "series_ticker=KXNFLSPREAD&": {"markets": lines["spreads"]},
            "series_ticker=KXNFLTOTAL&": {"markets": lines["totals"]},
            "/series/KXNFLGAME": load("kalshi_series_kxnflgame.json"),
            "/series/KXNFLSPREAD": {"series": {"ticker": "KXNFLSPREAD", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}},
            "/series/KXNFLTOTAL": {"series": {"ticker": "KXNFLTOTAL", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}},
        })
        self.adapter = KalshiAdapter(client=KalshiClient(env="prod", http=self.http))

    def test_fetch_nfl(self):
        snap = self.adapter.fetch("nfl")
        self.assertEqual(snap.errors, [])
        self.assertEqual(sorted(snap.events), ["nfl:BUF|DET:2026-09-17", "nfl:BUF|DET:2026-09-17:spread:BUF-1.5", "nfl:BUF|DET:2026-09-17:spread:DET-1.5", "nfl:BUF|DET:2026-09-17:total:49.5"])
        info = snap.events["nfl:BUF|DET:2026-09-17"]
        self.assertEqual(info.outcomes, ["BUF", "DET"])
        self.assertEqual(info.labels["DET"], "Detroit")
        self.assertEqual(info.tie_rule, "half")
        self.assertEqual(len(snap.quotes), 2 + 3 * 2)  # moneyline pair + YES/NO per line market
        det = next(q for q in snap.quotes if q.outcome == "DET")
        self.assertEqual(det.ask, 0.33)
        self.assertEqual(det.bid, 0.32)
        self.assertEqual(det.fee_params["fee_type"], "quadratic_with_maker_fees")
        self.assertEqual(det.book_id, "kalshi")

    def test_spread_and_total_lines(self):
        snap = self.adapter.fetch("nfl")
        sp = snap.events["nfl:BUF|DET:2026-09-17:spread:BUF-1.5"]
        self.assertEqual(sp.market_type, "spread")
        self.assertEqual(sp.line, 1.5)
        self.assertEqual(sp.outcomes, ["BUF-1.5", "DET+1.5"])
        self.assertEqual(sp.labels, {"BUF-1.5": "Buffalo -1.5", "DET+1.5": "Detroit +1.5"})
        self.assertEqual(sp.tie_rule, "no_push")
        cover = next(q for q in snap.quotes if q.outcome == "BUF-1.5")
        other = next(q for q in snap.quotes if q.outcome == "DET+1.5")
        self.assertEqual((cover.ask, cover.bid), (0.65, 0.63))
        self.assertEqual((other.ask, other.bid), (0.37, 0.35))  # NO side of the same market
        self.assertEqual(other.venue_market_id, "KXNFLSPREAD-26SEP17DETBUF-BUF2#no")
        self.assertEqual(other.meta["side"], "no")
        # The mirror line (Detroit -1.5) is a different event.
        self.assertIn("nfl:BUF|DET:2026-09-17:spread:DET-1.5", snap.events)
        tot = snap.events["nfl:BUF|DET:2026-09-17:total:49.5"]
        self.assertEqual((tot.market_type, tot.line, tot.outcomes), ("total", 49.5, ["over", "under"]))
        self.assertEqual(tot.title(), "DET @ BUF total 49.5 (over / under)")
        over = next(q for q in snap.quotes if q.outcome == "over" and q.event_key == tot.event_key)
        under = next(q for q in snap.quotes if q.outcome == "under" and q.event_key == tot.event_key)
        self.assertEqual((over.ask, under.ask), (0.64, 0.38))

    def test_fetch_with_books(self):
        adapter = KalshiAdapter(client=KalshiClient(env="prod", http=self.http), with_books=True)
        snap = adapter.fetch("nfl")
        q = next(x for x in snap.quotes if x.outcome == "DET")
        self.assertIsInstance(q.book, Book)
        self.assertTrue(q.book.asks)
        self.assertEqual(q.book.asks, sorted(q.book.asks, key=lambda l: l.price))
        self.assertEqual(q.ask, q.book.asks[0].price)  # top of book overrides the market summary
        no_side = next(x for x in snap.quotes if x.venue_market_id.endswith("#no"))
        self.assertIsInstance(no_side.book, Book)

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

    def test_fetch_nfl_all_market_types(self):
        snap = self.adapter.fetch("nfl")
        self.assertEqual(snap.errors, [])
        self.assertEqual(sorted(snap.events), ["nfl:BUF|DET:2026-09-17", "nfl:BUF|DET:2026-09-17:spread:BUF-1.5", "nfl:BUF|DET:2026-09-17:total:49.5"])
        self.assertEqual(len(snap.quotes), 6)
        # Spread: "Spread: Bills (-1.5)", outcomes [Bills, Lions] -> Bills cover / Lions +1.5.
        cover = next(q for q in snap.quotes if q.outcome == "BUF-1.5")
        dog = next(q for q in snap.quotes if q.outcome == "DET+1.5")
        self.assertEqual((cover.bid, cover.ask), (0.64, 0.65))
        self.assertEqual((dog.bid, dog.ask), (0.35, 0.36))
        self.assertEqual(cover.outcome_label, "Bills -1.5")
        self.assertEqual(dog.outcome_label, "Lions +1.5")
        over = next(q for q in snap.quotes if q.outcome == "over")
        self.assertEqual((over.bid, over.ask), (0.6, 0.63))
        self.assertEqual(snap.events["nfl:BUF|DET:2026-09-17:total:49.5"].title(), "DET @ BUF total 49.5 (over / under)")
        det = next(q for q in snap.quotes if q.outcome == "DET")
        buf = next(q for q in snap.quotes if q.outcome == "BUF")
        self.assertEqual((det.bid, det.ask), (0.33, 0.34))
        self.assertEqual((buf.bid, buf.ask), (0.66, 0.67))  # complement of the Lions token
        self.assertEqual(det.fee_params["feeSchedule"]["rate"], 0.05)
        self.assertEqual(det.outcome_label, "Lions")
        self.assertEqual(snap.events["nfl:BUF|DET:2026-09-17"].start_time.hour, 0)

    def test_positive_line_means_outcome0_is_the_underdog(self):
        events = load("polymarket_events_nfl.json")
        m = json.loads(json.dumps(next(x for x in events[0]["markets"] if x["sportsMarketType"] == "spreads")))
        m["question"], m["outcomes"], m["line"] = "Spread: Lions (+1.5)", json.dumps(["Lions", "Bills"]), 1.5
        from arb_engine.models import VenueSnapshot
        snap = VenueSnapshot(venue="polymarket")
        self.adapter._ingest_line_market(snap, "nfl", events[0], m, "spread")
        self.assertEqual(list(snap.events), ["nfl:BUF|DET:2026-09-17:spread:BUF-1.5"])
        lions = next(q for q in snap.quotes if q.outcome == "DET+1.5")
        self.assertEqual((lions.bid, lions.ask), (0.64, 0.65))  # outcome0's token prices now belong to the dog side

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

    def test_prices_the_refresh_did_not_return_keep_the_catalogues_age(self):
        """A refresh that fails - or silently drops contracts - must not pass the cached page's
        prices off as fresh: they keep the page's own time, so max_quote_age drops them."""
        cached_at = 1_700_000_000.0
        pp = dict(self.pp, cached_at=cached_at)
        ad = RobinhoodAdapter(http=self.http)
        ad.category_page = lambda category, use_cache=True: pp

        def boom(ids, *a, **k):
            raise RuntimeError("429 Too Many Requests")
        ad.quotes = boom
        snap = ad.fetch("nfl")
        self.assertTrue(snap.quotes)
        self.assertTrue(all(q.ts == cached_at for q in snap.quotes))
        self.assertTrue(any("quotes refresh" in e for e in snap.errors))
        # A refresh that answers for some contracts only: those are fresh, the rest are not.
        ids = [c for c in self.pp["quotes"]]
        half = set(ids[: len(ids) // 2])
        ad.quotes = lambda want, *a, **k: {i: self.pp["quotes"][i] for i in want if i in half}
        snap = ad.fetch("nfl")
        fresh = [q for q in snap.quotes if q.ts != cached_at]
        stale = [q for q in snap.quotes if q.ts == cached_at]
        self.assertTrue(fresh and stale)
        self.assertTrue(all(q.venue_market_id.split("#")[0] in half for q in fresh))
        self.assertTrue(any("not refreshed" in e for e in snap.errors))

    def test_extract_next_data(self):
        self.assertIn("events", extract_next_data('<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"events":[]}}}</script>')["props"]["pageProps"])
        with self.assertRaises(ValueError):
            extract_next_data("<html></html>")

    def test_fetch_nfl(self):
        snap = self.adapter.fetch("nfl")
        self.assertEqual(snap.errors, [])
        self.assertIn("nfl:BUF|DET:2026-09-17", snap.events)
        self.assertIn("nfl:PHI|TEN:2026-09-20", snap.events)
        self.assertIn("nfl:BUF|DET:2026-09-17:spread:BUF-1.5", snap.events)
        self.assertIn("nfl:BUF|DET:2026-09-17:spread:DET-1.5", snap.events)
        self.assertIn("nfl:BUF|DET:2026-09-17:total:49.5", snap.events)
        sp = snap.events["nfl:BUF|DET:2026-09-17:spread:BUF-1.5"]
        self.assertEqual(sp.labels["DET+1.5"], "Detroit +1.5")
        self.assertEqual(sp.venues["robinhood"]["exchange"], "rothera")
        dog = next(q for q in snap.quotes if q.outcome == "DET+1.5")
        self.assertTrue(dog.venue_market_id.endswith("#no"))
        self.assertEqual(dog.meta["side"], "no")
        self.assertIsNotNone(dog.ask)  # from no_ask_price
        cover = next(q for q in snap.quotes if q.outcome == "BUF-1.5")
        self.assertAlmostEqual(cover.ask + dog.bid, 1.0, places=6)  # NO bid mirrors YES ask
        self.assertEqual(snap.events["nfl:BUF|DET:2026-09-17:total:49.5"].line, 49.5)
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

    def test_moneyline_quotes_carry_tie_payouts_and_optional_no_side(self):
        snap = self.adapter.fetch("nfl")
        ml = [q for q in snap.quotes if q.event_key == "nfl:BUF|DET:2026-09-17"]
        self.assertEqual(len(ml), 2)  # default: today's row count (bridge/overlay unchanged)
        self.assertEqual({q.meta["side"] for q in ml}, {"yes"})
        self.assertEqual({q.meta["tie_payout"] for q in ml}, {0.0})  # Rothera YES pays nothing on a tie
        snap = self.adapter.fetch("nfl", emit_no_side=True)
        ml = {q.venue_market_id: q for q in snap.quotes if q.event_key == "nfl:BUF|DET:2026-09-17"}
        self.assertEqual(len(ml), 4)
        det = next(q for q in ml.values() if q.outcome == "DET" and q.meta["side"] == "yes")
        no_det = ml[det.venue_market_id + "#no"]
        self.assertEqual((no_det.outcome, no_det.meta["side"], no_det.meta["tie_payout"], no_det.meta["no_of"]), ("BUF", "no", 1.0, "DET"))
        self.assertEqual(no_det.outcome_label, "NO Detroit")
        self.assertEqual((no_det.ask, no_det.bid), (0.68, 0.66))      # no_ask_price / no_bid_price
        self.assertEqual(no_det.ask_size, det.bid_size)              # the NO ask is the YES bid seen from the other side
        self.assertEqual((no_det.book_id, no_det.fee_params["exchange"]), ("rothera", "rothera"))
        # Every event gets its two NO rows; line markets are untouched (already YES/NO).
        self.assertEqual(sum(1 for q in snap.quotes if q.venue_market_id.endswith("#no") and q.meta.get("no_of")), 4)
        self.assertEqual(sum(1 for q in snap.quotes if q.event_key == "nfl:BUF|DET:2026-09-17:total:49.5"), 2)

    def test_tie_payout_table(self):
        self.assertEqual((tie_payout_for("rothera", "yes"), tie_payout_for("rothera", "no")), (0.0, 1.0))
        self.assertEqual((tie_payout_for("kalshi", "yes"), tie_payout_for("kalshi", "no")), (0.5, 0.5))
        self.assertIsNone(tie_payout_for("cdna", "yes"))   # unverified -> the $0.50 default applies downstream
        self.assertIsNone(tie_payout_for(None, "yes"))

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
        self.assertEqual({q.meta["tie_payout"] for q in snap2.quotes}, {0.5})  # Kalshi's rules: $0.50 per side
        snap3 = VenueSnapshot(venue="robinhood")
        RobinhoodAdapter(http=self.http, refresh_quotes=False).ingest(snap3, "nfl", "nfl", [{"event": ev, "contracts": list(ev["eventContracts"].values())}], self.pp["quotes"], self.pp["eventStates"], emit_no_side=True)
        self.assertEqual({q.meta["tie_payout"] for q in snap3.quotes if q.meta["side"] == "no"}, {0.5})

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

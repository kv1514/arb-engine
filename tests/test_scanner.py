"""End-to-end: three recorded venues -> one merged NFL event -> fee-aware report."""

import json
import unittest

from arb_engine.scanner import analyze_event, scan
from arb_engine.matching.matcher import merge_snapshots
from arb_engine.venues.kalshi import KalshiAdapter, KalshiClient
from arb_engine.venues.polymarket import PolymarketAdapter
from arb_engine.venues.robinhood import RobinhoodAdapter

from .helpers import FakeHttp, load


def _adapters():
    kal = FakeHttp({"/markets?": load("kalshi_markets_nfl.json"), "/series/KXNFLGAME": load("kalshi_series_kxnflgame.json")})
    events = load("polymarket_events_nfl.json")
    poly = FakeHttp({"gamma-api.polymarket.com/events": lambda: list(events)})
    pp = load("robinhood_page_props_nfl.json")
    html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": pp}}) + "</script>"
    rh = FakeHttp({"/us/en/prediction-markets/nfl/": html})
    return [KalshiAdapter(client=KalshiClient(env="prod", http=kal)), PolymarketAdapter(http=poly), RobinhoodAdapter(http=rh, refresh_quotes=False)]


class ScannerTests(unittest.TestCase):
    def test_scan_merges_three_venues(self):
        res = scan("nfl", _adapters(), settings={"robinhood_gold": False})
        self.assertEqual(res.errors, {})
        keys = {e.event_key for e in res.events}
        self.assertIn("nfl:BUF|DET:2026-09-17", keys)
        ev = next(e for e in res.events if e.event_key == "nfl:BUF|DET:2026-09-17")
        self.assertEqual(ev.venues, ["kalshi", "polymarket", "robinhood"])
        self.assertEqual(ev.tie_rule, "half")
        self.assertIsNotNone(ev.margin)
        # Kalshi DET 0.33 + Polymarket BUF 0.67 = 1.00 gross -> negative after fees.
        self.assertAlmostEqual(ev.gross_sum, 1.00, places=6)
        self.assertLess(ev.margin, 0)
        det = next(o for o in ev.outcomes if o.outcome == "DET")
        self.assertEqual(det.label, "Detroit")
        self.assertAlmostEqual(det.fair, 0.33, delta=0.02)
        venues = {v.venue: v for v in det.venues}
        self.assertEqual(set(venues), {"kalshi", "polymarket", "robinhood"})
        self.assertEqual(venues["robinhood"].exchange, "rothera")
        self.assertIsNone(venues["robinhood"].mirror_of)  # Rothera is its own book
        # Robinhood all-in = 0.34 + 0.02 fees; Kalshi = 0.33 + 0.0155.
        self.assertAlmostEqual(venues["robinhood"].all_in, 0.36, places=6)
        self.assertAlmostEqual(venues["kalshi"].all_in, 0.33 + 0.0155, places=6)
        # Max-buy prices exist and maker >= taker.
        for v in det.venues:
            self.assertIsNotNone(v.max_buy_price)
            self.assertGreaterEqual(v.max_buy_maker, v.max_buy_price)
        self.assertLess(venues["kalshi"].max_buy_price, 0.33)  # no arb today
        self.assertEqual(res.arbs(), [])

    def test_kalshi_routed_robinhood_quote_is_marked_mirror(self):
        adapters = _adapters()
        snaps = [a.fetch("nfl") for a in adapters]
        rh = snaps[2]
        for q in rh.quotes:
            q.book_id = "kalshi"
            q.fee_params["exchange"] = "kalshi"
        merged = merge_snapshots(snaps)
        rep = analyze_event(merged["nfl:BUF|DET:2026-09-17"], settings={})
        det = next(o for o in rep.outcomes if o.outcome == "DET")
        rhp = next(v for v in det.venues if v.venue == "robinhood")
        self.assertEqual(rhp.mirror_of, "kalshi")
        # The arb legs must never pair the mirror with its own book.
        self.assertNotIn("robinhood", {l["venue"] for l in rep.arb["legs"]})

    def test_stale_snapshot_excluded(self):
        adapters = _adapters()
        snaps = [a.fetch("nfl") for a in adapters]
        for q in snaps[0].quotes:
            q.ts = 1.0  # ancient
        merged = merge_snapshots(snaps)
        rep = analyze_event(merged["nfl:BUF|DET:2026-09-17"], settings={}, max_quote_age=60, now=1000.0)
        self.assertIn("stale-quote", rep.flags)
        self.assertNotIn("kalshi", {l["venue"] for l in rep.arb["legs"]})


if __name__ == "__main__":
    unittest.main()

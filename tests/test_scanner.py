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
    lines = load("kalshi_markets_nfl_lines.json")
    kal = FakeHttp({"series_ticker=KXNFLGAME&": load("kalshi_markets_nfl.json"), "series_ticker=KXNFLSPREAD&": {"markets": lines["spreads"]}, "series_ticker=KXNFLTOTAL&": {"markets": lines["totals"]}, "/series/KXNFLGAME": load("kalshi_series_kxnflgame.json"), "/series/": {"series": {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}}})
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

    def test_spread_and_total_merge_across_three_venues(self):
        res = scan("nfl", _adapters(), settings={})
        sp = next(e for e in res.events if e.event_key == "nfl:BUF|DET:2026-09-17:spread:BUF-1.5")
        self.assertEqual(sp.venues, ["kalshi", "polymarket", "robinhood"])
        self.assertEqual((sp.market_type, sp.line, sp.tie_rule), ("spread", 1.5, "no_push"))
        self.assertEqual(sp.title, "Buffalo -1.5 vs Detroit +1.5")
        cover = next(o for o in sp.outcomes if o.outcome == "BUF-1.5")
        self.assertEqual({v.venue for v in cover.venues}, {"kalshi", "polymarket", "robinhood"})
        tot = next(e for e in res.events if e.event_key == "nfl:BUF|DET:2026-09-17:total:49.5")
        self.assertEqual(tot.venues, ["kalshi", "polymarket", "robinhood"])
        self.assertEqual(tot.title, "DET @ BUF total 49.5 (over / under)")
        under = next(o for o in tot.outcomes if o.outcome == "under")
        kal = next(v for v in under.venues if v.venue == "kalshi")
        self.assertEqual(kal.ask, 0.38)  # NO side of the over market
        # Market-type filter.
        only_ml = scan("nfl", _adapters(), settings={}, market_types={"moneyline"})
        self.assertTrue(all(e.market_type == "moneyline" for e in only_ml.events))
        self.assertEqual(len(only_ml.events), 2)

    def test_thin_arb_is_not_fillable(self):
        adapters = _adapters()
        snaps = [a.fetch("nfl") for a in adapters]
        # Force a fee-beating price on the Robinhood under with tiny size.
        for q in snaps[2].quotes:
            if q.outcome == "under":
                q.ask, q.ask_size = 0.30, 0.5
        merged = merge_snapshots(snaps)
        rep = analyze_event(merged["nfl:BUF|DET:2026-09-17:total:49.5"], settings={})
        self.assertGreater(rep.margin, 0)
        self.assertFalse(rep.fillable)
        self.assertIn("thin", rep.flags)
        for q in snaps[2].quotes:
            if q.outcome == "under":
                q.ask_size = 50
        merged = merge_snapshots(snaps)
        rep = analyze_event(merged["nfl:BUF|DET:2026-09-17:total:49.5"], settings={})
        self.assertTrue(rep.fillable)
        self.assertEqual(rep.sized_arb["contracts"], 50)

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




class SettlementTests(unittest.TestCase):
    def test_tennis_walkover_rules_differ_between_kalshi_and_polymarket(self):
        from arb_engine.matching.matcher import MergedEvent
        from arb_engine.models import EventInfo, OutcomeQuote
        from arb_engine.scanner import settlement_mismatches
        from arb_engine.venues.kalshi import TENNIS_SETTLEMENT as K
        from arb_engine.venues.polymarket import TENNIS_SETTLEMENT as P

        key = "tennis:a|b:2026-09-19"
        info = EventInfo(event_key=key, sport="tennis", market_type="moneyline", outcomes=["a", "b"], labels={"a": "A", "b": "B"}, tie_rule="void", venues={"kalshi": {"settlement": K}, "polymarket": {"settlement": P}, "robinhood": {"settlement": dict(K)}})
        q = lambda v, o, ask: OutcomeQuote(v, f"{v}-{o}", key, o, ask=ask, bid=ask - 0.02, fee_params={})  # noqa: E731
        me = MergedEvent(key, info, {"kalshi": [q("kalshi", "a", 0.6), q("kalshi", "b", 0.42)], "polymarket": [q("polymarket", "a", 0.58), q("polymarket", "b", 0.44)]})
        flags = settlement_mismatches(info, me.quotes_by_venue)
        self.assertEqual(flags, ["settlement-mismatch:cancelled", "settlement-mismatch:postponed", "settlement-mismatch:walkover"])
        self.assertEqual(K["retirement"], P["retirement"])   # retirements after the first ball agree
        # Kalshi + its Robinhood mirror only: same rules, no flag.
        me2 = MergedEvent(key, info, {"kalshi": me.quotes_by_venue["kalshi"], "robinhood": [q("robinhood", "a", 0.6), q("robinhood", "b", 0.42)]})
        self.assertEqual(settlement_mismatches(info, me2.quotes_by_venue), [])
        report = analyze_event(me, {}, contracts=100)
        self.assertIn("settlement-mismatch:walkover", report.flags)

if __name__ == "__main__":
    unittest.main()

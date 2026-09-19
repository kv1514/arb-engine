"""EventAnalyzer (what the overlay/bridge computes) against recorded Robinhood pages."""

import json
import unittest

from arb_engine.eventlookup import EventAnalyzer, parse_symbol, polymarket_nfl_slugs
from arb_engine.venues.kalshi import KalshiClient
from arb_engine.venues.polymarket import PolymarketAdapter
from arb_engine.venues.robinhood import RobinhoodAdapter

from .helpers import FakeHttp, load_text, load


def _analyzer_for_totals():
    rh = FakeHttp({"/prediction-markets/nfl/events/september-20-carolina-vs-atlanta-totals-sep-20-2026/": load_text("ext/rh_totals_page.html"), "/marketdata/event/contract/quotes/v1/": load("ext/rh_totals_quotes.json")})
    kal = FakeHttp({"/markets?event_ticker=KXNFLTOTAL-26SEP20CARATL": load("ext/kalshi_event_KXNFLTOTAL-26SEP20CARATL.json"), "/series/KXNFLTOTAL": {"series": {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}}})
    pm_event = load("ext/pm_event_nfl-car-atl-2026-09-20.json")
    pm = FakeHttp({"/events?slug=nfl-car-atl-2026-09-20": pm_event, "/events?slug=": []})
    return EventAnalyzer(robinhood=RobinhoodAdapter(http=rh), kalshi=KalshiClient(env="prod", http=kal), polymarket=PolymarketAdapter(http=pm))


class SymbolTests(unittest.TestCase):
    def test_parse_symbol(self):
        p = parse_symbol("NFLGAME-26SEP20PHITEN-PHI")
        self.assertEqual((p["family"], p["date"], p["teams"], p["kalshi_ticker"], p["routed"]), ("NFLGAME", "2026-09-20", ["PHI", "TEN"], "KXNFLGAME-26SEP20PHITEN-PHI", "other"))
        p = parse_symbol("KXWTAMATCH-26SEP14YOUCHA-YOU")
        self.assertEqual((p["teams"], p["routed"], p["kalshi_ticker"]), (["YOU", "CHA"], "kalshi", "KXWTAMATCH-26SEP14YOUCHA-YOU"))
        self.assertEqual(parse_symbol("NFLTOTAL-26SEP20CARATL-65")["pair"], "CARATL")
        self.assertIsNone(parse_symbol("garbage"))

    def test_polymarket_slugs(self):
        self.assertEqual(polymarket_nfl_slugs(["DET", "BUF"], "2026-09-17"), ["nfl-det-buf-2026-09-17", "nfl-buf-det-2026-09-17", "nfl-det-buf-2026-09-18", "nfl-buf-det-2026-09-18"])


class TotalsPageTests(unittest.TestCase):
    def test_lines_analysis(self):
        an = _analyzer_for_totals()
        res = an.analyze_url("https://robinhood.com/us/en/prediction-markets/nfl/events/september-20-carolina-vs-atlanta-totals-sep-20-2026/", settings={})
        self.assertTrue(res["ok"])
        self.assertEqual(res["event"]["market_type"], "total")
        self.assertEqual(res["event"]["game"], "CAR @ ATL")
        a = res["analysis"]
        self.assertEqual(a["market_type"], "total")
        lines = {l["line"]: l for l in a["lines"]}
        self.assertEqual(set(lines), {17.5, 44.5, 65.5})
        # 65.5: Robinhood over 0.04 (5000) + Kalshi under 0.93 (200) -> fillable arb of 200 contracts.
        l65 = lines[65.5]
        self.assertEqual(l65.get("venues"), ["kalshi", "robinhood"])
        self.assertGreater(l65["margin"], 0.01)
        self.assertTrue(l65["fillable"])
        self.assertEqual(l65["sized_arb"]["contracts"], 200)
        legs = {(x["venue"], x["outcome"]) for x in l65["arb"]["legs"]}
        self.assertEqual(legs, {("robinhood", "over"), ("kalshi", "under")})
        # 44.5 is on all three venues and is not an arb.
        l44 = lines[44.5]
        self.assertEqual(l44["venues"], ["kalshi", "polymarket", "robinhood"])
        self.assertLess(l44["margin"], 0)
        over = next(o for o in l44["outcomes"] if o["outcome"] == "over")
        pm = next(v for v in over["venues"] if v["venue"] == "polymarket")
        self.assertEqual(pm["ask"], 0.47)
        # 17.5 exists only on Robinhood (Kalshi lists no such line) and has no YES ask.
        l17 = lines[17.5]
        self.assertEqual(l17["venues"], ["robinhood"])
        self.assertIsNone(l17["margin"])
        self.assertTrue(any("no market for lines" in e and "17.5" in e for e in a["errors"]))
        # Arbs sort first.
        self.assertEqual(a["lines"][0]["line"], 65.5)
        self.assertEqual(a["lines"][0]["start_time"], "2026-09-20T17:00:00+00:00")




class PageCacheTests(unittest.TestCase):
    def test_event_page_is_cached_between_polls(self):
        an = _analyzer_for_totals()
        calls = []
        real = an.rh.event_page
        an.rh.event_page = lambda category, slug: (calls.append(slug), real(category, slug))[1]
        url = "https://robinhood.com/us/en/prediction-markets/nfl/events/september-20-carolina-vs-atlanta-totals-sep-20-2026/"
        an.analyze_url(url, settings={})
        an.analyze_url(url, settings={})
        self.assertEqual(len(calls), 1)      # one page fetch for two polls
        an.page_ttl = 0.0
        an.analyze_url(url, settings={})
        self.assertEqual(len(calls), 2)      # expired -> refetched
if __name__ == "__main__":
    unittest.main()

"""Bridge helpers that do not need a socket: /inplay reuses the scan /analyze just did."""

import unittest
from types import SimpleNamespace

from arb_engine.bridge import DEFAULT_INPLAY_MAX_AGE, recent_event
from arb_engine.eventlookup import EventAnalyzer
from arb_engine.venues.kalshi import KalshiClient
from arb_engine.venues.polymarket import PolymarketAdapter
from arb_engine.venues.robinhood import RobinhoodAdapter

from .helpers import FakeHttp, load, load_text

URL = "https://robinhood.com/us/en/prediction-markets/nfl/events/september-20-philadelphia-vs-tennessee-sep-20-2026/"


def _analyzer_for_game():
    rh = FakeHttp({"/prediction-markets/nfl/events/": load_text("ext/rh_event_page.html"), "/marketdata/event/contract/quotes/v1/": load("ext/rh_quotes.json")})
    kal = FakeHttp({
        "/markets/KXNFLGAME-26SEP20PHITEN-PHI": load("ext/kalshi_market_KXNFLGAME-26SEP20PHITEN-PHI.json"),
        "/markets/KXNFLGAME-26SEP20PHITEN-TEN": load("ext/kalshi_market_KXNFLGAME-26SEP20PHITEN-TEN.json"),
        "/series/KXNFLGAME": load("ext/kalshi_series_KXNFLGAME.json"),
    })
    pm = FakeHttp({"slug=nfl-phi-ten-2026-09-20": load("ext/pm_market_nfl-phi-ten-2026-09-20.json"), "slug=": []})
    return EventAnalyzer(robinhood=RobinhoodAdapter(http=rh), kalshi=KalshiClient(env="prod", http=kal), polymarket=PolymarketAdapter(http=pm)), (rh, kal, pm)


class RecentEventTests(unittest.TestCase):
    def test_reuses_fresh_analysis_for_the_same_url(self):
        me = object()
        an = SimpleNamespace(last_event=me, last_url=URL, last_analyzed_at=1000.0)
        self.assertIs(recent_event(an, URL, now=1005.0), me)
        self.assertIs(recent_event(an, URL, max_age=DEFAULT_INPLAY_MAX_AGE, now=1000.0 + DEFAULT_INPLAY_MAX_AGE), me)
        self.assertIsNone(recent_event(an, URL, now=1000.0 + DEFAULT_INPLAY_MAX_AGE + 0.01))
        self.assertIsNone(recent_event(an, URL + "x", now=1001.0))
        self.assertIsNone(recent_event(SimpleNamespace(last_event=None, last_url=URL, last_analyzed_at=1000.0), URL, now=1001.0))
        self.assertIsNone(recent_event(SimpleNamespace(), URL, now=1001.0))

    def test_analyze_url_stamps_url_and_time(self):
        an, (rh, kal, pm) = _analyzer_for_game()
        self.assertIsNone(recent_event(an, URL))
        res = an.analyze_url(URL, settings={})
        self.assertTrue(res["ok"], res)
        self.assertNotIn("lines", res["analysis"])
        self.assertIsNotNone(an.last_event)
        self.assertEqual(an.last_url, URL)
        self.assertGreater(an.last_analyzed_at, 0)
        n_rh, n_k, n_pm = len(rh.calls), len(kal.calls), len(pm.calls)
        # A second /inplay-style lookup within max_age reuses the scan: no venue calls.
        self.assertIs(recent_event(an, URL, now=an.last_analyzed_at + 2.0), an.last_event)
        self.assertEqual((len(rh.calls), len(kal.calls), len(pm.calls)), (n_rh, n_k, n_pm))
        self.assertEqual(an.last_event.event_key, "nfl:PHI|TEN:2026-09-20")


if __name__ == "__main__":
    unittest.main()

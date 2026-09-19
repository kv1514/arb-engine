import unittest
from datetime import datetime, timedelta, timezone

from arb_engine.matching.matcher import merge_snapshots
from arb_engine.strategy.live import LiveSlate, format_tick
from arb_engine.venues.espn import GameState

from .test_scanner import _adapters


class FakeFeed:
    def __init__(self, games):
        self._games = games
        self.enriched = 0

    def games(self, date=None):
        return list(self._games)

    def enrich(self, g):
        self.enriched += 1
        g.espn_home_wp = 0.61
        g.enriched = True
        return g


def _moneyline_key(adapters):
    merged = merge_snapshots([ad.fetch("nfl") for ad in adapters])
    for k, me in merged.items():
        if me.info.market_type == "moneyline" and len(me.quotes_by_venue) >= 2:
            return k, me
    raise AssertionError("no two-venue moneyline in fixtures")


class LiveSlateTests(unittest.TestCase):
    def test_tick_prices_live_games_and_reports_missing(self):
        adapters = _adapters()
        key, me = _moneyline_key(adapters)
        away, home = me.info.outcomes[0], me.info.outcomes[1]
        now = 1_800_000_000.0
        live = GameState(event_id="1", home=home, away=away, home_score=14, away_score=10, status="live", period=3, clock_seconds_remaining_in_period=600, game_seconds_remaining=1500, possession="home", down=2, distance=7, yardline_100=45, home_timeouts=3, away_timeouts=2, event_key=key)
        soon = GameState(event_id="2", home="ZZZ", away="YYY", status="pre", start_time=datetime.fromtimestamp(now + 1800, tz=timezone.utc), event_key="nfl:YYY|ZZZ:2026-09-20")
        later = GameState(event_id="3", home="QQQ", away="PPP", status="pre", start_time=datetime.fromtimestamp(now + 5 * 3600, tz=timezone.utc), event_key="nfl:PPP|QQQ:2026-09-20")
        done = GameState(event_id="4", home="A", away="B", status="final", event_key="nfl:A|B:2026-09-13")
        feed = FakeFeed([live, soon, later, done])
        slate = LiveSlate(adapters, feed=feed, settings={}, pre_hours=1.0, steal_edge=0.03)
        tick = slate.tick(now)
        self.assertEqual(tick.games, 2)                       # live + the one starting within 1h
        self.assertEqual(len(tick.views), 1)
        self.assertEqual(len(tick.missing), 1)                # the pre game has no venue quotes in fixtures
        v = tick.views[0]
        self.assertTrue(v.live)
        self.assertIn("Q3", v.game_line)
        self.assertEqual(feed.enriched, 1)
        for sv in v.sides:
            self.assertIsNotNone(sv.fair)
            self.assertIsNotNone(sv.model_p)
            self.assertAlmostEqual(sum(s.fair for s in v.sides), 1.0, places=6)
        self.assertEqual(v.sides[0].espn_p, 0.61 if v.sides[0].outcome == home else 0.39)
        # Second tick within the refresh window reuses the summary.
        slate.tick(now + 5)
        self.assertEqual(feed.enriched, 1)
        slate.tick(now + 40)
        self.assertEqual(feed.enriched, 2)
        text = format_tick(tick)
        self.assertIn("LIVE", text)
        self.assertIn("no quotes:", text)

    def test_no_games(self):
        slate = LiveSlate(_adapters(), feed=FakeFeed([]), settings={})
        tick = slate.tick(1_800_000_000.0)
        self.assertEqual((tick.games, tick.views), (0, []))
        self.assertIn("no live games", format_tick(tick))


if __name__ == "__main__":
    unittest.main()

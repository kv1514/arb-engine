"""NBA / NHL team tables and plumbing (season not started: no venue fixtures yet)."""

import unittest

from arb_engine.matching.normalize import team_event_key
from arb_engine.matching.teams import TEAM_SPORTS, team_code, team_name
from arb_engine.strategy.inplay import model_home_wp
from arb_engine.venues.espn import ESPNClient, GameState

from .helpers import FakeHttp


class TableTests(unittest.TestCase):
    def test_nhl_codes(self):
        for spelling, code in (("Vegas", "VGK"), ("Golden Knights", "VGK"), ("VGK", "VGK"), ("Los Angeles", "LA"), ("Kings", "LA"), ("Utah", "UTAH"), ("Utah Mammoth", "UTAH"), ("UTA", "UTAH"), ("Tampa Bay", "TB"), ("New York Rangers", "NYR"), ("NY Islanders", "NYI"), ("Montreal", "MTL")):
            self.assertEqual(team_code("nhl", spelling), code, spelling)
        self.assertEqual(team_name("nhl", "VGK"), "Vegas")
        self.assertEqual(team_event_key("nhl", ["LA", "UTA"], "2026-09-22"), "nhl:LA|UTA:2026-09-22")

    def test_nba_codes(self):
        for spelling, code in (("Golden State", "GS"), ("Warriors", "GS"), ("GSW", "GS"), ("San Antonio", "SA"), ("Oklahoma City", "OKC"), ("Philadelphia", "PHI"), ("New York", "NY"), ("Knicks", "NY"), ("LA Lakers", "LAL"), ("Los Angeles Clippers", "LAC")):
            self.assertEqual(team_code("nba", spelling), code, spelling)
        self.assertIn("nba", TEAM_SPORTS)

    def test_shared_alias_resolves_to_nothing(self):
        from arb_engine.matching import teams as T

        T._TABLES["toy"] = {"A": {"name": "Foo", "aliases": ["Same", "Foo Town"]}, "B": {"name": "Bar", "aliases": ["Same"]}}
        T._SPORT_INDEX.pop("toy", None)
        try:
            self.assertIsNone(team_code("toy", "Same"))
            self.assertEqual(team_code("toy", "Foo Town"), "A")
        finally:
            T._TABLES.pop("toy", None)
            T._SPORT_INDEX.pop("toy", None)
        self.assertIn("nhl", TEAM_SPORTS)

    def test_espn_clients_and_model_gate(self):
        self.assertIn("basketball/nba", ESPNClient(http=FakeHttp({}), sport="nba").base_url)
        self.assertIn("hockey/nhl", ESPNClient(http=FakeHttp({}), sport="nhl").base_url)
        gs = GameState(event_id="1", home="VGK", away="SJ", status="live", period=2, game_seconds_remaining=1800, sport="nhl")
        self.assertIsNone(model_home_wp(gs))       # football-only model
        gs.sport = "nfl"
        self.assertIsNotNone(model_home_wp(gs))


if __name__ == "__main__":
    unittest.main()

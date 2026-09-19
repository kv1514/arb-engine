"""NBA / NHL team tables and plumbing (season not started: no venue fixtures yet)."""

import unittest

from arb_engine.matching.normalize import team_event_key
from arb_engine.matching.teams import TEAM_SPORTS, team_code, team_name
from arb_engine.strategy.inplay import model_home_wp
from arb_engine.venues.espn import ESPNClient, GameState, parse_scoreboard_event, period_clock_to_gsr

from .helpers import FakeHttp


def _event(sport: str, home: str, away: str, period: int, clock: str, home_id: str = "1", away_id: str = "2", fmt: dict | None = None) -> dict:
    """Minimal ESPN scoreboard event for a live NBA / NHL game (no venue fixtures yet: season not started)."""
    comp = {
        "date": "2026-10-22T23:00Z",
        "status": {"displayClock": clock, "period": period, "type": {"name": "STATUS_IN_PROGRESS", "state": "in", "completed": False}},
        "competitors": [
            {"id": home_id, "homeAway": "home", "score": "3", "team": {"id": home_id, "abbreviation": home}},
            {"id": away_id, "homeAway": "away", "score": "2", "team": {"id": away_id, "abbreviation": away}},
        ],
    }
    if fmt:
        comp["format"] = fmt
    return {"id": "9", "date": comp["date"], "competitions": [comp], "status": comp["status"]}


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


class ClockTests(unittest.TestCase):
    """The per-sport clock: NBA 4 x 12 min + 5 min OT, NHL 3 x 20 min + 5 min OT (20 in the playoffs)."""

    def test_nhl_scoreboard_clock(self):
        g = parse_scoreboard_event(_event("nhl", "VGK", "SJ", 2, "12:30"), "nhl")
        self.assertEqual((g.sport, g.status, g.period), ("nhl", "live", 2))
        self.assertEqual(g.game_seconds_remaining, 1200 + 750)
        self.assertEqual((g.regulation_period_seconds, g.ot_seconds), (1200, 300))
        self.assertFalse(g.overtime)
        ot = parse_scoreboard_event(_event("nhl", "VGK", "SJ", 4, "3:10"), "nhl")
        self.assertEqual(ot.game_seconds_remaining, 190)
        self.assertTrue(ot.overtime)
        self.assertFalse(ot.overtime_sentinel)
        playoff = parse_scoreboard_event(_event("nhl", "VGK", "SJ", 4, "17:00", fmt={"overtime": {"clock": 1200.0}}), "nhl")
        self.assertEqual((playoff.game_seconds_remaining, playoff.ot_seconds), (1020, 1200))

    def test_nba_scoreboard_clock(self):
        g = parse_scoreboard_event(_event("nba", "GS", "LAL", 3, "5:00"), "nba")
        self.assertEqual(g.game_seconds_remaining, 720 + 300)
        self.assertEqual(g.regulation_period_seconds, 720)
        ot2 = parse_scoreboard_event(_event("nba", "GS", "LAL", 6, "1:00"), "nba")
        self.assertEqual(ot2.game_seconds_remaining, 60)
        self.assertTrue(ot2.overtime)
        self.assertEqual(period_clock_to_gsr("nba", 0, None), (2880, False))
        self.assertEqual(period_clock_to_gsr("nhl", 0, None), (3600, False))

    def test_pre_game_full_regulation(self):
        ev = _event("nba", "GS", "LAL", 0, "0:00")
        ev["status"]["type"] = ev["competitions"][0]["status"]["type"] = {"name": "STATUS_SCHEDULED", "state": "pre", "completed": False}
        self.assertEqual(parse_scoreboard_event(ev, "nba").game_seconds_remaining, 2880)
        ev = _event("nhl", "VGK", "SJ", 0, "0:00")
        ev["status"]["type"] = ev["competitions"][0]["status"]["type"] = {"name": "STATUS_SCHEDULED", "state": "pre", "completed": False}
        self.assertEqual(parse_scoreboard_event(ev, "nhl").game_seconds_remaining, 3600)


if __name__ == "__main__":
    unittest.main()

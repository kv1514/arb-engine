"""ESPN game-state feed parses recorded scoreboard / summary payloads offline."""

import copy
import unittest
from datetime import datetime, timezone

from arb_engine.venues.espn import (
    ESPNClient,
    ESPNFeed,
    GameState,
    apply_summary,
    game_seconds_remaining,
    live_games,
    map_status,
    normalize_home_spread,
    parse_clock,
    parse_scoreboard_event,
    yardline_100_from,
)

from .helpers import FakeHttp, load


def _feed(scoreboard=None, summary=None) -> tuple[ESPNFeed, FakeHttp]:
    http = FakeHttp({
        "/summary?event=401872932": summary or load("espn/summary_401872932.json"),
        "/scoreboard": scoreboard or load("espn/scoreboard.json"),
    })
    return ESPNFeed(ESPNClient(http=http)), http


class ScoreboardTests(unittest.TestCase):
    def setUp(self):
        self.feed, self.http = _feed()
        self.games = self.feed.games()
        self.by_id = {g.event_id: g for g in self.games}

    def test_one_call_three_games(self):
        self.assertEqual(len(self.http.calls), 1)
        self.assertTrue(self.http.calls[0].endswith("/scoreboard"))
        self.assertEqual(sorted(self.by_id), ["401872932", "401872937", "401872939"])

    def test_final_game(self):
        g = self.by_id["401872932"]
        self.assertEqual((g.away, g.home), ("DET", "BUF"))
        self.assertEqual((g.away_score, g.home_score), (31, 41))
        self.assertEqual(g.status, "final")
        self.assertEqual(g.status_name, "STATUS_FINAL")
        self.assertEqual(g.period, 4)
        self.assertEqual(g.clock_seconds_remaining_in_period, 0)
        self.assertEqual(g.game_seconds_remaining, 0)
        self.assertEqual(g.score_diff_home, 10)
        self.assertIsNone(g.possession)
        self.assertIsNone(g.down)
        self.assertIsNone(g.yardline_100)
        self.assertIsNone(g.vegas_spread_home)  # ESPN drops odds from the scoreboard once a game is final
        self.assertEqual(g.start_time, datetime(2026, 9, 18, 0, 15, tzinfo=timezone.utc))
        self.assertFalse(g.enriched)

    def test_event_key_uses_eastern_date(self):
        # Thursday night 00:15 UTC kickoff is Sept 17 in Eastern time, the date the venues use.
        self.assertEqual(self.by_id["401872932"].event_key, "nfl:BUF|DET:2026-09-17")
        self.assertEqual(self.by_id["401872937"].event_key, "nfl:CHI|MIN:2026-09-20")
        self.assertEqual(self.by_id["401872939"].event_key, "nfl:PHI|TEN:2026-09-20")

    def test_scheduled_game(self):
        g = self.by_id["401872937"]
        self.assertEqual((g.away, g.home), ("MIN", "CHI"))
        self.assertEqual(g.status, "pre")
        self.assertEqual(g.period, 0)
        self.assertEqual(g.game_seconds_remaining, 3600)
        self.assertEqual((g.home_score, g.away_score), (0, 0))
        self.assertEqual(g.vegas_spread_home, -4.5)  # 'CHI -4.5', Chicago at home
        self.assertEqual(g.vegas_total, 48.5)
        self.assertEqual(g.odds_provider, "Draft Kings")
        self.assertIsNone(g.espn_home_wp)
        self.assertEqual(g.espn_wp_series, [])

    def test_away_favourite_spread_is_positive_for_home(self):
        g = self.by_id["401872939"]  # 'PHI -7' with Tennessee at home
        self.assertEqual((g.away, g.home), ("PHI", "TEN"))
        self.assertEqual(g.vegas_spread_home, 7.0)
        self.assertEqual(g.vegas_total, 39.5)

    def test_find_and_robinhood_lookup(self):
        g = self.feed.find("nfl:BUF|DET:2026-09-17")
        self.assertIsNotNone(g)
        self.assertEqual(g.event_id, "401872932")
        self.assertIsNone(self.feed.find("nfl:BUF|DET:2026-09-24"))
        # Robinhood spellings, with and without the date; either orientation.
        self.assertEqual(self.feed.state_for_robinhood_teams("Detroit", "Buffalo", "2026-09-17").event_id, "401872932")
        self.assertEqual(self.feed.state_for_robinhood_teams("Bills", "Lions").event_id, "401872932")
        self.assertIsNone(self.feed.state_for_robinhood_teams("Detroit", "Buffalo", "2026-09-24"))
        self.assertIsNone(self.feed.state_for_robinhood_teams("Over 49.5", "Buffalo"))
        self.assertTrue(any("dates=20260917" in c for c in self.http.calls))

    def test_no_live_games_in_fixture(self):
        self.assertEqual(live_games(self.games), [])

    def test_as_dict_is_json_friendly(self):
        d = self.by_id["401872932"].as_dict()
        self.assertEqual(d["start_time"], "2026-09-18T00:15:00+00:00")
        self.assertEqual(d["score_diff_home"], 10)
        self.assertEqual(d["event_key"], "nfl:BUF|DET:2026-09-17")


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.feed, self.http = _feed()
        self.game = self.feed.find("nfl:BUF|DET:2026-09-17")

    def test_enrich_final_game(self):
        g = self.feed.enrich(self.game)
        self.assertTrue(g.enriched)
        self.assertTrue(self.http.calls[-1].endswith("/summary?event=401872932"))
        self.assertEqual(len(g.espn_wp_series), 190)
        self.assertEqual(g.espn_wp_series[0], {"play_id": "4018729321", "home_wp": 0.668, "tie": 0.0})
        self.assertEqual(g.espn_home_wp, 1.0)  # final: home won
        self.assertEqual(g.status, "final")
        self.assertEqual((g.home_score, g.away_score), (41, 31))
        self.assertEqual(g.game_seconds_remaining, 0)
        # Closing DraftKings line from pickcenter (the scoreboard had dropped it).
        self.assertEqual(g.vegas_spread_home, -5.5)
        self.assertEqual(g.vegas_total, 54.5)
        self.assertEqual(g.odds_provider, "Draft Kings")
        self.assertEqual(g.last_play_text, "END GAME")
        # Final games do not get down/distance from the last play.
        self.assertIsNone(g.down)
        self.assertIsNone(g.yardline_100)

    def test_second_half_kickoff_recipient_from_first_drive(self):
        from arb_engine.venues.espn import receive_2h_ko_home_from
        summary = load("espn/summary_401872932.json")
        # DET (away, id 8) had the game's first drive -> received the opening kickoff, so
        # BUF (home) receives the second-half kickoff.
        self.assertEqual(summary["drives"]["previous"][0]["team"]["id"], "8")
        self.assertIs(receive_2h_ko_home_from(summary, "2", "8"), True)
        self.assertIs(receive_2h_ko_home_from(summary, "8", "2"), False)
        self.assertIsNone(receive_2h_ko_home_from({"drives": {}}, "2", "8"))
        self.assertIsNone(receive_2h_ko_home_from(summary, None, None))
        g = apply_summary(copy.deepcopy(self.game), summary)
        self.assertIs(g.receive_2h_ko_home, True)
        self.assertIs(g.as_dict()["receive_2h_ko_home"], True)
        # Header competitor ids fill the team ids when the scoreboard did not.
        bare = copy.deepcopy(self.game)
        bare.home_team_id = bare.away_team_id = None
        g2 = apply_summary(bare, summary)
        self.assertEqual((g2.home_team_id, g2.away_team_id), ("2", "8"))
        self.assertIs(g2.receive_2h_ko_home, True)

    def test_summary_last_play_fills_live_state(self):
        summary = load("espn/summary_401872932.json")
        summary["header"]["competitions"][0]["status"] = {"type": {"name": "STATUS_IN_PROGRESS", "state": "in", "completed": False}, "period": 4, "displayClock": "0:13"}
        g = apply_summary(copy.deepcopy(self.game), summary)
        self.assertEqual(g.status, "live")
        self.assertEqual(g.game_seconds_remaining, 13)
        # Last recorded play ended with BUF (home) in possession 23 yards from the DET goal line.
        self.assertEqual(g.possession, "home")
        self.assertEqual(g.yardline_100, 23)
        self.assertIsNone(g.down)  # end-of-game play carries down 0


class LiveSituationTests(unittest.TestCase):
    """Hand-built from the scoreboard schema: ``situation`` appears only while a game is live."""

    def _live_event(self, **situation):
        ev = copy.deepcopy(next(e for e in load("espn/scoreboard.json")["events"] if e["id"] == "401872932"))
        comp = ev["competitions"][0]
        status = {"clock": 754.0, "displayClock": "12:34", "period": 3, "type": {"id": "2", "name": "STATUS_IN_PROGRESS", "state": "in", "completed": False, "description": "In Progress", "detail": "12:34 - 3rd Quarter", "shortDetail": "12:34 - 3rd"}}
        ev["status"] = comp["status"] = status
        for c in comp["competitors"]:
            c["score"] = "21" if c["homeAway"] == "home" else "17"
        sit = {
            "lastPlay": {"id": "40187293230", "type": {"id": "5", "text": "Rush"}, "text": "J.Gibbs right tackle to BUF 34 for 6 yards (C.Bishop).", "team": {"id": "8"}, "probability": {"tiePercentage": 0.0, "homeWinPercentage": 0.61, "awayWinPercentage": 0.39, "secondsLeft": 0}},
            "down": 2, "yardLine": 34, "distance": 4, "downDistanceText": "2nd & 4 at BUF 34", "shortDownDistanceText": "2nd & 4", "possessionText": "BUF 34",
            "isRedZone": False, "homeTimeouts": 3, "awayTimeouts": 2, "possession": "8",
        }
        sit.update(situation)
        comp["situation"] = sit
        return ev

    def test_away_possession_on_opponent_side(self):
        g = parse_scoreboard_event(self._live_event())
        self.assertEqual(g.status, "live")
        self.assertEqual(g.period, 3)
        self.assertEqual(g.clock_seconds_remaining_in_period, 754)
        self.assertEqual(g.game_seconds_remaining, 900 + 754)
        self.assertEqual((g.home_score, g.away_score), (21, 17))
        self.assertEqual(g.possession, "away")  # team id 8 = DET
        self.assertEqual((g.down, g.distance), (2, 4))
        self.assertEqual(g.yardline_100, 34)  # DET at the BUF 34 -> 34 yards to go
        self.assertEqual((g.home_timeouts, g.away_timeouts), (3, 2))
        self.assertEqual(g.espn_home_wp, 0.61)  # lastPlay.probability
        self.assertIs(g.is_red_zone, False)
        self.assertTrue(g.last_play_text.startswith("J.Gibbs"))

    def test_home_possession_on_own_side(self):
        g = parse_scoreboard_event(self._live_event(possession="2", yardLine=25, possessionText="BUF 25", downDistanceText="1st & 10 at BUF 25", down=1, distance=10))
        self.assertEqual(g.possession, "home")
        self.assertEqual(g.yardline_100, 75)  # BUF on its own 25 -> 75 yards to go

    def test_possession_text_fallback_when_yardline_missing(self):
        g = parse_scoreboard_event(self._live_event(yardLine=None, possessionText="DET 34"))
        self.assertEqual(g.yardline_100, 66)  # DET (away) on its own 34
        g = parse_scoreboard_event(self._live_event(yardLine=None, possessionText="50"))
        self.assertEqual(g.yardline_100, 50)
        g = parse_scoreboard_event(self._live_event(yardLine=None, possessionText=""))
        self.assertIsNone(g.yardline_100)

    def test_unknown_possession_leaves_field_position_none(self):
        g = parse_scoreboard_event(self._live_event(possession="999"))
        self.assertIsNone(g.possession)
        self.assertIsNone(g.yardline_100)
        self.assertEqual(g.down, 2)

    def test_kickoff_between_downs(self):
        g = parse_scoreboard_event(self._live_event(down=0, distance=0))
        self.assertIsNone(g.down)
        self.assertIsNone(g.distance)

    def test_halftime_and_overtime_clock(self):
        ev = self._live_event()
        ev["competitions"][0]["status"] = {"displayClock": "0:00", "period": 2, "type": {"name": "STATUS_HALFTIME", "state": "in", "completed": False}}
        self.assertEqual(parse_scoreboard_event(ev).game_seconds_remaining, 1800)
        ev["competitions"][0]["status"] = {"displayClock": "7:21", "period": 5, "type": {"name": "STATUS_IN_PROGRESS", "state": "in", "completed": False}}
        g = parse_scoreboard_event(ev)
        self.assertEqual(g.period, 5)
        self.assertEqual(g.game_seconds_remaining, 441)

    def test_live_games_filter(self):
        self.assertEqual([g.event_id for g in live_games([parse_scoreboard_event(self._live_event())])], ["401872932"])


class HelperTests(unittest.TestCase):
    def test_parse_clock(self):
        self.assertEqual(parse_clock("12:34"), 754)
        self.assertEqual(parse_clock("0:00"), 0)
        self.assertEqual(parse_clock("15:00"), 900)
        self.assertEqual(parse_clock("1:05.3"), 65)
        self.assertEqual(parse_clock(754.0), 754)
        self.assertEqual(parse_clock("42"), 42)
        self.assertIsNone(parse_clock(""))
        self.assertIsNone(parse_clock(None))
        self.assertIsNone(parse_clock("Final"))

    def test_game_seconds_remaining(self):
        self.assertEqual(game_seconds_remaining("pre", 0, None), 3600)
        self.assertEqual(game_seconds_remaining("final", 4, 0), 0)
        self.assertEqual(game_seconds_remaining("live", 1, 900), 3600)
        self.assertEqual(game_seconds_remaining("live", 4, 120), 120)
        self.assertEqual(game_seconds_remaining("live", 2, 0), 1800)
        self.assertEqual(game_seconds_remaining("live", 5, 600), 600)
        self.assertEqual(game_seconds_remaining("live", 5, 900), 600)  # clamp to the OT period
        self.assertIsNone(game_seconds_remaining("live", 3, None))

    def test_map_status(self):
        self.assertEqual(map_status({"type": {"name": "STATUS_SCHEDULED", "state": "pre", "completed": False}}), "pre")
        self.assertEqual(map_status({"type": {"name": "STATUS_IN_PROGRESS", "state": "in", "completed": False}}), "live")
        self.assertEqual(map_status({"type": {"name": "STATUS_HALFTIME", "state": "in", "completed": False}}), "live")
        self.assertEqual(map_status({"type": {"name": "STATUS_FINAL", "state": "post", "completed": True}}), "final")
        self.assertEqual(map_status({"type": {"name": "STATUS_POSTPONED", "state": "post", "completed": False}}), "other")
        self.assertEqual(map_status({"type": {"name": "STATUS_IN_PROGRESS"}}), "live")  # no 'state'
        self.assertEqual(map_status({}), "other")

    def test_spread_sign_normalisation(self):
        # details string only, home is the named favourite -> negative
        self.assertEqual(normalize_home_spread({"details": "CHI -4.5"}, "CHI"), -4.5)
        # details string only, away is the favourite -> home gets the positive line
        self.assertEqual(normalize_home_spread({"details": "PHI -7"}, "TEN"), 7.0)
        # explicit home line wins over everything else
        self.assertEqual(normalize_home_spread({"details": "PHI -7", "spread": 7.0, "pointSpread": {"home": {"close": {"line": "+6.5"}}}}, "TEN"), 6.5)
        # numeric fallback (already home-relative on the scoreboard)
        self.assertEqual(normalize_home_spread({"spread": 3.5}, "NYJ"), 3.5)
        self.assertEqual(normalize_home_spread({"details": "EVEN"}, "NYJ"), 0.0)
        self.assertIsNone(normalize_home_spread({}, "NYJ"))
        self.assertIsNone(normalize_home_spread(None, "NYJ"))

    def test_yardline_100(self):
        self.assertEqual(yardline_100_from(66, "away"), 66)
        self.assertEqual(yardline_100_from(66, "home"), 34)
        self.assertEqual(yardline_100_from(None, "away", "DET 34", "BUF", "DET"), 66)
        self.assertEqual(yardline_100_from(None, "home", "DET 34", "BUF", "DET"), 34)
        self.assertIsNone(yardline_100_from(66, None))
        self.assertIsNone(yardline_100_from(None, "home", "garbage", "BUF", "DET"))

    def test_scoreboard_date_param(self):
        feed, http = _feed()
        feed.games("2026-09-20")
        self.assertIn("dates=20260920", http.calls[-1])
        feed.games(datetime(2026, 9, 21, tzinfo=timezone.utc))
        self.assertIn("dates=20260921", http.calls[-1])
        feed.games()
        self.assertNotIn("dates=", http.calls[-1])

    def test_malformed_event_is_skipped(self):
        sb = load("espn/scoreboard.json")
        sb["events"].append({"id": "x", "competitions": "not-a-list"})
        feed, _ = _feed(scoreboard=sb)
        self.assertEqual(len(feed.games()), 3)

    def test_gamestate_defaults(self):
        g = GameState(event_id="1", home="BUF", away="DET")
        self.assertEqual(g.status, "other")
        self.assertIsNone(g.event_key)
        self.assertIsNone(g.et_date)


if __name__ == "__main__":
    unittest.main()

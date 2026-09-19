"""ESPN game-state feed parses recorded scoreboard / summary payloads offline."""

import copy
import json
import time
import unittest
from datetime import datetime, timezone

from arb_engine.venues.espn import (
    ESPNClient,
    ESPNFeed,
    GameState,
    StateGuard,
    apply_summary,
    classify_play,
    count_timeouts,
    game_seconds_remaining,
    live_games,
    map_status,
    normalize_home_spread,
    parse_clock,
    parse_pickcenter_moneylines,
    parse_scoreboard_event,
    period_clock_to_gsr,
    summary_plays,
    timeouts_timeline,
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


# ---- feed hardening (P02) ------------------------------------------------------------------

class ClockHelperTests(unittest.TestCase):
    """period_clock_to_gsr is the one clock the live feed and the replay share."""

    def test_per_sport_table(self):
        cases = [
            ("nfl", 1, 900, (3600, False)), ("nfl", 4, 120, (120, False)), ("nfl", 2, 0, (1800, False)),
            ("nfl", 5, 600, (600, False)), ("nfl", 5, 900, (600, False)),   # regular-season OT clamps to 600
            ("nfl", 6, 300, (300, False)),                                   # a second OT period: only it is left
            ("ncaaf", 4, 61, (61, False)), ("ncaaf", 5, 0, (None, True)), ("ncaaf", 6, None, (None, True)),
            ("nba", 1, 720, (2880, False)), ("nba", 4, 60, (60, False)), ("nba", 5, 300, (300, False)), ("nba", 5, 301, (300, False)),
            ("nhl", 1, 1200, (3600, False)), ("nhl", 3, 90, (90, False)), ("nhl", 4, 100, (100, False)),
            ("nfl", 0, None, (3600, False)), ("nba", 0, None, (2880, False)), ("nhl", 0, None, (3600, False)),
            ("nfl", 3, None, (None, False)),
        ]
        for sport, period, clock, want in cases:
            self.assertEqual(period_clock_to_gsr(sport, period, clock), want, (sport, period, clock))

    def test_nfl_playoff_overtime_clock_from_format(self):
        fmt = {"regulation": {"periods": 4, "clock": 900.0}, "overtime": {"periods": 1, "clock": 900.0}}
        self.assertEqual(period_clock_to_gsr("nfl", 5, 900, fmt), (900, False))
        self.assertEqual(period_clock_to_gsr("nfl", 5, 441, fmt), (441, False))
        self.assertEqual(period_clock_to_gsr("ncaaf", 5, 441, fmt), (None, True))  # college OT is untimed whatever the block says
        self.assertEqual(period_clock_to_gsr("nhl", 4, 1200, {"overtime": {"clock": 1200.0}}), (1200, False))  # playoff hockey OT

    def test_game_seconds_remaining_takes_the_sport(self):
        self.assertEqual(game_seconds_remaining("pre", 0, None, "nba"), 2880)
        self.assertEqual(game_seconds_remaining("live", 3, 60, "nhl"), 60)
        self.assertIsNone(game_seconds_remaining("live", 5, 0, "ncaaf"))
        self.assertEqual(game_seconds_remaining("final", 5, 0, "ncaaf"), 0)
        self.assertIsNone(game_seconds_remaining("live", 0, None))


class ClassifyPlayTests(unittest.TestCase):
    def test_synthetic_texts(self):
        cases = [
            ("T.Bass kicks 56 yards from BUF 35 to DET 9. T.Kennedy pushed ob at DET 32 for 23 yards.", "53", "kickoff"),
            ("J.Bates kicks onside 12 yards from DET 35 to DET 47. K.Shakir to DET 36 for 11 yards.", None, "kickoff"),
            ("J.Bates extra point is GOOD, Center-R.Ferguson, Holder-J.Bates.", None, "try"),
            ("(Pass formation) TWO-POINT CONVERSION ATTEMPT. J.Goff pass to A.St. Brown is complete. ATTEMPT SUCCEEDS.", None, "try"),
            ("Ryan Fitzgerald kick attempt good.", None, "try"),                                         # college PAT wording
            ("(Shotgun) J.Goff pass deep middle to A.St. Brown for 27 yards, TOUCHDOWN. J.Bates extra point is GOOD.", "67", "scrimmage"),  # try folded into the TD
            ("J.Cook right guard to BUF 1 for 1 yard (T.Lacy).", "5", "scrimmage"),                    # 1st & goal from the 2 is not a try
            ("1st & Goal at BUF 2", None, "scrimmage"),
            ("J.Goff pass short right to Pat Freiermuth for 8 yards.", None, "scrimmage"),              # 'Pat' is a receiver, not a PAT
            ("Timeout #1 by BUF at 01:48.", "21", "timeout"),
            ("Timeout ORE, clock 01:57.", None, "timeout"),
            ("Two-Minute Warning", "75", "timeout"),
            ("J.Allen kneels to DET 21 for -1 yards.", "5", "kneel"),
            ("END GAME", "66", "end_period"), ("END QUARTER 3", None, "end_period"), ("End of Half", "65", "end_period"),
            ("(01:57) No Huddle-Shotgun #26 B.Smith rush middle for 2 yards gain to the PSU12", "5", "scrimmage"),
            ("J.Cook left tackle to DET 38 for -3 yards.PENALTY on DET-T.Lacy, Face Mask, 15 yards, enforced at DET 38 - No Play.", "8", "scrimmage"),
            ("", "53", "kickoff"), ("", None, None), (None, "21", "timeout"), (None, "999", None),
        ]
        for text, tid, want in cases:
            self.assertEqual(classify_play(text, tid), want, (text, tid))

    def test_fixture_plays(self):
        plays = summary_plays(load("espn/summary_401872932.json"))
        classes = [classify_play(p["text"], p["type"]["id"]) for p in plays]
        self.assertEqual(classes.count("kickoff"), 2)
        self.assertEqual(classes.count("timeout"), 3)  # two team timeouts + the two-minute warning
        self.assertEqual(classes.count("kneel"), 3)
        self.assertEqual(classes[-1], "end_period")
        self.assertNotIn(None, classes)


class TimeoutCountTests(unittest.TestCase):
    @staticmethod
    def _play(period, text, tid="21"):
        return {"period": {"number": period}, "text": text, "type": {"id": tid, "text": "Timeout" if tid == "21" else "Rush"}}

    def test_football_resets_per_half_and_in_overtime(self):
        plays = [
            self._play(1, "J.Gibbs right tackle to BUF 34 for 6 yards.", "5"),
            self._play(1, "Timeout #1 by DET at 10:00."),
            self._play(2, "Timeout #2 by DET at 01:00."),
            self._play(2, "Timeout #1 by BUF at 00:30."),
            self._play(3, "J.Cook right guard to DET 35 for 1 yard.", "5"),   # second half: back to 3
            self._play(4, "Timeout #1 by BUF at 02:00."),
            self._play(4, "Two-Minute Warning", "75"),                         # charges nobody
            self._play(4, "Timeout at 01:00."),                                # official / unattributed: charges nobody
            self._play(5, "T.Bass kicks 65 yards from BUF 35 to end zone, Touchback.", "53"),  # OT: 2 each
            self._play(5, "Timeout #1 by DET at 08:00."),
        ]
        tl = timeouts_timeline(plays, "nfl", "BUF", "DET")
        self.assertEqual(tl[1], (3, 3))
        self.assertEqual(tl[2], (3, 2))       # after 'Timeout #1 by DET'
        self.assertEqual(tl[3], (3, 1))       # before BUF's first: DET has used two
        self.assertEqual(tl[4], (3, 3))       # first play of the second half: halftime reset
        self.assertEqual(tl[6], (2, 3))       # after 'Timeout #1 by BUF' in Q4
        self.assertEqual(tl[7], (2, 3))       # the two-minute warning changed nothing
        self.assertEqual(tl[8], (2, 2))       # first OT play: 2 each (the official timeout charged nobody)
        self.assertEqual(tl[9], (2, 2))
        self.assertEqual(count_timeouts(plays, "nfl", "BUF", "DET"), {"home": 2, "away": 1})
        self.assertEqual(count_timeouts(plays[:4], "nfl", "BUF", "DET"), {"home": 2, "away": 1})
        self.assertEqual(count_timeouts([], "nfl", "BUF", "DET"), {"home": 3, "away": 3})
        self.assertEqual(count_timeouts(plays[:2], "ncaaf", "BUF", "DET"), {"home": 3, "away": 2})
        self.assertEqual(count_timeouts(plays, "ncaaf", "BUF", "DET"), {"home": 1, "away": 0})  # college OT: one each

    def test_team_resolved_by_alias_and_floor_at_zero(self):
        plays = [self._play(1, "Timeout Detroit, clock 10:00."), self._play(1, "Timeout #2 by DET at 09:00."), self._play(1, "Timeout #3 by DET at 08:00."), self._play(1, "Timeout #4 by DET at 07:00.")]
        self.assertEqual(count_timeouts(plays, "nfl", "BUF", "DET"), {"home": 3, "away": 0})
        self.assertEqual(count_timeouts([self._play(1, "Timeout ORE, clock 01:57.")], "ncaaf", "ORE", "PRST"), {"home": 2, "away": 3})
        self.assertEqual(count_timeouts([self._play(1, "Timeout #1 by LAC at 01:57.")], "nhl", "VGK", "SJ"), {"home": 1, "away": 1})

    def test_fixture_counts(self):
        plays = summary_plays(load("espn/summary_401872932.json"))
        self.assertEqual(count_timeouts(plays, "nfl", "BUF", "DET"), {"home": 2, "away": 2})


class PickcenterTests(unittest.TestCase):
    def test_moneylines_from_recorded_summary(self):
        summary = load("espn/summary_401872932.json")
        self.assertEqual(parse_pickcenter_moneylines(summary["pickcenter"][0]), {"home": -245, "away": 200, "home_open": -162, "away_open": 136})
        self.assertEqual(parse_pickcenter_moneylines(None), {"home": None, "away": None, "home_open": None, "away_open": None})
        self.assertEqual(parse_pickcenter_moneylines({"homeTeamOdds": {"moneyLine": -130}, "awayTeamOdds": {"moneyLine": "EVEN"}}), {"home": -130, "away": 100, "home_open": None, "away_open": None})
        feed, _ = _feed()
        g = feed.enrich(feed.find("nfl:BUF|DET:2026-09-17"))
        self.assertEqual((g.sportsbook_ml_home, g.sportsbook_ml_away), (-245, 200))
        self.assertEqual((g.sportsbook_ml_home_open, g.sportsbook_ml_away_open), (-162, 136))
        self.assertEqual(g.pickcenter_spread, -5.5)
        self.assertEqual(g.vegas_spread_home, -5.5)
        self.assertEqual(g.espn_tie, 0.0)
        self.assertEqual(g.season, 2026)
        self.assertEqual(g.last_play_id, "4018729324530")
        self.assertEqual((g.last_play_type_id, g.last_play_type, g.play_class), ("66", "End of Game", "end_period"))
        self.assertEqual((g.regulation_period_seconds, g.ot_seconds), (900, 600))
        self.assertFalse(g.overtime)
        self.assertFalse(g.overtime_sentinel)
        d = g.as_dict()
        for k in ("last_play_id", "play_class", "overtime_sentinel", "espn_tie", "suspect", "review_pending", "state_changed_ts", "sportsbook_ml_home", "pickcenter_spread", "season", "final_soft"):
            self.assertIn(k, d)

    def test_scoreboard_only_state_has_no_pickcenter(self):
        feed, _ = _feed()
        g = feed.find("nfl:CHI|MIN:2026-09-20")
        self.assertIsNone(g.pickcenter_spread)
        self.assertEqual(g.vegas_spread_home, -4.5)  # the scoreboard odds still fill the generic field
        self.assertIsNone(g.sportsbook_ml_home)
        self.assertEqual(g.season, 2026)


_GOLDEN = json.loads(r"""
{"401872932": {"event_id": "401872932", "home": "BUF", "away": "DET", "home_score": 41, "away_score": 31, "status": "final", "period": 4, "clock_seconds_remaining_in_period": 0, "game_seconds_remaining": 0, "possession": null, "down": null, "distance": null, "yardline_100": null, "home_timeouts": null, "away_timeouts": null, "espn_home_wp": null, "vegas_spread_home": null, "vegas_total": null, "odds_provider": null, "start_time": "2026-09-18T00:15:00+00:00", "event_key": "nfl:BUF|DET:2026-09-17", "home_team_id": "2", "away_team_id": "8", "status_name": "STATUS_FINAL", "status_detail": "Final", "last_play_text": null, "is_red_zone": null, "receive_2h_ko_home": null, "enriched": false, "sport": "nfl", "score_diff_home": 10, "espn_wp_series_len": 0},
 "401872932:enriched": {"event_id": "401872932", "home": "BUF", "away": "DET", "home_score": 41, "away_score": 31, "status": "final", "period": 4, "clock_seconds_remaining_in_period": 0, "game_seconds_remaining": 0, "possession": null, "down": null, "distance": null, "yardline_100": null, "home_timeouts": null, "away_timeouts": null, "espn_home_wp": 1.0, "vegas_spread_home": -5.5, "vegas_total": 54.5, "odds_provider": "Draft Kings", "start_time": "2026-09-18T00:15:00+00:00", "event_key": "nfl:BUF|DET:2026-09-17", "home_team_id": "2", "away_team_id": "8", "status_name": "STATUS_FINAL", "status_detail": "Final", "last_play_text": "END GAME", "is_red_zone": null, "receive_2h_ko_home": true, "enriched": true, "sport": "nfl", "score_diff_home": 10, "espn_wp_series_len": 190},
 "401872937": {"event_id": "401872937", "home": "CHI", "away": "MIN", "home_score": 0, "away_score": 0, "status": "pre", "period": 0, "clock_seconds_remaining_in_period": 0, "game_seconds_remaining": 3600, "possession": null, "down": null, "distance": null, "yardline_100": null, "home_timeouts": null, "away_timeouts": null, "espn_home_wp": null, "vegas_spread_home": -4.5, "vegas_total": 48.5, "odds_provider": "Draft Kings", "start_time": "2026-09-20T17:00:00+00:00", "event_key": "nfl:CHI|MIN:2026-09-20", "home_team_id": "3", "away_team_id": "16", "status_name": "STATUS_SCHEDULED", "status_detail": "9/20 - 1:00 PM EDT", "last_play_text": null, "is_red_zone": null, "receive_2h_ko_home": null, "enriched": false, "sport": "nfl", "score_diff_home": 0, "espn_wp_series_len": 0},
 "401872939": {"event_id": "401872939", "home": "TEN", "away": "PHI", "home_score": 0, "away_score": 0, "status": "pre", "period": 0, "clock_seconds_remaining_in_period": 0, "game_seconds_remaining": 3600, "possession": null, "down": null, "distance": null, "yardline_100": null, "home_timeouts": null, "away_timeouts": null, "espn_home_wp": null, "vegas_spread_home": 7.0, "vegas_total": 39.5, "odds_provider": "Draft Kings", "start_time": "2026-09-20T17:00:00+00:00", "event_key": "nfl:PHI|TEN:2026-09-20", "home_team_id": "10", "away_team_id": "21", "status_name": "STATUS_SCHEDULED", "status_detail": "9/20 - 1:00 PM EDT", "last_play_text": null, "is_red_zone": null, "receive_2h_ko_home": null, "enriched": false, "sport": "nfl", "score_diff_home": 0, "espn_wp_series_len": 0}}
""")


class GoldenFieldsTests(unittest.TestCase):
    """Every field the parser produced before the hardening, captured from the pre-P02 parser on
    the recorded DET@BUF fixtures, must come out identical (the guard is on, as in production)."""

    def test_pre_existing_fields_unchanged(self):
        feed, _ = _feed()
        got = {}
        for g in feed.games():
            got[g.event_id] = g.as_dict()
            if g.event_id == "401872932":
                got["401872932:enriched"] = feed.enrich(g).as_dict()
        for key, want in _GOLDEN.items():
            have = got[key]
            have["espn_wp_series_len"] = len(have.pop("espn_wp_series"))
            for f, v in want.items():
                self.assertEqual(have[f], v, (key, f))


class SyntheticOvertimeTests(unittest.TestCase):
    """summary_ot_synthetic.json: playoff-format NFL OT (900 s clock), 27-27, 7:21 left in period 5."""

    def _state(self, sport="nfl"):
        return GameState(event_id="401872932", home="BUF", away="DET", status="live", period=4, clock_seconds_remaining_in_period=0, home_team_id="2", away_team_id="8", sport=sport)

    def test_nfl_playoff_overtime(self):
        g = apply_summary(self._state(), load("espn/summary_ot_synthetic.json"))
        self.assertEqual((g.status, g.period, g.clock_seconds_remaining_in_period), ("live", 5, 441))
        self.assertEqual(g.game_seconds_remaining, 441)   # 900 - clock, not the regular-season 600 clamp
        self.assertTrue(g.overtime)
        self.assertFalse(g.overtime_sentinel)
        self.assertEqual((g.regulation_period_seconds, g.ot_seconds), (900, 900))
        self.assertEqual((g.home_score, g.away_score), (27, 27))
        self.assertEqual(g.espn_home_wp, 0.47)
        self.assertEqual(g.espn_tie, 0.04)
        self.assertEqual(g.season, 2026)
        self.assertEqual(g.possession, "away")
        self.assertEqual((g.down, g.distance, g.yardline_100), (2, 1, 38))

    def test_negative_provisional_play_id_ignored(self):
        g = apply_summary(self._state(), load("espn/summary_ot_synthetic.json"))
        # situation.lastPlay.id is -4018729325001 (provisional): the last drive play supplies the id instead.
        self.assertEqual(g.last_play_id, "4018729325000")
        self.assertEqual(g.play_class, "scrimmage")
        self.assertEqual(g.last_play_type_id, "24")

    def test_timeouts_fallback_from_drives(self):
        g = apply_summary(self._state(), load("espn/summary_ot_synthetic.json"))
        # No homeTimeouts/awayTimeouts in the situation: counted from the drives. OT gives 2 each;
        # DET used one at 08:00 of the OT period.
        self.assertEqual((g.home_timeouts, g.away_timeouts), (2, 1))
        # A present value is never overridden.
        st = self._state()
        st.home_timeouts, st.away_timeouts = 1, 0
        g2 = apply_summary(st, load("espn/summary_ot_synthetic.json"))
        self.assertEqual((g2.home_timeouts, g2.away_timeouts), (1, 0))
        st = self._state()
        st.away_timeouts = 0
        g3 = apply_summary(st, load("espn/summary_ot_synthetic.json"))
        self.assertEqual((g3.home_timeouts, g3.away_timeouts), (2, 0))

    def test_college_overtime_sentinel(self):
        g = apply_summary(self._state("ncaaf"), load("espn/summary_ot_synthetic.json"))
        self.assertEqual(g.period, 5)
        self.assertIsNone(g.game_seconds_remaining)
        self.assertTrue(g.overtime)
        self.assertTrue(g.overtime_sentinel)
        self.assertIsNone(g.ot_seconds)
        self.assertEqual(g.as_dict()["overtime_sentinel"], True)
        # A final college game in OT is simply over: no sentinel, gsr 0.
        summary = load("espn/summary_ot_synthetic.json")
        summary["header"]["competitions"][0]["status"]["type"] = {"name": "STATUS_FINAL", "state": "post", "completed": True}
        g2 = apply_summary(self._state("ncaaf"), summary)
        self.assertEqual((g2.status, g2.game_seconds_remaining, g2.overtime_sentinel), ("final", 0, False))

    def test_scoreboard_college_overtime(self):
        ev = copy.deepcopy(next(e for e in load("ncaaf/espn_scoreboard_cfb.json")["events"] if e["status"]["type"]["state"] == "in"))
        ev["status"]["period"] = ev["competitions"][0]["status"]["period"] = 5
        g = parse_scoreboard_event(ev, "ncaaf")
        self.assertEqual((g.status, g.period), ("live", 5))
        self.assertIsNone(g.game_seconds_remaining)
        self.assertTrue(g.overtime_sentinel)


def _guard_event(base: dict, poll: dict) -> dict:
    """Patch the DET@BUF scoreboard event into the live snapshot a guard_sequences poll describes."""
    ev = copy.deepcopy(next(e for e in load("espn/scoreboard.json")["events"] if e["id"] == base["event_id"]))
    comp = ev["competitions"][0]
    final = poll.get("status") == "final"
    clock = poll.get("clock", base["clock"])
    status = {"displayClock": clock, "period": poll.get("period", base["period"]), "type": {"name": "STATUS_FINAL" if final else "STATUS_IN_PROGRESS", "state": "post" if final else "in", "completed": final}}
    ev["status"] = comp["status"] = status
    for c in comp["competitors"]:
        c["score"] = str(poll.get("home_score", base["home_score"]) if c["homeAway"] == "home" else poll.get("away_score", base["away_score"]))
    sit = copy.deepcopy(base["situation"])
    if "possession" in poll:
        sit["possession"] = poll["possession"]
    if "down" in poll:
        sit["down"] = poll["down"]
    lp = poll.get("last_play")
    if lp:
        sit["lastPlay"] = {"id": lp["id"], "text": lp["text"], "type": {"id": lp.get("type_id", ""), "text": ""}, "probability": {"homeWinPercentage": lp.get("wp", 0.61), "tiePercentage": lp.get("tie", 0.0)}}
    comp["situation"] = sit
    return ev


class GuardSequenceTests(unittest.TestCase):
    """tests/fixtures/espn/guard_sequences.json: each sequence is a run of scoreboard polls with the
    fields the guard must report after every poll (score holds, WP nulling, review windows, soft
    finals, state_changed_ts)."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = load("espn/guard_sequences.json")

    def _run(self, name):
        seq = self.fixture["sequences"][name]
        guard = StateGuard(event_id=self.fixture["base"]["event_id"])
        outs = []
        for i, poll in enumerate(seq["polls"]):
            gs = parse_scoreboard_event(_guard_event(self.fixture["base"], poll))
            out = guard.apply(gs, float(poll["t"]), poll.get("poll"))
            outs.append(out)
            for f, want in poll["expect"].items():
                self.assertEqual(getattr(out, f), want, f"{name} poll {i} (t={poll['t']}) field {f}: {getattr(out, f)!r} != {want!r}")
        return guard, outs

    def test_every_sequence(self):
        for name in self.fixture["sequences"]:
            with self.subTest(sequence=name):
                self._run(name)

    def test_score_before_lastplay_nulls_wp_until_id_advances(self):
        _, outs = self._run("score_before_lastplay")
        self.assertEqual([o.espn_home_wp for o in outs], [0.61, None, None, 0.81, 0.80])
        self.assertEqual([o.suspect for o in outs], [False, True, True, False, False])

    def test_score_decrease_held_one_poll(self):
        guard, outs = self._run("score_decrease_held_then_accepted")
        self.assertEqual([o.home_score for o in outs], [21, 21, 14, 14])
        self.assertEqual(guard.anomalies, 1)
        self.assertEqual(guard.confirmed.home_score, 14)
        _, outs = self._run("score_decrease_reversed_accepted")
        self.assertEqual([o.home_score for o in outs], [21, 14])

    def test_decrease_first_seen_on_repeated_poll_is_held(self):
        guard, outs = self._run("decrease_first_seen_on_repeated_poll_is_held")
        self.assertEqual([o.home_score for o in outs], [21, 21, 21, 21, 14])
        self.assertEqual(guard.anomalies, 1)  # the repeated-poll sighting neither spends the budget nor counts twice
        guard, outs = self._run("enrich_glitch_never_leaks")
        self.assertEqual(guard.anomalies, 0)

    def test_second_decrease_after_accepted_is_held(self):
        guard, outs = self._run("second_decrease_after_accepted_is_held")
        self.assertEqual([o.home_score for o in outs], [21, 21, 14, 14, 7])
        self.assertEqual(guard.anomalies, 2)
        self.assertEqual(guard.held_polls, 0)

    def test_held_copy_does_not_alias_confirmed(self):
        guard, outs = self._run("score_decrease_held_then_accepted")
        outs[-1].home_score = 99
        self.assertEqual(guard.confirmed.home_score, 14)

    def test_review_and_final_transitions(self):
        _, outs = self._run("review_text_after_td")
        self.assertEqual([o.review_pending for o in outs], [False, False, True, False])
        _, outs = self._run("final_then_live_within_300s")
        self.assertEqual([o.status for o in outs], ["live", "final", "live"])
        _, outs = self._run("final_then_live_after_300s")
        self.assertEqual([o.suspect for o in outs], [False, False, False, False, True])

    def test_first_poll_is_trusted(self):
        guard = StateGuard(event_id="x")
        g = GameState(event_id="x", home="BUF", away="DET", status="live", home_score=7)
        out = guard.apply(g, 5.0)
        self.assertIs(out, g)
        self.assertEqual(out.state_changed_ts, 5.0)
        self.assertFalse(out.suspect)


class FeedGuardTests(unittest.TestCase):
    """ESPNFeed runs every state through the event's guard; scoreboard poll + enrich share a poll id."""

    def _scoreboards(self, polls):
        fixture = load("espn/guard_sequences.json")
        payloads = [{"events": [_guard_event(fixture["base"], p)]} for p in polls]
        it = iter(payloads)
        return FakeHttp({"/summary?event=401872932": lambda: {"header": {"competitions": [{"competitors": [{"id": "2", "homeAway": "home", "score": "14"}, {"id": "8", "homeAway": "away", "score": "17"}]}]}}, "/scoreboard": lambda: next(it)})

    def test_decrease_held_across_games_and_enrich(self):
        http = self._scoreboards([{}, {"home_score": 14, "last_play": {"id": "40187293231", "text": "J.Cook right guard to DET 35 for 1 yard."}}, {"home_score": 14, "last_play": {"id": "40187293231", "text": "J.Cook right guard to DET 35 for 1 yard."}}])
        feed = ESPNFeed(ESPNClient(http=http))
        g0 = feed.games(now=0.0)[0]
        self.assertEqual(g0.home_score, 21)
        self.assertIn("401872932", feed.guards)
        g1 = feed.games(now=10.0)[0]
        self.assertEqual((g1.home_score, g1.suspect), (21, True))      # held
        e1 = feed.enrich(g1, now=11.0)                                   # same poll: still held even though the summary says 14 too
        self.assertEqual((e1.home_score, e1.suspect), (21, True))
        g2 = feed.games(now=20.0)[0]
        self.assertEqual((g2.home_score, g2.suspect), (14, True))      # next scoreboard poll: accepted
        self.assertEqual(feed.guards["401872932"].anomalies, 1)

    def test_decrease_first_seen_in_enrich_is_held(self):
        """The summary can disagree with the scoreboard of the same tick; that sighting is held too."""
        drop = {"home_score": 14, "last_play": {"id": "40187293231", "text": "J.Cook right guard to DET 35 for 1 yard."}}
        http = self._scoreboards([{}, {}, drop, drop])
        feed = ESPNFeed(ESPNClient(http=http))
        g0 = feed.games(now=0.0)[0]
        e0 = feed.enrich(g0, now=1.0)                                    # summary says 14 on poll 1
        self.assertEqual((e0.home_score, e0.suspect), (21, True))
        g1 = feed.games(now=10.0)[0]                                     # scoreboard still 21: glitch gone, clean
        self.assertEqual((g1.home_score, g1.suspect), (21, False))
        g2 = feed.games(now=20.0)[0]                                     # scoreboard now 14: the one extra poll
        self.assertEqual((g2.home_score, g2.suspect), (21, True))
        e2 = feed.enrich(g2, now=21.0)
        self.assertEqual((e2.home_score, e2.suspect), (21, True))
        g3 = feed.games(now=30.0)[0]
        self.assertEqual((g3.home_score, g3.suspect), (14, True))
        self.assertEqual(feed.guards["401872932"].anomalies, 1)

    def test_guard_can_be_disabled(self):
        http = self._scoreboards([{}, {"home_score": 14, "last_play": {"id": "40187293231", "text": "J.Cook right guard to DET 35 for 1 yard."}}])
        feed = ESPNFeed(ESPNClient(http=http), guard=False)
        feed.games(now=0.0)
        g = feed.games(now=10.0)[0]
        self.assertEqual((g.home_score, g.suspect), (14, False))
        self.assertEqual(feed.guards, {})

    def test_real_time_default(self):
        feed, _ = _feed()
        before = time.time()
        g = feed.games()[0]
        self.assertGreaterEqual(g.state_changed_ts, before)


if __name__ == "__main__":
    unittest.main()

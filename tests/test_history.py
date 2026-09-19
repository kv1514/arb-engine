"""History clients, ESPN play timeline, market alignment, play classes, timeouts, synthetic
rows, the offline cache and the game replay — offline."""

import json
import os
import tempfile
import unittest

from arb_engine.backtest import GameReplayer, summarize
from arb_engine.venues.espn import ESPNClient
from arb_engine.venues.history import Bar, HistoryClient, OfflineCacheMiss, bar_at, cache_key, classify_play, espn_timeline, period_clock_to_gsr, synthetic_rows, timeouts_timeline

from .helpers import FakeHttp, load


def _history(**kw):
    return HistoryClient(http=FakeHttp({
        "KXNFLGAME-26SEP17DETBUF-BUF/candlesticks": load("history/kalshi_candles_BUF.json"),
        "KXNFLGAME-26SEP17DETBUF-DET/candlesticks": load("history/kalshi_candles_DET.json"),
        "/series/KXNFLGAME": load("kalshi_series_kxnflgame.json"),
        "/marketdata/event/contract/historicals/v1/": load("history/robinhood_bars.json"),
        "clob.polymarket.com/prices-history": load("history/polymarket_history_DET.json"),
    }), **kw)


class HistoryClientTests(unittest.TestCase):
    def test_kalshi_candles(self):
        bars = _history().kalshi_candles("KXNFLGAME-26SEP17DETBUF-BUF", 1789690500, 1789691400)
        self.assertEqual(len(bars), 12)
        self.assertEqual(bars, sorted(bars, key=lambda b: b.ts))
        b = bars[0]
        self.assertIsNotNone(b.ask)
        self.assertIsNotNone(b.bid)
        self.assertGreater(b.ask, b.bid)
        self.assertAlmostEqual(b.mid, (b.bid + b.ask) / 2)

    def test_kalshi_fee_multiplier_from_series(self):
        h = _history()
        self.assertEqual(h.kalshi_fee_multiplier("KXNFLGAME-26SEP17DETBUF-BUF"), 1.0)
        h2 = HistoryClient(http=FakeHttp({"/series/KXMLBGAME": {"series": {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 0.5}}}))
        self.assertEqual(h2.kalshi_fee_multiplier("KXMLBGAME-26SEP17NYYBOS-BOS"), 0.5)
        h3 = HistoryClient(http=FakeHttp({}))  # lookup fails -> 1, never a silent zero-fee replay
        self.assertEqual(h3.kalshi_fee_multiplier("KXWHATEVER-X-Y"), 1.0)
        with tempfile.TemporaryDirectory() as d:  # ... but an offline miss surfaces like every other lookup
            with self.assertRaises(OfflineCacheMiss):
                _history(cache_dir=d, offline=True).kalshi_fee_multiplier("KXNFLGAME-26SEP17DETBUF-BUF")

    def test_robinhood_bars(self):
        bars = _history().robinhood_bars(["68a948d7-0087-436b-86ce-78a24d2677a6", "90efc89f-0754-444d-8416-f5caedee185d"], "2026-09-18T00:00:00.000Z")
        self.assertEqual(set(bars), {"68a948d7-0087-436b-86ce-78a24d2677a6", "90efc89f-0754-444d-8416-f5caedee185d"})
        buf = bars["68a948d7-0087-436b-86ce-78a24d2677a6"]
        self.assertEqual(len(buf), 8)
        self.assertIsNone(buf[0].ask)  # trade prices only
        self.assertIsNotNone(buf[0].close)
        self.assertEqual(buf[0].mid, buf[0].close)
        self.assertEqual(buf[1].ts - buf[0].ts, 300)

    def test_polymarket_history(self):
        pts = _history().polymarket_history("3594", 1789690500, 1789691400)
        self.assertEqual(len(pts), 10)
        self.assertTrue(all(0 <= p.close <= 1 for p in pts))

    def test_bar_at(self):
        bars = [Bar(ts=100, bid=None, ask=None, close=0.5), Bar(ts=160, bid=None, ask=None, close=0.6), Bar(ts=220, bid=None, ask=None, close=0.7)]
        self.assertEqual(bar_at(bars, 130, "kalshi").ts, 160)     # first bar ending at/after t
        self.assertEqual(bar_at(bars, 130, "start").ts, 100)      # last bar starting at/before t
        self.assertEqual(bar_at(bars, 300, "kalshi").ts, 220)
        self.assertIsNone(bar_at(bars, 5000, "start", max_gap=60))
        self.assertIsNone(bar_at([], 100))


class AlignmentTests(unittest.TestCase):
    """The three Kalshi alignments on a 3-candle fixture (ends at t0, t0+60, t0+120)."""

    def setUp(self):
        self.bars = HistoryClient(http=FakeHttp({"/candlesticks": load("history/kalshi_candles_before_after.json")})).kalshi_candles("KXNFLGAME-X-Y", 0, 1)
        self.t0 = self.bars[0].ts

    def test_before_is_last_candle_ending_at_or_before_the_play(self):
        t = self.t0 + 90  # between candle 2 (t0+60) and candle 3 (t0+120)
        self.assertEqual(bar_at(self.bars, t, "kalshi_before").ts, self.t0 + 60)
        self.assertEqual(bar_at(self.bars, t, "kalshi").ts, self.t0 + 120)          # after: the candle that has seen the play
        self.assertEqual(bar_at(self.bars, self.t0 + 60, "kalshi_before").ts, self.t0 + 60)  # exact boundary counts as before (candle closed at the play)
        self.assertEqual(bar_at(self.bars, self.t0 + 60, "kalshi").ts, self.t0 + 60)
        self.assertLess(bar_at(self.bars, t, "kalshi_before").mid, bar_at(self.bars, t, "kalshi").mid)  # the fixture books rise: the leak is visible

    def test_before_never_falls_forward(self):
        self.assertIsNone(bar_at(self.bars, self.t0 - 30, "kalshi_before"))       # nothing closed yet: no market information
        self.assertEqual(bar_at(self.bars, self.t0 - 30, "kalshi").ts, self.t0)   # after-mode may look forward
        self.assertIsNone(bar_at(self.bars, self.t0 + 120 + 901, "kalshi_before"))  # beyond max_gap

    def test_prev_anchors_on_the_previous_play(self):
        t = self.t0 + 130
        self.assertEqual(bar_at(self.bars, t, "kalshi_before").ts, self.t0 + 120)
        self.assertEqual(bar_at(self.bars, t, "kalshi_prev", anchor_ts=self.t0 + 61).ts, self.t0 + 60)
        self.assertEqual(bar_at(self.bars, t, "kalshi_prev").ts, self.t0 + 60)  # default anchor: one minute earlier


class ClockAndClassTests(unittest.TestCase):
    def test_period_clock_to_gsr_per_sport(self):
        self.assertEqual(period_clock_to_gsr("nfl", 1, 900), 3600)
        self.assertEqual(period_clock_to_gsr("nfl", 4, 0), 0)
        self.assertEqual(period_clock_to_gsr("nfl", 5, 420), 420)
        self.assertEqual(period_clock_to_gsr("nfl", 5, 700), 600)
        self.assertIsNone(period_clock_to_gsr("ncaaf", 5, None))   # college OT: untimed
        self.assertEqual(period_clock_to_gsr("ncaaf", 2, 100), 1900)
        self.assertEqual(period_clock_to_gsr("nba", 2, 720), 2160)
        self.assertEqual(period_clock_to_gsr("nhl", 3, 60), 60)
        # venues/espn.py's canonical helper (the local fallback agrees): period 0 = pre-game = full regulation.
        self.assertEqual(period_clock_to_gsr("nfl", 0, 100), 3600)

    def test_classify_play(self):
        self.assertEqual(classify_play("T.Bass kicks 56 yards from BUF 35 to DET 9."), "kickoff")
        self.assertEqual(classify_play("anything", "53"), "kickoff")
        self.assertEqual(classify_play("Timeout #1 by BUF at 01:48."), "timeout")
        self.assertEqual(classify_play("J.Allen kneels to DET 21 for -1 yards."), "kneel")
        self.assertEqual(classify_play("J.Bates extra point is GOOD."), "try")
        self.assertEqual(classify_play("TWO-POINT CONVERSION ATTEMPT. J.Goff pass to A.St. Brown is complete. ATTEMPT SUCCEEDS."), "try")
        self.assertEqual(classify_play("J.Goff pass deep middle to A.St. Brown for 27 yards, TOUCHDOWN. J.Bates extra point is GOOD."), "scrimmage")  # PAT folded into the TD play
        self.assertEqual(classify_play("END GAME"), "end_period")
        # 'PAT' is matched case-sensitively: college feeds carry full names.
        self.assertEqual(classify_play("Pat Bryant rush for 5 yds to the OSU 40"), "scrimmage")
        self.assertEqual(classify_play("Pat Bryant pass complete to Pat Smith for 12 yds"), "scrimmage")
        self.assertEqual(classify_play("J.Bates PAT is GOOD."), "try")
        self.assertEqual(classify_play("Two-Minute Warning"), "timeout")  # a stoppage charged to nobody (espn.classify_play)
        self.assertEqual(classify_play("(Shotgun) J.Goff pass incomplete short right."), "scrimmage")
        self.assertIsNone(classify_play(""))


class TimelineTests(unittest.TestCase):
    def test_espn_timeline_from_summary(self):
        rows, meta = espn_timeline(load("espn/summary_401872932.json"))
        self.assertEqual((meta["home"], meta["away"], meta["home_score"], meta["away_score"]), ("BUF", "DET", 41, 31))
        real = [r for r in rows if not r.synthetic]
        self.assertEqual(len(real), 18)
        self.assertEqual(len(rows), 20)  # one TD -> try_synth + kickoff_pending_synth
        self.assertEqual([r.ts for r in rows], sorted(r.ts for r in rows))
        last = real[-1]
        self.assertEqual((last.period, last.clock_seconds, last.game_seconds_remaining), (4, 0, 0))
        self.assertEqual((last.home_score_after, last.away_score_after), (41, 31))
        self.assertEqual(last.play_class, "end_period")
        # Score *before* the play is the previous play's score after.
        self.assertEqual((real[1].home_score, real[1].away_score), (real[0].home_score_after, real[0].away_score_after))
        self.assertTrue(any(r.espn_home_wp is not None for r in rows))
        rows_plain, _ = espn_timeline(load("espn/summary_401872932.json"), synthetic=False)
        self.assertEqual(len(rows_plain), 18)

    def test_kickoff_row_flips_possession_to_the_receiver(self):
        rows, _ = espn_timeline(load("espn/summary_401872932.json"), synthetic=False)
        ko = rows[0]  # "T.Bass kicks 56 yards from BUF 35": BUF (home) kicks, DET (away) receives
        self.assertEqual(ko.play_class, "kickoff")
        self.assertEqual(ko.possession, "away")
        self.assertEqual(ko.yardline_100, 35)  # kicker at its own 35 -> receiver 35 yards from the kicker's goal (nflverse convention)
        self.assertIsNone(ko.down)
        onside = next(r for r in rows if "onside" in r.text)  # DET kicks onside -> BUF receives
        self.assertEqual(onside.possession, "home")

    def test_next_play_fields_for_post_play_scoring(self):
        rows, _ = espn_timeline(load("espn/summary_401872932.json"), synthetic=False)
        sack = next(r for r in rows if "sacked" in r.text)
        self.assertEqual((sack.down, sack.yardline_100), (2, 68))
        self.assertEqual((sack.down_after, sack.distance_after, sack.yardline_100_after, sack.possession_after), (3, 18, 76, "away"))
        self.assertEqual(sack.gsr_after, rows[rows.index(sack) + 1].game_seconds_remaining)
        self.assertEqual(rows[-1].gsr_after, 0)
        self.assertIsNone(rows[-1].down_after)  # END GAME: end.down 0 is not a down

    def test_scoring_play_after_state_is_the_kickoff_pending_state(self):
        # ESPN's real TD end block is {team: scorer, down: -1, yardLine: 0, yardsToEndzone: 0}: the
        # scorer "on the goal line" is not a state the next snap sees. The after-state must be the
        # next live play's start (the kickoff row, flipped to the receiver) == the synthetic kickoff row.
        rows, _ = espn_timeline(load("espn/summary_401872932.json"))
        td = next(r for r in rows if "TOUCHDOWN" in r.text and not r.synthetic)
        self.assertEqual((td.possession_after, td.down_after, td.distance_after, td.yardline_100_after), ("home", None, None, 35))  # BUF receives
        ko_row = next(r for r in rows if r.play_class == "kickoff_pending_synth")
        self.assertEqual((td.possession_after, td.down_after, td.distance_after, td.yardline_100_after), (ko_row.possession, ko_row.down, ko_row.distance, ko_row.yardline_100))
        self.assertEqual((td.home_score_after, td.away_score_after), (ko_row.home_score, ko_row.away_score))
        # A walk-off score with no next play falls back to the kickoff-pending state (receiver = scored-against side).
        s = json.loads(json.dumps(load("espn/summary_401872932.json")))
        drives = s["drives"]["previous"]
        td_play = next(p for p in drives[0]["plays"] if "TOUCHDOWN" in p["text"])
        s["drives"]["previous"] = [{"plays": [p for p in drives[0]["plays"] if p["wallclock"] <= td_play["wallclock"]]}]
        rows2, _ = espn_timeline(s, synthetic=False)
        self.assertEqual((rows2[-1].possession_after, rows2[-1].down_after, rows2[-1].yardline_100_after), ("home", None, 35))
        # A dead-end block followed by a timeout row looks past the timeout for the next live start.
        s = json.loads(json.dumps(load("espn/summary_401872932.json")))
        drives = s["drives"]["previous"]
        to = json.loads(json.dumps(next(p for p in drives[1]["plays"] if "Timeout" in p["text"])))  # a real timeout row (start yardsToEndzone 0)
        to["id"], to["wallclock"] = "4018729324331", td_play["wallclock"]  # same second as the TD, sorted after it by id
        drives[0]["plays"].append(to)
        rows3, _ = espn_timeline(s, synthetic=False)
        td3 = next(r for r in rows3 if "TOUCHDOWN" in r.text)
        self.assertEqual(rows3[rows3.index(td3) + 1].play_class, "timeout")
        self.assertEqual((td3.possession_after, td3.down_after, td3.yardline_100_after), ("home", None, 35))

    def test_timeouts_match_hand_counts(self):
        # Q4 of DET@BUF: "Timeout #1 by BUF" (home 3 -> 2) then "Timeout #2 by DET" (away -> 1, the
        # feed's own number wins over our decrement since DET's #1 is outside the trimmed window).
        rows, _ = espn_timeline(load("history/summary_timeouts_trim.json"), synthetic=False)
        before = rows[0]
        self.assertEqual((before.home_timeouts, before.away_timeouts), (3, 3))
        t1 = next(r for r in rows if r.text.startswith("Timeout #1 by BUF"))
        self.assertEqual(t1.play_class, "timeout")
        self.assertEqual((t1.home_timeouts, t1.away_timeouts), (2, 3))  # the timeout row carries the post-timeout state
        t2 = next(r for r in rows if r.text.startswith("Timeout #2 by DET"))
        self.assertEqual((t2.home_timeouts, t2.away_timeouts), (2, 1))
        self.assertEqual((rows[-1].home_timeouts, rows[-1].away_timeouts), (2, 1))

    def test_timeouts_reset_per_half_and_overtime(self):
        def play(period, text="", tid="5"):
            return {"period": {"number": period}, "text": text, "type": {"id": tid}, "start": {"team": {"id": "1"}}}

        plays = [play(1), play(1, "Timeout by BUF at 10:00.", "21"), play(2, "Official Timeout at 07:29.", "21"), play(3), play(3, "Timeout #1 by DET at 05:00.", "21"), play(5), play(5, "Timeout by BUF at 09:00.", "21")]
        tl = timeouts_timeline(plays, "BUF", "DET", "nfl", "1", "2")
        self.assertEqual(tl, [(3, 3), (2, 3), (2, 3), (3, 3), (3, 2), (2, 2), (1, 2)])  # the official timeout changes nothing
        tl_c = timeouts_timeline(plays, "BUF", "DET", "ncaaf", "1", "2")
        self.assertEqual(tl_c[-2:], [(1, 1), (0, 1)])  # college OT: one per period

    def test_synthetic_rows_once_per_touchdown(self):
        rows, _ = espn_timeline(load("espn/summary_401872932.json"))
        synth = [r for r in rows if r.synthetic]
        self.assertEqual([r.play_class for r in synth], ["try_synth", "kickoff_pending_synth"])
        td = next(r for r in rows if "TOUCHDOWN" in r.text)
        self.assertEqual((td.home_score, td.away_score, td.home_score_after, td.away_score_after), (41, 24, 41, 31))
        try_row, ko_row = synth
        self.assertEqual((try_row.home_score, try_row.away_score), (41, 30))   # score before the try: TD only
        self.assertEqual(try_row.possession, "away")                          # DET scored: DET attempts the try
        self.assertEqual(try_row.yardline_100, 15)
        self.assertEqual((ko_row.home_score, ko_row.away_score), (41, 31))    # score after the try
        self.assertEqual(ko_row.possession, "home")                           # BUF receives the kickoff
        self.assertEqual(ko_row.yardline_100, 35)
        self.assertEqual(try_row.ts, td.ts)
        self.assertEqual(rows.index(try_row), rows.index(td) + 1)
        self.assertIsNone(try_row.espn_home_wp)  # ESPN has no entry for a folded try
        self.assertEqual(synthetic_rows(rows[1]), [])  # a non-scoring play adds nothing

    def test_college_overtime_rows_kept_with_no_clock(self):
        s = load("espn/summary_401872932.json")
        s = json.loads(json.dumps(s))
        plays = [p for d in s["drives"]["previous"] for p in d["plays"]]
        ot = json.loads(json.dumps(plays[1]))
        ot["id"], ot["period"], ot["clock"], ot["wallclock"] = "999", {"number": 5}, {"displayValue": "0:00"}, "2026-09-18T03:40:00Z"
        s["drives"]["previous"][-1]["plays"].append(ot)
        rows, _ = espn_timeline(s, sport="ncaaf", synthetic=False)
        r = rows[-1]
        self.assertEqual((r.period, r.game_seconds_remaining, r.play_class, r.overtime), (5, None, "ot", True))
        rows_nfl, _ = espn_timeline(s, sport="nfl", synthetic=False)
        self.assertEqual((rows_nfl[-1].game_seconds_remaining, rows_nfl[-1].overtime), (0, True))  # NFL OT keeps its clock


class CacheTests(unittest.TestCase):
    def test_read_through_cache_skips_the_transport(self):
        with tempfile.TemporaryDirectory() as d:
            h = _history(cache_dir=d)
            bars = h.kalshi_candles("KXNFLGAME-26SEP17DETBUF-BUF", 1789690500, 1789691400)
            self.assertEqual(len(bars), 12)
            self.assertEqual((h.cache_hits, h.cache_misses), (0, 1))
            self.assertTrue(os.path.exists(os.path.join(d, "kalshi", cache_key("candles", "KXNFLGAME-26SEP17DETBUF-BUF", 1789690500, 1789691400, 1) + ".json")))
            h2 = HistoryClient(http=FakeHttp({}), cache_dir=d)  # a transport that would fail on any request
            bars2 = h2.kalshi_candles("KXNFLGAME-26SEP17DETBUF-BUF", 1789690500, 1789691400)
            self.assertEqual(bars2, bars)
            self.assertEqual((h2.cache_hits, h2.cache_misses), (1, 0))
            self.assertEqual(h2.http.calls, [])

    def test_offline_raises_on_a_miss(self):
        with tempfile.TemporaryDirectory() as d:
            h = _history(cache_dir=d, offline=True)
            with self.assertRaises(OfflineCacheMiss):
                h.kalshi_candles("KXNFLGAME-26SEP17DETBUF-BUF", 1789690500, 1789691400)
            self.assertEqual(h.http.calls, [])
            with self.assertRaises(OfflineCacheMiss):
                h.espn_summary(ESPNClient(http=FakeHttp({})), "401872932")

    def test_espn_and_gamma_go_through_the_cache(self):
        with tempfile.TemporaryDirectory() as d:
            espn = ESPNClient(http=FakeHttp({"/summary": load("espn/summary_401872932.json"), "/scoreboard": load("history/espn_week1_scoreboard.json")}))
            h = HistoryClient(http=FakeHttp({"gamma-api.polymarket.com/events": load("history/gamma_event_det_buf.json")}), cache_dir=d)
            h.espn_summary(espn, "401872932")
            h.espn_scoreboard_week(espn, 2026, 1)
            h.cached_http("gamma").get("https://gamma-api.polymarket.com/events", {"slug": "nfl-det-buf-2026-09-18"})
            self.assertEqual(sorted(os.listdir(os.path.join(d, "espn"))), ["scoreboard_nfl_2026_2_1.json", "summary_nfl_401872932.json"])
            self.assertEqual(len(os.listdir(os.path.join(d, "gamma"))), 1)
            off = HistoryClient(http=FakeHttp({}), cache_dir=d, offline=True)
            self.assertEqual(off.espn_summary(ESPNClient(http=FakeHttp({})), "401872932")["header"]["id"], "401872932")
            self.assertEqual(off.cached_http("gamma").get("https://gamma-api.polymarket.com/events", {"slug": "nfl-det-buf-2026-09-18"})[0]["slug"], load("history/gamma_event_det_buf.json")[0]["slug"])


class ReplayTests(unittest.TestCase):
    def test_replay_scores_every_source(self):
        espn = ESPNClient(http=FakeHttp({"/summary": load("espn/summary_401872932.json")}))
        rep = GameReplayer(espn=espn, history=_history())
        res = rep.replay("401872932", rh_contracts={"home": "68a948d7-0087-436b-86ce-78a24d2677a6", "away": "90efc89f-0754-444d-8416-f5caedee185d"}, pm_tokens={"away": "3594"})
        self.assertEqual((res.home, res.away, res.home_won, res.final), ("BUF", "DET", True, "DET 31-41 BUF"))
        self.assertEqual(res.n_plays, 18)
        self.assertEqual(res.arb_minutes["synthetic_rows"], 2)
        self.assertEqual(res.arb_minutes["kalshi_tickers"], {"home": "KXNFLGAME-26SEP17DETBUF-BUF", "away": "KXNFLGAME-26SEP17DETBUF-DET"})
        for k in ("model", "model_after", "espn", "blend"):
            self.assertGreater(res.metrics[k]["n"], 0)
            self.assertGreaterEqual(res.metrics[k]["log_loss"], 0.0)
        # The fixture candles cover only the 12 minutes before kickoff: late plays have no Kalshi bar within max_gap.
        self.assertTrue(all(r.model_p is None or 0 <= r.model_p <= 1 for r in res.rows))
        real = [r for r in res.rows if not r.synthetic]
        self.assertEqual(real[-1].model_p, 1.0)  # final whistle, BUF up 41-31: decided
        self.assertFalse(real[-1].in_play)
        self.assertEqual(res.n_inplay, 17)
        self.assertEqual((res.spread_home, res.spread_source), (-5.5, "pickcenter"))
        self.assertAlmostEqual(res.pregame_kalshi_p, 0.695, places=3)  # last candle before the 00:15Z kickoff
        self.assertEqual(res.kalshi_fee_multiplier, 1.0)
        self.assertEqual(res.bar_mode, "before")
        text = summarize(res)
        self.assertIn("DET 31-41 BUF", text)
        self.assertIn("arb minutes", text)
        self.assertIn("pickcenter", text)


if __name__ == "__main__":
    unittest.main()

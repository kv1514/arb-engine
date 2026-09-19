import unittest

from arb_engine.quant.eventstudy import absorption, event_study, events_from_rows, format_study, load_games, ols, regression_offset, reversal_episodes, summarize

from .helpers import FIXTURES

T0 = 1_789_400_000.0


def _rows(moves):
    """Replay-style rows: one large model move per (secs, pre, post, period); fillers between."""
    rows = []
    for secs, pre, post, period in moves:
        rows.append({"ts": T0 + secs - 30, "model_p": pre, "espn_p": pre, "period": period, "play_class": "scrimmage", "home_score": 0, "away_score": 0})
        rows.append({"ts": T0 + secs, "model_p": pre, "model_after_p": post, "espn_p": pre, "period": period, "play_class": "td", "home_score": 0, "away_score": 0, "text": "big play"})
    return rows


def _linear_tape(moves, full_of, last_pre=-10):
    """Prints flat at ``pre`` until ``t0 + last_pre``, then repriced linearly to pre + full
    over 900 s (``last_pre`` older than the pre-window makes the event unscorable)."""
    tape = []
    for secs, pre, post, _ in moves:
        t0 = T0 + secs
        for k in range(6):
            tape.append((t0 - 600 + 100 * k, pre, 10))
        tape.append((t0 + last_pre, pre, 10))
        for k in range(1, 91):
            tape.append((t0 + 10 * k, pre + full_of(pre, post) * (10 * k / 900.0), 5))
    return tape


class EventStudyTests(unittest.TestCase):
    MOVES = [(1000, 0.30, 0.45, 1), (3000, 0.50, 0.65, 2), (5000, 0.70, 0.85, 3)]

    def test_linear_repricing_absorbed_fractions_and_slopes(self):
        rows = _rows(self.MOVES)
        tape = _linear_tape(self.MOVES, lambda pre, post: post - pre)
        res = event_study(rows, {"kalshi": tape})
        self.assertEqual((res["n_events"], res["n"]), (3, 3))
        for e in res["events"]:
            a = e["venues"]["kalshi"]
            self.assertAlmostEqual(a["absorbed_30"], 30 / 900, places=3)
            self.assertAlmostEqual(a["absorbed_120"], 120 / 900, places=3)
            self.assertAlmostEqual(a["absorbed_300"], 300 / 900, places=3)
            self.assertAlmostEqual(a["absorbed_900"], 1.0, places=6)
            self.assertAlmostEqual(a["first_print_lag"], 10.0)
            self.assertEqual(a["prints_in_window"], 90)
        s = res["summary"]["venues"]["kalshi"]
        self.assertEqual(s["n"], 3)
        self.assertAlmostEqual(s["table"]["all"]["absorbed_120"], 0.1333, places=3)
        self.assertEqual(s["table"]["all"]["sign_agree"], 1.0)
        # Same full move at three price levels: the +2 min price forecasts the +15 min price
        # with slope 1 (MZ) while the 2-minute move is 2/15 of the full one (underreaction 7.5).
        self.assertAlmostEqual(s["mincer_zarnowitz_120"]["slope"], 1.0, places=3)
        self.assertIsNone(s["underreaction_120"]["slope"])   # identical 2-minute moves: no variance to regress on
        varied = [(1000, 0.30, 0.45, 1), (3000, 0.50, 0.70, 2), (5000, 0.70, 0.80, 3)]
        res2 = event_study(_rows(varied), {"kalshi": _linear_tape(varied, lambda pre, post: post - pre)})
        self.assertAlmostEqual(res2["summary"]["venues"]["kalshi"]["underreaction_120"]["slope"], 7.5, places=2)
        self.assertEqual((res["summary"]["regression_offset"], res["summary"]["offsets"]), (120, [30, 120, 300, 900]))
        self.assertEqual(set(s["table"]), {"all", "mover=dog", "mover=fav", "dwp=10-20%", "quarter=Q1", "quarter=Q2", "quarter=Q3", "dog|10-20%|Q1", "fav|10-20%|Q2", "fav|10-20%|Q3"})
        text = format_study(res["summary"])
        self.assertIn("kalshi: n=3", text)
        self.assertIn("mover=dog", text)

    def test_empty_trades_and_missing_windows(self):
        rows = _rows(self.MOVES)
        res = event_study(rows, {})
        self.assertEqual((res["n"], res["n_events"], res["summary"]["venues"]), (0, 3, {}))
        # A venue with no prints after the event scores nothing; one that never moved either.
        before_only = [(T0 + 990, 0.30, 1)]
        flat = [(T0 + 990, 0.30, 1), (T0 + 1100, 0.30, 1), (T0 + 1900, 0.301, 1)]
        res = event_study(rows, {"a": before_only, "b": flat})
        self.assertEqual(res["n"], 0)
        self.assertIsNone(absorption([1.0], [0.5], 2.0))
        self.assertEqual(ols([1.0], [2.0])["slope"], None)

    def test_stale_pre_print_is_not_scored_and_window_pre_widens(self):
        # The last print before a Q3 touchdown is a pre-game one: scoring it would attribute
        # the whole first-half drift to the touchdown, so the default 60 s pre-window drops it.
        moves = [(1000, 0.30, 0.45, 3)]
        rows = _rows(moves)
        stale = _linear_tape(moves, lambda pre, post: post - pre, last_pre=-100)
        self.assertEqual(event_study(rows, {"kalshi": stale})["n"], 0)
        self.assertEqual(event_study(rows, {"kalshi": stale}, window=(-120, 900))["n"], 1)
        ts = [T0 + 900.0, T0 + 1010.0, T0 + 1900.0]
        px = [0.30, 0.40, 0.45]
        self.assertIsNone(absorption(ts, px, T0 + 1000))                        # pre print 100 s old
        self.assertEqual(absorption(ts, px, T0 + 1000, window=(-100, 900))["pre"], 0.30)   # exactly at the bound counts
        self.assertIsNone(absorption(ts, px, T0 + 1000, window=(-99, 900)))

    def test_regressions_follow_the_offsets_argument(self):
        rows = _rows(self.MOVES)
        tape = _linear_tape(self.MOVES, lambda pre, post: post - pre)
        res = event_study(rows, {"kalshi": tape}, offsets=(30, 300, 900))          # no 120: nearest offset, not a KeyError
        s = res["summary"]
        self.assertEqual((s["regression_offset"], s["offsets"]), (30, [30, 300, 900]))
        k = s["venues"]["kalshi"]
        self.assertEqual(set(k) - {"n", "table"}, {"mincer_zarnowitz_30", "underreaction_30"})
        self.assertAlmostEqual(k["mincer_zarnowitz_30"]["slope"], 1.0, places=3)
        self.assertNotIn("absorbed_120", k["table"]["all"])
        self.assertIn("MZ(+30s)", format_study(s, offsets=(30, 300, 900)))
        self.assertEqual(regression_offset((30, 300, 900)), 30)
        self.assertEqual(regression_offset((60, 180)), 60)                           # tie -> the smaller offset
        with self.assertRaises(ValueError):
            regression_offset(())

    def test_events_from_rows_uses_next_row_when_no_after_state(self):
        rows = [{"ts": T0, "model_p": 0.40, "period": 4, "home_score": 3, "away_score": 0}, {"ts": T0 + 20, "model_p": 0.52, "period": 4, "home_score": 3, "away_score": 0}, {"ts": T0 + 40, "model_p": 0.53, "period": 5, "home_score": 3, "away_score": 3}]
        evs = events_from_rows(rows, dwp_min=0.05)
        self.assertEqual(len(evs), 1)
        self.assertEqual((evs[0]["dwp"], evs[0]["mover"], evs[0]["band"], evs[0]["quarter"], evs[0]["scoring"]), (0.12, "dog", "10-20%", "Q4", False))
        self.assertTrue(events_from_rows(rows[1:], dwp_min=0.005)[0]["scoring"])   # score changed on the next row
        self.assertEqual(events_from_rows(rows[1:], dwp_min=0.005)[0]["quarter"], "Q4")

    def test_trimmed_replay_rows_fixture_parses(self):
        games = load_games(str(FIXTURES / "trades/replay_rows_trim.json"))
        self.assertEqual(len(games), 1)
        rows = games[0]["rows"]
        self.assertEqual(len(rows), 12)
        self.assertTrue({"ts", "model_p", "model_after_p", "espn_p", "kalshi_before_p", "kalshi_after_p", "play_class", "slice"} <= set(rows[0]))
        evs = events_from_rows(rows, dwp_min=0.05)
        self.assertEqual([e["play_class"] for e in evs], ["td", "turnover", "td", "turnover"])
        self.assertEqual([e["mover"] for e in evs], ["fav", "dog", "dog", "fav"])
        self.assertEqual([e["quarter"] for e in evs], ["Q1", "Q2", "Q2", "Q4"])
        # Kalshi's own before/after rows make a coarse tape: the study runs end to end.
        tape = [(r["ts"] - 1, r["kalshi_before_p"], 1) for r in rows] + [(r["ts"] + 60, r["kalshi_after_p"], 1) for r in rows]
        res = event_study(rows, {"kalshi": tape}, window=(-60, 900))
        self.assertGreaterEqual(res["n"], 1)
        pooled = summarize(res["events"] + res["events"])
        self.assertEqual(pooled["n_events"], 2 * res["n_events"])


class ReversalTests(unittest.TestCase):
    def test_episodes(self):
        key = "nfl:DEN|KC:2026-09-21"
        mk = lambda ts, hs, as_, pid, clock, **kw: {"ts": ts, "event_key": key, "status": "live", "period": 3, "clock": clock, "home_score": hs, "away_score": as_, "last_play_id": pid, **kw}  # noqa: E731
        ticks = [
            mk(10, 14, 17, "p1", 500), mk(20, 14, 17, "p1", 495),
            mk(30, 14, 24, "p1", 490),                                   # score moved, play id did not
            mk(40, 14, 24, "p2", 485),
            mk(50, 21, 24, "p3", 480, review_pending=1, suspect=1), mk(60, 21, 24, "p3", 480, review_pending=1),
            mk(70, 14, 24, "p4", 470),                                   # reversed
            mk(80, 14, 24, "p4", 475),                                   # clock ran backwards
            {"ts": 5, "event_key": "other", "status": "live", "period": 1, "clock": 900, "home_score": 0, "away_score": 0, "last_play_id": None},
        ]
        eps = reversal_episodes(ticks)
        kinds = {(e["kind"], e["ts_start"], e["n_ticks"]) for e in eps}
        self.assertEqual(kinds, {("score-before-lastplay", 30, 1), ("review-pending", 50, 2), ("suspect", 50, 1), ("score-decrease", 70, 1), ("clock-reversal", 80, 1)})
        self.assertTrue(all(not k.startswith("_") for e in eps for k in e))
        self.assertEqual(reversal_episodes([]), [])

    def test_cooccurring_kinds_merge_into_one_episode_each(self):
        # A StateGuard review window sets suspect and review_pending together on every tick:
        # each kind is one run, not one episode per tick alternating with the other kind.
        key = "nfl:DEN|KC:2026-09-21"
        mk = lambda ts, **kw: {"ts": ts, "event_key": key, "status": "live", "period": 3, "clock": 500 - ts, "home_score": 14, "away_score": 17, "last_play_id": "p1", **kw}  # noqa: E731
        ticks = [mk(10), mk(20, suspect=1, review_pending=1), mk(30, suspect=1, review_pending=1), mk(40, suspect=1, review_pending=1), mk(50), mk(60, suspect=1)]
        eps = reversal_episodes(ticks)
        self.assertEqual([(e["kind"], e["ts_start"], e["ts_end"], e["n_ticks"]) for e in eps], [("review-pending", 20, 40, 3), ("suspect", 20, 40, 3), ("suspect", 60, 60, 1)])
        # A second game's run interleaved in time does not extend the first game's episode.
        other = [dict(t, event_key="other") for t in ticks[1:4]]
        eps = reversal_episodes(ticks + other)
        self.assertEqual(sorted((e["event_key"], e["kind"], e["n_ticks"]) for e in eps), [(key, "review-pending", 3), (key, "suspect", 1), (key, "suspect", 3), ("other", "review-pending", 3), ("other", "suspect", 3)])


if __name__ == "__main__":
    unittest.main()

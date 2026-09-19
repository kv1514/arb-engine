"""Replay input verification — offline: nflverse parity, ESPN WP alignment, college experiment."""

import contextlib
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from arb_engine.quant import feedparity as fp

from .helpers import FIXTURES, load

PBP = FIXTURES / "nflverse" / "pbp_2025_trim.csv.gz"
SUMMARY = "nflverse/espn_summary_401772718_trim.json"  # ARI @ NO, 2025 week 1 (the same game as the pbp rows)
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _script(name: str):
    spec = importlib.util.spec_from_file_location(f"scripts_{name}", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class NflverseTests(unittest.TestCase):
    def test_trimmed_pbp_parses(self):
        games = fp.load_nflverse_pbp(PBP)
        self.assertEqual(list(games), ["2025_01_ARI_NO"])
        g = games["2025_01_ARI_NO"]
        self.assertEqual((g.home, g.away, g.date, g.week), ("NO", "ARI", "2025-09-07", 1))
        self.assertEqual(len(g.plays), 182)
        first = g.plays[1]
        self.assertTrue(first.kickoff)
        self.assertEqual((first.possession, first.yardline_100, first.down), ("away", 35, None))  # receiver's perspective
        # scores are "before the play": the FG row carries 0-0 and 0-3 after; the next row starts at 0-3
        fg = next(p for p in g.plays if "42 yard field goal" in p.text)
        self.assertEqual((fg.home_score, fg.away_score, fg.home_score_after, fg.away_score_after), (0, 0, 0, 3))
        self.assertEqual((g.plays[g.plays.index(fg) + 1].home_score, g.plays[g.plays.index(fg) + 1].away_score), (0, 3))
        # timeouts are "after the play": the timeout row itself is already decremented
        to = next(p for p in g.plays if p.text.startswith("Timeout #1 by NO"))
        self.assertEqual((to.home_timeouts, to.away_timeouts), (2, 3))
        self.assertEqual(g.plays[g.plays.index(to) - 1].home_timeouts, 3)

    def test_filters(self):
        self.assertEqual(fp.load_nflverse_pbp(PBP, weeks=[2]), {})
        self.assertEqual(len(fp.load_nflverse_pbp(PBP, games=["2025_01_ARI_NO"], limit=1)), 1)

    def test_find_game_by_codes_and_eastern_date(self):
        games = fp.load_nflverse_pbp(PBP)
        self.assertIsNotNone(fp.find_nfl_game(games, "NO", "ARZ", "2025-09-07T17:00Z"))  # GSIS alias
        self.assertIsNotNone(fp.find_nfl_game(games, "NO", "ARI", "2025-09-14T17:00Z"))  # wrong date: the pair is unique, so it falls back to codes
        self.assertIsNone(fp.find_nfl_game(games, "ARI", "NO", "2025-09-07T17:00Z"))  # swapped home/away
        self.assertIsNone(fp.find_nfl_game(games, "NO", "ATL", None))


class ParityTests(unittest.TestCase):
    """Real cross-source check: ESPN's summary of ARI @ NO 2025 vs nflverse's rows of the same game."""

    def setUp(self):
        self.summary = load(SUMMARY)
        self.nfl = fp.load_nflverse_pbp(PBP)["2025_01_ARI_NO"]

    def test_espn_rows_shape(self):
        esp, meta = fp.espn_state_plays(self.summary)
        self.assertEqual((meta["home"], meta["away"], meta["event_id"]), ("NO", "ARI", "401772718"))
        self.assertEqual(meta["wallclock_anomalies"], 0)
        self.assertEqual(len(esp), 195)
        ko = esp[0]
        self.assertTrue(ko.kickoff)
        self.assertEqual((ko.possession, ko.yardline_100), ("away", 35))  # flipped to the receiver like nflverse
        self.assertEqual((esp[1].down, esp[1].distance, esp[1].yardline_100, esp[1].possession), (1, 10, 78, "away"))

    def test_full_agreement_except_clock(self):
        esp, _ = fp.espn_state_plays(self.summary)
        pairs = fp.align_plays(esp, self.nfl.plays, clock_tolerance=10)
        rep = fp.agreement(pairs)
        for f in ("down", "distance", "yardline_100", "possession", "home_timeouts", "away_timeouts"):
            self.assertEqual(rep[f]["rate"], 1.0, f)
            self.assertGreater(rep[f]["n"], 150, f)
        # ESPN stamps scoring plays with the post-play clock (a few seconds late)
        self.assertLess(rep["clock"]["rate"], 1.0)
        self.assertGreater(rep["clock"]["rate"], 0.9)
        dis = rep["clock"]["disagreements"]
        self.assertGreaterEqual(sum(1 for d in dis if "TOUCHDOWN" in d["text"] or "field goal" in d["text"]), len(dis) - 1)
        self.assertTrue(all(0 < d["nflverse"] - d["espn"] <= 10 for d in dis))  # ESPN's clock is later, never earlier
        a = rep["_alignment"]
        self.assertGreater(a["id_match_rate"], 0.95)  # the state alignment reproduces the play ids
        self.assertEqual(a["espn_unmatched"], 0)
        self.assertEqual(a["nfl_unmatched"], 3)  # the three extra-point rows ESPN folds into the TD
        self.assertEqual(a["espn_admin"], 17)  # official timeouts ESPN lists as plays

    def test_zero_tolerance_leaves_scoring_plays_unmatched(self):
        esp, _ = fp.espn_state_plays(self.summary)
        rep = fp.agreement(fp.align_plays(esp, self.nfl.plays, clock_tolerance=0))
        self.assertEqual(rep["clock"]["rate"], 1.0)
        self.assertGreater(rep["_alignment"]["espn_unmatched"], 5)

    def test_wallclock_anomaly_is_counted_and_reordered(self):
        s = json.loads(json.dumps(self.summary))
        plays = s["drives"]["previous"][3]["plays"]
        plays[2]["wallclock"] = plays[2]["wallclock"].replace("2025-09-07", "2025-09-08")  # ESPN's +1 day timeout bug
        esp, meta = fp.espn_state_plays(s)
        self.assertEqual(meta["wallclock_anomalies"], 1)
        self.assertEqual([p.play_id for p in esp], [p.play_id for p in fp.espn_state_plays(self.summary)[0]])  # drive order, not wallclock order

    def test_merge_and_format(self):
        esp, _ = fp.espn_state_plays(self.summary)
        rep = fp.agreement(fp.align_plays(esp, self.nfl.plays, clock_tolerance=10))
        rep["_game"] = "ARI@NO"
        pooled = fp.merge_agreement([rep, rep])
        self.assertEqual(pooled["down"]["n"], 2 * rep["down"]["n"])
        self.assertEqual(pooled["_alignment"]["games"], 2)
        text = fp.format_agreement(pooled, "pooled")
        self.assertIn("home_timeouts", text)
        self.assertIn("clock disagreements", text)


class AlignmentTests(unittest.TestCase):
    """Hand-built ESPN rows from the nflverse rows: an off-by-one, a dropped row and a planted disagreement."""

    def setUp(self):
        nfl = fp.load_nflverse_pbp(PBP)["2025_01_ARI_NO"].plays[:60]
        self.nfl = nfl
        self.esp = [replace(p, source="espn", play_id="401772718" + p.play_id) for p in nfl if not fp.is_admin_text(p.text)]

    def test_identity(self):
        rep = fp.agreement(fp.align_plays(self.esp, self.nfl))
        self.assertEqual(rep["_alignment"]["matched"], len(self.esp))
        self.assertEqual(rep["_alignment"]["id_match_rate"], 1.0)
        for f in fp.FIELDS:
            self.assertIn(rep[f]["rate"], (1.0, None))

    def test_off_by_one_and_dropped_row(self):
        esp = list(self.esp)
        esp[10], esp[11] = esp[11], esp[10]  # ESPN lists two plays in the other order
        del esp[30]  # and lacks one
        pairs = fp.align_plays(esp, self.nfl)
        rep = fp.agreement(pairs)
        self.assertEqual(rep["_alignment"]["matched"], len(esp))
        self.assertEqual(rep["_alignment"]["id_match_rate"], 1.0)
        self.assertGreaterEqual(rep["_alignment"]["shifted"], 1)
        self.assertEqual(rep["_alignment"]["nfl_unmatched"], 1)
        self.assertEqual(rep["_alignment"]["espn_unmatched"], 0)

    def test_planted_disagreement_is_reported(self):
        esp = list(self.esp)
        target = next(p for p in esp if p.down == 2)
        esp[esp.index(target)] = replace(target, down=3, home_timeouts=2)
        rep = fp.agreement(fp.align_plays(esp, self.nfl))
        self.assertEqual(rep["down"]["agree"], rep["down"]["n"] - 1)
        d = rep["down"]["disagreements"]
        self.assertEqual(len(d), 1)
        self.assertEqual((d[0]["espn"], d[0]["nflverse"]), (3, 2))
        self.assertEqual(d[0]["espn_play"], target.play_id)
        self.assertEqual(rep["home_timeouts"]["disagreements"][0]["espn"], 2)
        self.assertAlmostEqual(rep["home_timeouts"]["rate"], (rep["home_timeouts"]["n"] - 1) / rep["home_timeouts"]["n"], places=4)

    def test_admin_row_cannot_steal_a_twin(self):
        esp = list(self.esp)
        esp.insert(12, replace(esp[12], play_id="x", text="Official Timeout at 11:27."))
        rep = fp.agreement(fp.align_plays(esp, self.nfl))
        self.assertEqual(rep["_alignment"]["id_match_rate"], 1.0)
        self.assertEqual(rep["_alignment"]["espn_admin"], 1)

    def test_disagreement_list_is_capped(self):
        esp = [replace(p, down=(p.down or 0) + 1 if p.down else None) for p in self.esp]
        rep = fp.agreement(fp.align_plays(esp, self.nfl), max_disagreements=5)
        self.assertEqual(len(rep["down"]["disagreements"]), 5)
        self.assertEqual(rep["down"]["agree"], 0)


class TimeoutTests(unittest.TestCase):
    def test_count_with_half_reset_overtime_and_gsis_codes(self):
        texts = [
            (1, "B.Grupe kicks 65 yards from NO 35 to ARZ 0.", "53"),
            (1, "Timeout #1 by ARZ at 05:07.", "21"),  # GSIS code for ARI
            (2, "Timeout #2 by NO at 00:20.", "21"),  # numbered #2 with no #1 row seen: the number wins (a dropped row)
            (2, "Timeout #2 by NO at 00:15.", "21"),  # duplicate number: still a timeout (the count wins)
            (3, "(15:00) kickoff", "53"),  # half reset
            (4, "(:32) K.Murray pass short middle. New Orleans challenged the pass completion ruling, and the play was Upheld.", "24"),  # lost challenge
            (4, "The Replay Official reviewed the fumble ruling, and the play was REVERSED.", "5"),  # booth review: free
            (5, "kickoff", "53"),  # overtime: 2 each
            (5, "Timeout #1 by ARZ at 03:00.", "21"),
        ]
        out = fp.timeouts_after_play(texts, home="NO", away="ARI")
        self.assertEqual(out, [(3, 3), (3, 2), (1, 2), (0, 2), (3, 3), (2, 3), (2, 3), (2, 2), (2, 1)])

    def test_exact_match_rate_against_nflverse(self):
        summary = load(SUMMARY)
        esp, _ = fp.espn_state_plays(summary)
        nfl = fp.load_nflverse_pbp(PBP)["2025_01_ARI_NO"].plays
        rep = fp.agreement(fp.align_plays(esp, nfl, clock_tolerance=10), fields=("home_timeouts", "away_timeouts"))
        self.assertEqual((rep["home_timeouts"]["rate"], rep["away_timeouts"]["rate"]), (1.0, 1.0))
        self.assertEqual(rep["home_timeouts"]["n"], rep["_alignment"]["matched"])
        # the "rows" source is the replay timeline's own timeout count (P03): it must agree with
        # nflverse on every aligned play too, so the two independent counts corroborate each other
        esp2, _ = fp.espn_state_plays(summary, timeouts="rows")
        rep2 = fp.agreement(fp.align_plays(esp2, nfl, clock_tolerance=10), fields=("home_timeouts", "away_timeouts"))
        self.assertEqual(rep2["home_timeouts"]["n"], rep["_alignment"]["matched"])
        self.assertEqual((rep2["home_timeouts"]["rate"], rep2["away_timeouts"]["rate"]), (1.0, 1.0))


class WpAlignmentTests(unittest.TestCase):
    def test_classifier_post_and_pre(self):
        # post: ESPN's entry has already moved with the score (jump between prev and this)
        post = [{"espn": 0.70, "espn_prev": 0.50, "espn_next": 0.71, "pre": 0.5, "post": 0.7} for _ in range(8)]
        pre = [{"espn": 0.50, "espn_prev": 0.49, "espn_next": 0.70, "pre": 0.5, "post": 0.7} for _ in range(8)]
        self.assertEqual(fp.classify_wp_alignment(post)["alignment"], "post")
        self.assertEqual(fp.classify_wp_alignment(pre)["alignment"], "pre")
        c = fp.classify_wp_alignment(post)
        self.assertEqual((c["n"], c["share_jump_before"], c["model_share_closer_to_post"]), (8, 1.0, 1.0))
        self.assertEqual(fp.classify_wp_alignment(post[:3])["alignment"], "unknown")  # too few
        self.assertEqual(fp.classify_wp_alignment(post[:4] + pre[:4])["alignment"], "unknown")  # split
        self.assertEqual(fp.classify_wp_alignment([])["n"], 0)

    def test_samples_on_the_summary(self):
        summary = load(SUMMARY)
        calls = []

        def wp_fn(**kw):
            calls.append(kw)
            return 0.5 + 0.01 * (kw["home_score"] - kw["away_score"])

        res = fp.wp_alignment_samples(summary, wp_fn=wp_fn, spread_home=-6.0)
        scoring = sum(1 for d in summary["drives"]["previous"] for p in d["plays"] if p.get("scoringPlay"))
        self.assertEqual(len(res["samples"]), scoring)
        self.assertEqual(res["fallback_plays"], 6)  # ESPN's official-timeout rows have no WP entry (2025 summaries)
        s = res["samples"][0]
        self.assertEqual((s["score_before"], s["score_after"]), ("0-0", "3-0"))
        self.assertLess(s["pre"], s["post"] + 1)  # away scored: post-state home WP is lower
        self.assertGreater(s["pre"], s["post"])
        self.assertIsNotNone(s["espn_prev"])
        cls = fp.classify_wp_alignment(res["samples"])
        self.assertEqual(cls["alignment"], "post")  # ESPN's own entries jump before the scoring play's entry
        self.assertTrue(all(c["vegas_spread_home"] == -6.0 for c in calls))
        self.assertEqual(fp.wp_alignment_samples(summary, wp_fn=wp_fn)["samples"][0]["pre"], 0.5)  # no pickcenter on old summaries: spread 0

    def test_receive_2h_ko_home_is_the_opening_kicker(self):
        # ARI @ NO: NO (home) kicks off to open ('B.Grupe kicks 65 yards from NO 35'), so NO
        # receives the second-half kickoff; ESPN's start.team on the kickoff row is the kicker.
        summary = load(SUMMARY)
        rows, texts, type_ids, _meta = fp.ordered_rows(summary)
        self.assertEqual(rows[0].possession, "home")
        self.assertIs(fp.receive_2h_ko_home(rows, texts, type_ids), True)
        calls = []
        fp.wp_alignment_samples(summary, wp_fn=lambda **kw: calls.append(kw) or 0.5, spread_home=0.0)
        self.assertTrue(calls and all(c["receive_2h_ko_home"] is True for c in calls))
        # an admin row before the kickoff does not decide the flag; away kicking flips it;
        # no kickoff row (or no possession on it) leaves it None for the model to average
        ko = replace(rows[0], possession="away")
        admin = replace(rows[0], play_id="x", possession=None, text="Game start")
        self.assertIs(fp.receive_2h_ko_home([admin, ko]), False)
        self.assertIs(fp.receive_2h_ko_home([admin, replace(ko, possession=None)]), None)
        self.assertIs(fp.receive_2h_ko_home([replace(ko, period=3)]), None)  # 2H kickoff is not the opener
        self.assertIs(fp.receive_2h_ko_home([replace(ko, text="(15:00) J.Doe pass short left to A.Bee for 5 yards")]), None)
        self.assertIs(fp.receive_2h_ko_home([]), None)

    def test_recorded_week1_answer(self):
        rec = load("results/espn_wp_alignment_p04.json")
        self.assertEqual(rec["pooled"]["alignment"], "post")
        self.assertGreaterEqual(rec["pooled"]["n"], 100)
        self.assertGreater(rec["pooled"]["share_jump_before"], 0.8)
        self.assertEqual(rec["pooled"]["fallback_share"], 0.0)


class RecordedParityTests(unittest.TestCase):
    def test_week1_record_meets_the_timeouts_bar(self):
        rec = load("results/feed_parity_w1_p04.json")
        self.assertEqual(rec["pooled"]["_alignment"]["games"], 16)
        for f in ("home_timeouts", "away_timeouts"):
            self.assertGreaterEqual(rec["pooled"][f]["rate"], 0.95, f)
            self.assertGreater(rec["pooled"][f]["n"], 2000)
        for f in ("down", "distance", "yardline_100", "possession"):
            self.assertGreaterEqual(rec["pooled"][f]["rate"], 0.99, f)
        self.assertLess(rec["pooled"]["clock"]["rate"], 0.99)  # ESPN's post-play clock on scoring plays
        self.assertGreater(rec["pooled"]["wallclock_anomalies"], 0)  # the +1 day timeout rows (P03)


class CachedJsonTests(unittest.TestCase):
    def test_read_through_and_offline(self):
        d = tempfile.mkdtemp()
        try:
            n = [0]

            def fetch():
                n[0] += 1
                return {"a": 1}

            self.assertEqual(fp.cached_json(d, "k", fetch), {"a": 1})
            self.assertEqual(fp.cached_json(d, "k", fetch, offline=True), {"a": 1})
            self.assertEqual(n[0], 1)
            with self.assertRaises(FileNotFoundError):
                fp.cached_json(d, "missing", fetch, offline=True)
            self.assertEqual(fp.cached_json(None, "k", fetch), {"a": 1})  # no cache dir: always fetch
            self.assertEqual(n[0], 2)
        finally:
            shutil.rmtree(d)


class CollegeExperimentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ce = _script("college_experiment")

    def test_paired_diff_zero_on_identical(self):
        d = self.ce.paired_diff({"g1": [0.0, 0.0, 0.0], "g2": [0.0]}, B=50)
        self.assertEqual((d["mean"], d["lo90"], d["hi90"], d["frac_positive"], d["n_games"], d["n_rows"]), (0.0, 0.0, 0.0, 0.0, 2, 4))
        self.assertEqual(self.ce.paired_diff({}, B=10)["n_games"], 0)
        d = self.ce.paired_diff({"g1": [0.1, 0.1], "g2": [-0.3], "g3": [0.2]}, B=200, seed=1)
        self.assertAlmostEqual(d["mean"], 0.025, places=5)  # row-weighted
        self.assertLessEqual(d["lo90"], d["mean"])
        self.assertGreaterEqual(d["hi90"], d["mean"])

    def test_rescale_and_ot_mapping(self):
        self.assertEqual(self.ce.rescale_spread(-24.0, 13.5 / 16, 19.5), -19.5)
        self.assertAlmostEqual(self.ce.rescale_spread(-8.0, 0.5, None), -4.0)
        self.assertIsNone(self.ce.rescale_spread(None, 0.5, 19.5))
        self.assertEqual(self.ce.variant_gsr(5, 0, 0, 120), 120)
        self.assertEqual(self.ce.variant_gsr(5, 0, 0, None), 0)
        self.assertEqual(self.ce.variant_gsr(4, 61, 61, 120), 61)

    def test_score_summary_and_experiment(self):
        summary = load(SUMMARY)

        def wp_fn(**kw):  # a stub that depends on the spread and the clock only
            return min(0.99, max(0.01, 0.5 - 0.02 * kw["vegas_spread_home"] * kw["game_seconds_remaining"] / 3600.0))

        self.assertIsNone(self.ce.score_summary(summary, wp_fn=wp_fn)["spread"])  # no pickcenter on this old summary
        sc = self.ce.score_summary(summary, scale=1.0, clamp=None, ot_seconds=120, wp_fn=wp_fn, spread_home=-6.0)
        self.assertTrue(sc["rows"])
        self.assertEqual(sc["spread"], -6.0)
        self.assertTrue(all(r["p"]["baseline"] == r["p"]["rescale"] == r["p"]["clamp"] == r["p"]["rescale_ot"] for r in sc["rows"]))  # scale 1, no clamp, no OT: identical
        exp = self.ce.experiment([sc], B=50)
        self.assertEqual(exp["regulation"]["vs_baseline"]["rescale"]["mean"], 0.0)
        self.assertEqual(exp["overtime"]["n_rows"], 0)
        sc2 = self.ce.score_summary(summary, scale=0.5, clamp=None, ot_seconds=120, wp_fn=wp_fn, spread_home=-6.0)
        self.assertTrue(all(r["p"]["baseline"] != r["p"]["rescale"] for r in sc2["rows"] if r["p"]["baseline"] not in (0.01, 0.99)))
        text = self.ce.format_experiment(self.ce.experiment([sc2], B=20), {"scale": 0.5, "clamp": 19.5, "ot_seconds": 120})
        self.assertIn("vs baseline", text)

    def test_rematch_stays_two_bootstrap_clusters(self):
        # the same away@home label in two pooled weeks is two games: n_games and the cluster
        # bootstrap must key on the event id, not the label
        rows = [{"period": 1, "ot": False, "y": 1, "p": {v: 0.6 for v in self.ce.VARIANTS}}]
        rows[0]["p"]["rescale"] = 0.7
        a = {"id": "401", "home": "UGA", "away": "ALA", "rows": rows}
        b = {"id": "402", "home": "UGA", "away": "ALA", "rows": [dict(rows[0], p={v: 0.5 for v in self.ce.VARIANTS})]}
        exp = self.ce.experiment([a, b], B=20)["all"]
        self.assertEqual(exp["n_games"], 2)
        self.assertEqual(exp["vs_baseline"]["rescale"]["n_games"], 2)
        self.assertEqual(exp["vs_baseline"]["rescale"]["n_rows"], 2)
        self.assertEqual(self.ce.score_summary(load(SUMMARY), wp_fn=lambda **kw: 0.5, spread_home=0.0)["id"], "401772718")
        c = {"home": "UGA", "away": "ALA", "rows": rows}  # no id: object identity still separates the clusters
        self.assertEqual(self.ce.experiment([c, dict(c)], B=10)["all"]["vs_baseline"]["rescale"]["n_games"], 2)

    def test_recorded_result_shape(self):
        rec = load("results/college_experiment_p04.json")
        self.assertEqual(rec["params"]["scale"], 13.5 / 16)
        for slice_name in ("regulation", "overtime", "all"):
            r = rec["results"][slice_name]
            for v in ("clamp", "rescale", "rescale_ot"):
                d = r["vs_baseline"][v]
                self.assertEqual(set(d) >= {"mean", "lo90", "hi90", "frac_positive", "n_games"}, True)
        reg = rec["results"]["regulation"]["vs_baseline"]["rescale"]
        self.assertLessEqual(reg["lo90"], 0.0)  # the interval straddles zero: no case for R27 from the rescale
        self.assertGreaterEqual(reg["hi90"], 0.0)
        self.assertLess(rec["results"]["overtime"]["log_loss"]["rescale_ot"], rec["results"]["overtime"]["log_loss"]["baseline"])


class ScriptTests(unittest.TestCase):
    """The three by-hand scripts run end to end on the fixtures with --offline (no network)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copy(FIXTURES / SUMMARY, self.tmp / "summary_401772718.json")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_feed_parity_script(self):
        mod = _script("backtest_live_feed")
        out = self.tmp / "parity.txt"
        with contextlib.redirect_stdout(io.StringIO()):
            rc = mod.main(["--season", "2025", "--espn", "401772718", "--pbp", str(PBP), "--cache-dir", str(self.tmp), "--offline", "--out", str(out)])
        self.assertEqual(rc, 0)
        text = out.read_text()
        self.assertIn("home_timeouts", text)
        rep = json.loads(out.with_suffix(".json").read_text())
        self.assertEqual(rep["pooled"]["home_timeouts"]["rate"], 1.0)
        self.assertEqual(rep["pooled"]["_alignment"]["games"], 1)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mod.main(["--pbp", str(self.tmp / "nope.csv.gz")]), 2)

    def test_wp_alignment_script(self):
        mod = _script("check_espn_wp_alignment")
        out = self.tmp / "wp.json"
        with contextlib.redirect_stdout(io.StringIO()):
            rc = mod.main(["--espn", "401772718", "--cache-dir", str(self.tmp), "--offline", "--out", str(out)])
        self.assertEqual(rc, 0)
        rec = json.loads(out.read_text())
        self.assertEqual(rec["pooled"]["alignment"], "post")
        self.assertEqual(rec["games"][0]["game"], "ARI@NO")

    def test_college_script(self):
        mod = _script("college_experiment")
        out = self.tmp / "college.json"
        with contextlib.redirect_stdout(io.StringIO()):
            rc = mod.main(["--espn", "401772718", "--cache-dir", str(self.tmp), "--offline", "--workers", "1", "--bootstrap", "20", "--out", str(out)])
        self.assertEqual(rc, 0)
        rec = json.loads(out.read_text())
        self.assertEqual(rec["games"], 1)
        self.assertIsNotNone(rec["results"]["regulation"]["log_loss"]["baseline"])


if __name__ == "__main__":
    unittest.main()

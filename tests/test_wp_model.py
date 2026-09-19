"""In-game NFL win-probability model: exported JSON loads and behaves sensibly."""

from __future__ import annotations

import json
import unittest
from importlib import resources

import os
from pathlib import Path

from arb_engine.models.wp import (
    FEATURES,
    WinProbModel,
    home_win_probability,
    kickoff_state_wp,
    kneel_out_wp,
    load_model,
    load_rules,
    neutral_yardline,
    posteam_features,
    predict_posteam_wp,
    pregame_home_probability,
    pregame_spread_table,
    spread_from_pregame_probability,
    time_features,
    try_state_wp,
    two_point_attempt,
)

# docs/MODEL.md pre-game table (P(home) by the home team's ESPN-sign spread).
MODEL_MD_PREGAME = {-10: 0.792, -7: 0.714, -3: 0.572, -1: 0.500, 0: 0.485, 1: 0.471, 3: 0.400, 7: 0.257}
P05_FIXTURE = Path(__file__).parent / "fixtures" / "results" / "week1_p05.json"


class ExportedModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = load_model()

    def _home(self, **kw) -> float:
        kw.setdefault("model", self.model)
        return home_win_probability(**kw)

    def test_export_shape_and_metadata(self):
        self.assertEqual(tuple(self.model.features), FEATURES)
        self.assertIn(self.model.model_type, ("xgboost", "logistic"))
        if self.model.model_type == "xgboost":
            self.assertGreaterEqual(self.model.n_trees, 50)
        meta = self.model.meta
        self.assertIn("heldout", meta)
        self.assertLess(meta["heldout"]["model"]["log_loss"], 0.6)
        # The packaged meta file mirrors the metrics used in docs/MODEL.md.
        with resources.files("arb_engine.data").joinpath("nfl_wp_model.meta.json").open("r", encoding="utf-8") as f:
            side = json.load(f)
        self.assertAlmostEqual(side["metrics"]["model"]["log_loss"], meta["heldout"]["model"]["log_loss"], places=6)
        self.assertLess(side["walker_parity_max_abs_diff"], 1e-4)

    def test_predictions_are_probabilities(self):
        states = [
            dict(score_differential=0, game_seconds_remaining=3600),
            dict(score_differential=-35, game_seconds_remaining=120, down=4, ydstogo=25, yardline_100=95),
            dict(score_differential=40, game_seconds_remaining=1, down=1, ydstogo=1, yardline_100=1),
            dict(score_differential=3, game_seconds_remaining=1800, half_seconds_remaining=0, receive_2h_ko=1),
        ]
        for st in states:
            p = predict_posteam_wp(posteam_features(**st), self.model)
            self.assertGreater(p, 0.0)
            self.assertLess(p, 1.0)
        self.assertRaises(KeyError, predict_posteam_wp, {"score_differential": 1}, self.model)

    def test_more_lead_means_higher_wp(self):
        prev = 0.0
        for lead in (-21, -14, -7, -3, 0, 3, 7, 14, 21):
            p = self._home(home_score=20 + lead, away_score=20, game_seconds_remaining=900, possession="home", down=1, distance=10, yardline_100=60)
            self.assertGreater(p, prev, f"lead {lead}")
            prev = p

    def test_bigger_lead_never_lowers_the_leader(self):
        # Scan leads -28..28 across clocks, spreads and both possessions: P(home) must be
        # non-decreasing in the home margin (the raw trees dip by up to ~10 points).
        for poss in ("home", "away"):
            for gsr in (2700, 900, 120):
                for spread in (-7, 0, 7):
                    prev = -1.0
                    for lead in range(-28, 29):
                        p = self._home(home_score=20 + lead, away_score=20, game_seconds_remaining=gsr, possession=poss, down=1, distance=10, yardline_100=60, vegas_spread_home=spread)
                        self.assertGreaterEqual(p, prev - 1e-12, f"poss {poss} gsr {gsr} spread {spread} lead {lead}: {p} < {prev}")
                        prev = p
        # Dead-ball state is an average of monotone perspectives, so it is monotone too.
        prev = -1.0
        for lead in range(-28, 29):
            p = self._home(home_score=20 + lead, away_score=20, game_seconds_remaining=2700, vegas_spread_home=-3)
            self.assertGreaterEqual(p, prev - 1e-12, f"neutral lead {lead}")
            prev = p

    def test_monotone_guard_fixes_the_q2_dip(self):
        # Away up 16 vs 17, away ball, Q2 (2700 s), home -7: the raw export *raises* P(home)
        # by ~10 points for the bigger away lead; the guard clamps it to the 16-point value.
        st = dict(game_seconds_remaining=2700, possession="away", down=1, distance=10, yardline_100=60, vegas_spread_home=-7)
        raw16 = self._home(home_score=20, away_score=36, monotone=False, **st)
        raw17 = self._home(home_score=20, away_score=37, monotone=False, **st)
        self.assertGreater(raw17 - raw16, 0.05, "the shipped export no longer dips here; keep the guard anyway")
        self.assertAlmostEqual(self._home(home_score=20, away_score=36, **st), raw16, places=12)
        self.assertLessEqual(self._home(home_score=20, away_score=37, **st), raw16 + 1e-12)
        # Home leading with the ball, home +7: same shape from the other side.
        st = dict(game_seconds_remaining=2700, possession="home", down=1, distance=10, yardline_100=60, vegas_spread_home=7)
        self.assertGreaterEqual(self._home(home_score=37, away_score=20, **st), self._home(home_score=36, away_score=20, **st) - 1e-12)
        # A tie is never touched by the guard.
        self.assertAlmostEqual(self._home(home_score=20, away_score=20, **st), self._home(home_score=20, away_score=20, monotone=False, **st), places=12)

    def test_less_time_with_a_lead_means_higher_wp(self):
        prev = 0.0
        for gsr in (3000, 2400, 1500, 900, 300, 60):
            p = self._home(home_score=24, away_score=17, game_seconds_remaining=gsr, possession="home", down=1, distance=10, yardline_100=50)
            self.assertGreater(p, prev, f"gsr {gsr}")
            prev = p
        # Trailing team runs out of time: WP falls as the clock drains.
        prev = 1.0
        for gsr in (3000, 1500, 300, 60):
            p = self._home(home_score=17, away_score=24, game_seconds_remaining=gsr, possession="home", down=1, distance=10, yardline_100=50)
            self.assertLess(p, prev, f"gsr {gsr}")
            prev = p

    def test_pregame_spread_ordering(self):
        pre = {s: self._home(home_score=0, away_score=0, game_seconds_remaining=3600, vegas_spread_home=s) for s in (-10, -7, -3, 0, 3, 7)}
        self.assertGreater(pre[-10], pre[-7])
        self.assertGreater(pre[-7], pre[-3])
        self.assertGreater(pre[-3], pre[0])
        self.assertGreater(pre[0], pre[3])
        self.assertGreater(pre[3], pre[7])
        # Close to the usual spread → probability conversions (about 0.55-0.65 for -3, 0.68-0.80 for -7).
        self.assertTrue(0.55 <= pre[-3] <= 0.66, pre[-3])
        self.assertTrue(0.66 <= pre[-7] <= 0.82, pre[-7])
        self.assertTrue(0.34 <= pre[3] <= 0.46, pre[3])
        # Pick'em with nobody in possession is essentially a coin flip (home edge allowed).
        self.assertTrue(0.45 <= pre[0] <= 0.58, pre[0])

    def test_decided_late_game(self):
        p = self._home(home_score=31, away_score=10, game_seconds_remaining=90, possession="home", down=1, distance=10, yardline_100=60, home_timeouts=3, away_timeouts=0)
        self.assertGreater(p, 0.97)
        q = self._home(home_score=10, away_score=31, game_seconds_remaining=90, possession="away", down=1, distance=10, yardline_100=60)
        self.assertLess(q, 0.03)
        # Dead ball with the same score, still decided.
        self.assertGreater(self._home(home_score=31, away_score=10, game_seconds_remaining=90), 0.97)

    def test_possession_and_field_position_matter(self):
        tied = dict(home_score=20, away_score=20, game_seconds_remaining=120)
        with_ball_deep = self._home(possession="home", down=1, distance=10, yardline_100=20, **tied)
        with_ball_own = self._home(possession="home", down=1, distance=10, yardline_100=90, **tied)
        without_ball = self._home(possession="away", down=1, distance=10, yardline_100=20, **tied)
        self.assertGreater(with_ball_deep, with_ball_own)
        self.assertGreater(with_ball_own, without_ball)
        # Away perspective is the mirror image of the home perspective.
        home_pov = self._home(home_score=21, away_score=17, game_seconds_remaining=600, possession="home", down=2, distance=5, yardline_100=40, vegas_spread_home=-3)
        away_pov = self._home(home_score=17, away_score=21, game_seconds_remaining=600, possession="away", down=2, distance=5, yardline_100=40, vegas_spread_home=3)
        self.assertAlmostEqual(home_pov, 1 - away_pov, delta=0.03)  # is_home differs, so only approximately

    def test_neutral_state_is_average_of_perspectives(self):
        p = self._home(home_score=14, away_score=10, game_seconds_remaining=1000)
        as_home = self._home(home_score=14, away_score=10, game_seconds_remaining=1000, possession="home", down=1, distance=10, yardline_100=75)
        as_away = self._home(home_score=14, away_score=10, game_seconds_remaining=1000, possession="away", down=1, distance=10, yardline_100=75)
        self.assertAlmostEqual(p, (as_home + as_away) / 2, places=9)


class FeatureMathTests(unittest.TestCase):
    def test_time_features_match_nflfastr(self):
        st, ratio = time_features(7, 3600, 3)
        self.assertAlmostEqual(st, 3.0)
        self.assertAlmostEqual(ratio, 7.0)
        st, ratio = time_features(7, 0, 3)
        self.assertAlmostEqual(st, 3 * 0.01831563888, places=8)
        self.assertAlmostEqual(ratio, 7 / 0.01831563888, places=4)

    def test_half_seconds_default(self):
        self.assertEqual(posteam_features(score_differential=0, game_seconds_remaining=2000)["half_seconds_remaining"], 200.0)
        self.assertEqual(posteam_features(score_differential=0, game_seconds_remaining=1000)["half_seconds_remaining"], 1000.0)

    def test_walker_handles_missing_branch_and_logistic_payload(self):
        # One stump: score_differential < 0.5 -> -1, else +1, missing -> +1.
        payload = {
            "model_type": "xgboost", "features": list(FEATURES), "base_score": 0.5,
            "trees": [{"f": [0, -1, -1], "t": [0.5, 0, 0], "y": [1, -1, -1], "n": [2, -1, -1], "m": [2, -1, -1], "v": [0, -1.0, 1.0]}],
        }
        m = WinProbModel(payload)
        base = posteam_features(score_differential=0, game_seconds_remaining=100)
        self.assertAlmostEqual(m.predict_posteam_wp(base), 1 / (1 + 2.718281828459045))
        self.assertAlmostEqual(m.predict_posteam_wp({**base, "score_differential": 3}), 1 / (1 + 2.718281828459045 ** -1))
        self.assertAlmostEqual(m.predict_posteam_wp({**base, "score_differential": float("nan")}), 1 / (1 + 2.718281828459045 ** -1))
        n = len(FEATURES)
        lr = {
            "model_type": "logistic", "features": list(FEATURES),
            "logistic": {"mean": [0.0] * n, "std": [1.0] * n, "intercept": 0.0, "coef": [0.5, 0.25], "terms": [[0], [0, 0]]},
        }
        self.assertAlmostEqual(WinProbModel(lr).predict_posteam_wp({**base, "score_differential": 2}), 1 / (1 + 2.718281828459045 ** -2))



def offline_p05_metrics(model) -> dict:
    """Metrics-only summary of the P05 corrections on synthetic states (no network).

    This is the offline half of the item's acceptance: the replay per-class table
    (kalshi_before alignment, STEAL-qualifying counts) needs P03 and a cached week and is
    run by hand; the numbers here are reproducible from the shipped model alone and are
    what tests/fixtures/results/week1_p05.json holds.
    """
    H = lambda **k: home_win_probability(model=model, **k)  # noqa: E731
    r6 = lambda v: round(float(v), 6)  # noqa: E731
    out: dict = {"item": "P05", "model": model.meta.get("trained_at"), "rules": load_rules().get("version")}
    # 1) pre-game spread inversion against the MODEL.md table.
    errs = {str(s): r6(spread_from_pregame_probability(p, model) - s) for s, p in MODEL_MD_PREGAME.items()}
    tab = pregame_spread_table(model)
    out["spread_inversion"] = {"errors_pts": errs, "max_abs_error_pts": r6(max(abs(v) for v in errs.values())), "table_monotone": bool(tab["monotone"]), "grid_points": len(tab["spreads"])}
    # 2) era neutral yardline: pre-game shift (should be ~0, the perspectives cancel) and a late one-score kickoff-pending shift.
    seasons = (2023, 2024, 2025)
    late = dict(home_score=24, away_score=21, game_seconds_remaining=120, possession="away", play_class="kickoff")
    out["neutral_yardline"] = {
        "yardline_by_season": {str(y): neutral_yardline(y) for y in seasons},
        "pregame_delta_at_minus3": {str(y): r6(pregame_home_probability(-3, model, season=y) - pregame_home_probability(-3, model)) for y in seasons},
        "late_one_score_kickoff_pending_delta": {str(y): r6(H(season=y, **late) - H(season=None, **late)) for y in seasons},
    }
    # 3) kickoff-pending synthetic grid: receiver at the 2025 start vs the pre-P05 receiver-at-own-25 state.
    diffs, absd, n = 0.0, 0.0, 0
    for margin in (-8, -7, -6, -4, -3, -1, 1, 3, 4, 6, 7, 8):
        for gsr in (1800, 900, 300, 120, 60):
            for receiver in ("home", "away"):
                st = dict(home_score=20 + margin, away_score=20, game_seconds_remaining=gsr, possession=receiver)
                old = H(down=1, distance=10, yardline_100=75, **st)
                new = H(play_class="kickoff", season=2025, p_onside=0.0, **st)
                sign = 1 if receiver == "home" else -1
                diffs += sign * (new - old)
                absd += abs(new - old)
                n += 1
    out["kickoff_pending_synth"] = {"rows": n, "mean_receiver_shift": r6(diffs / n), "mean_abs_shift": r6(absd / n)}
    # 4) try-state synthetic grid: bracketing by the two kickoff-pending states and the shift from the old 1st-and-goal-from-2 reading.
    viol, absd, n, two = 0, 0.0, 0, 0
    for margin in range(-14, 15):
        for gsr in (1500, 700, 240, 60):
            st = dict(home_score=20 + margin, away_score=20, game_seconds_remaining=gsr)
            pts = 2 if two_point_attempt(margin, gsr) else 1
            two += pts == 2
            lo = kickoff_state_wp(kicker="home", model=model, **st)
            hi = kickoff_state_wp(kicker="home", model=model, home_score=20 + margin + pts, away_score=20, game_seconds_remaining=gsr)
            t = H(possession="home", play_class="try", **st)
            if not (min(lo, hi) - 1e-12 <= t <= max(lo, hi) + 1e-12):
                viol += 1
            absd += abs(t - H(possession="home", down=1, distance=2, yardline_100=2, **st))
            n += 1
    out["try_synth"] = {"rows": n, "two_point_rows": two, "bracket_violations": viol, "mean_abs_shift_vs_first_and_goal_from_2": r6(absd / n)}
    # 5) overtime guard: 0:00 with a margin no longer short-circuits; mid-OT never hits 0/1.
    exact, n = 0, 0
    for margin in (-7, -3, 3, 7):
        for gsr in (0, 1, 30, 120, 600):
            for poss in (None, "home", "away"):
                p = H(home_score=20 + margin, away_score=20, game_seconds_remaining=gsr, possession=poss, overtime=True)
                exact += p in (0.0, 1.0)
                n += 1
    out["ot"] = {"rows": n, "exact_0_or_1": exact, "tie_at_zero": r6(H(home_score=20, away_score=20, game_seconds_remaining=0, overtime=True))}
    # 6) kneel floor: rows lifted with the flag on (none with it off).
    lifted_on, lifted_off, n = 0, 0, 0
    for to in (0, 1, 2, 3):
        for gsr in (20, 40, 80, 100, 120, 150):
            st = dict(home_score=23, away_score=20, game_seconds_remaining=gsr, possession="home", down=1, distance=10, yardline_100=60, away_timeouts=to)
            base = H(**st)
            lifted_off += H(rules={"kneel_floor": {"enabled": False}}, **st) != base
            lifted_on += H(rules={"kneel_floor": {"enabled": True}}, **st) != base
            n += 1
    out["kneel"] = {"rows": n, "lifted_flag_off": lifted_off, "lifted_flag_on": lifted_on, "flag_default": bool(load_rules()["kneel_floor"]["enabled"])}
    out["replay_per_class"] = {"status": "by-hand (needs P03 per-class tables and a cached week)", "old_after_alignment_kickoff_mean_abs": 0.088, "note": "0.088 is the pre-P05 after-alignment kickoff number quoted in the plan; the before-alignment baseline and the post-P05 rerun are recorded here once P03 lands"}
    return out


class DeadBallRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = load_model()

    def _home(self, **kw) -> float:
        kw.setdefault("model", self.model)
        return home_win_probability(**kw)

    def test_rules_table_loads_and_overrides(self):
        rules = load_rules()
        self.assertEqual(rules["neutral_yardline"]["default"], 75)
        self.assertFalse(rules["kneel_floor"]["enabled"])
        self.assertAlmostEqual(rules["try"]["p_pat"], 0.94)
        self.assertEqual(neutral_yardline(None), 75)
        self.assertEqual(neutral_yardline(2016), 75)
        self.assertEqual(neutral_yardline(2023), 75)
        self.assertEqual(neutral_yardline(2024), 70)
        self.assertEqual(neutral_yardline(2025), 69)
        self.assertEqual(neutral_yardline(2027), 69)  # newest rule carries forward
        self.assertEqual(neutral_yardline(2024, {"neutral_yardline": {"by_season": {"2024": 72}}}), 72)

    def test_spread_inversion_reproduces_model_md_table_and_is_monotone(self):
        for spread, p in MODEL_MD_PREGAME.items():
            self.assertAlmostEqual(spread_from_pregame_probability(p, self.model), spread, delta=0.5, msg=f"spread {spread}")
        tab = pregame_spread_table(self.model)
        self.assertTrue(tab["monotone"])
        prev = None
        for p in [x / 100 for x in range(2, 99)]:
            s = spread_from_pregame_probability(p, self.model)
            self.assertTrue(-30 <= s <= 30)
            if prev is not None:
                self.assertLessEqual(s, prev + 1e-9, f"p {p}: spread {s} > {prev}")  # more P(home) -> more negative spread
            prev = s
        self.assertEqual(spread_from_pregame_probability(0.999, self.model), -30.0)
        self.assertEqual(spread_from_pregame_probability(0.001, self.model), 30.0)
        # Round trip: the inverted spread scores back to the target within the tree wiggle.
        for p in (0.3, 0.45, 0.6, 0.8):
            self.assertAlmostEqual(pregame_home_probability(spread_from_pregame_probability(p, self.model), self.model), p, delta=0.02)

    def test_neutral_yardline_by_season_moves_the_right_states(self):
        for y in (2024, 2025):
            self.assertLess(abs(pregame_home_probability(-3, self.model, season=y) - pregame_home_probability(-3, self.model)), 0.01)
        late = dict(home_score=24, away_score=21, game_seconds_remaining=120, possession="away", play_class="kickoff")
        old = self._home(**late)
        for y in (2024, 2025):
            self.assertGreater(abs(self._home(season=y, **late) - old), 0.005, f"season {y}")
        # A shorter field for the trailing receiver helps it: P(home) falls for the home kicker.
        self.assertLess(self._home(season=2025, **late), old)
        # season=None keeps today's neutral state bit for bit.
        neutral = dict(home_score=14, away_score=10, game_seconds_remaining=1000)
        self.assertEqual(self._home(**neutral), self._home(season=None, **neutral))

    def test_kickoff_pending_state(self):
        st = dict(home_score=24, away_score=21, game_seconds_remaining=120)
        recv = self._home(possession="away", down=1, distance=10, yardline_100=75, **st)
        self.assertAlmostEqual(kickoff_state_wp(kicker="home", model=self.model, **st), recv, places=12)
        self.assertAlmostEqual(self._home(possession="away", play_class="kickoff", **st), recv, places=12)
        self.assertAlmostEqual(self._home(possession="away", play_class="kickoff_pending", **st), recv, places=12)
        # Explicit onside mixture: between the receiver and kicker states.
        kick = self._home(possession="home", down=1, distance=10, yardline_100=55, **st)
        mix = kickoff_state_wp(kicker="home", p_onside=0.25, model=self.model, **st)
        self.assertAlmostEqual(mix, 0.25 * kick + 0.75 * recv, places=12)
        # Table policy: only a trailing kicker late in the game gets the provisional 0.06.
        late_trailing = dict(home_score=17, away_score=27, game_seconds_remaining=200)
        self.assertGreater(kickoff_state_wp(kicker="home", model=self.model, **late_trailing), kickoff_state_wp(kicker="home", p_onside=0.0, model=self.model, **late_trailing))
        early_trailing = dict(home_score=17, away_score=27, game_seconds_remaining=1200)
        self.assertEqual(kickoff_state_wp(kicker="home", model=self.model, **early_trailing), kickoff_state_wp(kicker="home", p_onside=0.0, model=self.model, **early_trailing))
        blowout = dict(home_score=3, away_score=27, game_seconds_remaining=200)
        self.assertEqual(kickoff_state_wp(kicker="home", model=self.model, **blowout), kickoff_state_wp(kicker="home", p_onside=0.0, model=self.model, **blowout))
        self.assertRaises(ValueError, kickoff_state_wp, kicker="nobody", model=self.model, **st)
        # No possession on a kickoff row: the plain neutral state.
        self.assertEqual(self._home(play_class="kickoff", **st), self._home(**st))

    def test_try_state_between_the_kickoff_pending_outcomes(self):
        st = dict(home_score=23, away_score=17, game_seconds_remaining=600)  # home up 6 after the TD, Q4
        t = self._home(possession="home", play_class="try", **st)
        ko6 = self._home(possession="away", play_class="kickoff", **st)
        ko7 = self._home(possession="away", play_class="kickoff", home_score=24, away_score=17, game_seconds_remaining=600)
        self.assertGreater(t, ko6)
        self.assertLess(t, ko7)
        self.assertAlmostEqual(t, 0.94 * ko7 + 0.06 * ko6, places=12)
        self.assertAlmostEqual(try_state_wp(scorer="home", model=self.model, **st), t, places=12)
        # Two-point chart: down 2 after the TD in Q4 goes for two (48%), earlier it kicks.
        self.assertTrue(two_point_attempt(-2, 600))
        self.assertFalse(two_point_attempt(-2, 1500))
        self.assertFalse(two_point_attempt(6, 600))
        down2 = dict(home_score=18, away_score=20, game_seconds_remaining=600)
        ko_tie = self._home(possession="away", play_class="kickoff", home_score=20, away_score=20, game_seconds_remaining=600)
        ko_m2 = self._home(possession="away", play_class="kickoff", **down2)
        self.assertAlmostEqual(self._home(possession="home", play_class="try", **down2), 0.48 * ko_tie + 0.52 * ko_m2, places=12)
        # play_class None on a real 1st-and-goal from the 2 is untouched.
        goal = dict(possession="home", down=1, distance=2, yardline_100=2, **st)
        self.assertEqual(self._home(**goal), self._home(play_class=None, **goal))
        self.assertNotAlmostEqual(self._home(**goal), t, places=3)
        self.assertRaises(ValueError, try_state_wp, scorer="x", model=self.model, **st)

    def test_overtime_guard(self):
        tie = self._home(home_score=20, away_score=20, game_seconds_remaining=0, overtime=True)
        self.assertTrue(0.0 < tie < 1.0)
        self.assertAlmostEqual(tie, self._home(home_score=20, away_score=20, game_seconds_remaining=0), places=12)  # tied-Q4 fallback
        # A lead at 0:00 in OT is not the regulation shortcut; nothing mid-OT is exactly 0 or 1.
        for margin in (-7, -3, 3, 7):
            for gsr in (0, 1, 60, 600):
                for poss in (None, "home", "away"):
                    p = self._home(home_score=20 + margin, away_score=20, game_seconds_remaining=gsr, possession=poss, overtime=True)
                    self.assertTrue(0.0 < p < 1.0, f"margin {margin} gsr {gsr} poss {poss}: {p}")
        self.assertEqual(self._home(home_score=23, away_score=20, game_seconds_remaining=0), 1.0)  # regulation unchanged
        # Unknown possession in OT: neutral state, and OT clocks beyond 600 clamp to the period.
        self.assertAlmostEqual(self._home(home_score=20, away_score=20, game_seconds_remaining=900, overtime=True), self._home(home_score=20, away_score=20, game_seconds_remaining=600, overtime=True), places=12)

    def test_final_flag(self):
        self.assertEqual(self._home(home_score=23, away_score=20, game_seconds_remaining=300, final=True), 1.0)
        self.assertEqual(self._home(home_score=20, away_score=23, game_seconds_remaining=0, final=True, overtime=True), 0.0)
        self.assertEqual(self._home(home_score=20, away_score=20, game_seconds_remaining=0, final=True), 0.5)

    def test_kneel_floor_only_behind_the_flag(self):
        st = dict(home_score=23, away_score=20, game_seconds_remaining=100, possession="home", down=1, distance=10, yardline_100=60, away_timeouts=0)
        base = self._home(**st)
        self.assertLess(base, 0.995)
        self.assertEqual(self._home(rules={"kneel_floor": {"enabled": False}}, **st), base)
        self.assertEqual(self._home(rules={"kneel_floor": {"enabled": True}}, **st), 0.995)
        on = {"kneel_floor": {"enabled": True}}
        # Two defensive timeouts with 2:30 left: not a kneel-out (and not past the two-minute warning).
        two_to = dict(st, away_timeouts=2, game_seconds_remaining=150)
        self.assertEqual(self._home(rules=on, **two_to), self._home(**two_to))
        # Two timeouts with 0:40 left: 40 * (3 - 2) = 40 -> floor applies.
        self.assertEqual(self._home(rules=on, **dict(st, away_timeouts=2, game_seconds_remaining=40)), 0.995)
        self.assertNotEqual(self._home(rules=on, **dict(st, away_timeouts=2, game_seconds_remaining=41)), 0.995)
        # Trailer with the ball, 2nd down, or three defensive timeouts: never.
        self.assertEqual(self._home(rules=on, **dict(st, home_score=17)), self._home(**dict(st, home_score=17)))
        self.assertEqual(self._home(rules=on, **dict(st, down=2)), self._home(**dict(st, down=2)))
        self.assertEqual(self._home(rules=on, **dict(st, away_timeouts=3, game_seconds_remaining=1)), self._home(**dict(st, away_timeouts=3, game_seconds_remaining=1)))
        # Away leader mirrors through 1 - p.
        away = dict(home_score=20, away_score=23, game_seconds_remaining=100, possession="away", down=1, distance=10, yardline_100=60, home_timeouts=0)
        self.assertAlmostEqual(self._home(rules=on, **away), 0.005, places=12)
        self.assertEqual(kneel_out_wp(0.9, leader_margin=3, down=1, game_seconds_remaining=100, defteam_timeouts=0), 0.995)
        self.assertEqual(kneel_out_wp(0.9, leader_margin=3, down=1, game_seconds_remaining=100, defteam_timeouts=1), 0.9)
        # Environment override wins over the table.
        old = os.environ.get("ARB_WP_KNEEL_FLOOR")
        try:
            os.environ["ARB_WP_KNEEL_FLOOR"] = "1"
            self.assertEqual(self._home(**st), 0.995)
            os.environ["ARB_WP_KNEEL_FLOOR"] = "0"
            self.assertEqual(self._home(rules=on, **st), base)
        finally:
            if old is None:
                os.environ.pop("ARB_WP_KNEEL_FLOOR", None)
            else:
                os.environ["ARB_WP_KNEEL_FLOOR"] = old

    def test_defaults_reproduce_the_pre_rules_output(self):
        # No play_class / season / overtime: the routing must be invisible to today's callers.
        states = [
            dict(home_score=14, away_score=10, game_seconds_remaining=1000),
            dict(home_score=14, away_score=10, game_seconds_remaining=1000, possession="home", down=2, distance=7, yardline_100=40, vegas_spread_home=-3),
            dict(home_score=0, away_score=0, game_seconds_remaining=3600, vegas_spread_home=-7, receive_2h_ko_home=True),
        ]
        for st in states:
            self.assertEqual(self._home(**st), self._home(play_class=None, overtime=False, final=False, season=None, p_onside=None, rules=None, **st))

    def test_offline_metrics_fixture(self):
        got = offline_p05_metrics(self.model)
        with open(P05_FIXTURE, encoding="utf-8") as f:
            want = json.load(f)
        self.assertLessEqual(got["spread_inversion"]["max_abs_error_pts"], 0.5)
        self.assertEqual(got["try_synth"]["bracket_violations"], 0)
        self.assertEqual(got["ot"]["exact_0_or_1"], 0)
        self.assertEqual(got["kneel"]["lifted_flag_off"], 0)
        self.assertGreater(got["kneel"]["lifted_flag_on"], 0)
        self.assertGreater(got["kickoff_pending_synth"]["mean_receiver_shift"], 0.0)

        def walk(a, b, path=""):
            if isinstance(a, dict):
                self.assertEqual(set(a), set(b), path)
                for k in a:
                    walk(a[k], b[k], f"{path}/{k}")
            elif isinstance(a, float):
                self.assertAlmostEqual(a, b, places=5, msg=path)
            else:
                self.assertEqual(a, b, path)

        for key in ("spread_inversion", "neutral_yardline", "kickoff_pending_synth", "try_synth", "ot", "kneel"):
            walk(got[key], want[key], key)


class FinalWhistleTests(unittest.TestCase):
    def test_decided_at_zero_seconds(self):
        from arb_engine.models.wp import home_win_probability

        self.assertEqual(home_win_probability(home_score=24, away_score=20, game_seconds_remaining=0, possession="away"), 1.0)
        self.assertEqual(home_win_probability(home_score=20, away_score=24, game_seconds_remaining=0, possession="home"), 0.0)
        tie = home_win_probability(home_score=20, away_score=20, game_seconds_remaining=0, possession="home")
        self.assertTrue(0.2 < tie < 0.8)  # overtime: still modelled


if __name__ == "__main__":
    import sys

    if "--write-fixture" in sys.argv:  # python3 -m tests.test_wp_model --write-fixture
        P05_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        P05_FIXTURE.write_text(json.dumps(offline_p05_metrics(load_model()), indent=2) + "\n", encoding="utf-8")
        print(f"wrote {P05_FIXTURE}")
    else:
        unittest.main()

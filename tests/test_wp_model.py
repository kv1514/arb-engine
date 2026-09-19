"""In-game NFL win-probability model: exported JSON loads and behaves sensibly."""

from __future__ import annotations

import json
import unittest
from importlib import resources

from arb_engine.models.wp import (
    FEATURES,
    WinProbModel,
    home_win_probability,
    load_model,
    posteam_features,
    predict_posteam_wp,
    time_features,
)


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


if __name__ == "__main__":
    unittest.main()

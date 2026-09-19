import inspect
import unittest

from arb_engine.quant.inplay_fair import DEFAULT_WEIGHTS, blended_fair, market_home_probability
from arb_engine.strategy.inplay import model_home_wp


class BlendTests(unittest.TestCase):
    def test_weights_normalise_over_available_sources(self):
        b = blended_fair({"KC": 0.60, "DEN": 0.40}, model_home_wp=0.50, espn_home_wp=None, home_outcome="KC", away_outcome="DEN", live=True)
        self.assertAlmostEqual(sum(b.weights.values()), 1.0)
        self.assertEqual(set(b.weights), {"market", "model"})
        self.assertAlmostEqual(b.home_p, (0.6 * 0.30 + 0.5 * 0.55) / 0.85)
        self.assertAlmostEqual(b.fair["KC"] + b.fair["DEN"], 1.0)
        self.assertAlmostEqual(b.disagreement, 0.10)

    def test_pregame_uses_market_only(self):
        b = blended_fair({"KC": 0.60, "DEN": 0.40}, 0.70, 0.75, "KC", "DEN", live=False)
        self.assertEqual(b.weights, {"market": 1.0})
        self.assertAlmostEqual(b.home_p, 0.60)
        b2 = blended_fair(None, 0.70, 0.75, "KC", "DEN", live=False)
        self.assertEqual(b2.weights, {"model": 1.0})
        self.assertAlmostEqual(b2.home_p, 0.70)

    def test_no_sources(self):
        b = blended_fair(None, None, None, "KC", "DEN")
        self.assertIsNone(b.home_p)
        self.assertEqual(b.fair, {})

    def test_market_confidence_scales_market_weight(self):
        full = blended_fair({"KC": 0.60, "DEN": 0.40}, 0.50, None, "KC", "DEN", live=True)
        low = blended_fair({"KC": 0.60, "DEN": 0.40}, 0.50, None, "KC", "DEN", live=True, market_confidence=0.2)
        self.assertLess(low.weights["market"], full.weights["market"])
        self.assertLess(low.home_p, full.home_p)

    def test_one_sided_market(self):
        self.assertAlmostEqual(market_home_probability({"KC": None, "DEN": 0.4}, "KC", "DEN"), 0.6)
        self.assertAlmostEqual(market_home_probability({"KC": 0.62, "DEN": 0.40}, "KC", "DEN"), 0.62 / 1.02)
        self.assertIsNone(market_home_probability({}, "KC", "DEN"))

    def test_probabilities_stay_in_range(self):
        b = blended_fair({"KC": 1.4, "DEN": -0.2}, 1.3, -0.1, "KC", "DEN", live=True)
        self.assertTrue(0.0 <= b.home_p <= 1.0)
        self.assertEqual(set(DEFAULT_WEIGHTS), {"market", "model", "espn"})


class PoolAndTieTests(unittest.TestCase):
    def test_logit_pool_is_selectable_and_close_to_linear_but_not_default(self):
        self.assertEqual(inspect.signature(blended_fair).parameters["pool"].default, "linear")
        mk = {"KC": 0.60, "DEN": 0.40}
        lin = blended_fair(mk, 0.62, 0.58, "KC", "DEN", live=True)
        lgt = blended_fair(mk, 0.62, 0.58, "KC", "DEN", live=True, pool="logit")
        self.assertNotEqual(lin.home_p, lgt.home_p)
        self.assertAlmostEqual(lin.home_p, lgt.home_p, delta=0.01)
        # Equal inputs: both pools return the input exactly.
        self.assertAlmostEqual(blended_fair(mk, 0.60, 0.60, "KC", "DEN", live=True, pool="logit").home_p, 0.60, places=9)
        # Logit pooling is more extreme than linear when the sources agree on the direction.
        far = blended_fair({"KC": 0.90, "DEN": 0.10}, 0.95, 0.80, "KC", "DEN", live=True)
        far_l = blended_fair({"KC": 0.90, "DEN": 0.10}, 0.95, 0.80, "KC", "DEN", live=True, pool="logit")
        self.assertAlmostEqual(far.home_p, far_l.home_p, delta=0.02)
        with self.assertRaises(ValueError):
            blended_fair(mk, 0.6, 0.6, "KC", "DEN", pool="geometric")

    def test_p_tie_splits_the_tie_mass_without_changing_fair(self):
        mk = {"KC": 0.60, "DEN": 0.40}
        plain = blended_fair(mk, 0.62, 0.58, "KC", "DEN", live=True)
        tied = blended_fair(mk, 0.62, 0.58, "KC", "DEN", live=True, p_tie=0.04)
        self.assertEqual(plain.fair, tied.fair)                  # half-tie convention untouched
        self.assertEqual(plain.as_dict(), {k: v for k, v in tied.as_dict().items() if k not in ("p_tie", "win")})
        self.assertAlmostEqual(tied.win["KC"], tied.fair["KC"] - 0.02)
        self.assertAlmostEqual(tied.win["DEN"], tied.fair["DEN"] - 0.02)
        self.assertEqual(tied.leg_fair("KC", 0.5), tied.fair["KC"])      # bit-identical for the default rule
        self.assertAlmostEqual(tied.leg_fair("KC", 0.0), tied.fair["KC"] - 0.02)
        self.assertAlmostEqual(tied.leg_fair("KC", 1.0), tied.fair["KC"] + 0.02)
        self.assertEqual(plain.leg_fair("KC", 0.0), plain.fair["KC"])   # no tie estimate -> no adjustment
        self.assertIsNone(plain.p_tie)

    def test_overtime_sentinel_gives_no_model_probability(self):
        class GS:
            sport = "ncaaf"; status = "live"; game_seconds_remaining = None; home_score = 21; away_score = 21
            possession = None; down = None; distance = None; yardline_100 = None; home_timeouts = 1; away_timeouts = 1
            vegas_spread_home = -3.0; period = 5; overtime_sentinel = True
        self.assertIsNone(model_home_wp(GS()))
        GS.overtime_sentinel = False
        GS.game_seconds_remaining = 0
        self.assertIsNotNone(model_home_wp(GS()))


if __name__ == "__main__":
    unittest.main()

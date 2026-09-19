import unittest

from arb_engine.quant.inplay_fair import DEFAULT_WEIGHTS, blended_fair, market_home_probability


class BlendTests(unittest.TestCase):
    def test_weights_normalise_over_available_sources(self):
        b = blended_fair({"KC": 0.60, "DEN": 0.40}, model_home_wp=0.50, espn_home_wp=None, home_outcome="KC", away_outcome="DEN", live=True)
        self.assertAlmostEqual(sum(b.weights.values()), 1.0)
        self.assertEqual(set(b.weights), {"market", "model"})
        self.assertAlmostEqual(b.home_p, (0.6 * 0.5 + 0.5 * 0.35) / 0.85)
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


if __name__ == "__main__":
    unittest.main()

import unittest

from arb_engine.quant.sizing import hedge_kelly, kelly_fraction, kelly_stake


class KellyTests(unittest.TestCase):
    def test_kelly_fraction_and_stake(self):
        self.assertAlmostEqual(kelly_fraction(0.60, 0.50), 0.20)
        self.assertEqual(kelly_fraction(0.40, 0.50), 0.0)
        ks = kelly_stake(1000.0, 0.60, 0.50, fraction=0.25)
        self.assertAlmostEqual(ks["stake"], 50.0)
        self.assertIn(ks["contracts"], (99, 100))   # floor of a float stake / cost


class HedgeKellyTests(unittest.TestCase):
    """Hold 100 of side A at 0.50 all-in with a $1,000 bankroll; hedge B at all-in h."""

    def test_full_hedge_when_fair_equals_avg_cost(self):
        for h in (0.40, 0.45, 0.499):
            r = hedge_kelly(1000.0, 100, 0.50, h, fair_p=0.50)
            self.assertEqual(r["contracts"], 100, h)
            self.assertTrue(r["locks"])
            self.assertAlmostEqual(r["wealth_if_a"], r["wealth_if_b"])        # fully hedged: same wealth either way
            self.assertGreater(r["wealth_if_a"], 1000.0)                      # ...and it is a locked profit

    def test_zero_when_the_hedge_cannot_lock(self):
        for h in (0.50, 0.55, 0.70):
            r = hedge_kelly(1000.0, 100, 0.50, h, fair_p=0.50)
            self.assertEqual(r["contracts"], 0, h)
            self.assertFalse(r["locks"])
        # Even with a losing position (fair well below cost) the function does not turn a
        # non-locking hedge into a directional bet: that is kelly_stake's job.
        self.assertEqual(hedge_kelly(1000.0, 100, 0.50, 0.60, fair_p=0.20)["contracts"], 0)

    def test_fair_priced_hedge_is_fully_taken_and_dear_hedge_is_partial(self):
        # p + h = 1: the hedge is exactly fair -> Kelly removes all variance (n = held).
        self.assertEqual(hedge_kelly(1000.0, 100, 0.50, 0.45, fair_p=0.55)["contracts"], 100)
        # Cheaper than fair (p + h < 1) is a lock: full.
        self.assertEqual(hedge_kelly(1000.0, 100, 0.50, 0.40, fair_p=0.55)["contracts"], 100)
        # Dearer than fair: partial, shrinking as our edge in holding grows, and zero when
        # the hedge is far above fair.
        partial = hedge_kelly(1000.0, 100, 0.50, 0.45, fair_p=0.57)["contracts"]
        self.assertTrue(0 < partial < 100, partial)
        self.assertLess(hedge_kelly(1000.0, 100, 0.50, 0.45, fair_p=0.59)["contracts"], partial)
        self.assertEqual(hedge_kelly(1000.0, 100, 0.50, 0.45, fair_p=0.70)["contracts"], 0)

    def test_closed_form_matches_the_derivative(self):
        # n* from the docstring: [(1-p)(1-h)X - p h Y] / (h(1-h)), X = B - held*c + held, Y = B - held*c.
        b, held, c, h, p = 2000.0, 100, 0.52, 0.44, 0.58
        x, y = b - held * c + held, b - held * c
        n = ((1 - p) * (1 - h) * x - p * h * y) / (h * (1 - h))
        r = hedge_kelly(b, held, c, h, p)
        self.assertAlmostEqual(r["raw"], n)
        self.assertEqual(r["contracts"], int(min(max(n, 0), held) + 1e-9))
        # Fractional Kelly scales before the clamp.
        self.assertEqual(hedge_kelly(b, held, c, h, p, fraction=0.5)["contracts"], int(min(max(n * 0.5, 0), held) + 1e-9))

    def test_degenerate_inputs(self):
        self.assertEqual(hedge_kelly(1000.0, 0, 0.5, 0.4, 0.5)["contracts"], 0)
        self.assertEqual(hedge_kelly(1000.0, 100, 0.5, 1.0, 0.5)["contracts"], 0)
        self.assertEqual(hedge_kelly(1000.0, 100, 0.5, 0.4, 1.5)["contracts"], 0)


if __name__ == "__main__":
    unittest.main()

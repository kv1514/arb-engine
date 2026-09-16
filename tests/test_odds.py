import unittest

from arb_engine.quant import american_to_decimal, decimal_to_american, devig_multiplicative, devig_power, devig_shin, implied_from_decimal, overround


class OddsTests(unittest.TestCase):
    def test_american_decimal_roundtrip(self):
        for a in (-110, +150, -250, +100, +2000):
            d = american_to_decimal(a)
            self.assertAlmostEqual(decimal_to_american(d), a, places=6)
        self.assertAlmostEqual(american_to_decimal(-110), 1.9090909, places=6)

    def test_implied_and_overround(self):
        probs = [implied_from_decimal(1.909), implied_from_decimal(1.909)]
        self.assertAlmostEqual(overround(probs), 0.0477, places=3)

    def test_devig_methods_sum_to_one(self):
        raw = [0.55, 0.50]
        for f in (devig_multiplicative, devig_power, devig_shin):
            out = f(raw)
            self.assertAlmostEqual(sum(out), 1.0, places=9)
            self.assertGreater(out[0], out[1])

    def test_power_and_shin_penalise_longshots_more(self):
        raw = [0.90, 0.15]
        mult, power, shin = devig_multiplicative(raw), devig_power(raw), devig_shin(raw)
        self.assertLess(power[1], mult[1])
        self.assertLess(shin[1], mult[1])

    def test_three_way(self):
        raw = [0.40, 0.35, 0.30]
        self.assertAlmostEqual(sum(devig_shin(raw)), 1.0, places=9)
        self.assertAlmostEqual(sum(devig_power(raw)), 1.0, places=9)


if __name__ == "__main__":
    unittest.main()

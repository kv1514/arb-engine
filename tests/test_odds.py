import unittest

from arb_engine.quant import american_to_decimal, decimal_to_american, devig_multiplicative, devig_power, devig_shin, implied_from_decimal, overround
from arb_engine.quant.odds import (
    SportsbookProbs,
    devig,
    devig_additive,
    devig_auto,
    devig_range,
    devig_shin_closed,
    shin_z_two_way,
    sportsbook_probs_from_moneylines,
)

from .helpers import load


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
        for f in (devig_multiplicative, devig_power, devig_shin, devig_additive, devig_shin_closed):
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


class DevigMethodTests(unittest.TestCase):
    """Hand-checked anchors per method, then the committed vector file."""

    def test_additive_by_hand(self):
        # overround 0.05 on two outcomes -> 0.025 off each
        self.assertEqual([round(x, 6) for x in devig_additive([0.55, 0.50])], [0.525, 0.475])
        # a long shot below zero is floored and the vector renormalised, never negative
        out = devig_additive([0.98, 0.03])
        self.assertGreater(out[1], 0)
        self.assertAlmostEqual(sum(out), 1.0, places=9)

    def test_multiplicative_by_hand(self):
        self.assertAlmostEqual(devig_multiplicative([0.55, 0.50])[0], 0.55 / 1.05, places=9)

    def test_power_by_hand(self):
        # k solves 0.7^k + 0.4^k = 1; check the solution satisfies the equation
        raw = [0.7, 0.4]
        out = devig_power(raw)
        # recover k from the first outcome and verify on the second
        import math
        k = math.log(out[0]) / math.log(0.7)
        self.assertAlmostEqual(0.7 ** k + 0.4 ** k, 1.0, places=6)
        self.assertAlmostEqual(out[1], 0.4 ** k, places=6)

    def test_shin_closed_form_matches_bisection(self):
        for q in ([0.55, 0.50], [0.90, 0.15], [0.7106, 0.3333], [0.5238, 0.5238], [0.923, 0.125]):
            closed, bis = devig_shin_closed(q), devig_shin(q)
            self.assertAlmostEqual(closed[0], bis[0], places=8, msg=str(q))
            z = shin_z_two_way(*q)
            self.assertIsNotNone(z)
            self.assertGreaterEqual(z, 0.0)
        # two-way Shin coincides with the additive method (a known identity) - a second, independent check
        self.assertAlmostEqual(devig_shin_closed([0.7106, 0.3333])[0], devig_additive([0.7106, 0.3333])[0], places=9)
        # no overround -> z undefined, falls back to plain normalisation
        self.assertIsNone(shin_z_two_way(0.6, 0.4))
        self.assertEqual(devig_shin_closed([0.6, 0.4]), [0.6, 0.4])

    def test_devig_dispatch_and_auto(self):
        raw = [0.55, 0.50]
        self.assertEqual(devig(raw, "additive"), devig_additive(raw))
        self.assertEqual(devig(raw, "shin"), devig_shin_closed(raw))
        self.assertEqual(devig(raw, "auto"), devig_auto(raw))
        self.assertEqual(devig_auto(raw), devig_shin_closed(raw))
        self.assertEqual(devig_auto([0.6, 0.4]), [0.6, 0.4])
        self.assertAlmostEqual(sum(devig_auto([0.40, 0.35, 0.30])), 1.0, places=9)
        with self.assertRaises(ValueError):
            devig(raw, "bogus")

    def test_vectors_file(self):
        vec = load("devig_vectors.json")["vectors"]
        self.assertGreaterEqual(len(vec), 6)
        for v in vec:
            for method, expected in v["expected"].items():
                got = devig(v["implied"], method)
                for g, e in zip(got, expected):
                    self.assertAlmostEqual(g, e, places=8, msg=f"{v['name']} {method}")
            rng = devig_range(v["implied"])
            for i in range(len(v["implied"])):
                self.assertLessEqual(rng["fair_min"][i], rng["fair_max"][i] + 1e-12)
                self.assertAlmostEqual(rng["fair_min"][i], v["fair_min"][i], places=8)
                self.assertAlmostEqual(rng["fair_max"][i], v["fair_max"][i], places=8)

    def test_fair_range_widens_on_heavy_favourites(self):
        light = sportsbook_probs_from_moneylines(-110, -110)
        heavy = sportsbook_probs_from_moneylines(-600, 425)
        extreme = sportsbook_probs_from_moneylines(-1200, 700)
        self.assertAlmostEqual(light.range, 0.0, places=9)
        self.assertLess(light.range, heavy.range)
        self.assertLess(heavy.range, extreme.range)
        self.assertGreater(extreme.range, 0.025)
        for sp in (light, heavy, extreme):
            self.assertLessEqual(sp.fair_min, sp.home + 1e-12)
            self.assertLessEqual(sp.home, sp.fair_max + 1e-12)
            self.assertAlmostEqual(sp.home + sp.away, 1.0, places=9)
            self.assertEqual(set(sp.by_method), {"multiplicative", "power", "additive", "shin"})

    def test_sportsbook_probs_from_espn_close(self):
        # DraftKings close in tests/fixtures/espn/summary_401872932.json: BUF -245 / NYJ +200
        sp = sportsbook_probs_from_moneylines(-245, 200, method="power")
        self.assertIsInstance(sp, SportsbookProbs)
        self.assertAlmostEqual(sp.home, 0.6925, places=3)
        self.assertAlmostEqual(sp.overround, 0.0435, places=3)
        m = sp.as_mapping("BUF", "NYJ")
        self.assertAlmostEqual(m["BUF"]["fair"], sp.home)
        self.assertAlmostEqual(m["NYJ"]["fair_min"], 1.0 - sp.fair_max)
        self.assertAlmostEqual(m["NYJ"]["fair_max"], 1.0 - sp.fair_min)


if __name__ == "__main__":
    unittest.main()

import math
import random
import sys
import unittest

from arb_engine.quant.calibration import (
    bootstrap_paired,
    corp_decomposition,
    extreme_path_audit,
    games_needed,
    pav_isotonic,
    per_game_loss_diff,
    reliability_band,
)


def _martingale(rng: random.Random, steps: int = 40) -> tuple[list[float], float]:
    """p_{t+1} = p_t +/- 0.5*min(p, 1-p): stays in (0,1), E[p_{t+1}|p_t] = p_t; y ~ Bernoulli(p_T)."""
    p = 0.5
    ps = [p]
    for _ in range(steps):
        p += 0.5 * min(p, 1.0 - p) * rng.choice((-1.0, 1.0))
        ps.append(p)
    return ps, 1.0 if rng.random() < p else 0.0


class PerGameLossDiffTest(unittest.TestCase):
    def test_tuple_rows_and_mapping_rows_agree(self):
        tup = {"g1": [(0.8, 0.6, 1), (0.3, 0.5, 0)], "g2": [(0.5, 0.5, 1)]}
        mp = {g: [{"m": a, "k": b, "y": y} for a, b, y in rows] for g, rows in tup.items()}
        d1 = per_game_loss_diff(tup)
        d2 = per_game_loss_diff(mp, a="m", b="k", y="y")
        self.assertEqual(d1, d2)
        # brier: g1 = mean((0.04-0.16), (0.09-0.25)) = -0.14 ; g2 = 0
        self.assertAlmostEqual(d1["g1"], -0.14, places=12)
        self.assertEqual(d1["g2"], 0.0)

    def test_rows_with_missing_probability_are_skipped_on_both_sides(self):
        d = per_game_loss_diff({"g": [(None, 0.5, 1), (0.9, 0.5, 1)]})
        self.assertAlmostEqual(d["g"], 0.01 - 0.25, places=12)
        self.assertEqual(per_game_loss_diff({"g": [(None, 0.5, 1)]}), {})

    def test_log_loss_option(self):
        d = per_game_loss_diff({"g": [(0.9, 0.5, 1)]}, loss="log")
        self.assertAlmostEqual(d["g"], -math.log(0.9) + math.log(0.5), places=12)


class BootstrapPairedTest(unittest.TestCase):
    def test_seeded_and_deterministic(self):
        diffs = {i: random.Random(i).gauss(0.0, 1.0) for i in range(30)}
        a = bootstrap_paired(diffs, B=300, seed=3)
        b = bootstrap_paired(diffs, B=300, seed=3)
        c = bootstrap_paired(diffs, B=300, seed=4)
        self.assertEqual(a, b)
        self.assertNotEqual(a["lo90"], c["lo90"])

    def test_all_games_equal_collapses_to_the_population_mean(self):
        r = bootstrap_paired([0.37] * 12, B=200)
        self.assertAlmostEqual(r["mean"], 0.37, delta=1e-9)
        self.assertAlmostEqual(r["lo90"], 0.37, delta=1e-9)
        self.assertAlmostEqual(r["hi90"], 0.37, delta=1e-9)
        self.assertAlmostEqual(r["sd_per_game"], 0.0, delta=1e-12)
        self.assertEqual(r["n_games"], 12)

    def test_null_contains_zero_and_signal_excludes_it(self):
        rng = random.Random(11)
        null = [rng.gauss(0.0, 0.02) for _ in range(60)]
        r = bootstrap_paired(null, B=500, seed=1)
        self.assertLess(r["lo90"], 0.0)
        self.assertGreater(r["hi90"], 0.0)
        self.assertTrue(0.05 < r["frac_positive"] < 0.95)
        signal = [0.03 + rng.gauss(0.0, 0.02) for _ in range(60)]
        r = bootstrap_paired(signal, B=500, seed=1)
        self.assertGreater(r["lo90"], 0.0)
        self.assertEqual(r["frac_positive"], 1.0)
        self.assertAlmostEqual(r["mean"], sum(signal) / 60, places=12)

    def test_empty(self):
        r = bootstrap_paired({}, B=10)
        self.assertEqual(r["n_games"], 0)
        self.assertTrue(math.isnan(r["mean"]))

    def test_no_numpy_needed(self):
        self.assertNotIn("numpy", sys.modules)


class GamesNeededTest(unittest.TestCase):
    def test_monotone_in_sd_and_delta(self):
        self.assertLess(games_needed(0.01, 0.05), games_needed(0.01, 0.10))
        self.assertGreater(games_needed(0.01, 0.05), games_needed(0.02, 0.05))
        self.assertGreater(games_needed(0.01, 0.05, power=0.9), games_needed(0.01, 0.05, power=0.8))
        # closed form: ((1.6449 + 0.8416) * 0.05 / 0.01)^2 = 154.6 -> 155
        self.assertEqual(games_needed(0.01, 0.05), 155)
        self.assertEqual(games_needed(0.01, 0.0), 1)
        with self.assertRaises(ValueError):
            games_needed(0.0, 0.05)


class IsotonicTest(unittest.TestCase):
    def test_hand_computed_pav(self):
        # sorted y = 1,0,0,1 -> first three pooled to 1/3, last stays 1
        self.assertEqual(pav_isotonic([0.1, 0.2, 0.3, 0.4], [1, 0, 0, 1]), [1 / 3, 1 / 3, 1 / 3, 1.0])
        # classic textbook case: 1,3,2,4,5 -> 1,2.5,2.5,4,5 (fitted in input order)
        self.assertEqual(pav_isotonic([1, 2, 3, 4, 5], [1, 3, 2, 4, 5]), [1.0, 2.5, 2.5, 4.0, 5.0])

    def test_monotone_and_ties_share_a_value(self):
        rng = random.Random(5)
        p = [round(rng.random(), 1) for _ in range(300)]
        y = [1.0 if rng.random() < pi else 0.0 for pi in p]
        f = pav_isotonic(p, y)
        order = sorted(range(len(p)), key=lambda i: p[i])
        for a, b in zip(order, order[1:]):
            self.assertLessEqual(f[a], f[b] + 1e-12)
            if p[a] == p[b]:
                self.assertEqual(f[a], f[b])
        self.assertEqual(pav_isotonic([], []), [])
        with self.assertRaises(ValueError):
            pav_isotonic([0.1], [])


class CorpTest(unittest.TestCase):
    def test_identity_holds(self):
        rng = random.Random(9)
        p = [rng.random() for _ in range(500)]
        y = [1.0 if rng.random() < pi ** 1.3 else 0.0 for pi in p]  # deliberately miscalibrated
        d = corp_decomposition(p, y)
        self.assertAlmostEqual(d["brier"], d["MCB"] - d["DSC"] + d["UNC"], delta=1e-9)
        self.assertGreaterEqual(d["MCB"], -1e-12)
        self.assertGreaterEqual(d["DSC"], -1e-12)
        self.assertAlmostEqual(d["UNC"], (sum(y) / 500) * (1 - sum(y) / 500), places=12)

    def test_perfect_and_constant_forecasts(self):
        d = corp_decomposition([1.0, 0.0, 1.0, 0.0], [1, 0, 1, 0])
        self.assertAlmostEqual(d["brier"], 0.0)
        self.assertAlmostEqual(d["DSC"], 0.25)
        d = corp_decomposition([0.5] * 4, [1, 0, 1, 0])
        self.assertAlmostEqual(d["MCB"], 0.0)
        self.assertAlmostEqual(d["DSC"], 0.0)


class ReliabilityBandTest(unittest.TestCase):
    def test_bins_and_band(self):
        rng = random.Random(2)
        p, y, g = [], [], []
        for game in range(80):
            for _ in range(20):
                pi = rng.random()
                p.append(pi)
                y.append(1.0 if rng.random() < pi else 0.0)
                g.append(game)
        rows = reliability_band(p, y, g, B=100, seed=0, bins=5)
        self.assertEqual(len(rows), 5)
        self.assertEqual(sum(r["n"] for r in rows), len(p))
        for r in rows:
            self.assertLessEqual(r["obs_lo90"], r["obs"])
            self.assertLessEqual(r["obs"], r["obs_hi90"])
            self.assertLess(abs(r["gap"]), 0.15)
        self.assertEqual(rows, reliability_band(p, y, g, B=100, seed=0, bins=5))

    def test_empty_bin_is_reported(self):
        rows = reliability_band([0.05, 0.06], [0, 1], ["a", "b"], B=5, bins=2)
        self.assertEqual(rows[1]["n"], 0)
        self.assertTrue(math.isnan(rows[1]["obs"]))


class ExtremePathAuditTest(unittest.TestCase):
    def test_martingale_has_no_excess(self):
        rng = random.Random(7)
        paths = [_martingale(rng) for _ in range(3000)]
        rows = extreme_path_audit(paths)
        self.assertEqual(len(rows), 6)
        for r in rows:
            self.assertGreater(r["n_hit"], 500)
            self.assertLess(abs(r["excess"]), 0.02, r)

    def test_overconfident_paths_show_positive_high_side_excess(self):
        # every path claims 0.95 once but only 60% win
        rng = random.Random(3)
        paths = [{"p": [0.5, 0.95, 0.7], "y": 1.0 if rng.random() < 0.6 else 0.0} for _ in range(400)]
        high = [r for r in extreme_path_audit(paths, thresholds=(0.9,)) if r["side"] == "high"][0]
        self.assertEqual(high["n_hit"], 400)
        self.assertAlmostEqual(high["claimed"], 0.95)
        self.assertGreater(high["excess"], 0.25)
        low = [r for r in extreme_path_audit(paths, thresholds=(0.9,)) if r["side"] == "low"][0]
        self.assertEqual(low["n_hit"], 0)
        self.assertTrue(math.isnan(low["excess"]))


if __name__ == "__main__":
    unittest.main()

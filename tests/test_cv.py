import json
import random
import sys
import unittest

from arb_engine.models.cv import OOFLedger, fold_train_test, grouped_folds, loso_folds, oof_record


def _game_ids(n_games: int, seed: int = 0) -> list[str]:
    rng = random.Random(seed)
    ids = []
    for g in range(n_games):
        ids.extend([f"2024_{g:03d}"] * rng.randint(40, 90))  # NFL games run ~60 real plays
    return ids


class GroupedFoldsTest(unittest.TestCase):
    def test_every_game_in_exactly_one_fold_and_sizes_balanced(self):
        ids = _game_ids(120)
        folds = grouped_folds(ids, k=5, seed=1)
        self.assertEqual(len(folds), 5)
        seen = sorted(i for f in folds for i in f)
        self.assertEqual(seen, list(range(len(ids))))  # partition of the rows
        for f in folds:
            self.assertEqual(f, sorted(f))
            games = {ids[i] for i in f}
            for other in folds:
                if other is not f:
                    self.assertFalse(games & {ids[i] for i in other})
        sizes = [len(f) for f in folds]
        self.assertLess((max(sizes) - min(sizes)) / max(sizes), 0.10, sizes)

    def test_seeded_and_deterministic(self):
        ids = _game_ids(40)
        self.assertEqual(grouped_folds(ids, k=4, seed=2), grouped_folds(ids, k=4, seed=2))
        self.assertNotEqual(grouped_folds(ids, k=4, seed=2), grouped_folds(ids, k=4, seed=3))

    def test_validation(self):
        with self.assertRaises(ValueError):
            grouped_folds(["a", "a", "b"], k=3)
        with self.assertRaises(ValueError):
            grouped_folds(["a"], k=0)
        self.assertEqual(grouped_folds(["a", "b", "a"], k=1), [[0, 1, 2]])

    def test_loso(self):
        ids = ["g2", "g2", "g1", "g3", "g1"]
        folds = loso_folds(ids)
        self.assertEqual(folds, [[0, 1], [2, 4], [3]])
        self.assertEqual(len(folds), len(set(ids)))
        train, test = fold_train_test(folds, 1)
        self.assertEqual((train, test), ([0, 1, 3], [2, 4]))

    def test_no_numpy(self):
        self.assertNotIn("numpy", sys.modules)


class OOFLedgerTest(unittest.TestCase):
    def test_table_best_and_json(self):
        lg = OOFLedger()
        lg.record("a", 0, 0.20, n=100)
        lg.record("a", 1, 0.10, n=300)
        lg.record("b", 0, 0.15, n=100)
        lg.record("b", 1, 0.15, n=300)
        lg.record("partial", 0, 0.01, n=100)  # missing fold 1: must not win by default
        t = lg.table()
        self.assertAlmostEqual(t["a"]["mean"], (0.2 * 100 + 0.1 * 300) / 400)
        self.assertEqual(t["a"]["n_folds"], 2)
        self.assertEqual(t["partial"]["n_folds"], 1)
        self.assertEqual(lg.best(), "a")
        self.assertEqual(lg.best(require_folds=1), "partial")
        self.assertEqual(json.loads(lg.to_json())["folds"], [0, 1])

    def test_module_level_helper_uses_default_ledger(self):
        own = OOFLedger()
        self.assertIs(oof_record("x", 0, 0.5, ledger=own), own)
        self.assertEqual(own.table()["x"]["per_fold"], {0: 0.5})
        default = oof_record("test_cv_default_candidate", 0, 0.25)
        self.assertIs(default, oof_record.ledger)
        self.assertIn("test_cv_default_candidate", default.table())
        del default.cells["test_cv_default_candidate"]


if __name__ == "__main__":
    unittest.main()

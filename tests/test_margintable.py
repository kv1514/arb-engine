"""Margin tables are a deterministic function of games.csv: the trimmed fixture rebuilds byte-for-byte."""

import csv
import hashlib
import json
import unittest
from collections import Counter
from pathlib import Path

from arb_engine.quant.margintable import (
    DEFAULT_MIN_SEASON,
    bucket_key,
    build_margin_dist,
    build_sigma,
    build_tables,
    dump_json,
    favourite_margin,
    incomplete_seasons,
    parse_games_csv,
    usable_rows,
)

from .helpers import FIXTURES, load_text

DATA = Path(__file__).resolve().parents[1] / "arb_engine" / "data"


class MarginTableBuildTests(unittest.TestCase):
    def setUp(self):
        self.text = load_text("games_trim.csv")
        self.rows = parse_games_csv(self.text)

    def test_trimmed_fixture_rebuilds_byte_for_byte(self):
        dist, sig = build_tables(self.text, name="games_trim.csv")
        got = dump_json({"dist": dist, "sigma": sig})
        expected = load_text("margin_table_expected.json")
        self.assertEqual(got, expected)
        # and a second build of the same bytes is identical (no timestamps, no dict-order leaks)
        dist2, sig2 = build_tables(self.text, name="games_trim.csv")
        self.assertEqual(dump_json({"dist": dist2, "sigma": sig2}), got)

    def test_source_block_traces_the_csv(self):
        dist, sig = build_tables(self.text, name="games_trim.csv")
        self.assertEqual(dist["source"]["sha256"], hashlib.sha256(self.text.encode("utf-8")).hexdigest())
        self.assertEqual(dist["source"]["rows"], len(self.rows))
        self.assertEqual(dist["source"]["seasons"], [2023, 2024])  # 2015 filtered (min season), 2026 unplayed
        self.assertEqual(sig["source"]["seasons"], [2015, 2024])   # sigma keeps every finished season
        self.assertEqual(dist["source"]["excluded_incomplete_seasons"], [2026])
        self.assertEqual(dist["max_season"], 2024)
        self.assertEqual(sig["ref_max_season"], 2024)
        self.assertEqual(sig["overtime"]["n_ot"], sum(1 for r in self.rows if r["overtime"] == "1" and int(r["season"]) >= DEFAULT_MIN_SEASON and r["result"]))

    def test_in_progress_season_never_enters_the_tables(self):
        """Finished games of a season that still has unplayed rows are excluded (no leakage of
        the games being evaluated); an explicit max_season overrides."""
        def row(season, result, spread="3", gt="REG"):
            return {"season": str(season), "game_type": gt, "result": result, "spread_line": spread, "total": "40", "total_line": "44.5", "overtime": "0"}
        rows = [row(2024, "3"), row(2024, "-7"), row(2025, "10"), row(2025, ""), row(2025, "3")]
        self.assertEqual(incomplete_seasons(rows), [2025])
        self.assertEqual([r["season"] for r in usable_rows(rows)], [2024, 2024])
        self.assertEqual([r["season"] for r in usable_rows(rows, max_season=2025)], [2024, 2024, 2025, 2025])
        self.assertEqual([r["season"] for r in usable_rows(rows, max_season=2023)], [])
        dist = build_margin_dist(rows)
        self.assertEqual(dist["all"]["n"], 2)
        self.assertEqual(dist["max_season"], 2024)
        self.assertEqual(build_margin_dist(rows, max_season=2025)["all"]["n"], 4)
        sig = build_sigma(rows)
        self.assertNotIn("2025", sig["margin"]["by_season"])
        self.assertEqual(sig["ref_max_season"], 2024)
        # an unplayed row of an ignored game type (preseason) does not mark the season incomplete
        self.assertEqual(incomplete_seasons([row(2025, "3"), row(2025, "", gt="PRE")]), [])

    def test_usable_rows_skip_unplayed_and_old_seasons(self):
        used = usable_rows(self.rows)
        self.assertTrue(all(r["season"] >= DEFAULT_MIN_SEASON for r in used))
        self.assertEqual(len(used), sum(1 for r in self.rows if r["result"] and int(r["season"]) >= DEFAULT_MIN_SEASON))
        self.assertTrue(any(r["season"] == "2026" and r["result"] == "" for r in self.rows))

    def test_key_number_mass_matches_hand_count(self):
        """Bucket '3' pmf equals a direct count over the CSV, favourite-oriented."""
        hand = Counter()
        for r in csv.DictReader(self.text.splitlines()):
            if not r["result"] or int(r["season"]) < DEFAULT_MIN_SEASON:
                continue
            spread = float(r["spread_line"])
            if abs(spread) != 3.0:
                continue
            margin = int(float(r["result"]))
            hand[margin if spread > 0 else -margin] += 1
        dist = build_margin_dist(self.rows)
        b = dist["buckets"]["3"]
        self.assertEqual(b["n"], sum(hand.values()))
        self.assertEqual({int(k): v for k, v in b["pmf"].items()}, dict(hand))
        self.assertEqual(b["pmf"].get("3"), hand[3])
        self.assertEqual(favourite_margin(-7, -3.0), 7)
        self.assertEqual(favourite_margin(-7, 3.0), -7)
        self.assertEqual(bucket_key(-2.5), "2.5")
        self.assertEqual(bucket_key(3.0), "3")

    def test_sigma_has_standard_errors_and_tie_rate(self):
        sig = build_sigma(self.rows)
        self.assertGreater(sig["margin"]["sigma"], 8)
        self.assertLess(sig["margin"]["sigma"], 20)
        self.assertGreater(sig["margin"]["se"], 0)
        self.assertIn("2024", sig["margin"]["by_season"])
        self.assertIn("total", sig)
        self.assertIsNotNone(sig["tie_rate"])


class CommittedTablesTests(unittest.TestCase):
    """The shipped JSON was built by scripts/build_margin_table.py from the full nflverse file."""

    def test_committed_margin_dist_source_and_shape(self):
        with open(DATA / "nfl_margin_dist.json", encoding="utf-8") as f:
            d = json.load(f)
        src = d["source"]
        self.assertEqual(len(src["sha256"]), 64)
        self.assertGreater(src["rows"], 7000)
        self.assertGreater(src["rows_used"], 2000)
        self.assertEqual(src["seasons"][0], DEFAULT_MIN_SEASON)
        self.assertEqual(src["seasons"][1], 2025)                    # last complete season; 2026 (in progress) excluded
        self.assertEqual(src["excluded_incomplete_seasons"], [2026])
        self.assertEqual(d["max_season"], 2025)
        b3 = d["buckets"]["3"]
        self.assertGreater(b3["n"], 200)
        # the key-number lump: P(fav wins by exactly 3 | spread 3) is far above the normal's ~3%
        self.assertGreater(b3["pmf"]["3"] / b3["n"], 0.06)
        self.assertGreater(d["all"]["n"], 2000)

    def test_committed_sigma_source(self):
        with open(DATA / "margin_sigma.json", encoding="utf-8") as f:
            s = json.load(f)
        self.assertEqual(len(s["source"]["sha256"]), 64)
        self.assertGreater(s["source"]["rows"], 7000)
        self.assertAlmostEqual(s["margin"]["sigma"], 12.7, delta=1.0)
        self.assertAlmostEqual(s["total"]["sigma"], 13.2, delta=1.0)
        self.assertLess(s["tie_rate"], 0.01)
        self.assertGreaterEqual(len(s["margin"]["by_season"]), 20)
        self.assertNotIn("2026", s["margin"]["by_season"])          # the evaluated season is not in the fit
        self.assertEqual(s["source"]["excluded_incomplete_seasons"], [2026])
        ot = s["overtime"]
        self.assertGreater(ot["n_ot"], 100)
        self.assertGreater(ot["tie_rate_given_ot"], 0.03)
        self.assertLess(ot["tie_rate_given_ot"], 0.15)
        self.assertAlmostEqual(ot["ot_rate"], ot["n_ot"] / s["margin"]["n"], places=5)

    def test_committed_and_fixture_share_the_same_url(self):
        d = json.loads((DATA / "nfl_margin_dist.json").read_text(encoding="utf-8"))
        e = json.loads((FIXTURES / "margin_table_expected.json").read_text(encoding="utf-8"))
        self.assertEqual(d["source"]["url"], e["dist"]["source"]["url"])
        self.assertEqual(d["schema"], e["dist"]["schema"])


if __name__ == "__main__":
    unittest.main()

"""The docs' results tables are rendered from the committed metrics fixtures.

``scripts/render_results.py`` owns the rendering; this test imports it as a module (no
subprocess) and checks that every ``<!-- results:<name> -->`` block in README.md and
docs/**/*.md equals the table rendered from its fixture, that every fixture the script names
exists, and that the renderer is deterministic. Nothing here touches the network.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "render_results.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("render_results", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules.setdefault("render_results", mod)
    spec.loader.exec_module(mod)
    return mod


rr = _load_module()


class FixtureRegistryTests(unittest.TestCase):
    def test_every_named_fixture_exists_and_is_canonical_json(self):
        for name, path in rr.fixture_paths().items():
            with self.subTest(fixture=name):
                self.assertTrue(path.is_file(), f"missing results fixture {path}")
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertIsInstance(data, dict)

    def test_required_fixtures_are_registered(self):
        required = {
            "replay_nfl_2026_w1", "replay_ncaaf_2026_w2", "week1_p05", "college_experiment_p04",
            "espn_wp_alignment_p04", "feed_parity_w1_p04", "fee_flip_p10", "eligibility_p11",
            "lines_eval_p13", "arb_fixture_p09",
        }
        self.assertTrue(required <= set(rr.RENDERERS), required - set(rr.RENDERERS))

    def test_render_is_deterministic_and_block_names_unique(self):
        a = rr.render_all()
        b = rr.render_all()
        self.assertEqual(a, b)
        for name, (fixture, text) in a.items():
            with self.subTest(block=name):
                self.assertIn(fixture, rr.RENDERERS)
                self.assertTrue(text.startswith("| "), f"{name} is not a Markdown table")
                self.assertTrue(text.endswith("|\n"), f"{name} must end with one newline")
                cells = lambda line: line.replace("\\|", "").count("|")  # noqa: E731 — escaped pipes are cell text
                header_cols = cells(text.split("\n", 1)[0])
                for line in text.rstrip("\n").split("\n"):
                    self.assertEqual(cells(line), header_cols, f"{name}: ragged row {line!r}")
                self.assertNotIn("\n\n", text)

    def test_docs_report_headline_numbers(self):
        """The conclusions the docs state in prose must be what the fixtures say."""
        nfl = rr.load("replay_nfl_2026_w1")
        ps = nfl["pooled_scrimmage"]
        self.assertAlmostEqual(ps["model"]["log_loss"], 0.406, places=3)
        self.assertLess(ps["model"]["log_loss"], min(ps["kalshi_before"]["log_loss"], ps["kalshi_after"]["log_loss"]))
        self.assertLess(nfl["intervals"]["model_minus_kalshi_before"]["hi90"], 0)
        self.assertLess(nfl["intervals"]["model_minus_kalshi_after"]["hi90"], 0)
        # the current blend is worse than the model alone on NFL week 1, interval excludes zero
        self.assertGreater(nfl["intervals"]["blend_minus_model"]["lo90"], 0)
        ncaaf = rr.load("replay_ncaaf_2026_w2")
        iv = ncaaf["intervals"]["blend_minus_model"]
        self.assertLessEqual(iv["lo90"], 0)
        self.assertGreaterEqual(iv["hi90"], 0)
        # NFL STEAL hold-to-settlement (executable pairing) intervals include zero at every edge
        for sim in nfl["simulations"]:
            if sim["pairing"] == "post_after" and not sim["lock"] and sim["source"] == "blend" and not sim["placebo"]:
                for edge, v in sim["by_edge"].items():
                    self.assertLessEqual(v["pnl_per_game_90"]["lo"], 0, edge)
                    self.assertGreaterEqual(v["pnl_per_game_90"]["hi"], 0, edge)
        espn = rr.load("espn_wp_alignment_p04")
        self.assertEqual(espn["pooled"]["alignment"], "post")
        # "every break-even LOCK variant loses" is an NFL-week statement: the first lock
        # simulation is the 0 %-of-hold-EV (break-even) rule (backtest_flags order 0/50/100 %)
        nfl_lock = next(s for s in nfl["simulations"] if s["lock"] and s["pairing"] == "post_after" and not s["placebo"])
        for edge, v in nfl_lock["by_edge"].items():
            self.assertLess(v["pnl"], 0, edge)
        self.assertAlmostEqual(nfl_lock["by_edge"]["0.02"]["roi"], -0.047, places=3)
        self.assertAlmostEqual(nfl_lock["by_edge"]["0.06"]["roi"], -0.130, places=3)
        # ... but on college week 2 the break-even lock is positive at the 10 % edge with an
        # interval that excludes zero, so the docs must not say it loses everywhere
        nc_lock = next(s for s in ncaaf["simulations"] if s["lock"] and s["pairing"] == "post_after" and not s["placebo"])
        for edge in ("0.02", "0.04", "0.06"):
            self.assertLess(nc_lock["by_edge"][edge]["pnl"], 0, edge)
        self.assertAlmostEqual(nc_lock["by_edge"]["0.1"]["roi"], 0.063, places=3)
        self.assertGreater(nc_lock["by_edge"]["0.1"]["pnl_per_game_90"]["lo"], 0)
        for doc in ("README.md", "docs/FEES_EXPLAINED.md", "docs/EXTENSION.md", "docs/MODEL.md"):
            text = (ROOT / doc).read_text(encoding="utf-8").lower()
            self.assertNotIn("every break-even lock variant loses (", text, doc)
            self.assertNotIn("every break-even lock variant loses.", text, doc)
            self.assertNotIn("in every replay so far a break-even lock has lost", text, doc)
        # feed parity: the timeout counts are 2,632 / 2,633 home and 2,633 / 2,633 away, not "100 %"
        parity = rr.load("feed_parity_w1_p04")["pooled"]
        self.assertEqual((parity["home_timeouts"]["agree"], parity["home_timeouts"]["n"]), (2632, 2633))
        self.assertEqual((parity["away_timeouts"]["agree"], parity["away_timeouts"]["n"]), (2633, 2633))
        # lines: the in-play numbers the docs quote, and the in-sample caveat behind them
        lines = rr.load("lines_eval_p13")
        model_text = (ROOT / "docs/MODEL.md").read_text(encoding="utf-8")
        self.assertIn("were chosen on these same 16 games", model_text)
        self.assertIn("0.632 on spreads and 0.718", model_text)
        readme_text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("picked on those same 16 games", readme_text)
        self.assertEqual(lines["games_scored"], 16)


class DocBlockTests(unittest.TestCase):
    def test_every_doc_block_matches_its_render(self):
        problems = rr.check()
        self.assertEqual(problems, {}, "\n".join(f"{p}: {q}" for p, qs in problems.items() for q in qs))

    def test_every_rendered_block_is_used_by_a_doc(self):
        used = rr.used_blocks()
        unused = sorted(set(rr.render_all()) - set(used))
        self.assertEqual(unused, [], f"rendered but not placed in any doc: {unused}")

    def test_docs_carry_the_replay_blocks(self):
        used = rr.used_blocks()
        for name in ("nfl_w1_pooled", "nfl_w1_intervals", "nfl_w1_fit", "nfl_w1_steal", "nfl_w1_lock", "ncaaf_w2_pooled", "ncaaf_w2_steal", "lines_eval_p13", "week1_p05_rules", "feed_parity_w1_p04"):
            self.assertIn("docs/MODEL.md", used.get(name, []), name)

    def test_check_detects_stale_and_unknown_blocks(self):
        rendered = rr.render_all()
        good = "intro\n<!-- results:nfl_w1_fit -->\n" + rendered["nfl_w1_fit"][1] + "<!-- /results:nfl_w1_fit -->\nafter\n"
        self.assertEqual(rr.check_text(good, rendered), [])
        stale = good.replace("| linear |", "| LINEAR |")
        self.assertEqual(len(rr.check_text(stale, rendered)), 1)
        unknown = good.replace("nfl_w1_fit", "no_such_block")
        self.assertIn("unknown", rr.check_text(unknown, rendered)[0])
        self.assertEqual(rr.rewrite_text(stale, rendered), good)

    def test_write_rewrites_only_stale_files(self):
        rendered = rr.render_all()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            stale = "x\n<!-- results:nfl_w1_fit -->\nold\n<!-- /results:nfl_w1_fit -->\n"
            (root / "README.md").write_text(stale, encoding="utf-8")
            (root / "docs" / "OK.md").write_text("no blocks here\n", encoding="utf-8")
            self.assertEqual(rr.check(root), {"README.md": ["block 'nfl_w1_fit' is stale (run: python scripts/render_results.py --write)"]})
            self.assertEqual(rr.write(root), ["README.md"])
            self.assertEqual(rr.check(root), {})
            self.assertIn(rendered["nfl_w1_fit"][1], (root / "README.md").read_text(encoding="utf-8"))
            self.assertEqual(rr.write(root), [])


if __name__ == "__main__":
    unittest.main()

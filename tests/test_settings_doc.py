"""Every settings key the engine reads is declared (``config.declare_setting``) *and*
documented in AGENTS.md's settings table — zero undocumented, zero undeclared.

Keys are declared at import time by the module that owns them, so the registry is only
complete once every module under ``arb_engine`` has been imported; ``pkgutil.walk_packages``
does that here (an import error is a test failure: a module that cannot import cannot
declare its keys, and the docs table would silently drift)."""

import importlib
import pkgutil
import re
import unittest
from pathlib import Path

import arb_engine
from arb_engine.config import KNOWN_SETTINGS

AGENTS_MD = Path(__file__).resolve().parents[1] / "AGENTS.md"
ROW = re.compile(r"^\|\s*`(?P<key>[^`]+)`\s*\|\s*(?P<env>[^|]*)\|\s*(?P<default>[^|]*)\|\s*(?P<doc>.*)\|\s*$")


def import_every_module() -> list[str]:
    names: list[str] = []
    for info in pkgutil.walk_packages(arb_engine.__path__, prefix="arb_engine."):
        importlib.import_module(info.name)
        names.append(info.name)
    return names


def documented_settings(text: str) -> dict[str, dict[str, str]]:
    """``{key: {"env": ..., "default": ..., "doc": ...}}`` from the ``## Settings`` table."""
    section = text.split("## Settings", 1)[1].split("\n## ", 1)[0]
    rows: dict[str, dict[str, str]] = {}
    for line in section.splitlines():
        m = ROW.match(line.strip())
        if not m or m.group("key") in ("Key", "key"):
            continue
        env = m.group("env").strip().strip("`").strip()
        rows[m.group("key")] = {"env": "" if env in ("", "—", "-") else env, "default": m.group("default").strip(), "doc": m.group("doc").strip()}
    return rows


class SettingsDocTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = import_every_module()
        cls.text = AGENTS_MD.read_text(encoding="utf-8")
        cls.documented = documented_settings(cls.text)

    def test_every_module_imports_and_declares(self):
        self.assertGreater(len(self.modules), 40, self.modules)
        for must in ("arb_engine.scanner", "arb_engine.compliance", "arb_engine.strategy.inplay", "arb_engine.strategy.maker", "arb_engine.bridge"):
            self.assertIn(must, self.modules)
        self.assertGreaterEqual(len(KNOWN_SETTINGS), 20, sorted(KNOWN_SETTINGS))

    def test_zero_undocumented(self):
        undocumented = sorted(k for k in KNOWN_SETTINGS if k not in self.documented)
        self.assertEqual(undocumented, [], f"declared but missing from AGENTS.md settings table: {undocumented}")
        for key, spec in KNOWN_SETTINGS.items():
            # The key name or its env var must appear in the prose/table as well (grep-able docs).
            self.assertTrue(f"`{key}`" in self.text or (spec.env and spec.env in self.text), f"{key} is not mentioned in AGENTS.md")

    def test_zero_undeclared(self):
        undeclared = sorted(k for k in self.documented if k not in KNOWN_SETTINGS)
        self.assertEqual(undeclared, [], f"documented in AGENTS.md but never declared with config.declare_setting: {undeclared}")

    def test_table_matches_the_registry(self):
        for key, spec in KNOWN_SETTINGS.items():
            row = self.documented[key]
            self.assertEqual(row["env"], spec.env or "", f"{key}: env var in AGENTS.md ({row['env']!r}) != declared ({spec.env!r})")
            self.assertTrue(row["doc"], f"{key}: empty doc cell")
            self.assertTrue(row["default"], f"{key}: empty default cell")
            self.assertTrue(spec.doc, f"{key}: declare_setting(...) has no doc string")
            self.assertIn(f"`{key}`", self.text)

    def test_table_has_no_duplicate_rows(self):
        section = self.text.split("## Settings", 1)[1].split("\n## ", 1)[0]
        keys = [m.group("key") for line in section.splitlines() if (m := ROW.match(line.strip())) and m.group("key") != "Key"]
        self.assertEqual(len(keys), len(set(keys)), f"duplicate settings rows: {[k for k in keys if keys.count(k) > 1]}")


if __name__ == "__main__":
    unittest.main()

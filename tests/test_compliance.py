"""Venue eligibility table: parses, is sourced, excludes global Polymarket by default,
honours the EXECUTABLE_VENUES override and per-event restricted quotes, flags stale rows."""

import datetime as dt
import os
import unittest
from unittest import mock

from arb_engine import compliance
from arb_engine.models import OutcomeQuote


class TableTests(unittest.TestCase):
    def test_table_parses_and_every_row_is_sourced(self):
        rules = compliance.load_rules()
        self.assertEqual(set(rules), {"kalshi", "robinhood", "polymarket", "polymarket_us"})
        self.assertEqual(compliance.check_table(rules), [])
        for v, r in rules.items():
            self.assertIsInstance(r["executable_for_us"], bool, v)
            dt.date.fromisoformat(r["verified"])
            self.assertTrue(r["source"].startswith("http"), v)
        self.assertFalse(rules["polymarket"]["executable_for_us"])
        self.assertTrue(rules["kalshi"]["executable_for_us"])
        self.assertTrue(rules["robinhood"]["executable_for_us"])
        self.assertTrue(rules["polymarket_us"]["executable_for_us"])
        self.assertFalse(rules["polymarket_us"]["adapter"])

    def test_check_table_reports_missing_fields(self):
        bad = {"x": {"executable_for_us": "yes", "verified": "soon", "source": ""}}
        problems = compliance.check_table(bad)
        self.assertEqual(len(problems), 3)
        self.assertTrue(any("bool" in p for p in problems))
        self.assertTrue(any("ISO date" in p for p in problems))
        self.assertTrue(any("source" in p for p in problems))


class ExecutableVenuesTests(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("EXECUTABLE_VENUES", None)

    def tearDown(self):
        self._env.stop()

    def test_polymarket_excluded_by_default(self):
        self.assertEqual(compliance.executable_venues(), {"kalshi", "robinhood"})
        self.assertEqual(compliance.executable_venues({}), {"kalshi", "robinhood"})
        self.assertTrue(compliance.is_executable("robinhood"))
        self.assertFalse(compliance.is_executable("polymarket"))
        # polymarket_us is executable but has no adapter; only with_adapter_only=False lists it.
        self.assertNotIn("polymarket_us", compliance.executable_venues())
        self.assertIn("polymarket_us", compliance.executable_venues(with_adapter_only=False))

    def test_settings_and_env_override(self):
        self.assertEqual(compliance.executable_venues({"executable_venues": "kalshi, Polymarket"}), {"kalshi", "polymarket"})
        self.assertEqual(compliance.executable_venues({"executable_venues": ["robinhood"]}), {"robinhood"})
        with mock.patch.dict(os.environ, {"EXECUTABLE_VENUES": "kalshi,robinhood,polymarket"}):
            self.assertEqual(compliance.executable_venues(), {"kalshi", "robinhood", "polymarket"})
            self.assertTrue(compliance.is_executable("polymarket"))
            # An explicit settings value beats the environment.
            self.assertEqual(compliance.executable_venues({"executable_venues": "kalshi"}), {"kalshi"})

    def test_restricted_meta_excludes_the_venue_for_that_event(self):
        q_ok = OutcomeQuote("robinhood", "c1", "nfl:A|B:2026-09-20", "A", ask=0.5, meta={"side": "yes"})
        q_restricted = OutcomeQuote("polymarket", "tok", "nfl:A|B:2026-09-20", "B", ask=0.5, meta={"restricted": True})
        opted_in = {"executable_venues": "kalshi,robinhood,polymarket"}
        self.assertEqual(compliance.executable_venues(opted_in), {"kalshi", "robinhood", "polymarket"})
        # The venue-wide restricted flag backs the table's default but never vetoes an explicit override.
        self.assertEqual(compliance.executable_venues(opted_in, quotes=[q_ok, q_restricted]), {"kalshi", "robinhood", "polymarket"})
        self.assertEqual(compliance.executable_venues({"executable_venues": "kalshi,robinhood"}, quotes=[q_ok, q_restricted]), {"kalshi", "robinhood"})
        self.assertNotIn("polymarket", compliance.executable_venues(None, quotes=[q_ok, q_restricted]))
        self.assertIn("restricted", compliance.ineligible_reason("polymarket", None, quote=q_restricted))
        self.assertIsNone(compliance.ineligible_reason("polymarket", opted_in, quote=q_restricted))
        self.assertIsNone(compliance.ineligible_reason("polymarket", opted_in))

    def test_home_state_restrictions(self):
        rules = {"kalshi": {**compliance.rule("kalshi"), "state_restrictions": ["NV"]}, "robinhood": compliance.rule("robinhood"), "polymarket": compliance.rule("polymarket")}
        with mock.patch.object(compliance, "load_rules", lambda path=None: rules):
            self.assertEqual(compliance.executable_venues(home_state="nv"), {"robinhood"})
            self.assertEqual(compliance.executable_venues(home_state="CA"), {"kalshi", "robinhood"})

    def test_reasons_and_notes(self):
        self.assertIsNone(compliance.ineligible_reason("kalshi"))
        r = compliance.ineligible_reason("polymarket")
        self.assertIn("not executable for US persons", r)
        self.assertIn("cftc.gov", r)
        self.assertIn("no adapter", compliance.ineligible_reason("polymarket_us"))
        self.assertIn("not in venue_rules.json", compliance.ineligible_reason("betfair"))
        self.assertEqual(compliance.eligibility_note("robinhood"), "executable for US accounts")
        self.assertTrue(compliance.eligibility_note("polymarket").startswith("NOT EXECUTABLE"))


class StaleTests(unittest.TestCase):
    def test_stale_verification_flags_40_day_old_row(self):
        rules = {"kalshi": {"verified": "2026-08-10"}, "robinhood": {"verified": "2026-09-01"}, "polymarket": {"verified": "n/a"}}
        stale = compliance.stale_verification(days=30, today=dt.date(2026, 9, 19), rules=rules)
        self.assertEqual(stale["kalshi"], 40)
        self.assertNotIn("robinhood", stale)
        self.assertEqual(stale["polymarket"], 10**6)  # unparseable date is always stale
        self.assertEqual(compliance.stale_verification(days=45, today=dt.date(2026, 9, 19), rules={"kalshi": {"verified": "2026-08-10"}}), {})

    def test_shipped_table_is_fresh_relative_to_its_own_dates(self):
        # The committed table must not be stale on the day it was written; a live check
        # (today) is what stale_verification() without ``today`` is for before each week.
        latest = max(dt.date.fromisoformat(r["verified"]) for r in compliance.load_rules().values())
        self.assertEqual(compliance.stale_verification(today=latest), {})


if __name__ == "__main__":
    unittest.main()

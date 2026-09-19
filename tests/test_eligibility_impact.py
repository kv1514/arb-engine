"""scripts/eligibility_impact.py: synthetic snapshots give the expected before/after shares,
a recorded SQLite file round-trips, and the committed fixture result is reproducible."""

import importlib.util
import json
import os
import unittest
from pathlib import Path

from arb_engine.scanner import EventReport, OutcomeReport, ScanResult, VenuePrice

ROOT = Path(__file__).resolve().parent.parent
RESULT = ROOT / "tests" / "fixtures" / "results" / "eligibility_p11.json"


def _load_script():
    spec = importlib.util.spec_from_file_location("eligibility_impact", ROOT / "scripts" / "eligibility_impact.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(key, outcomes, live=False):
    return {"ts": 1.0, "event_key": key, "market_type": "moneyline", "live": live, "outcomes": outcomes}


class ArbImpactTests(unittest.TestCase):
    def setUp(self):
        self.ei = _load_script()
        self.exec = {"kalshi", "robinhood"}

    def test_synthetic_shares(self):
        rows = [
            # arb only thanks to Polymarket: lost after
            _row("e1", {"A": {"polymarket": 0.45, "kalshi": 0.55}, "B": {"kalshi": 0.50, "robinhood": 0.52}}),
            # arb on executable venues in both views
            _row("e2", {"A": {"kalshi": 0.48}, "B": {"robinhood": 0.50, "polymarket": 0.51}}),
            # Polymarket leg before, but Robinhood is close enough that the arb survives
            _row("e3", {"A": {"polymarket": 0.40, "robinhood": 0.41}, "B": {"kalshi": 0.55}}),
            # not an arb
            _row("e4", {"A": {"kalshi": 0.60}, "B": {"robinhood": 0.50}}),
            # live: ignored
            _row("e5", {"A": {"polymarket": 0.30}, "B": {"kalshi": 0.30}}, live=True),
            # one outcome only quoted on a non-executable venue: not an arb after
            _row("e6", {"A": {"polymarket": 0.40}, "B": {"kalshi": 0.50}}),
        ]
        m = self.ei.arb_impact(rows, self.exec)
        self.assertEqual(m["snapshots"], 5)
        self.assertEqual(m["arbs_before"], 4)           # e1, e2, e3, e6
        self.assertEqual(m["arbs_before_with_non_executable_leg"], 3)   # e1, e3, e6
        self.assertAlmostEqual(m["share_before_non_executable"], 0.75)
        self.assertEqual(m["arbs_after"], 2)            # e2, e3
        self.assertEqual(m["arbs_lost"], 2)
        self.assertAlmostEqual(m["share_after_of_before"], 0.5)
        self.assertEqual(m["non_executable_legs_by_venue"], {"polymarket": 3})
        self.assertEqual(self.ei.arb_impact([], self.exec)["share_before_non_executable"], None)

    def test_rows_from_reports_skip_stale_and_mirrors(self):
        vp = lambda venue, all_in, **kw: VenuePrice(venue=venue, market_id="m", ask=all_in, bid=None, ask_size=None, fee_per_contract=0, all_in=all_in, max_buy_price=None, url=None, **kw)  # noqa: E731
        rep = EventReport(event_key="e", title="t", sport="nfl", start_time=None, venues=["kalshi", "robinhood", "polymarket"], outcomes=[
            OutcomeReport("A", "A", None, [vp("kalshi", 0.5), vp("robinhood", 0.49, mirror_of="kalshi"), vp("polymarket", 0.4, stale=True)], None, None, None),
            OutcomeReport("B", "B", None, [vp("kalshi", 0.5), vp("robinhood", None)], None, None, None),
        ], arb=None, sized_arb=None, gross_sum=None, margin=None, tie_rule="half")
        rows = self.ei.rows_from_reports([rep], ts=5.0)
        self.assertEqual(rows[0]["outcomes"], {"A": {"kalshi": 0.5}, "B": {"kalshi": 0.5}})
        # dict form (scan --json) is accepted too
        from dataclasses import asdict

        self.assertEqual(self.ei.rows_from_reports([asdict(rep)]), [{**rows[0], "ts": 0.0}])

    def test_rows_from_db_round_trip(self):
        from arb_engine.store import Store

        path = os.path.join(os.environ.get("TMPDIR", "/tmp"), "eligibility_impact_test.db")
        if os.path.exists(path):
            os.remove(path)
        vp = lambda venue, all_in: VenuePrice(venue=venue, market_id="m", ask=all_in, bid=None, ask_size=None, fee_per_contract=0, all_in=all_in, max_buy_price=None, url=None)  # noqa: E731
        ev = EventReport(event_key="nfl:A|B:2026-09-20", title="t", sport="nfl", start_time=None, venues=["kalshi", "polymarket"], outcomes=[
            OutcomeReport("A", "A", None, [vp("polymarket", 0.45), vp("kalshi", 0.55)], None, None, None),
            OutcomeReport("B", "B", None, [vp("kalshi", 0.50)], None, None, None),
        ], arb=None, sized_arb=None, gross_sum=0.95, margin=0.05, tie_rule="half")
        st = Store(path)
        st.record_scan(ScanResult(sport="nfl", fetched_at=1.0, venues=["kalshi", "polymarket"], events=[ev], errors={}))
        st.close()
        rows = self.ei.rows_from_db(path, limit=10, sport="nfl")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcomes"], {"A": {"polymarket": 0.45, "kalshi": 0.55}, "B": {"kalshi": 0.50}})
        m = self.ei.arb_impact(rows, self.exec)
        self.assertEqual((m["arbs_before"], m["arbs_before_with_non_executable_leg"], m["arbs_after"]), (1, 1, 0))
        self.assertEqual(self.ei.rows_from_db(path, sport="tennis"), [])
        os.remove(path)


class FixtureResultTests(unittest.TestCase):
    def test_fixture_report_matches_committed_result(self):
        """The offline acceptance: the script over the fixture scans reproduces
        tests/fixtures/results/eligibility_p11.json exactly."""
        ei = _load_script()
        rep = ei.report(ei.fixture_sources())
        with open(RESULT, encoding="utf-8") as f:
            committed = json.load(f)
        self.assertEqual(rep, committed)
        self.assertEqual(rep["executable_venues"], ["kalshi", "robinhood"])
        # After the table: zero maker watches with a non-executable hedge in either fixture.
        for name, src in rep["sources"].items():
            self.assertEqual(src["maker_hedges"]["watches_after_non_executable_hedge"], 0, name)
            self.assertGreater(src["maker_hedges"]["watches_before_non_executable_hedge"], 0, name)
        self.assertGreater(rep["sources"]["ncaaf"]["maker_hedges"]["share_before_non_executable"], 0.5)
        text = ei.format_report(rep)
        self.assertIn("[ncaaf] maker watches before=", text)

    def test_main_writes_metrics_json(self):
        ei = _load_script()
        out = os.path.join(os.environ.get("TMPDIR", "/tmp"), "eligibility_p11_test.json")
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ei.main(["--fixtures", "--out", out]), 0)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["item"], "P11")
        os.remove(out)


if __name__ == "__main__":
    unittest.main()

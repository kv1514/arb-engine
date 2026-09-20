"""L4: the preflight report on fixtures (no network), the CLI plugin, the Sunday launcher's
supervisor mechanics with a stub interpreter, and the runbook's commands all answering --help."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from arb_engine import cli, preflight as pf
from arb_engine.venues import KalshiClient, PolymarketAdapter, RobinhoodAdapter
from arb_engine.venues.espn import ESPNClient
from arb_engine.venues.http import HttpError

from .helpers import FakeHttp, load
from .test_scanner import _adapters

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DATE = "2026-09-17"  # BUF @ DET (final) in tests/fixtures/espn/scoreboard.json, merged on all three venues
SUNDAY = "2026-09-20"        # CHI @ MIN and PHI @ TEN on the same scoreboard; only PHI @ TEN has a (Robinhood) quote


class BoomHttp:
    """A transport whose every call fails the way a dead host does."""

    transport = "fake"

    def get(self, url, params=None, headers=None, raw=False):
        raise HttpError(0, url, "curl: (7) Failed to connect")


def _clients(bridge_health=None, bridge_fail=False, espn=True, ext_rc=0):
    adapters = _adapters()
    by = {a.venue: a for a in adapters}
    if bridge_fail:
        bridge = BoomHttp()
    else:
        bridge = FakeHttp({"/health": bridge_health if bridge_health is not None else {"ok": True, "service": "arb-engine bridge"}})
    espn_client = ESPNClient(http=FakeHttp({"/scoreboard": load("espn/scoreboard.json")}), sport="nfl") if espn else None
    return pf.Clients(kalshi=by["kalshi"].client, polymarket_http=by["polymarket"].http, robinhood=by["robinhood"], espn=espn_client, adapters=adapters, bridge_http=bridge, ext_checker=lambda d: (ext_rc, "check_extension: x\nPASS (0 warning(s))" if ext_rc == 0 else "FAIL: manifest.json is not valid JSON\nFAIL (1 error(s))"), now=1_800_000_000.0)


class LocalChecks(unittest.TestCase):
    def test_python_version_gate(self):
        self.assertEqual(pf.check_python((3, 13, 2)).status, pf.PASS)
        c = pf.check_python((3, 12, 9))  # CI's 3.10-3.12 are supported; 3.13 is recommended
        self.assertEqual(c.status, pf.PASS)
        self.assertIn("3.13", c.detail)
        self.assertEqual(pf.check_python((3, 9, 1)).status, pf.FAIL)

    def test_imports_every_module_stdlib_only(self):
        c = pf.check_imports()
        self.assertEqual(c.status, pf.PASS, c.detail)
        self.assertGreater(c.data["modules"], 40)
        self.assertEqual(c.data["failed"], {})
        self.assertEqual([m for m in c.data["third_party"] if m not in pf.OPTIONAL_THIRD_PARTY], [])

    def test_imports_walk_is_independent_of_this_process(self):
        """The reviewer's scenario: the parent has the package (and a fake third-party module)
        loaded already; the subprocess walk still sees what the package itself pulls in."""
        import types

        fake = types.ModuleType("requests")
        sys.modules["requests"] = fake
        try:
            c = pf.check_imports()
            self.assertEqual(c.status, pf.PASS, c.detail)  # the parent's sys.modules is not consulted
            self.assertNotIn("requests", c.data["third_party"])
        finally:
            sys.modules.pop("requests", None)
        # An injected walk result stands in for a package that imports a third-party module.
        seen = {}

        def walk(root, package):
            seen["args"] = (root, package)
            return {"modules": 59, "failed": {}, "loaded": ["arb_engine", "json", "requests", "sys", "_frozen_importlib", "sitecustomize", "cryptography"]}

        c = pf.check_imports(walk=walk)
        self.assertEqual(c.status, pf.FAIL)
        self.assertIn("non-stdlib requests", c.detail)
        self.assertEqual(c.data["third_party"], ["cryptography", "requests"])
        self.assertEqual(seen["args"], (str(ROOT), "arb_engine"))
        opt = pf.check_imports(walk=lambda r, p: {"modules": 59, "failed": {}, "loaded": ["arb_engine", "cryptography"]})
        self.assertEqual(opt.status, pf.PASS)
        self.assertIn("optional: cryptography", opt.detail)
        bad = pf.check_imports(walk=lambda r, p: {"modules": 59, "failed": {"arb_engine.scanner": "ModuleNotFoundError: No module named 'requests'"}, "loaded": []})
        self.assertEqual(bad.status, pf.FAIL)
        self.assertIn("arb_engine.scanner", bad.detail)
        self.assertEqual(pf.check_imports(walk=lambda r, p: {"modules": 0, "failed": {}, "loaded": []}).status, pf.FAIL)

        def boom(r, p):
            raise subprocess.TimeoutExpired("python", 1)

        crash = pf.check_imports(walk=boom)
        self.assertEqual(crash.status, pf.FAIL)
        self.assertIn("import walk failed", crash.detail)

    def test_import_walk_subprocess_detects_third_party_import(self):
        """End to end on a throwaway package: a module with a top-level ``import fakepkg``
        (a non-stdlib module sitting next to the package) shows up as third-party."""
        import types

        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "pkg", "sub"))
            Path(d, "pkg", "__init__.py").write_text("", encoding="utf-8")
            Path(d, "pkg", "sub", "__init__.py").write_text("", encoding="utf-8")
            Path(d, "pkg", "sub", "clean.py").write_text("import json\n", encoding="utf-8")
            Path(d, "pkg", "dirty.py").write_text("import fakepkg\n", encoding="utf-8")
            Path(d, "fakepkg.py").write_text("X = 1\n", encoding="utf-8")
            res = pf.run_import_walk(d, "pkg")
            self.assertEqual(res["modules"], 3)
            self.assertEqual(res["failed"], {})
            self.assertIn("fakepkg", res["loaded"])
            pkg = types.SimpleNamespace(__name__="pkg", __path__=[os.path.join(d, "pkg")])
            c = pf.check_imports(pkg)
            self.assertEqual(c.status, pf.FAIL, c.detail)
            self.assertEqual(c.data["third_party"], ["fakepkg"])
            Path(d, "pkg", "broken.py").write_text("import nosuchmodule_xyz\n", encoding="utf-8")
            c2 = pf.check_imports(pkg)
            self.assertEqual(c2.status, pf.FAIL)
            self.assertIn("pkg.broken", c2.detail)
            self.assertIn("ModuleNotFoundError", c2.data["failed"]["pkg.broken"])

    def test_wp_model_prices_known_state(self):
        c = pf.check_wp_model()
        self.assertEqual(c.status, pf.PASS, c.detail)
        self.assertGreater(c.data["p_home_known_state"], 0.5)
        self.assertGreater(c.data["p_home_pregame_minus3"], 0.5)
        self.assertLess(c.data["p_home_pregame_plus3"], 0.5)
        bad = pf.check_wp_model({"home_score": 0, "away_score": 21, "game_seconds_remaining": 300})
        self.assertEqual(bad.status, pf.FAIL)  # a trailing home side is not > 0.5: the sanity gate fires

    def test_settings_table_default_then_override_warns(self):
        env = os.environ.pop("EXECUTABLE_VENUES", None)
        try:
            c = pf.check_settings({}, bankroll=1000, kelly=0.25)
            self.assertEqual(c.status, pf.PASS, c.detail)
            self.assertEqual(c.data["executable_venues"], ["kalshi", "robinhood"])
            self.assertIn("bankroll $1000", c.detail)
            w = pf.check_settings({"executable_venues": "kalshi,robinhood,polymarket"})
            self.assertEqual(w.status, pf.WARN)
            self.assertIn("polymarket is executable", w.detail)
            a = pf.check_settings({"executable_venues": "all"})
            self.assertEqual(a.status, pf.WARN)
            self.assertIsNone(a.data["executable_venues"])
            f = pf.check_settings({}, bankroll=-5, kelly=1.5)
            self.assertEqual(f.status, pf.FAIL)
            self.assertIn("bankroll", f.detail)
            self.assertIn("kelly", f.detail)
        finally:
            if env is not None:
                os.environ["EXECUTABLE_VENUES"] = env

    def test_out_dir_thresholds(self):
        class DU:
            def __init__(self, free):
                self.free = free

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "out")
            self.assertEqual(pf.check_out_dir(p, disk_usage=lambda _: DU(50 << 30)).status, pf.PASS)
            self.assertTrue(os.path.isdir(os.path.join(p, "logs")) and os.path.isdir(os.path.join(p, "run")))
            self.assertEqual(pf.check_out_dir(p, disk_usage=lambda _: DU(500 << 20)).status, pf.WARN)
            self.assertEqual(pf.check_out_dir(p, disk_usage=lambda _: DU(10 << 20)).status, pf.FAIL)
            self.assertEqual([f for f in os.listdir(p) if f.startswith(".preflight")], [])  # the probe file is removed
        if os.name == "posix" and os.geteuid() != 0:
            with tempfile.TemporaryDirectory() as d:
                os.chmod(d, stat.S_IRUSR | stat.S_IXUSR)
                try:
                    self.assertEqual(pf.check_out_dir(os.path.join(d, "out")).status, pf.FAIL)
                finally:
                    os.chmod(d, stat.S_IRWXU)

    def test_extension_checker_outcomes(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(pf.check_extension(lambda _: (0, "PASS"), d).status, pf.FAIL)  # no manifest at all
            Path(d, "manifest.json").write_text("{}", encoding="utf-8")
            self.assertEqual(pf.check_extension(lambda _: (0, "x\nPASS (0 warning(s))"), d).status, pf.PASS)
            c = pf.check_extension(lambda _: (1, "FAIL: bad\nFAIL (1 error(s))"), d)
            self.assertEqual(c.status, pf.FAIL)
            self.assertIn("FAIL (1 error(s))", c.detail)
            self.assertEqual(pf.check_extension(None, d).status, pf.WARN)

    def test_real_extension_dir_passes_checker(self):
        c = pf.check_extension(pf.run_extension_checker, str(ROOT / "extension"))
        self.assertEqual(c.status, pf.PASS, c.detail)


class VenueChecks(unittest.TestCase):
    def test_kalshi_polymarket_robinhood_on_fixtures(self):
        cl = _clients()
        k = pf.check_kalshi(cl.kalshi, "nfl")
        self.assertEqual(k.status, pf.PASS, k.detail)
        self.assertEqual(k.data["series"], "KXNFLGAME")
        self.assertIsNotNone(k.latency_ms)
        self.assertTrue(any("series_ticker=KXNFLGAME&limit=1" in u or ("series_ticker=KXNFLGAME" in u and "limit=1" in u) for u in cl.kalshi.http.calls))
        p = pf.check_polymarket(cl.polymarket_http, "nfl")
        self.assertEqual(p.status, pf.PASS, p.detail)
        self.assertEqual(p.data["slug"], "nfl-det-buf-2026-09-18")
        r = pf.check_robinhood(cl.robinhood, "nfl")
        self.assertEqual(r.status, pf.PASS, r.detail)
        self.assertGreater(r.data["game_events"], 0)

    def test_dead_hosts_fail_with_latency(self):
        dead = KalshiClient(env="prod", http=BoomHttp())
        c = pf.check_kalshi(dead, "nfl")
        self.assertEqual(c.status, pf.FAIL)
        self.assertIn("Failed to connect", c.detail)
        self.assertIsNotNone(c.latency_ms)
        self.assertEqual(pf.check_polymarket(BoomHttp(), "nfl").status, pf.FAIL)
        self.assertEqual(pf.check_robinhood(RobinhoodAdapter(http=BoomHttp()), "nfl").status, pf.FAIL)
        self.assertEqual(pf.check_kalshi(None).status, pf.WARN)  # offline

    def test_empty_answers_warn_not_fail(self):
        self.assertEqual(pf.check_kalshi(KalshiClient(env="prod", http=FakeHttp({"/markets": {"markets": []}})), "nfl").status, pf.WARN)
        self.assertEqual(pf.check_polymarket(FakeHttp({"/events": []}), "nfl").status, pf.WARN)

    def test_robinhood_row_bypasses_the_catalogue_cache(self):
        """A fresh out/cache/robinhood_nfl_catalogue.json must not turn a dead host into a PASS
        (the row claims reachability + latency); a live fetch rewrites the cache for the merge."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "robinhood_nfl_catalogue.json")
            pp = load("robinhood_page_props_nfl.json")
            Path(path).write_text(json.dumps({"events": pp.get("events"), "eventStates": pp.get("eventStates"), "quotes": pp.get("quotes"), "cached_at": time.time()}), encoding="utf-8")
            dead = RobinhoodAdapter(http=BoomHttp(), cache_dir=d)
            dead.catalogue_ttl = 1800.0  # what RobinhoodAdapter() (no injected http) uses
            self.assertEqual(dead.category_page("nfl")["events"], pp.get("events"))  # the adapter itself would serve the cache
            c = pf.check_robinhood(dead, "nfl")
            self.assertEqual(c.status, pf.FAIL, c.detail)
            self.assertIn("Failed to connect", c.detail)
            os.remove(path)
            live = [a for a in _adapters() if a.venue == "robinhood"][0]
            live.cache_dir, live.catalogue_ttl = d, 1800.0
            ok = pf.check_robinhood(live, "nfl")
            self.assertEqual(ok.status, pf.PASS, ok.detail)
            self.assertTrue(any("/prediction-markets/nfl/" in u for u in live.http.calls))
            self.assertTrue(os.path.exists(path))  # rewritten on the way out: the merge stays warm

    def test_espn_scoreboard_rows(self):
        cl = _clients()
        c = pf.check_espn(cl.espn, "nfl", FIXTURE_DATE)
        self.assertEqual(c.status, pf.PASS, c.detail)  # the one game on the date is a final: no spread needed
        games = c.data["games"]
        self.assertEqual([g["event_key"] for g in games], ["nfl:BUF|DET:2026-09-17"])
        self.assertEqual(c.data["with_spread"], 0)
        self.assertEqual(c.data["other_dates"], 2)  # ESPN's NFL scoreboard answers with the whole week
        self.assertIn("kickoffs", c.detail)
        self.assertIn(" ET", c.detail)
        self.assertIn("+2 on other dates", c.detail)
        sun = pf.check_espn(cl.espn, "nfl", SUNDAY)
        self.assertEqual(sun.status, pf.PASS, sun.detail)
        self.assertEqual([g["event_key"] for g in sun.data["games"]], ["nfl:CHI|MIN:2026-09-20", "nfl:PHI|TEN:2026-09-20"])
        self.assertEqual(sun.data["with_spread"], 2)
        self.assertTrue(all(g["kickoff"] for g in sun.data["games"]))
        # A pre-game without an odds block is a WARN (the model has no anchor), a wrong date a FAIL.
        sb = load("espn/scoreboard.json")
        sb["events"][1]["competitions"][0].pop("odds")
        warn = pf.check_espn(ESPNClient(http=FakeHttp({"/scoreboard": sb}), sport="nfl"), "nfl", SUNDAY)
        self.assertEqual(warn.status, pf.WARN)
        self.assertIn("no spread yet for MIN@CHI", warn.detail)
        wrong = pf.check_espn(cl.espn, "nfl", "2026-09-21")
        self.assertEqual(wrong.status, pf.FAIL)
        self.assertIn("3 on other dates", wrong.detail)
        self.assertEqual(pf.check_espn(ESPNClient(http=FakeHttp({"/scoreboard": {"events": []}}), sport="nfl"), "nfl", "2026-01-01").status, pf.FAIL)
        self.assertEqual(pf.check_espn(ESPNClient(http=BoomHttp(), sport="nfl"), "nfl", FIXTURE_DATE).status, pf.FAIL)
        self.assertEqual(pf.check_espn(None, "nfl", FIXTURE_DATE).status, pf.WARN)


class MatchChecks(unittest.TestCase):
    def _games(self, date=FIXTURE_DATE):
        return pf.check_espn(_clients().espn, "nfl", date).data["games"]

    def test_match_counts_per_venue_and_executable(self):
        c = pf.check_matches(_adapters(), "nfl", self._games(), {"kalshi", "robinhood"}, limit=16, timeout_s=30)
        self.assertEqual(c.status, pf.PASS, c.detail)  # BUF@DET is quoted on all three venues
        d = c.data
        self.assertEqual(d["per_venue"], {"kalshi": 1, "polymarket": 1, "robinhood": 1})
        self.assertEqual((d["on_all_executable"], d["on_two_executable"], d["executable"], d["unmatched"], d["fetch_errors"]), (1, 1, ["kalshi", "robinhood"], [], {}))
        self.assertIn("kalshi 1/1", c.detail)
        self.assertIn("on all executable (kalshi+robinhood): 1/1", c.detail)
        # Sunday: PHI@TEN is only on Robinhood, CHI@MIN nowhere -> nothing cross-venue -> FAIL, with the unmatched game named.
        sun = pf.check_matches(_adapters(), "nfl", self._games(SUNDAY), {"kalshi", "robinhood"}, timeout_s=30)
        self.assertEqual(sun.status, pf.FAIL, sun.detail)
        self.assertEqual(sun.data["per_venue"], {"robinhood": 1})
        self.assertEqual(sun.data["unmatched"], ["MIN@CHI"])
        self.assertIn("nothing is cross-venue", sun.detail)
        # Polymarket alone does not count as a leg: with only Kalshi executable, on_all_executable is still 1 but two-venue is 0.
        k_only = pf.check_matches(_adapters(), "nfl", self._games(), {"kalshi"}, timeout_s=30)
        self.assertEqual((k_only.data["on_all_executable"], k_only.data["on_two_executable"]), (1, 0))
        self.assertEqual(k_only.status, pf.FAIL)

    def test_limit_and_date_tolerance(self):
        games = self._games(SUNDAY)
        c = pf.check_matches(_adapters(), "nfl", games, {"kalshi", "robinhood"}, limit=1, timeout_s=30)
        self.assertEqual(len(c.data["games"]), 1)
        # A game keyed one day off (the ESPN UTC date vs the venue's ET date) still matches.
        shifted = [dict(g, event_key="nfl:BUF|DET:2026-09-18") for g in self._games()]
        c2 = pf.check_matches(_adapters(), "nfl", shifted, {"kalshi", "robinhood"}, timeout_s=30)
        self.assertEqual(c2.data["per_venue"], {"kalshi": 1, "polymarket": 1, "robinhood": 1})
        # EXECUTABLE_VENUES=all (None): every quoting venue counts.
        c3 = pf.check_matches(_adapters(), "nfl", self._games(), None, timeout_s=30)
        self.assertEqual(c3.data["executable"], ["kalshi", "polymarket", "robinhood"])
        self.assertEqual(c3.status, pf.PASS)

    def test_nothing_cross_venue_fails(self):
        kal_only = [a for a in _adapters() if a.venue == "kalshi"]
        c = pf.check_matches(kal_only, "nfl", self._games(), {"kalshi", "robinhood"}, timeout_s=30)
        self.assertEqual(c.status, pf.FAIL)
        self.assertIn("nothing is cross-venue", c.detail)

    def test_fetch_errors_and_timeouts_surface(self):
        games = self._games()

        class Hang:
            venue = "robinhood"

            def fetch(self, sport):
                time.sleep(5)
                raise AssertionError("never")

        adapters = [a for a in _adapters() if a.venue != "robinhood"] + [Hang()]
        c = pf.check_matches(adapters, "nfl", games, {"kalshi", "robinhood"}, timeout_s=0.2)
        self.assertEqual(c.status, pf.FAIL, c.detail)  # only Kalshi is executable and present: nothing cross-venue
        self.assertIn("timeout", c.data["fetch_errors"]["robinhood"])
        self.assertIn("robinhood: timeout", c.detail)
        dead = [a for a in _adapters() if a.venue != "polymarket"] + [PolymarketAdapter(http=BoomHttp())]
        c2 = pf.check_matches(dead, "nfl", games, {"kalshi", "robinhood"}, timeout_s=30)
        self.assertEqual(c2.status, pf.WARN, c2.detail)  # the executable pair still matches; the signal venue's error is reported
        self.assertIn("polymarket", c2.data["fetch_errors"])
        self.assertEqual(pf.check_matches([], "nfl", games, None).status, pf.WARN)
        self.assertEqual(pf.check_matches(_adapters(), "nfl", [], None).status, pf.WARN)

    def test_fetch_all_slots_ignore_a_fetch_that_lands_after_its_deadline(self):
        import threading

        release = threading.Event()
        landed = threading.Event()

        class Late:
            venue = "robinhood"

            def fetch(self, sport):
                release.wait(5)
                landed.set()
                return "late-snapshot"

        class Fast:
            venue = "kalshi"

            def fetch(self, sport):
                return "fast-snapshot"

        class Broken:
            venue = "polymarket"

            def fetch(self, sport):
                raise HttpError(0, "x", "curl: (7) Failed to connect")

        snaps, errors = pf._fetch_all([Fast(), Late(), Broken()], "nfl", timeout_s=0.2)
        self.assertEqual(snaps, ["fast-snapshot"])
        self.assertIn("timeout", errors["robinhood"])
        self.assertIn("Failed to connect", errors["polymarket"])
        release.set()
        self.assertTrue(landed.wait(2))
        time.sleep(0.05)
        self.assertEqual(snaps, ["fast-snapshot"])  # the late result has nowhere to go
        self.assertEqual(set(errors), {"robinhood", "polymarket"})
        for _ in range(5):  # nothing raises when the deadline and the completion coincide
            ev = threading.Event()

            class Edge:
                venue = "robinhood"

                def fetch(self, sport):
                    ev.wait(0.02)
                    return "edge"

            pf._fetch_all([Edge()], "nfl", timeout_s=0.02)


class BridgeChecks(unittest.TestCase):
    def test_bridge_states(self):
        ex = {"kalshi", "robinhood"}
        self.assertEqual(pf.check_bridge(_clients().bridge_http, "http://127.0.0.1:8765/", ex).status, pf.PASS)
        down = pf.check_bridge(BoomHttp(), "http://127.0.0.1:8765", ex)
        self.assertEqual(down.status, pf.WARN)
        self.assertIn("not running", down.detail)
        self.assertFalse(down.data["running"])
        bad = pf.check_bridge(FakeHttp({"/health": {"ok": False}}), "http://127.0.0.1:8765", ex)
        self.assertEqual(bad.status, pf.FAIL)
        same = pf.check_bridge(FakeHttp({"/health": {"ok": True, "executable_venues": ["robinhood", "kalshi"]}}), "http://127.0.0.1:8765", ex)
        self.assertEqual(same.status, pf.PASS)
        self.assertIn("match", same.detail)
        diff = pf.check_bridge(FakeHttp({"/health": {"ok": True, "executable_venues": "kalshi,robinhood,polymarket"}}), "http://127.0.0.1:8765", ex)
        self.assertEqual(diff.status, pf.FAIL)
        self.assertIn("restart the bridge", diff.detail)
        text = pf.check_bridge(FakeHttp({"/health": '{"ok": true}'}), "http://127.0.0.1:8765", ex)
        self.assertEqual(text.status, pf.PASS)
        self.assertEqual(pf.check_bridge(None, "http://127.0.0.1:8765", ex).status, pf.WARN)


class ReportTests(unittest.TestCase):
    def test_full_report_on_fixtures(self):
        env = os.environ.pop("EXECUTABLE_VENUES", None)
        try:
            with tempfile.TemporaryDirectory() as d:
                seen = []
                rep = pf.run_report("nfl", FIXTURE_DATE, {}, _clients(), out_dir=os.path.join(d, "out"), ext_dir=str(ROOT / "extension"), limit=16, venue_timeout_s=30, bankroll=1000, kelly=0.25, progress=seen.append)
        finally:
            if env is not None:
                os.environ["EXECUTABLE_VENUES"] = env
        names = [c.name for c in rep.checks]
        self.assertEqual(names, ["python", "imports", "wp-model", "settings", "venue:kalshi", "venue:polymarket", "venue:robinhood", "espn", "matches", "bridge", "out-dir", "extension"])
        self.assertEqual(len(seen), len(rep.checks))
        self.assertEqual([c.name for c in rep.checks if c.status != pf.PASS], [], [(c.name, c.detail) for c in rep.checks if c.status != pf.PASS])
        self.assertEqual((rep.verdict, rep.exit_code), (pf.PASS, 0))
        text = pf.format_report(rep)
        self.assertTrue(text.splitlines()[-1].startswith("VERDICT PASS: GO"))
        self.assertIn("PASS venue:kalshi", text)
        self.assertTrue(all(c.latency_ms is not None for c in rep.checks if c.name.startswith("venue:") or c.name in ("espn", "matches", "bridge")))
        js = json.loads(json.dumps(rep.as_dict(), default=str))
        self.assertEqual(js["verdict"], "PASS")
        self.assertEqual(js["date"], FIXTURE_DATE)
        self.assertEqual(len(js["checks"]), 12)
        self.assertEqual(rep.generated_at, 1_800_000_000.0)

    def test_warn_verdict_still_goes(self):
        env = os.environ.pop("EXECUTABLE_VENUES", None)
        try:
            with tempfile.TemporaryDirectory() as d:
                rep = pf.run_report("nfl", FIXTURE_DATE, {"executable_venues": "kalshi,robinhood,polymarket"}, _clients(), out_dir=os.path.join(d, "out"), ext_dir=str(ROOT / "extension"), venue_timeout_s=30)
        finally:
            if env is not None:
                os.environ["EXECUTABLE_VENUES"] = env
        by = {c.name: c for c in rep.checks}
        self.assertEqual(by["settings"].status, pf.WARN)
        self.assertIn("polymarket is executable", by["settings"].detail)
        self.assertEqual(by["matches"].status, pf.PASS, by["matches"].detail)  # BUF@DET is on all three, so the override's set is fully matched
        self.assertEqual(by["matches"].data["executable"], ["kalshi", "polymarket", "robinhood"])
        self.assertEqual((rep.verdict, rep.exit_code), (pf.WARN, 0))
        self.assertTrue(pf.format_report(rep).splitlines()[-1].startswith("VERDICT WARN: GO WITH WARNINGS"))

    def test_fail_dominates_and_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            rep = pf.run_report("nfl", FIXTURE_DATE, {}, _clients(bridge_health={"ok": False}, ext_rc=1), out_dir=os.path.join(d, "out"), ext_dir=str(ROOT / "extension"), venue_timeout_s=30)
        self.assertEqual(rep.verdict, pf.FAIL)
        self.assertEqual(rep.exit_code, 2)
        self.assertEqual({c.name for c in rep.checks if c.status == pf.FAIL}, {"bridge", "extension"})
        self.assertTrue(pf.format_report(rep).splitlines()[-1].startswith("VERDICT FAIL: NO-GO"))

    def test_offline_clients_warn_every_network_row(self):
        with tempfile.TemporaryDirectory() as d:
            rep = pf.run_report("nfl", "2026-09-20", {}, pf.Clients(ext_checker=lambda _: (0, "PASS (0 warning(s))")), out_dir=os.path.join(d, "out"), ext_dir=str(ROOT / "extension"))
        skipped = {c.name for c in rep.checks if c.status == pf.WARN and "skipped" in c.detail}
        self.assertEqual(skipped, {"venue:kalshi", "venue:polymarket", "venue:robinhood", "espn", "matches", "bridge"})
        self.assertEqual(rep.exit_code, 0)

    def test_today_et_is_a_date(self):
        self.assertRegex(pf.today_et(), r"^\d{4}-\d{2}-\d{2}$")


class PluginTests(unittest.TestCase):
    def test_registered_with_help_and_defaults(self):
        p, overrides = cli.build_parser()
        self.assertNotIn("preflight", overrides)
        args = p.parse_args(["preflight"])
        self.assertEqual((args.sport, args.date, args.bridge, args.json, args.limit, args.offline), ("nfl", None, "http://127.0.0.1:8765", False, 16, False))
        args = p.parse_args(["preflight", "--sport", "nfl", "--date", "2026-09-20", "--bridge", "http://127.0.0.1:8765", "--json"])
        self.assertTrue(args.json)
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            p.parse_args(["preflight", "--help"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("--venue-timeout", buf.getvalue())

    def test_offline_handler_json_and_exit_code(self):
        from arb_engine.cli_plugins.preflight_flags import cmd_preflight

        p, _ = cli.build_parser()
        with tempfile.TemporaryDirectory() as d:
            args = p.parse_args(["preflight", "--offline", "--json", "--date", "2026-09-20", "--out-dir", os.path.join(d, "out"), "--ext-dir", str(ROOT / "extension")])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cmd_preflight(args, {})
            js = json.loads(buf.getvalue())
            self.assertEqual(rc, 0)
            self.assertEqual(js["verdict"], "WARN")
            self.assertEqual({c["name"] for c in js["checks"] if c["status"] == "PASS"} >= {"python", "imports", "wp-model", "out-dir", "extension"}, True)
            args = p.parse_args(["preflight", "--offline", "--date", "2026-09-20", "--out-dir", os.path.join(d, "out"), "--ext-dir", d])  # no manifest here
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cmd_preflight(args, {})
            self.assertEqual(rc, 2)
            self.assertIn("VERDICT FAIL", buf.getvalue())
            self.assertIn("FAIL extension", buf.getvalue())


STUB = """#!/usr/bin/env bash
# stand-in for python3: the launcher only needs preflight's exit code, live --help, and processes
echo "stub: $*"
case "$*" in
  *"arb_engine.preflight"*) echo 2026-09-20; exit 0 ;;
  *"preflight"*) echo "VERDICT ${VERDICT:-PASS}: x"; exit ${PREFLIGHT_RC:-0} ;;
  *"--help"*) echo "usage (no quiet flag)"; exit 0 ;;
  *maker*) echo "maker boom"; exit 1 ;;
  *) exec @PY@ - "$INT_LOG" <<'PYEOF'
# a long-running child that logs every SIGINT and spends 1 s "cleaning up" inside the first
# one, the way the maker's cancel-all does: a second SIGINT in that window is the bug
import signal, sys, time
log = sys.argv[1]
state = {"n": 0}
def on_int(sig, frame):
    state["n"] += 1
    with open(log, "a") as f:
        f.write("INT\\n")
    if state["n"] > 1:
        return
    time.sleep(1.0)
    sys.exit(0)
signal.signal(signal.SIGINT, on_int)
signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
with open(log, "a") as f:  # handlers installed: the test waits for this before signalling
    f.write("READY\\n")
time.sleep(60)
PYEOF
  ;;
esac
""".replace("@PY@", sys.executable)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@unittest.skipUnless(shutil.which("bash"), "bash required")
class SundayLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sunday-")
        os.makedirs(os.path.join(self.tmp, "scripts"))
        shutil.copy(ROOT / "scripts" / "sunday.sh", os.path.join(self.tmp, "scripts", "sunday.sh"))
        self.stub = os.path.join(self.tmp, "stub.sh")
        Path(self.stub).write_text(STUB, encoding="utf-8")
        os.chmod(self.stub, 0o755)
        self.int_log = os.path.join(self.tmp, "ints.log")
        self.env = {**os.environ, "PYTHON": self.stub, "BACKOFF_S": "1", "BANKROLL": "250", "BRIDGE_WAIT_S": "0", "INT_LOG": self.int_log}
        self.env.pop("DATE", None)

    def tearDown(self):
        self._sh("stop")
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _sh(self, *args, env=None, timeout=30):
        return subprocess.run(["bash", "scripts/sunday.sh", *args], cwd=self.tmp, env=env or self.env, capture_output=True, text=True, timeout=timeout)

    def _child(self, name, not_pid=None):
        p = os.path.join(self.tmp, "out", "run", f"{name}.child")
        for _ in range(60):
            if os.path.exists(p):
                try:
                    pid = int(Path(p).read_text().strip())
                    if pid != not_pid:
                        return pid
                except ValueError:
                    pass
            time.sleep(0.1)
        raise AssertionError(f"{name} never started")

    def _ints(self):
        try:
            return Path(self.int_log).read_text().count("INT")
        except OSError:
            return 0

    def _wait_ready(self, n: int, timeout: float = 15.0) -> None:
        """Block until ``n`` stub children have installed their signal handlers (a SIGINT
        delivered before that is Python's default KeyboardInterrupt and is never logged)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if Path(self.int_log).read_text().count("READY") >= n:
                    return
            except OSError:
                pass
            time.sleep(0.1)
        self.fail(f"stub children not ready: {self._ints()} INT, log={self.int_log}")

    def test_help_and_bad_action(self):
        out = self._sh("--help")
        self.assertEqual(out.returncode, 0)
        self.assertIn("sunday.sh start", out.stdout)
        self.assertNotIn("set -uo", out.stdout)
        self.assertEqual(self._sh().returncode, 1)
        self.assertEqual(self._sh("bogus").returncode, 1)

    def test_start_supervises_restarts_and_stops(self):
        out = self._sh("start", "--", "--steal-edge", "0.04")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("preflight ok", out.stdout)
        run = os.path.join(self.tmp, "out", "run")
        bridge, live = self._child("bridge"), self._child("live")
        self.assertTrue(_alive(bridge) and _alive(live))
        self.assertEqual(sorted(f for f in os.listdir(run) if f.endswith(".pid")), ["bridge.pid", "live.pid", "maker.pid"])
        logs = os.path.join(self.tmp, "out", "logs")
        live_log = Path(logs, "live-2026-09-20.log").read_text()
        self.assertIn("-m arb_engine live --sport nfl --every 5 --record out/history.db --journal out/live_journal.jsonl --bankroll 250 --kelly 0.25 --steal-edge 0.04", live_log)
        self.assertNotIn("--quiet", live_log)  # the stub's live --help has no --quiet: not passed
        self.assertIn("-m arb_engine bridge --port 8765", Path(logs, "bridge-2026-09-20.log").read_text())
        self.assertTrue(os.path.exists(os.path.join(logs, "preflight-2026-09-20.log")))
        # the maker crashes on purpose: the supervisor logs the exit and restarts after the backoff
        deadline = time.time() + 20  # CI runners are slow: the 10 s restart backoff plus scheduling slack
        def _maker_log() -> str:  # the supervisor creates the log asynchronously; missing = nothing yet
            try:
                return Path(logs, "maker-2026-09-20.log").read_text()
            except FileNotFoundError:
                return ""

        while time.time() < deadline and _maker_log().count("starting maker") < 2:
            time.sleep(0.2)
        mk = _maker_log()
        self.assertGreaterEqual(mk.count("starting maker"), 2)
        self.assertIn("exited rc=1; restart in 1s", mk)
        self.assertIn("--mode paper --size 10", mk)
        st = self._sh("status")
        self.assertIn("bridge  up", st.stdout)
        self.assertIn("live    up", st.stdout)
        again = self._sh("start")
        self.assertEqual(again.returncode, 1)
        self.assertIn("already running", again.stdout)
        self.assertIn("[--steal-edge 0.04]", st.stdout)  # status shows the extras live runs with
        self.assertEqual(Path(run, "live.args").read_text().strip(), "--steal-edge 0.04")
        self._wait_ready(2)  # bridge + live (the maker stub crashes on purpose)
        t0 = time.monotonic()
        stop = self._sh("stop")
        self.assertEqual(stop.returncode, 0)
        # SIGINT reached the children (the shim reset bash's ignored SIGINT): no 10 s SIGTERM fallback per process
        self.assertLess(time.monotonic() - t0, 8.0)
        time.sleep(0.5)
        self.assertFalse(_alive(bridge) or _alive(live))
        self.assertEqual([f for f in os.listdir(run) if f.endswith((".pid", ".child", ".args"))], [])
        self.assertIn("down", self._sh("status").stdout)
        # exactly one SIGINT per child (bridge + live): the supervisor's TERM trap forwards it and
        # stop_one does not send a second one into the child's cleanup window
        self.assertEqual(self._ints(), 2)

    def test_restart_keeps_and_replaces_live_extras(self):
        out = self._sh("start", "--", "--steal-edge", "0.04", "--pre-hours", "0.5")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        first = self._child("live")
        log = Path(self.tmp, "out", "logs", "live-2026-09-20.log")
        self.assertEqual(self._sh("restart", "bogus").returncode, 1)
        self._wait_ready(2)  # bridge + live handlers installed before the restart's SIGINT
        r = self._sh("restart", "live")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        second = self._child("live", not_pid=first)
        self.assertNotEqual(first, second)
        starts = [ln for ln in log.read_text().splitlines() if "starting live" in ln]
        self.assertEqual(len(starts), 2)
        self.assertIn("--bankroll 250 --kelly 0.25 --steal-edge 0.04 --pre-hours 0.5", starts[-1])  # the start extras survive
        self.assertEqual(self._ints(), 1)  # the old child got exactly one SIGINT
        self._wait_ready(3)  # the restarted live child
        r2 = self._sh("restart", "live", "--", "--steal-edge", "0.06")
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        third = self._child("live", not_pid=second)
        starts = [ln for ln in log.read_text().splitlines() if "starting live" in ln]
        self.assertEqual(len(starts), 3)
        self.assertIn("--kelly 0.25 --steal-edge 0.06", starts[-1])
        self.assertNotIn("--pre-hours", starts[-1])  # new extras replace, not append
        self.assertEqual(Path(self.tmp, "out", "run", "live.args").read_text().strip(), "--steal-edge 0.06")
        self.assertTrue(_alive(third))
        # restart applies to the other processes too (no extras: cmd_for ignores them for bridge / maker)
        self._sh("restart", "bridge")
        self.assertIn("-m arb_engine bridge --port 8765", Path(self.tmp, "out", "logs", "bridge-2026-09-20.log").read_text())

    def test_refuses_on_preflight_fail(self):
        out = self._sh("start", env={**self.env, "PREFLIGHT_RC": "2", "VERDICT": "FAIL"})
        self.assertEqual(out.returncode, 2)
        self.assertIn("not starting", out.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "out", "run", "bridge.pid")))
        warn = self._sh("start", env={**self.env, "VERDICT": "WARN", "START_ON_WARN": "0"})
        self.assertEqual(warn.returncode, 2)
        self.assertIn("START_ON_WARN", warn.stdout)

    def test_killed_supervisor_takes_its_child_down(self):
        out = self._sh("start")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        live = self._child("live")
        sup = int(Path(self.tmp, "out", "run", "live.pid").read_text().strip())
        os.kill(sup, 15)  # a stray TERM on the supervisor, not `stop`
        deadline = time.time() + 5
        while time.time() < deadline and (_alive(sup) or _alive(live)):
            time.sleep(0.1)
        self.assertFalse(_alive(sup))
        self.assertFalse(_alive(live))
        self.assertIn("live    down", self._sh("status").stdout)  # the stale pid file reads as down, not up

    def test_preflight_only(self):
        out = self._sh("preflight")
        self.assertEqual(out.returncode, 0)
        self.assertIn("--sport nfl --date 2026-09-20 --bridge http://127.0.0.1:8765 --limit 16 --bankroll 250 --kelly 0.25", out.stdout)


class RunbookCommandsTests(unittest.TestCase):
    """Every ``python3 -m arb_engine <cmd>`` and ``scripts/*.sh|.py`` the runbook shows answers --help."""

    def test_documented_commands_have_help(self):
        text = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
        subs = sorted(set(re.findall(r"python3? -m arb_engine ([a-z][a-z-]*)", text)))
        self.assertIn("preflight", subs)
        self.assertIn("live", subs)
        p, _ = cli.build_parser()
        for sub in subs:
            buf = io.StringIO()
            with redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
                p.parse_args([sub, "--help"])
            self.assertEqual(cm.exception.code, 0, sub)
            self.assertIn("usage:", buf.getvalue(), sub)
        scripts = sorted(set(re.findall(r"(?:bash |python3? )?(scripts/[a-z_]+\.(?:sh|py))", text)))
        self.assertIn("scripts/sunday.sh", scripts)
        # check_extension.py takes a directory (no --help; pre-dates this item), test_js.sh runs the JS suite
        exempt = {"scripts/check_extension.py", "scripts/test_js.sh"}
        for s in scripts:
            self.assertTrue((ROOT / s).exists(), s)
            if s in exempt:
                continue
            runner = ["bash"] if s.endswith(".sh") else [sys.executable]
            out = subprocess.run([*runner, s, "--help"], cwd=ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, f"{s} --help: {out.stdout[-300:]} {out.stderr[-300:]}")


if __name__ == "__main__":
    unittest.main()

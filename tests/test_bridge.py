"""Bridge tests without a socket: ``recent_event`` reuse, and the request handler run
against fixture adapters (``Handler.do_GET`` on a stub request) for the JSON the overlay
reads — Rothera NO-side rows, tie margin, Polymarket signal-only by default, execution
gates and the per-event feed-freshness memory across successive ``/inplay`` calls."""

import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote

from arb_engine.bridge import DEFAULT_INPLAY_MAX_AGE, Handler, executable_venues_for, recent_event, with_gate_fields, with_overlay_fields
from arb_engine.eventlookup import EventAnalyzer
from arb_engine.venues.espn import GameState
from arb_engine.venues.kalshi import KalshiClient
from arb_engine.venues.polymarket import PolymarketAdapter
from arb_engine.venues.robinhood import RobinhoodAdapter

from .helpers import FakeHttp, load, load_text

URL = "https://robinhood.com/us/en/prediction-markets/nfl/events/september-20-philadelphia-vs-tennessee-sep-20-2026/"
EVENT_KEY = "nfl:PHI|TEN:2026-09-20"


def _analyzer_for_game():
    rh = FakeHttp({"/prediction-markets/nfl/events/": load_text("ext/rh_event_page.html"), "/marketdata/event/contract/quotes/v1/": load("ext/rh_quotes.json")})
    kal = FakeHttp({
        "/markets/KXNFLGAME-26SEP20PHITEN-PHI": load("ext/kalshi_market_KXNFLGAME-26SEP20PHITEN-PHI.json"),
        "/markets/KXNFLGAME-26SEP20PHITEN-TEN": load("ext/kalshi_market_KXNFLGAME-26SEP20PHITEN-TEN.json"),
        "/series/KXNFLGAME": load("ext/kalshi_series_KXNFLGAME.json"),
    })
    pm = FakeHttp({"slug=nfl-phi-ten-2026-09-20": load("ext/pm_market_nfl-phi-ten-2026-09-20.json"), "slug=": []})
    return EventAnalyzer(robinhood=RobinhoodAdapter(http=rh), kalshi=KalshiClient(env="prod", http=kal), polymarket=PolymarketAdapter(http=pm)), (rh, kal, pm)


def _gs(**kw) -> GameState:
    base = dict(event_id="401", home="PHI", away="TEN", home_score=14, away_score=17, status="live", period=3, clock_seconds_remaining_in_period=252, game_seconds_remaining=900 + 252, possession="away", down=2, distance=7, yardline_100=35, home_timeouts=3, away_timeouts=2, espn_home_wp=0.43, vegas_spread_home=-3.0, last_play_id="p1")
    base.update(kw)
    return GameState(**base)


def _bridge():
    """The real handler with per-test state: its own analyzer, ESPN fetchers and poll memory."""
    cls = type("_BridgeCase", (Handler,), {"espn_fetchers": {}, "freshness": {}})
    cls.analyzer, transports = _analyzer_for_game()
    cls.kalshi = cls.analyzer.kalshi
    return cls, transports


def _get(cls, path: str):
    """Run ``do_GET`` on a handler that never touched a socket; returns (status, JSON body)."""
    h = cls.__new__(cls)
    h.path = path
    h.wfile = io.BytesIO()
    h.send_response = lambda code, message=None: setattr(h, "status", code)
    h.send_header = lambda k, v: None
    h.end_headers = lambda: None
    h.do_GET()
    return h.status, json.loads(h.wfile.getvalue().decode("utf-8"))


def _rows(analysis: dict, venue: str) -> list[dict]:
    return [v for o in analysis["outcomes"] for v in o["venues"] if v["venue"] == venue]


def _clean_env(case: unittest.TestCase) -> None:
    """The operator's own EXECUTABLE_VENUES must not leak into the eligibility assertions."""
    ctx = mock.patch.dict(os.environ, {}, clear=False)
    ctx.start()
    case.addCleanup(ctx.stop)
    os.environ.pop("EXECUTABLE_VENUES", None)


class RecentEventTests(unittest.TestCase):
    def test_reuses_fresh_analysis_for_the_same_url(self):
        me = object()
        an = SimpleNamespace(last_event=me, last_url=URL, last_analyzed_at=1000.0)
        self.assertIs(recent_event(an, URL, now=1005.0), me)
        self.assertIs(recent_event(an, URL, max_age=DEFAULT_INPLAY_MAX_AGE, now=1000.0 + DEFAULT_INPLAY_MAX_AGE), me)
        self.assertIsNone(recent_event(an, URL, now=1000.0 + DEFAULT_INPLAY_MAX_AGE + 0.01))
        self.assertIsNone(recent_event(an, URL + "x", now=1001.0))
        self.assertIsNone(recent_event(SimpleNamespace(last_event=None, last_url=URL, last_analyzed_at=1000.0), URL, now=1001.0))
        self.assertIsNone(recent_event(SimpleNamespace(), URL, now=1001.0))

    def test_analyze_url_stamps_url_and_time(self):
        an, (rh, kal, pm) = _analyzer_for_game()
        self.assertIsNone(recent_event(an, URL))
        res = an.analyze_url(URL, settings={})
        self.assertTrue(res["ok"], res)
        self.assertNotIn("lines", res["analysis"])
        self.assertIsNotNone(an.last_event)
        self.assertEqual(an.last_url, URL)
        self.assertGreater(an.last_analyzed_at, 0)
        n_rh, n_k, n_pm = len(rh.calls), len(kal.calls), len(pm.calls)
        # A second /inplay-style lookup within max_age reuses the scan: no venue calls.
        self.assertIs(recent_event(an, URL, now=an.last_analyzed_at + 2.0), an.last_event)
        self.assertEqual((len(rh.calls), len(kal.calls), len(pm.calls)), (n_rh, n_k, n_pm))
        self.assertEqual(an.last_event.event_key, EVENT_KEY)


class ExecutableVenuesTests(unittest.TestCase):
    def test_load_settings_none_falls_through_to_the_compliance_table(self):
        # load_settings() always carries executable_venues=None; the bridge reads that as
        # "no override", not as the scanner's explicit "unrestricted".
        self.assertEqual(executable_venues_for({"executable_venues": None}), {"kalshi", "robinhood"})
        self.assertEqual(executable_venues_for({}), {"kalshi", "robinhood"})
        self.assertEqual(executable_venues_for({"executable_venues": ["kalshi", "robinhood", "polymarket"]}), {"kalshi", "robinhood", "polymarket"})
        self.assertEqual(executable_venues_for({"executable_venues": "kalshi, polymarket"}), {"kalshi", "polymarket"})


class OverlayFieldTests(unittest.TestCase):
    def test_pins_report_and_row_keys_without_renaming(self):
        res = {"ok": True, "analysis": {"outcomes": [{"outcome": "A", "venues": [{"venue": "kalshi", "ask": 0.5}]}], "arb": None, "margin": None}}
        out = with_overlay_fields(res)
        self.assertIs(out, res)
        a = out["analysis"]
        for k in ("tie_margin", "tie_payout_total"):
            self.assertIn(k, a)
            self.assertIsNone(a[k])
        self.assertEqual(a["flags"], [])
        self.assertFalse(a["fillable"])
        row = a["outcomes"][0]["venues"][0]
        self.assertEqual(row["ask"], 0.5)
        self.assertIsNone(row["ineligible"])
        self.assertIsNone(row["side"])
        # lines pages: every line report is pinned too; existing values are never overwritten.
        res2 = {"ok": True, "analysis": {"lines": [{"tie_margin": 0.01, "outcomes": []}], "market_type": "spread"}}
        self.assertEqual(with_overlay_fields(res2)["analysis"]["lines"][0]["tie_margin"], 0.01)
        self.assertIn("tie_payout_total", res2["analysis"]["lines"][0])
        self.assertEqual(with_overlay_fields({"ok": False, "error": "x"}), {"ok": False, "error": "x"})
        self.assertEqual(with_overlay_fields({"ok": True, "analysis": None, "note": "n"})["analysis"], None)

    def test_pins_gate_keys_and_marks_ineligible_best_venue(self):
        view = {"sides": [{"outcome": "A", "best_venue": "polymarket", "gated_reasons": None}, {"outcome": "B", "best_venue": "kalshi"}]}
        out = with_gate_fields(view, {"kalshi", "robinhood"})
        self.assertEqual(out["gated_reasons"], [])
        self.assertEqual(out["executable_venues"], ["kalshi", "robinhood"])
        a, b = out["sides"]
        self.assertEqual(a["gated_reasons"], [])
        self.assertFalse(a["steal_gated"])
        self.assertFalse(a["lock_gated"])
        self.assertIsNone(a["steal_threshold"])
        self.assertEqual(a["best_ineligible"], "not executable")
        self.assertIsNone(b["best_ineligible"])
        unrestricted = with_gate_fields({"sides": [{"best_venue": "polymarket"}], "gated_reasons": ["feed-stale"]}, None)
        self.assertIsNone(unrestricted["executable_venues"])
        self.assertIsNone(unrestricted["sides"][0]["best_ineligible"])
        self.assertEqual(unrestricted["gated_reasons"], ["feed-stale"])


class AnalyzeRouteTests(unittest.TestCase):
    """``/analyze`` on the NFL fixture (Robinhood Rothera + Kalshi + Polymarket)."""

    def setUp(self):
        _clean_env(self)

    def test_health_and_unknown_route(self):
        cls, _ = _bridge()
        self.assertEqual(_get(cls, "/health"), (200, {"ok": True, "service": "arb-engine bridge"}))
        st, body = _get(cls, "/nope")
        self.assertEqual((st, body["ok"]), (404, False))

    def test_robinhood_no_side_rows_and_tie_margin(self):
        cls, _ = _bridge()
        st, res = _get(cls, f"/analyze?url={quote(URL, safe='')}")
        self.assertEqual(st, 200)
        self.assertTrue(res["ok"], res)
        a = res["analysis"]
        self.assertEqual(res["event"]["key"], EVENT_KEY)
        rh = _rows(a, "robinhood")
        yes = [v for v in rh if v["side"] == "yes"]
        no = [v for v in rh if v["side"] == "no"]
        self.assertEqual((len(yes), len(no)), (2, 2))
        self.assertTrue(all(v["market_id"].endswith("#no") for v in no))
        self.assertTrue(all(not v["market_id"].endswith("#no") for v in yes))
        # Rothera: YES pays $0 on a tie, NO pays $1 — the NO leg is the tie-aware hedge.
        self.assertEqual({v["tie_payout"] for v in yes}, {0.0})
        self.assertEqual({v["tie_payout"] for v in no}, {1.0})
        # NO PHI is priced from the fixture's no_ask_price (0.25) as a bet on TEN.
        ten_no = next(v for o in a["outcomes"] if o["outcome"] == "TEN" for v in o["venues"] if v["venue"] == "robinhood" and v["side"] == "no")
        self.assertAlmostEqual(ten_no["ask"], 0.25)
        self.assertIsNone(ten_no["ineligible"])
        # Tie-aware margin beside the plain margin (both Kalshi legs here: $0.50 each on a tie).
        self.assertIn("tie_margin", a)
        self.assertIn("tie_payout_total", a)
        self.assertIsInstance(a["tie_margin"], float)
        self.assertIsInstance(a["margin"], float)
        self.assertAlmostEqual(a["tie_payout_total"], 1.0)
        self.assertEqual({l["venue"] for l in a["arb"]["legs"]}, {"kalshi"})
        # The keys the extension already consumes are untouched.
        for k in ("outcomes", "arb", "sized_arb", "flags", "venues", "errors", "market_type"):
            self.assertIn(k, a)
        for o in a["outcomes"]:
            for k in ("outcome", "label", "fair", "best_buy_venue", "best_buy_all_in", "edge_at_best", "venues"):
                self.assertIn(k, o)
            for v in o["venues"]:
                for k in ("venue", "exchange", "mirror_of", "ineligible", "ask", "bid", "ask_size", "fee_per_contract", "all_in", "max_buy_price", "max_buy_maker", "url"):
                    self.assertIn(k, v)

    def test_polymarket_signal_only_by_default(self):
        cls, _ = _bridge()
        _, res = _get(cls, f"/analyze?url={quote(URL, safe='')}")
        a = res["analysis"]
        pm = _rows(a, "polymarket")
        self.assertEqual(len(pm), 2)
        self.assertEqual({v["ineligible"] for v in pm}, {"not executable"})
        self.assertIn("signal-only:polymarket", a["flags"])
        self.assertNotIn("polymarket", {l["venue"] for l in a["arb"]["legs"]})
        self.assertTrue(all(o["best_buy_venue"] != "polymarket" for o in a["outcomes"]))
        # ...but its quotes still feed the consensus fair value (a signal, not a leg).
        self.assertTrue(all(o["fair"] is not None for o in a["outcomes"]))
        for v in _rows(a, "kalshi") + _rows(a, "robinhood"):
            self.assertIsNone(v["ineligible"])

    def test_executable_venues_override_makes_polymarket_a_leg(self):
        os.environ["EXECUTABLE_VENUES"] = "kalshi,robinhood,polymarket"
        cls, _ = _bridge()
        _, res = _get(cls, f"/analyze?url={quote(URL, safe='')}")
        a = res["analysis"]
        self.assertEqual({v["ineligible"] for v in _rows(a, "polymarket")}, {None})
        self.assertNotIn("signal-only:polymarket", a["flags"])
        # Polymarket's asks are the cheapest all-in on both sides of this fixture.
        self.assertIn("polymarket", {l["venue"] for l in a["arb"]["legs"]})
        self.assertIn("polymarket", {o["best_buy_venue"] for o in a["outcomes"]})

    def test_gold_flag_lowers_robinhood_fees(self):
        cls, _ = _bridge()
        _, plain = _get(cls, f"/analyze?url={quote(URL, safe='')}")
        _, gold = _get(cls, f"/analyze?url={quote(URL, safe='')}&gold=1")
        fee = lambda res: _rows(res["analysis"], "robinhood")[0]["fee_per_contract"]  # noqa: E731
        self.assertLess(fee(gold), fee(plain))

    def test_bad_url_is_a_200_error_payload(self):
        cls, _ = _bridge()
        st, res = _get(cls, "/analyze?url=https://example.com/x")
        self.assertEqual(st, 200)
        self.assertFalse(res["ok"])
        self.assertIn("error", res)


class InplayRouteTests(unittest.TestCase):
    """``/inplay`` carries the execution gates and keeps one FeedFreshness per event."""

    def setUp(self):
        _clean_env(self)

    def test_view_carries_gate_fields_and_eligibility(self):
        cls, _ = _bridge()
        st, res = _get(cls, f"/inplay?url={quote(URL, safe='')}&espn=0")
        self.assertEqual(st, 200)
        self.assertTrue(res["ok"], res)
        view = res["view"]
        self.assertEqual(view["event_key"], EVENT_KEY)
        self.assertIsInstance(view["gated_reasons"], list)
        self.assertEqual(view["executable_venues"], ["kalshi", "robinhood"])
        self.assertIsInstance(view["freshness"], dict)
        self.assertEqual(view["freshness"]["polls"], 1)
        self.assertEqual(len(view["sides"]), 2)
        for sv in view["sides"]:
            self.assertIsInstance(sv["gated_reasons"], list)
            self.assertIs(sv["steal_gated"], False)
            self.assertIs(sv["lock_gated"], False)
            self.assertAlmostEqual(sv["steal_threshold"], 0.03)
            self.assertIn("best_ineligible", sv)
            if sv["best_venue"] == "polymarket":
                self.assertEqual(sv["best_ineligible"], "not executable")
        for k in ("title", "live", "sides", "total_cost", "payout_if", "locked_pnl", "balanced", "actions", "game_line", "fair_line", "game_state", "blend", "disagreement"):
            self.assertIn(k, view)

    def test_freshness_persists_across_calls_and_gates_a_pending_score(self):
        cls, (rh, kal, pm) = _bridge()
        states = [_gs(), _gs(home_score=21)]  # PHI scores between the two polls; the play id has not advanced
        cls.espn_fetchers[EVENT_KEY] = lambda: states.pop(0)
        path = f"/inplay?url={quote(URL, safe='')}&steal_edge=0.03"
        _, first = _get(cls, path)
        n_calls = (len(rh.calls), len(kal.calls), len(pm.calls))
        _, second = _get(cls, path)
        self.assertTrue(first["ok"] and second["ok"], (first, second))
        self.assertTrue(second["view"]["live"])
        # One venue scan served both polls (recent_event), and one FeedFreshness saw both.
        self.assertEqual((len(rh.calls), len(kal.calls), len(pm.calls)), n_calls)
        self.assertEqual(list(cls.freshness), [EVENT_KEY])
        self.assertEqual(first["view"]["freshness"]["polls"], 1)
        self.assertEqual(second["view"]["freshness"]["polls"], 2)
        self.assertEqual(first["view"]["gated_reasons"], [])
        self.assertIn("score-pending", second["view"]["gated_reasons"])
        self.assertTrue(second["view"]["freshness"]["score_pending"])
        self.assertEqual(second["view"]["game_state"]["home_score"], 21)
        for sv in second["view"]["sides"]:
            self.assertIn("score-pending", sv["gated_reasons"])
            self.assertFalse(sv["steal"])  # nothing fires while the scoring play is unpublished
        # A fresh handler class (new bridge process) starts its poll memory from scratch.
        self.assertEqual(_bridge()[0].freshness, {})

    def test_espn_failure_still_answers(self):
        cls, _ = _bridge()

        def boom():
            raise RuntimeError("espn down")

        cls.espn_fetchers[EVENT_KEY] = boom
        st, res = _get(cls, f"/inplay?url={quote(URL, safe='')}")
        self.assertEqual(st, 200)
        self.assertTrue(res["ok"], res)
        self.assertIsNone(res["view"]["game_state"])
        self.assertEqual(res["view"]["gated_reasons"], [])


if __name__ == "__main__":
    unittest.main()

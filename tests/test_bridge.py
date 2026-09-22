"""Bridge tests without a socket: ``recent_event`` reuse, and the request handler run
against fixture adapters (``Handler.do_GET`` on a stub request) for the JSON the overlay
reads — Rothera NO-side rows, tie margin, Polymarket signal-only by default, execution
gates and the per-event feed-freshness memory across successive ``/inplay`` calls — plus
the 1 s-polling contract: ``timings`` on every response, the ESPN 2 s cache and ``?fresh=1``
bypass, never a 500, and the ``/health`` reachability / request-log shape."""

import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote

from arb_engine.bridge import DEFAULT_INPLAY_MAX_AGE, ESPN_TTL, BridgeStats, Handler, executable_venues_for, recent_event, with_gate_fields, with_overlay_fields
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


class FakeClock:
    """A callable clock the handler class and the analyzer share (never bound: it is an object)."""

    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> float:
        self.t += dt
        return self.t


def _bridge(clock: FakeClock | None = None):
    """The real handler with per-test state: its own analyzer, ESPN fetchers, poll memory,
    ESPN cache and request log, all on one mocked clock."""
    clock = clock or FakeClock()
    cls = type("_BridgeCase", (Handler,), {"clock": clock})
    cls.analyzer, transports = _analyzer_for_game()
    cls.analyzer.clock = clock
    cls.kalshi = cls.analyzer.kalshi
    cls.reset_state()
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
        # In play a quote must never be served older than ~2 s, so the reuse window is 2 s.
        self.assertEqual(DEFAULT_INPLAY_MAX_AGE, 2.0)
        me = object()
        an = SimpleNamespace(last_event=me, last_url=URL, last_analyzed_at=1000.0)
        self.assertIs(recent_event(an, URL, now=1001.5), me)
        self.assertIs(recent_event(an, URL, max_age=DEFAULT_INPLAY_MAX_AGE, now=1000.0 + DEFAULT_INPLAY_MAX_AGE), me)
        self.assertIsNone(recent_event(an, URL, now=1000.0 + DEFAULT_INPLAY_MAX_AGE + 0.01))
        self.assertIs(recent_event(an, URL, max_age=10, now=1005.0), me)
        self.assertIsNone(recent_event(an, URL + "x", now=1001.0))
        self.assertIsNone(recent_event(SimpleNamespace(last_event=None, last_url=URL, last_analyzed_at=1000.0), URL, now=1001.0))
        self.assertIsNone(recent_event(SimpleNamespace(), URL, now=1001.0))

    def test_one_slot_per_url_so_two_tabs_do_not_evict_each_other(self):
        a, b = object(), object()
        an = SimpleNamespace(last_event=b, last_url=URL + "b", last_analyzed_at=1001.0, recent_events={URL: (a, 1000.0), URL + "b": (b, 1001.0)})
        self.assertIs(recent_event(an, URL, now=1001.5), a)
        self.assertIs(recent_event(an, URL + "b", now=1001.5), b)
        self.assertIsNone(recent_event(an, URL, now=1002.5))
        self.assertIsNone(recent_event(an, URL + "c", now=1001.5))

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
        st, body = _get(cls, "/health")
        self.assertEqual((st, body["ok"], body["service"]), (200, True, "arb-engine bridge"))
        self.assertEqual(body["executable_venues"], ["kalshi", "robinhood"])
        st, body = _get(cls, "/nope")
        self.assertEqual((st, body["ok"], body["where"]), (404, False, "/nope"))

    def test_health_shape_after_requests(self):
        clock = FakeClock()
        cls, _ = _bridge(clock)
        _get(cls, f"/analyze?url={quote(URL, safe='')}")
        clock.advance(0.5)
        _get(cls, "/analyze?url=https://example.com/x")
        clock.advance(0.5)
        _, h = _get(cls, "/health")
        self.assertTrue(h["ok"])
        self.assertEqual(sorted(h["venues"]), ["espn", "kalshi", "polymarket", "robinhood"])
        for v in ("robinhood", "kalshi", "polymarket"):
            row = h["venues"][v]
            self.assertTrue(row["reachable"], (v, row))
            self.assertAlmostEqual(row["last_ok_age_s"], 1.0)
            self.assertEqual(sorted(row["cache"]), ["hit_rate", "hits", "misses", "stale"])
            self.assertEqual(row["cache"]["misses"], 1)
        # Polymarket answered (reachable) but had no CLOB book in the fixture: that is its last error.
        self.assertIn("book sizes unavailable", h["venues"]["polymarket"]["last_error"])
        self.assertIsNone(h["venues"]["kalshi"]["last_error"])
        espn = h["venues"]["espn"]
        self.assertEqual((espn["reachable"], espn["last_ok_age_s"], espn["last_error"]), (False, None, None))
        req = h["requests"]
        self.assertEqual((req["total"], req["errors"], req["window_s"]), (2, 1, 10.0))
        self.assertGreater(req["per_s"], 0)
        self.assertEqual([r["route"] for r in req["last"]], ["/analyze", "/analyze"])
        self.assertEqual([r["ok"] for r in req["last"]], [True, False])
        self.assertIn("not a Robinhood", req["last"][1]["error"])
        for k in ("t", "route", "ok", "seconds", "error"):
            self.assertIn(k, req["last"][0])
        self.assertAlmostEqual(h["uptime_s"], 1.0)
        caches = h["caches"]
        self.assertEqual((caches["quotes_ttl_s"], caches["quotes_max_age_s"], caches["page_ttl_s"], caches["espn_ttl_s"]), (1.0, 2.0, 60.0, ESPN_TTL))
        self.assertEqual(caches["quotes"]["misses"], 3)   # rh quotes, kalshi batch, pm market
        self.assertEqual(caches["quotes"]["errors"], 1)   # the pm books fetch failed (remembered for the TTL, not a miss)
        self.assertIn("venue_timeout_s", caches)
        self.assertIn("timings", h)

    def test_health_reports_a_gamma_outage_as_unreachable_not_unlisted(self):
        clock = FakeClock()
        cls, (_, _, pm) = _bridge(clock)
        _get(cls, f"/analyze?url={quote(URL, safe='')}")
        pm.routes = {"polymarket": lambda: (_ for _ in ()).throw(RuntimeError("gamma 503"))}
        clock.advance(2.5)
        _, res = _get(cls, f"/analyze?url={quote(URL, safe='')}")
        self.assertTrue(res["ok"])
        self.assertEqual(_rows(res["analysis"], "polymarket"), [])
        self.assertIn("polymarket: gamma 503", res["analysis"]["errors"])
        _, h = _get(cls, "/health")
        p = h["venues"]["polymarket"]
        self.assertEqual((p["reachable"], p["last_error"], p["last_ok_age_s"]), (False, "polymarket: gamma 503", 2.5))
        self.assertTrue(h["venues"]["kalshi"]["reachable"])

    def test_analyze_carries_timings_and_venue_status(self):
        cls, _ = _bridge()
        _, res = _get(cls, f"/analyze?url={quote(URL, safe='')}")
        self.assertTrue(res["ok"])
        t = res["timings"]
        self.assertEqual(sorted(t), ["kalshi", "polymarket", "robinhood", "total"])
        self.assertTrue(all(isinstance(v, float) and v >= 0 for v in t.values()), t)
        self.assertGreaterEqual(t["total"], max(t["robinhood"], t["kalshi"], t["polymarket"]))
        vs = res["venue_status"]
        self.assertEqual(sorted(vs), ["kalshi", "polymarket", "robinhood"])
        # The pm book fetch failed in the fixture: the status is the Gamma payload's, the error says why.
        self.assertEqual({v: vs[v]["cache"] for v in vs}, {"robinhood": "miss", "kalshi": "miss", "polymarket": "miss"})
        self.assertTrue(all(vs[v]["ok"] for v in vs), vs)
        self.assertIn("book sizes unavailable", vs["polymarket"]["error"])
        self.assertIsNone(vs["kalshi"]["error"])
        # Warm: the same second is served from the analyzer's caches without a venue call.
        _, res2 = _get(cls, f"/analyze?url={quote(URL, safe='')}")
        self.assertEqual({v: res2["venue_status"][v]["cache"] for v in vs}, {"robinhood": "hit", "kalshi": "hit", "polymarket": "hit"})
        self.assertEqual(res2["analysis"]["outcomes"], res["analysis"]["outcomes"])

    def test_fresh_bypasses_the_analyzer_caches(self):
        cls, (rh, kal, pm) = _bridge()
        _get(cls, f"/analyze?url={quote(URL, safe='')}")
        n = (len(rh.calls), len(kal.calls), len(pm.calls))
        _get(cls, f"/analyze?url={quote(URL, safe='')}")
        self.assertEqual((len(rh.calls), len(kal.calls), len(pm.calls)), n)  # cached
        _, res = _get(cls, f"/analyze?url={quote(URL, safe='')}&fresh=1")
        self.assertTrue(res["ok"])
        self.assertGreater(len(rh.calls), n[0])
        self.assertGreater(len(kal.calls), n[1])
        self.assertGreater(len(pm.calls), n[2])
        self.assertEqual(res["venue_status"]["robinhood"]["cache"], "miss")

    def test_a_raising_handler_answers_200_with_where_and_logs_one_line(self):
        cls, _ = _bridge()

        def boom(*a, **k):
            raise RuntimeError("venue exploded")

        cls.analyzer.analyze_url = boom
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            st, res = _get(cls, f"/analyze?url={quote(URL, safe='')}")
        self.assertEqual(st, 200)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "venue exploded")
        self.assertTrue(res["where"].startswith("/analyze RuntimeError at test_bridge.py:"), res["where"])
        self.assertIn("total", res["timings"])
        lines = [l for l in err.getvalue().splitlines() if l.startswith("bridge: error")]
        self.assertEqual(len(lines), 1)
        self.assertIn("venue exploded", lines[0])
        with mock.patch("sys.stderr", new=io.StringIO()):
            st, res = _get(cls, f"/inplay?url={quote(URL, safe='')}")
            self.assertEqual((st, res["ok"], res["error"]), (200, False, "venue exploded"))
            cls.kalshi.market = boom
            st, res = _get(cls, "/kalshi/market/KXNFLGAME-26SEP20PHITEN-PHI")
            self.assertEqual((st, res["ok"]), (200, False))
            self.assertIn("RuntimeError", res["where"])
        _, h = _get(cls, "/health")
        self.assertEqual(h["requests"]["errors"], 3)
        self.assertEqual(h["requests"]["last"][-1]["error"], "RuntimeError: venue exploded")

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
        clock = FakeClock()
        cls, (rh, kal, pm) = _bridge(clock)
        states = [_gs(), _gs(home_score=21)]  # PHI scores between the two polls; the play id has not advanced
        cls.espn_fetchers[EVENT_KEY] = lambda: states.pop(0)
        path = f"/inplay?url={quote(URL, safe='')}&steal_edge=0.03&max_age=5"
        _, first = _get(cls, path)
        n_calls = (len(rh.calls), len(kal.calls), len(pm.calls))
        clock.advance(ESPN_TTL)  # the ESPN state cache expires; the venue scan is still within max_age
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
        self.assertEqual(res["espn_error"], "espn: espn down")
        self.assertIn("espn", res["timings"])
        _, h = _get(cls, "/health")
        self.assertEqual((h["venues"]["espn"]["reachable"], h["venues"]["espn"]["last_error"]), (False, "espn down"))

    def test_espn_state_is_cached_two_seconds_per_event_and_fresh_bypasses(self):
        clock = FakeClock()
        cls, (rh, kal, pm) = _bridge(clock)
        calls = []
        cls.espn_fetchers[EVENT_KEY] = lambda: (calls.append(clock()), _gs())[1]
        path = f"/inplay?url={quote(URL, safe='')}"
        _, first = _get(cls, path)
        self.assertEqual(sorted(first["timings"]), ["espn", "kalshi", "polymarket", "robinhood", "total"])  # cold: scan + espn
        self.assertIsNone(first["espn_error"])
        clock.advance(0.9)
        _, second = _get(cls, path)
        self.assertEqual(len(calls), 1)                       # ESPN hit
        self.assertEqual(sorted(second["timings"]), ["espn", "total"])  # the venue scan was reused (recent_event)
        self.assertEqual(second["view"]["freshness"]["polls"], 2)
        clock.advance(ESPN_TTL)
        _get(cls, path)
        self.assertEqual(len(calls), 2)                       # expired
        _get(cls, path + "&fresh=1")
        self.assertEqual(len(calls), 3)                       # bypassed
        _, h = _get(cls, "/health")
        self.assertEqual(h["venues"]["espn"]["cache"], {"hits": 1, "misses": 3, "stale": 0, "hit_rate": 0.25})
        self.assertTrue(h["venues"]["espn"]["reachable"])
        self.assertEqual(h["caches"]["espn"]["entries"], 1)

    def test_inplay_reuses_the_scan_within_two_seconds_then_rescans(self):
        clock = FakeClock()
        cls, (rh, kal, pm) = _bridge(clock)
        cls.espn_fetchers[EVENT_KEY] = _gs
        _get(cls, f"/analyze?url={quote(URL, safe='')}")
        n = (len(rh.calls), len(kal.calls), len(pm.calls))
        clock.advance(1.5)
        _, ip = _get(cls, f"/inplay?url={quote(URL, safe='')}")
        self.assertTrue(ip["ok"])
        self.assertEqual((len(rh.calls), len(kal.calls), len(pm.calls)), n)   # /analyze's scan served it
        self.assertNotIn("robinhood", ip["timings"])
        clock.advance(1.0)   # 2.5 s after the scan: too old for play, re-scan (the 1 s caches expired too)
        _, ip = _get(cls, f"/inplay?url={quote(URL, safe='')}")
        self.assertTrue(ip["ok"])
        self.assertGreater(len(rh.calls), n[0])
        self.assertIn("robinhood", ip["timings"])
        self.assertEqual(ip["venue_status"]["kalshi"]["cache"], "miss")


class BridgeStatsTests(unittest.TestCase):
    def test_reachability_and_rates(self):
        clock = FakeClock(100.0)
        st = BridgeStats(clock)
        st.record("/analyze", True, 0.3, venue_status={"kalshi": {"ok": True, "cache": "miss", "error": None}, "robinhood": {"ok": True, "cache": "hit", "error": None}, "polymarket": {"ok": False, "cache": None, "error": "polymarket: timed out after 2s"}})
        clock.advance(5.0)
        st.record("/analyze", True, 0.01, venue_status={"kalshi": {"ok": True, "cache": "hit", "error": None}, "polymarket": {"ok": True, "cache": "miss", "error": None}})
        snap = st.snapshot()
        k, r, p = snap["venues"]["kalshi"], snap["venues"]["robinhood"], snap["venues"]["polymarket"]
        self.assertEqual((k["reachable"], k["last_ok_age_s"], k["cache"]), (True, 5.0, {"hits": 1, "misses": 1, "stale": 0, "hit_rate": 0.5}))
        self.assertEqual((r["reachable"], r["last_ok_age_s"]), (False, None))   # only ever served from cache: unknown
        self.assertEqual((p["reachable"], p["last_ok_age_s"], p["last_error"], p["last_error_age_s"]), (True, 0.0, "polymarket: timed out after 2s", 5.0))
        self.assertEqual(snap["requests"]["total"], 2)
        self.assertEqual(snap["requests"]["per_s"], 0.4)   # 2 requests over a 5 s uptime
        self.assertEqual(snap["uptime_s"], 5.0)
        clock.advance(20.0)
        self.assertEqual(st.snapshot()["requests"]["per_s"], 0.0)   # nothing in the last 10 s


if __name__ == "__main__":
    unittest.main()


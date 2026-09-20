"""EventAnalyzer (what the overlay/bridge computes) against recorded Robinhood pages, and
the 1 s-polling discipline: the TTL caches on a mocked clock, the concurrent venue fetch
(never reordering or merging quotes), per-venue timeouts (partial results plus an error
string) and the ``quotes_max_age`` bound on what a failed refresh may serve."""

import json
import threading
import time
import unittest

from arb_engine.eventlookup import CacheMiss, EventAnalyzer, FetchInFlight, TtlCache, parse_symbol, polymarket_nfl_slugs, polymarket_order_meta
from arb_engine.venues.kalshi import KalshiClient
from arb_engine.venues.polymarket import PolymarketAdapter
from arb_engine.venues.robinhood import RobinhoodAdapter

from .helpers import FakeHttp, load_text, load


def _analyzer_for_totals():
    rh = FakeHttp({"/prediction-markets/nfl/events/september-20-carolina-vs-atlanta-totals-sep-20-2026/": load_text("ext/rh_totals_page.html"), "/marketdata/event/contract/quotes/v1/": load("ext/rh_totals_quotes.json")})
    kal = FakeHttp({"/markets?event_ticker=KXNFLTOTAL-26SEP20CARATL": load("ext/kalshi_event_KXNFLTOTAL-26SEP20CARATL.json"), "/series/KXNFLTOTAL": {"series": {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}}})
    pm_event = load("ext/pm_event_nfl-car-atl-2026-09-20.json")
    pm = FakeHttp({"/events?slug=nfl-car-atl-2026-09-20": pm_event, "/events?slug=": []})
    return EventAnalyzer(robinhood=RobinhoodAdapter(http=rh), kalshi=KalshiClient(env="prod", http=kal), polymarket=PolymarketAdapter(http=pm))


class SymbolTests(unittest.TestCase):
    def test_parse_symbol(self):
        p = parse_symbol("NFLGAME-26SEP20PHITEN-PHI")
        self.assertEqual((p["family"], p["date"], p["teams"], p["kalshi_ticker"], p["routed"]), ("NFLGAME", "2026-09-20", ["PHI", "TEN"], "KXNFLGAME-26SEP20PHITEN-PHI", "other"))
        p = parse_symbol("KXWTAMATCH-26SEP14YOUCHA-YOU")
        self.assertEqual((p["teams"], p["routed"], p["kalshi_ticker"]), (["YOU", "CHA"], "kalshi", "KXWTAMATCH-26SEP14YOUCHA-YOU"))
        self.assertEqual(parse_symbol("NFLTOTAL-26SEP20CARATL-65")["pair"], "CARATL")
        self.assertIsNone(parse_symbol("garbage"))

    def test_polymarket_slugs(self):
        self.assertEqual(polymarket_nfl_slugs(["DET", "BUF"], "2026-09-17"), ["nfl-det-buf-2026-09-17", "nfl-buf-det-2026-09-17", "nfl-det-buf-2026-09-18", "nfl-buf-det-2026-09-18"])


class TotalsPageTests(unittest.TestCase):
    def test_lines_analysis(self):
        an = _analyzer_for_totals()
        res = an.analyze_url("https://robinhood.com/us/en/prediction-markets/nfl/events/september-20-carolina-vs-atlanta-totals-sep-20-2026/", settings={})
        self.assertTrue(res["ok"])
        self.assertEqual(res["event"]["market_type"], "total")
        self.assertEqual(res["event"]["game"], "CAR @ ATL")
        a = res["analysis"]
        self.assertEqual(a["market_type"], "total")
        lines = {l["line"]: l for l in a["lines"]}
        self.assertEqual(set(lines), {17.5, 44.5, 65.5})
        # 65.5: Robinhood over 0.04 (5000) + Kalshi under 0.93 (200) -> fillable arb of 200 contracts.
        l65 = lines[65.5]
        self.assertEqual(l65.get("venues"), ["kalshi", "robinhood"])
        self.assertGreater(l65["margin"], 0.01)
        self.assertTrue(l65["fillable"])
        self.assertEqual(l65["sized_arb"]["contracts"], 200)
        legs = {(x["venue"], x["outcome"]) for x in l65["arb"]["legs"]}
        self.assertEqual(legs, {("robinhood", "over"), ("kalshi", "under")})
        # 44.5 is on all three venues and is not an arb.
        l44 = lines[44.5]
        self.assertEqual(l44["venues"], ["kalshi", "polymarket", "robinhood"])
        self.assertLess(l44["margin"], 0)
        over = next(o for o in l44["outcomes"] if o["outcome"] == "over")
        pm = next(v for v in over["venues"] if v["venue"] == "polymarket")
        self.assertEqual(pm["ask"], 0.47)
        # 17.5 exists only on Robinhood (Kalshi lists no such line) and has no YES ask.
        l17 = lines[17.5]
        self.assertEqual(l17["venues"], ["robinhood"])
        self.assertIsNone(l17["margin"])
        self.assertTrue(any("no market for lines" in e and "17.5" in e for e in a["errors"]))
        # Arbs sort first.
        self.assertEqual(a["lines"][0]["line"], 65.5)
        self.assertEqual(a["lines"][0]["start_time"], "2026-09-20T17:00:00+00:00")




class OrderGridParityTests(unittest.TestCase):
    """The overlay's bridge path carries the same tick / min_size / tie facts as scan()."""

    def test_polymarket_order_meta(self):
        self.assertEqual(polymarket_order_meta({"orderPriceMinTickSize": 0.001, "orderMinSize": 5, "restricted": True}), {"tick": 0.001, "min_size": 5.0, "restricted": True})
        self.assertEqual(polymarket_order_meta({"orderPriceMinTickSize": 0.01, "orderMinSize": 5, "restricted": False}), {"tick": 0.01, "min_size": 5.0})
        self.assertEqual(polymarket_order_meta({}), {"tick": None, "min_size": None})

    def _analyzer(self, pm_event):
        rh = FakeHttp({"/prediction-markets/nfl/events/september-20-carolina-vs-atlanta-totals-sep-20-2026/": load_text("ext/rh_totals_page.html"), "/marketdata/event/contract/quotes/v1/": load("ext/rh_totals_quotes.json")})
        kal = FakeHttp({"/markets?event_ticker=KXNFLTOTAL-26SEP20CARATL": load("ext/kalshi_event_KXNFLTOTAL-26SEP20CARATL.json"), "/series/KXNFLTOTAL": {"series": {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}}})
        pm = FakeHttp({"/events?slug=nfl-car-atl-2026-09-20": pm_event, "/events?slug=": []})
        return EventAnalyzer(robinhood=RobinhoodAdapter(http=rh), kalshi=KalshiClient(env="prod", http=kal), polymarket=PolymarketAdapter(http=pm))

    def test_polymarket_0_001_max_buy_in_the_lookup_path(self):
        pm_event = json.loads(json.dumps(load("ext/pm_event_nfl-car-atl-2026-09-20.json")))
        m = pm_event[0]["markets"][0]                       # O/U 44.5, bestBid 0.46 / bestAsk 0.47
        m["orderPriceMinTickSize"], m["restricted"] = 0.001, False
        an = self._analyzer(pm_event)
        res = an.analyze_url("https://robinhood.com/us/en/prediction-markets/nfl/events/september-20-carolina-vs-atlanta-totals-sep-20-2026/", settings={"executable_venues": "all"})  # "all": Polymarket may be a leg (compliance default keeps it signal-only)
        l44 = {l["line"]: l for l in res["analysis"]["lines"]}[44.5]
        over = next(o for o in l44["outcomes"] if o["outcome"] == "over")
        pm = next(v for v in over["venues"] if v["venue"] == "polymarket")
        kal = next(v for v in over["venues"] if v["venue"] == "kalshi")
        self.assertEqual(pm["tick"], 0.001)
        self.assertEqual(kal["tick"], 0.01)
        self.assertIsNone(pm["ineligible"])
        self.assertEqual(round(pm["max_buy_price"] * 1000) / 1000, pm["max_buy_price"])
        self.assertNotEqual(round(pm["max_buy_price"] * 100) / 100, pm["max_buy_price"])   # genuinely on the finer grid
        self.assertEqual(round(kal["max_buy_price"] * 100) / 100, kal["max_buy_price"])
        # Recorded Gamma payloads mark NFL markets restricted: the row stays, as a signal only.
        an = self._analyzer(load("ext/pm_event_nfl-car-atl-2026-09-20.json"))
        res = an.analyze_url("https://robinhood.com/us/en/prediction-markets/nfl/events/september-20-carolina-vs-atlanta-totals-sep-20-2026/", settings={})
        l44 = {l["line"]: l for l in res["analysis"]["lines"]}[44.5]
        self.assertIn("signal-only:polymarket", l44["flags"])
        over = next(o for o in l44["outcomes"] if o["outcome"] == "over")
        pm = next(v for v in over["venues"] if v["venue"] == "polymarket")
        self.assertEqual((pm["ask"], pm["ineligible"], pm["tick"]), (0.47, "not executable", 0.01))
        self.assertNotIn("polymarket", {x["venue"] for x in l44["arb"]["legs"]})


class GameNoSideTests(unittest.TestCase):
    URL = "https://robinhood.com/us/en/prediction-markets/nfl/events/september-20-philadelphia-vs-tennessee-sep-20-2026/"

    def _analyzer(self):
        rh = FakeHttp({"/prediction-markets/nfl/events/": load_text("ext/rh_event_page.html"), "/marketdata/event/contract/quotes/v1/": load("ext/rh_quotes.json")})
        kal = FakeHttp({"/markets/KXNFLGAME-26SEP20PHITEN-PHI": load("ext/kalshi_market_KXNFLGAME-26SEP20PHITEN-PHI.json"), "/markets/KXNFLGAME-26SEP20PHITEN-TEN": load("ext/kalshi_market_KXNFLGAME-26SEP20PHITEN-TEN.json"), "/series/KXNFLGAME": load("ext/kalshi_series_KXNFLGAME.json")})
        pm = FakeHttp({"slug=nfl-phi-ten-2026-09-20": load("ext/pm_market_nfl-phi-ten-2026-09-20.json"), "slug=": []})
        return EventAnalyzer(robinhood=RobinhoodAdapter(http=rh), kalshi=KalshiClient(env="prod", http=kal), polymarket=PolymarketAdapter(http=pm))

    def test_row_count_unchanged_by_default_and_no_side_on_request(self):
        res = self._analyzer().analyze_url(self.URL, settings={})
        a = res["analysis"]
        rh_rows = [v for o in a["outcomes"] for v in o["venues"] if v["venue"] == "robinhood"]
        self.assertEqual(len(rh_rows), 2)
        self.assertEqual({v["tie_payout"] for v in rh_rows}, {0.0})
        self.assertIsNotNone(a["tie_margin"])
        res = self._analyzer().analyze_url(self.URL, settings={}, emit_no_side=True)
        a = res["analysis"]
        rh_rows = [v for o in a["outcomes"] for v in o["venues"] if v["venue"] == "robinhood"]
        self.assertEqual(len(rh_rows), 4)
        self.assertEqual(sorted(v["tie_payout"] for v in rh_rows), [0.0, 0.0, 1.0, 1.0])
        no_rows = [v for v in rh_rows if v["side"] == "no"]
        self.assertTrue(all(v["market_id"].endswith("#no") for v in no_rows))
        self.assertIsNotNone(a["arb"])
        self.assertEqual({l["venue"] for l in a["arb"]["legs"]} & {"kalshi", "robinhood"}, {l["venue"] for l in a["arb"]["legs"]})  # Polymarket (restricted) is signal-only


class FakeClock:
    def __init__(self, t: float = 1_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> float:
        self.t += dt
        return self.t


class TtlCacheTests(unittest.TestCase):
    def test_hit_inside_ttl_expires_at_ttl_and_fresh_bypasses(self):
        clock = FakeClock()
        c = TtlCache(clock)
        calls = []
        fetch = lambda: (calls.append(clock()), len(calls))[1]  # noqa: E731
        got = c.get("k", 1.0, fetch)
        self.assertEqual((got.value, got.status, got.age, got.at), (1, "miss", 0.0, 1000.0))
        clock.advance(0.99)
        got = c.get("k", 1.0, fetch)
        self.assertEqual((got.value, got.status), (1, "hit"))
        self.assertAlmostEqual(got.age, 0.99)
        clock.advance(0.01)   # exactly the TTL: expired
        self.assertEqual(c.get("k", 1.0, fetch).status, "miss")
        self.assertEqual(len(calls), 2)
        self.assertEqual(c.get("k", 1.0, fetch, fresh=True).status, "miss")
        self.assertEqual(len(calls), 3)
        self.assertEqual(c.stats(), {"hits": 1, "misses": 3, "stale": 0, "errors": 0, "hit_rate": 0.25, "entries": 1})
        # peek never fetches and honours its own bound.
        clock.advance(1.5)
        self.assertIsNone(c.peek("k", 1.0))
        self.assertEqual(c.peek("k", 2.0).value, 3)
        self.assertIsNone(c.peek("missing", 10.0))

    def test_failed_refresh_serves_a_payload_no_older_than_stale_ok(self):
        clock = FakeClock()
        c = TtlCache(clock)
        c.put("k", "v0")

        def boom():
            raise RuntimeError("down")

        clock.advance(1.5)
        got = c.get("k", 1.0, boom, stale_ok=2.0)
        self.assertEqual((got.value, got.status, got.error), ("v0", "stale", "down"))
        self.assertAlmostEqual(got.age, 1.5)
        clock.advance(1.0)   # 2.5 s old: too old to serve, the failure is raised...
        with self.assertRaises(RuntimeError):
            c.get("k", 1.0, boom, stale_ok=2.0)
        # ...and remembered for the TTL: no refetch, the same error again.
        calls = []
        with self.assertRaises(RuntimeError):
            c.get("k", 1.0, lambda: calls.append(1), stale_ok=2.0)
        self.assertEqual(calls, [])
        self.assertIsNone(c.peek("k", 100.0))   # a remembered failure is not a value
        clock.advance(1.0)   # the failure expired: fetched again
        self.assertEqual(c.get("k", 1.0, lambda: "v1").value, "v1")
        self.assertEqual(c.stats()["errors"], 1)

    def test_single_flight_two_threads_one_fetch(self):
        c = TtlCache()
        started = threading.Event()
        release = threading.Event()
        calls = []

        def fetch():
            calls.append(threading.current_thread().name)
            started.set()
            release.wait(2)
            return "v"

        out = []
        t1 = threading.Thread(target=lambda: out.append(c.get("k", 1.0, fetch).status))
        t1.start()
        self.assertTrue(started.wait(2))
        t2 = threading.Thread(target=lambda: out.append(c.get("k", 1.0, fetch).status))
        t2.start()
        time.sleep(0.02)
        release.set()
        t1.join(2)
        t2.join(2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(out), ["hit", "miss"])

    def test_a_reader_waits_at_most_lock_timeout_for_a_hung_fetch(self):
        """A fetch hung in a TCP connect holds its key lock: a later reader gives up after
        ``lock_timeout`` (stale fallback if young enough, else FetchInFlight) instead of
        parking behind it, and nothing is remembered as a failure (the fetch will land)."""
        clock = FakeClock()
        c = TtlCache(clock)
        c.put("k", "old")
        gate = threading.Event()
        self.addCleanup(gate.set)
        started = threading.Event()

        def hung():
            started.set()
            gate.wait(5)
            return "new"

        clock.advance(1.5)   # "old" is 1.5 s old: too old for the TTL, young enough for stale_ok
        t = threading.Thread(target=lambda: c.get("k", 1.0, hung), daemon=True)
        t.start()
        self.assertTrue(started.wait(2))
        t0 = time.perf_counter()
        got = c.get("k", 1.0, lambda: "never", stale_ok=2.0, lock_timeout=0.05)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual((got.value, got.status, got.age), ("old", "stale", 1.5))
        self.assertIn("in flight", got.error)
        clock.advance(1.0)   # 2.5 s old: nothing young enough to serve
        with self.assertRaises(FetchInFlight):
            c.get("k", 1.0, lambda: "never", stale_ok=2.0, lock_timeout=0.05)
        self.assertEqual((c.stats()["stale"], c.stats()["errors"], c.stats()["misses"]), (1, 0, 0))
        self.assertIsNone(c.peek("k", 1.0))                     # no _Failed was remembered for the key
        gate.set()
        t.join(2)
        self.assertEqual(c.get("k", 1.0, lambda: "never").value, "new")   # the late fetch landed


def _game_analyzer():
    rh = FakeHttp({"/prediction-markets/nfl/events/": load_text("ext/rh_event_page.html"), "/marketdata/event/contract/quotes/v1/": load("ext/rh_quotes.json")})
    kal = FakeHttp({"/markets/KXNFLGAME-26SEP20PHITEN-PHI": load("ext/kalshi_market_KXNFLGAME-26SEP20PHITEN-PHI.json"), "/markets/KXNFLGAME-26SEP20PHITEN-TEN": load("ext/kalshi_market_KXNFLGAME-26SEP20PHITEN-TEN.json"), "/series/KXNFLGAME": load("ext/kalshi_series_KXNFLGAME.json")})
    pm = FakeHttp({"slug=nfl-phi-ten-2026-09-20": load("ext/pm_market_nfl-phi-ten-2026-09-20.json"), "slug=": [], "clob.polymarket.com/books": []})
    return EventAnalyzer(robinhood=RobinhoodAdapter(http=rh), kalshi=KalshiClient(env="prod", http=kal), polymarket=PolymarketAdapter(http=pm)), (rh, kal, pm)


GAME_URL = "https://robinhood.com/us/en/prediction-markets/nfl/events/september-20-philadelphia-vs-tennessee-sep-20-2026/"


def _venue_rows(res, venue):
    return [(o["outcome"], v["market_id"], v["ask"], v["bid"], v["ask_size"]) for o in res["analysis"]["outcomes"] for v in o["venues"] if v["venue"] == venue]


class QuoteCacheTests(unittest.TestCase):
    """The analyzer's read-through caches on a mocked clock: 1 s quotes, 60 s page, never a
    quote older than ``quotes_max_age`` from a failed refresh, ``fresh`` bypasses."""

    def _counts(self, transports):
        rh, kal, pm = transports
        return (sum("quotes" in u for u in rh.calls), sum("/markets/" in u for u in kal.calls), sum("slug=" in u for u in pm.calls), sum("/books" in u for u in pm.calls))

    def test_quotes_refresh_every_second_page_every_minute(self):
        clock = FakeClock()
        an, tr = _game_analyzer()
        an.clock = clock
        rh, kal, pm = tr
        res = an.analyze_url(GAME_URL, settings={})
        self.assertTrue(res["ok"], res)
        self.assertEqual(self._counts(tr), (1, 2, 1, 1))
        self.assertEqual({v: res["venue_status"][v]["cache"] for v in res["venue_status"]}, {"robinhood": "miss", "kalshi": "miss", "polymarket": "miss"})
        self.assertEqual(sorted(res["timings"]), ["kalshi", "polymarket", "robinhood", "total"])
        clock.advance(0.5)
        res2 = an.analyze_url(GAME_URL, settings={})
        self.assertEqual(self._counts(tr), (1, 2, 1, 1))   # every venue served from the 1 s cache
        self.assertEqual({v: res2["venue_status"][v]["cache"] for v in res2["venue_status"]}, {"robinhood": "hit", "kalshi": "hit", "polymarket": "hit"})
        self.assertEqual(res2["analysis"]["outcomes"], res["analysis"]["outcomes"])
        clock.advance(0.5)   # 1 s: quotes expire, the page (60 s) and the resolved Polymarket slug do not
        res3 = an.analyze_url(GAME_URL, settings={})
        self.assertEqual(self._counts(tr), (2, 4, 2, 2))
        self.assertEqual(sum("/prediction-markets/nfl/events/" in u for u in rh.calls), 1)
        self.assertEqual({v: res3["venue_status"][v]["cache"] for v in res3["venue_status"]}, {"robinhood": "miss", "kalshi": "miss", "polymarket": "miss"})
        # Only the one known slug is re-read (not the four candidates of the first lookup).
        self.assertEqual([u for u in pm.calls if "slug=" in u][-1].rsplit("slug=", 1)[-1], "nfl-phi-ten-2026-09-20")
        clock.advance(0.2)
        an.analyze_url(GAME_URL, settings={}, fresh=True)
        self.assertEqual(self._counts(tr), (3, 6, 3, 3))
        self.assertEqual(sum("/prediction-markets/nfl/events/" in u for u in rh.calls), 2)   # fresh refetches the page too
        clock.advance(60.0)
        an.analyze_url(GAME_URL, settings={})
        self.assertEqual(sum("/prediction-markets/nfl/events/" in u for u in rh.calls), 3)   # page TTL expired
        # Quote timestamps are the clock's, per venue, so the quote-old gate sees real ages.
        me = an.last_event
        self.assertEqual({q.ts for qs in me.quotes_by_venue.values() for q in qs}, {clock()})

    def test_failed_refresh_never_serves_a_quote_older_than_two_seconds(self):
        clock = FakeClock()
        an, (rh, kal, pm) = _game_analyzer()
        an.clock = clock
        base = an.analyze_url(GAME_URL, settings={})
        self.assertTrue(base["ok"])
        rh.routes["/marketdata/event/contract/quotes/v1/"] = lambda: (_ for _ in ()).throw(RuntimeError("rh 503"))
        kal.routes = {"/series/KXNFLGAME": load("ext/kalshi_series_KXNFLGAME.json"), "/markets/": lambda: (_ for _ in ()).throw(RuntimeError("kalshi 503"))}
        clock.advance(1.5)
        res = an.analyze_url(GAME_URL, settings={})
        self.assertTrue(res["ok"])
        vs = res["venue_status"]
        self.assertEqual((vs["robinhood"]["cache"], vs["kalshi"]["cache"], vs["polymarket"]["cache"]), ("stale", "stale", "miss"))   # Polymarket is up: refreshed
        self.assertFalse(vs["robinhood"]["ok"] or vs["kalshi"]["ok"])
        self.assertTrue(any(e.startswith("robinhood: refresh failed (rh 503); serving 1.5s-old quotes") for e in res["analysis"]["errors"]), res["analysis"]["errors"])
        self.assertTrue(any(e.startswith("kalshi: refresh failed") and "1.5s-old" in e for e in res["analysis"]["errors"]))
        self.assertEqual(_venue_rows(res, "kalshi"), _venue_rows(base, "kalshi"))    # the 1.5 s-old payload, verbatim
        self.assertEqual({q.ts for q in an.last_event.quotes_by_venue["kalshi"]}, {1000.0})
        clock.advance(1.0)   # 2.5 s old: too old for play
        res = an.analyze_url(GAME_URL, settings={})
        self.assertTrue(res["ok"])
        vs = res["venue_status"]
        self.assertEqual(vs["kalshi"]["cache"], None)
        self.assertTrue(vs["kalshi"]["error"].startswith("kalshi: ") and "kalshi 503" in vs["kalshi"]["error"], vs["kalshi"])
        self.assertEqual(_venue_rows(res, "kalshi"), [])                            # no Kalshi rows rather than stale ones
        self.assertEqual(vs["robinhood"]["cache"], "page")                          # the page's server-rendered quotes, stamped at the page fetch
        self.assertIn("robinhood quotes: rh 503", vs["robinhood"]["error"])
        self.assertEqual({q.ts for q in an.last_event.quotes_by_venue["robinhood"]}, {1000.0})
        self.assertEqual(sorted(res["analysis"]["venues"]), ["polymarket", "robinhood"])
        # The failure is remembered for the TTL: the next poll inside it does not hit Kalshi again.
        n = len(kal.calls)
        clock.advance(0.5)
        an.analyze_url(GAME_URL, settings={})
        self.assertEqual(len(kal.calls), n)
        clock.advance(0.5)
        an.analyze_url(GAME_URL, settings={})
        self.assertGreater(len(kal.calls), n)

    def test_polymarket_not_listed_is_not_searched_every_poll(self):
        clock = FakeClock()
        an, (rh, kal, pm) = _game_analyzer()
        an.clock = clock
        pm.routes = {"slug=": []}
        res = an.analyze_url(GAME_URL, settings={})
        self.assertIn("polymarket: no matching market found", res["analysis"]["errors"])
        n = len(pm.calls)
        self.assertEqual(n, 4)   # the four slug candidates, once
        clock.advance(1.0)
        an.analyze_url(GAME_URL, settings={})
        self.assertEqual(len(pm.calls), n)
        clock.advance(an.pm_miss_ttl)
        an.analyze_url(GAME_URL, settings={})
        self.assertEqual(len(pm.calls), 2 * n)

    def test_a_gamma_outage_is_reported_not_remembered_as_not_listed(self):
        """Gamma down longer than ``quotes_max_age``: the venue reports the network error
        (ok False, so /health sees it), the resolved slug is kept, nothing is negative-cached
        and the rows return on the first poll after Gamma recovers, via the known slug only."""
        clock = FakeClock()
        an, (rh, kal, pm) = _game_analyzer()
        an.clock = clock
        base = an.analyze_url(GAME_URL, settings={})
        self.assertEqual(len(_venue_rows(base, "polymarket")), 2)
        good = dict(pm.routes)
        pm.routes = {"polymarket": lambda: (_ for _ in ()).throw(RuntimeError("gamma 503"))}
        clock.advance(1.5)
        res = an.analyze_url(GAME_URL, settings={})
        vs = res["venue_status"]["polymarket"]
        self.assertEqual(_venue_rows(res, "polymarket"), _venue_rows(base, "polymarket"))   # 1.5 s-old payload
        self.assertEqual((vs["ok"], vs["cache"]), (False, "stale"))
        self.assertEqual(vs["error"], "polymarket: refresh failed (gamma 503); serving 1.5s-old quotes")   # the Gamma failure, once
        clock.advance(1.0)   # 2.5 s: too old to serve
        n = len(pm.calls)
        res = an.analyze_url(GAME_URL, settings={})
        vs = res["venue_status"]["polymarket"]
        self.assertEqual(_venue_rows(res, "polymarket"), [])
        self.assertEqual((vs["ok"], vs["cache"], vs["error"]), (False, None, "polymarket: gamma 503"))
        self.assertIn("polymarket: gamma 503", res["analysis"]["errors"])
        self.assertNotIn("polymarket: no matching market found", res["analysis"]["errors"])
        self.assertEqual(len(pm.calls) - n, 1)                                          # the known slug, not four candidates
        self.assertEqual(an.slug_cache.peek(("nfl", ("PHI", "TEN"), "2026-09-20"), 60.0).value, "nfl-phi-ten-2026-09-20")
        clock.advance(0.5)   # the failure is remembered for the TTL: no Gamma call
        an.analyze_url(GAME_URL, settings={})
        self.assertEqual(len(pm.calls) - n, 1)
        pm.routes = good
        clock.advance(1.5)   # Gamma is back: the next poll has the rows again
        n = len(pm.calls)
        res = an.analyze_url(GAME_URL, settings={})
        vs = res["venue_status"]["polymarket"]
        self.assertEqual(_venue_rows(res, "polymarket"), _venue_rows(base, "polymarket"))
        self.assertEqual((vs["ok"], vs["cache"]), (True, "miss"))
        self.assertEqual([u.rsplit("slug=", 1)[-1] for u in pm.calls[n:] if "slug=" in u], ["nfl-phi-ten-2026-09-20"])

    def test_a_cold_lookup_during_a_gamma_outage_is_retried_not_remembered(self):
        """No slug known yet and every candidate read fails: the venue reports the error and
        the next poll after Gamma recovers finds the market (a "not listed" answer is only
        remembered when Gamma answered)."""
        clock = FakeClock()
        an, (rh, kal, pm) = _game_analyzer()
        an.clock = clock
        good = dict(pm.routes)
        pm.routes = {"polymarket": lambda: (_ for _ in ()).throw(RuntimeError("gamma 503"))}
        res = an.analyze_url(GAME_URL, settings={})
        self.assertEqual((res["venue_status"]["polymarket"]["ok"], res["venue_status"]["polymarket"]["error"]), (False, "polymarket: gamma 503"))
        self.assertEqual(len(pm.calls), 4)                                              # the four candidates, each failed
        self.assertIsNone(an.slug_cache.peek(("nfl", ("PHI", "TEN"), "2026-09-20"), 60.0))   # nothing remembered
        pm.routes = good
        clock.advance(1.0)
        res = an.analyze_url(GAME_URL, settings={})
        self.assertEqual(len(_venue_rows(res, "polymarket")), 2)
        self.assertTrue(res["venue_status"]["polymarket"]["ok"])

    def test_a_gamma_outage_keeps_the_lines_slug(self):
        """Same discipline on a spread/total page (``_pm_game``): the lines event slug is
        kept through an outage and the Polymarket lines return with Gamma."""
        url = "https://robinhood.com/us/en/prediction-markets/nfl/events/september-20-carolina-vs-atlanta-totals-sep-20-2026/"
        clock = FakeClock()
        an = _analyzer_for_totals()
        an.clock = clock
        pm = an.pm.http
        good = dict(pm.routes)

        def venues_44(res):
            return {l["line"]: l for l in res["analysis"]["lines"]}[44.5]["venues"]

        self.assertEqual(venues_44(an.analyze_url(url, settings={})), ["kalshi", "polymarket", "robinhood"])
        pm.routes = {"polymarket": lambda: (_ for _ in ()).throw(RuntimeError("gamma 503"))}
        clock.advance(1.5)
        res = an.analyze_url(url, settings={})
        self.assertEqual(venues_44(res), ["kalshi", "polymarket", "robinhood"])
        self.assertEqual(res["venue_status"]["polymarket"], {"ok": False, "seconds": res["venue_status"]["polymarket"]["seconds"], "cache": "stale", "error": "polymarket: refresh failed (gamma 503); serving 1.5s-old quotes"})
        clock.advance(1.0)
        n = len(pm.calls)
        res = an.analyze_url(url, settings={})
        self.assertEqual(venues_44(res), ["kalshi", "robinhood"])
        self.assertEqual((res["venue_status"]["polymarket"]["ok"], res["venue_status"]["polymarket"]["error"]), (False, "polymarket: gamma 503"))
        self.assertNotIn("polymarket: game not found", res["analysis"]["errors"])
        self.assertEqual(pm.calls[n:], ["https://gamma-api.polymarket.com/events?slug=nfl-car-atl-2026-09-20"])   # the known slug only
        pm.routes = good
        clock.advance(2.0)
        n = len(pm.calls)
        res = an.analyze_url(url, settings={})
        self.assertEqual(venues_44(res), ["kalshi", "polymarket", "robinhood"])
        self.assertTrue(res["venue_status"]["polymarket"]["ok"])
        self.assertEqual(pm.calls[n:], ["https://gamma-api.polymarket.com/events?slug=nfl-car-atl-2026-09-20"])
        # ``fresh`` re-reads the known slug too (it is the first candidate).
        an.analyze_url(url, settings={}, fresh=True)
        self.assertEqual(pm.calls[-1], "https://gamma-api.polymarket.com/events?slug=nfl-car-atl-2026-09-20")


class ConcurrentVenueTests(unittest.TestCase):
    """The three venues are fetched at once; the answer never depends on who answered first."""

    def test_answer_order_does_not_reorder_or_merge_quotes(self):
        an, _ = _game_analyzer()
        an.venue_timeout = 5.0
        ref = an.analyze_url(GAME_URL, settings={}, emit_no_side=True)
        self.assertTrue(ref["ok"])
        for slow in ("robinhood", "kalshi", "polymarket"):
            an, _ = _game_analyzer()
            an.venue_timeout = 5.0
            if slow == "robinhood":
                real = an.rh.quotes
                an.rh.quotes = lambda ids, _r=real: (time.sleep(0.05), _r(ids))[1]
            elif slow == "kalshi":
                real_k = an.kalshi.market
                an.kalshi.market = lambda t, _r=real_k: (time.sleep(0.05), _r(t))[1]
            else:
                real_p = an.pm.http.get
                an.pm.http.get = lambda *a, _r=real_p, **k: (time.sleep(0.05), _r(*a, **k))[1]
            res = an.analyze_url(GAME_URL, settings={}, emit_no_side=True)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["analysis"]["outcomes"], ref["analysis"]["outcomes"], slow)
            self.assertEqual(res["analysis"]["venues"], ref["analysis"]["venues"], slow)
            self.assertEqual(list(an.last_event.quotes_by_venue), ["robinhood", "kalshi", "polymarket"], slow)
            self.assertGreaterEqual(res["timings"][slow], 0.05, slow)
            self.assertLess(res["timings"]["total"], 0.5, slow)
            self.assertEqual(sorted(res["timings"]), ["kalshi", "polymarket", "robinhood", "total"])
        # The concurrent scan is as fast as its slowest venue, not their sum.
        an, _ = _game_analyzer()
        real_q, real_m, real_g = an.rh.quotes, an.kalshi.market, an.pm.http.get
        an.rh.quotes = lambda ids: (time.sleep(0.08), real_q(ids))[1]
        an.kalshi.market = lambda t: (time.sleep(0.08), real_m(t))[1]
        an.pm.http.get = lambda *a, **k: (time.sleep(0.08), real_g(*a, **k))[1]
        t0 = time.perf_counter()
        res = an.analyze_url(GAME_URL, settings={})
        self.assertLess(time.perf_counter() - t0, 0.2)
        self.assertTrue(res["ok"])

    def test_a_stalled_venue_yields_partial_results_with_an_error(self):
        an, _ = _game_analyzer()
        an.venue_timeout = 0.05
        gate = threading.Event()
        self.addCleanup(gate.set)
        real = an.kalshi.market
        an.kalshi.market = lambda t: (gate.wait(5), real(t))[1]
        t0 = time.perf_counter()
        res = an.analyze_url(GAME_URL, settings={}, emit_no_side=True)
        took = time.perf_counter() - t0
        self.assertTrue(res["ok"], res)
        self.assertLess(took, 1.0)
        self.assertEqual(sorted(res["analysis"]["venues"]), ["polymarket", "robinhood"])   # the other venues' rows are kept
        self.assertEqual(len(_venue_rows(res, "robinhood")), 4)
        self.assertEqual(len(_venue_rows(res, "polymarket")), 2)
        self.assertEqual(_venue_rows(res, "kalshi"), [])
        self.assertIn("kalshi: timed out after 0.05s", res["analysis"]["errors"])
        vs = res["venue_status"]
        self.assertEqual((vs["kalshi"]["ok"], vs["kalshi"]["cache"], vs["kalshi"]["error"]), (False, None, "kalshi: timed out after 0.05s"))
        self.assertTrue(vs["robinhood"]["ok"] and vs["polymarket"]["ok"])
        self.assertAlmostEqual(res["timings"]["kalshi"], 0.05)
        self.assertGreaterEqual(res["timings"]["total"], 0.05)
        # The late answer still lands in the cache for the next poll.
        gate.set()
        time.sleep(0.05)
        an.kalshi.market = real
        res2 = an.analyze_url(GAME_URL, settings={}, emit_no_side=True)
        self.assertEqual(sorted(res2["analysis"]["venues"]), ["kalshi", "polymarket", "robinhood"])
        self.assertEqual(res2["venue_status"]["kalshi"]["cache"], "hit")

    def test_a_hung_venue_parks_one_thread_not_one_per_poll(self):
        """Polls against a venue hung in its fetch: every poll answers on time, and the pool
        threads of the polls that waited on the key lock exit after ``venue_timeout`` (only
        the fetch itself stays parked), so a hung TCP connect cannot pile up threads."""
        an, _ = _game_analyzer()
        an.venue_timeout = 0.05
        gate = threading.Event()
        self.addCleanup(gate.set)
        real = an.kalshi.market
        an.kalshi.market = lambda t: (gate.wait(5), real(t))[1]
        for _ in range(6):
            res = an.analyze_url(GAME_URL, settings={})
            self.assertTrue(res["ok"], res)
            self.assertEqual(sorted(res["analysis"]["venues"]), ["polymarket", "robinhood"])
            err = res["venue_status"]["kalshi"]["error"]
            self.assertTrue(err in ("kalshi: timed out after 0.05s", "kalshi: a fetch already in flight has not answered within 0.05s"), err)
        time.sleep(0.3)
        parked = [t.name for t in threading.enumerate() if t.name.startswith("venue")]
        self.assertLessEqual(len(parked), 1, parked)   # the one thread inside the hung fetch
        gate.set()
        time.sleep(0.3)
        self.assertEqual([t.name for t in threading.enumerate() if t.name.startswith("venue")], [])
        an.kalshi.market = real
        res = an.analyze_url(GAME_URL, settings={})
        self.assertEqual(sorted(res["analysis"]["venues"]), ["kalshi", "polymarket", "robinhood"])   # the late fetch landed

    def test_a_timed_out_venue_serves_its_cached_quotes_when_young_enough(self):
        an, _ = _game_analyzer()
        base = an.analyze_url(GAME_URL, settings={})
        self.assertTrue(base["ok"])
        an.quotes_ttl = 0.0          # every poll refreshes...
        an.venue_timeout = 0.05
        gate = threading.Event()
        self.addCleanup(gate.set)
        real = an.kalshi.market
        an.kalshi.market = lambda t: (gate.wait(5), real(t))[1]
        res = an.analyze_url(GAME_URL, settings={})   # ...and Kalshi hangs: the <2 s-old payload stands
        self.assertTrue(res["ok"])
        self.assertEqual(_venue_rows(res, "kalshi"), _venue_rows(base, "kalshi"))
        self.assertEqual(res["venue_status"]["kalshi"]["cache"], "stale")
        self.assertFalse(res["venue_status"]["kalshi"]["ok"])
        self.assertIn("kalshi: timed out after 0.05s (serving cached quotes)", res["analysis"]["errors"])
        self.assertEqual(sorted(res["analysis"]["venues"]), ["kalshi", "polymarket", "robinhood"])


class PageCacheTests(unittest.TestCase):
    def test_event_page_is_cached_between_polls(self):
        an = _analyzer_for_totals()
        calls = []
        real = an.rh.event_page
        an.rh.event_page = lambda category, slug: (calls.append(slug), real(category, slug))[1]
        url = "https://robinhood.com/us/en/prediction-markets/nfl/events/september-20-carolina-vs-atlanta-totals-sep-20-2026/"
        an.analyze_url(url, settings={})
        an.analyze_url(url, settings={})
        self.assertEqual(len(calls), 1)      # one page fetch for two polls
        an.page_ttl = 0.0
        an.analyze_url(url, settings={})
        self.assertEqual(len(calls), 2)      # expired -> refetched
if __name__ == "__main__":
    unittest.main()

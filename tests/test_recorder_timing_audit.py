"""Adversarial timing tests for the recorder and Kalshi public-print poller.

Everything here runs offline: fake HTTP clients, injected clocks and temp-directory stores.
Nothing opens a socket, reads a credential or touches a deployed database.
"""

import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from unittest.mock import patch

from arb_engine.models import EventInfo, OutcomeQuote, VenueSnapshot
from arb_engine.store import Store
from arb_engine.strategy.fastlane import FastLane, refresh_kalshi, refresh_robinhood
from arb_engine.strategy.live import LiveSlate
from arb_engine.venues.espn import GameState


KEY = "nfl:DEN|KC:2026-09-21"
# Robinhood's quotes API answers a contract it has never priced with Go's zero time.
GO_ZERO_TIME = -6795364578


def quote(ticker="T-A", outcome="KC", ts=90.0, **meta):
    data = {"ticker": ticker, "side": "yes"}
    data.update(meta)
    return OutcomeQuote("kalshi", ticker, KEY, outcome, bid=.49, ask=.51,
                        bid_size=20, ask_size=30, ts=ts, meta=data)


def tmp_db(name="history.db"):
    return os.path.join(tempfile.mkdtemp(prefix="arb-recorder-test-"), name)


def trade_row(tid, ts, price=0.55, count=10, ticker="T-A"):
    return {"trade_id": tid, "ticker": ticker, "created_time": ts,
            "yes_price": int(price * 100), "count": count, "taker_side": "yes"}


class Clock:
    """A drop-in for ``arb_engine.strategy.live.time`` driven by the test."""

    def __init__(self, t0=1_780_000_000.0):
        self.t = float(t0)

    def time(self):
        return self.t

    def advance(self, dt):
        self.t += float(dt)
        return self.t

    def sleep(self, seconds):
        self.advance(max(0.0, float(seconds)))

    def strftime(self, fmt, *a):
        return "00:00:00"

    def monotonic(self):
        return self.t


class SlateAdapter:
    """A venue adapter whose fetch consumes ``delay`` seconds of the injected clock."""

    def __init__(self, venue, quotes, clock, delay=0.0):
        self.venue, self._quotes, self.clock, self.delay = venue, list(quotes), clock, delay

    def fetch(self, sport):
        if self.delay:
            self.clock.advance(self.delay)
        info = EventInfo(KEY, sport, "moneyline", ["KC", "DEN"], labels={"KC": "KC", "DEN": "DEN"})
        return VenueSnapshot(self.venue, {KEY: info}, list(self._quotes), fetched_at=self.clock.t)


class SlateFeed:
    def __init__(self, games):
        self.games_list = list(games)

    def games(self, date=None):
        return list(self.games_list)

    def enrich(self, g):
        return g


class CapturingAlerter:
    def __init__(self):
        self.infos, self.alerts, self.events = [], [], []

    def info(self, text, **kw):
        self.infos.append((text, kw))

    def alert(self, title, text, **kw):
        self.alerts.append((title, text, kw))

    def journal(self, *a, **kw):
        pass

    def start_button(self, *a, **kw):
        pass


def slate_game(status="live"):
    return GameState(event_id="401", event_key=KEY, sport="nfl", home="KC", away="DEN",
                     status=status, period=3, clock_seconds_remaining_in_period=240,
                     home_score=17, away_score=14,
                     start_time=datetime.now(timezone.utc) - timedelta(hours=1))


def slate_quote(venue, outcome, ts, market_id, **meta):
    fee = ({"exchange": "kalshi", "fee_type": "standard", "fee_multiplier": 0.07}
           if venue == "kalshi" else {"exchange": "rothera", "symbol": "NFLGAME"})
    data = {"side": "yes"}
    data.update(meta)
    return OutcomeQuote(venue, market_id, KEY, outcome, outcome_label=outcome, ask=.55, bid=.53,
                        ask_size=250, bid_size=180, fee_params=fee, ts=ts, meta=data,
                        book_id="kalshi" if venue == "kalshi" else "rothera")


def build_slate(clock, *, store=None, games=None, alerter=None, fast=0.0,
                kalshi_delay=0.4, robinhood_delay=0.3, robinhood_ts=None):
    adapters = [
        SlateAdapter("kalshi", [slate_quote("kalshi", "KC", clock.t, "KXT-KC", ticker="KXT-KC")],
                     clock, delay=kalshi_delay),
        SlateAdapter("robinhood", [slate_quote("robinhood", "KC",
                                               clock.t if robinhood_ts is None else robinhood_ts,
                                               "rh-kc", contract_id="rh-kc", exchange="rothera")],
                     clock, delay=robinhood_delay),
    ]
    return LiveSlate(adapters, feed=SlateFeed(games if games is not None else [slate_game()]),
                     settings={}, sport="nfl", alerter=alerter or CapturingAlerter(),
                     store=store, interval=5.0, fast=fast, bankroll=500.0)


class RecorderTimingAuditTests(unittest.TestCase):
    def test_full_adapter_fetch_gets_exact_request_and_receipt_times(self):
        class Adapter:
            venue = "kalshi"

            def fetch(self, sport):
                info = EventInfo(KEY, sport, "moneyline", ["DEN", "KC"])
                return VenueSnapshot("kalshi", {KEY: info}, [quote(ts=100.1)], fetched_at=100.1)

        slate = LiveSlate.__new__(LiveSlate)
        slate.adapters, slate.sport = [Adapter()], "nfl"
        with patch("arb_engine.strategy.live.time.time", side_effect=[100.0, 100.4]):
            merged = slate.merged_events([])
        q = merged[KEY].quotes_by_venue["kalshi"][0]
        self.assertEqual((q.meta["req_ts"], q.meta["obs_ts"], q.ts),
                         (100.0, 100.4, 100.1))
        self.assertEqual((q.meta["approx_time"], q.meta["refreshed"]), (False, True))

    def test_cached_price_older_than_the_request_is_not_stamped_fresh(self):
        # A Robinhood contract the quotes refresh did not answer keeps the cached catalogue's
        # time (venues/robinhood._quote_ts); stamping it with this fetch's receipt time would
        # record a 30-minute-old price as just observed.
        class Adapter:
            venue = "robinhood"

            def fetch(self, sport):
                info = EventInfo(KEY, sport, "moneyline", ["DEN", "KC"])
                fresh, cached = (replace(quote(t, o, ts=ts), venue="robinhood") for t, o, ts in (("R-A", "KC", 100.1), ("R-B", "DEN", -1700.0)))
                return VenueSnapshot("robinhood", {KEY: info}, [fresh, cached], fetched_at=100.1)

        slate = LiveSlate.__new__(LiveSlate)
        slate.adapters, slate.sport = [Adapter()], "nfl"
        with patch("arb_engine.strategy.live.time.time", side_effect=[100.0, 100.4]):
            merged = slate.merged_events([])
        by_id = {q.venue_market_id: q for q in merged[KEY].quotes_by_venue["robinhood"]}
        self.assertEqual((by_id["R-A"].meta["obs_ts"], by_id["R-A"].meta["refreshed"]), (100.4, True))
        self.assertEqual((by_id["R-B"].meta["req_ts"], by_id["R-B"].meta["obs_ts"]), (-1700.0, -1700.0))
        self.assertEqual((by_id["R-B"].meta["approx_time"], by_id["R-B"].meta["refreshed"]), (True, False))

    def test_missing_row_in_partial_fast_response_is_carried_at_original_time(self):
        class Client:
            def get(self, path, params):
                return {"markets": [{"ticker": "T-A", "yes_bid_dollars": ".55",
                                      "yes_ask_dollars": ".57"}]}

        old = [quote("T-A", "KC", 90, obs_ts=90, req_ts=89, refreshed=True),
               quote("T-B", "DEN", 80, obs_ts=80, req_ts=79, refreshed=True)]
        ticks = iter([100.0, 100.5])
        rows = refresh_kalshi(Client(), old, clock=lambda: next(ticks))
        by_id = {q.venue_market_id: q for q in rows}
        self.assertEqual((by_id["T-A"].meta["req_ts"], by_id["T-A"].meta["obs_ts"],
                          by_id["T-A"].meta["refreshed"]), (100.0, 100.5, True))
        self.assertEqual((by_id["T-B"].meta["req_ts"], by_id["T-B"].meta["obs_ts"],
                          by_id["T-B"].meta["refreshed"]), (79, 80, False))

    def test_l1_only_tick_never_precedes_exact_observation(self):
        store = Store(":memory:")
        q = quote(obs_ts=105.0, req_ts=104.0, refreshed=True)
        store.record_l1(100.0, KEY, {"kalshi": [q]}, home="KC", away="DEN")
        row = store.tick_rows()[0]
        self.assertEqual(row["ts"], 105.0)
        store.close()

    def test_slow_tickers_poll_concurrently_without_blocking_caller(self):
        entered = {ticker: threading.Event() for ticker in ("T-A", "T-B")}
        release = threading.Event()

        class Client:
            def get(self, path, params):
                entered[params["ticker"]].set()
                release.wait(2)
                return {"trades": [], "cursor": ""}

        lane = FastLane(kalshi_client=Client())
        lane.seed({KEY: {"kalshi": [quote("T-A", "KC"), quote("T-B", "DEN")]}})
        store = Store(":memory:")
        started = time.monotonic()
        lane.poll_trades(store, background=True)
        self.assertLess(time.monotonic() - started, .25)
        self.assertTrue(entered["T-A"].wait(1))
        self.assertTrue(entered["T-B"].wait(1))
        release.set()
        self.assertTrue(lane.wait_for_trade_polls(2))
        store.close()

    def test_trade_cadence_uses_request_starts_and_exposes_receipt_gaps(self):
        class Client:
            def get(self, path, params):
                return {"trades": [], "cursor": ""}

        ticks = iter([0.0, 0.0, 4.0, 5.0, 5.0, 9.0])
        lane = FastLane(kalshi_client=Client(), clock=lambda: next(ticks))
        lane.seed({KEY: {"kalshi": [quote()]}})
        store = Store(":memory:")
        lane.poll_trades(store, cadence_s=5)
        lane.poll_trades(store, cadence_s=5)
        status = lane.trade_poll_status(now=9)["T-A"]
        self.assertEqual((status["last_request_ts"], status["last_poll_ts"]), (5.0, 9.0))
        self.assertEqual((status["last_gap_s"], status["receipt_gap_s"]), (5.0, 5.0))
        store.close()


class RunLoopClockTests(unittest.TestCase):
    """``run()`` is the production call path. Every timing branch in ``tick()`` is guarded by
    ``pinned is None``, so a ``tick(t0)`` there silently disables all of them while the tests
    (which call ``tick()``) keep passing."""

    def _run_once(self, slate, clock, last_tick_at=None):
        import arb_engine.strategy.live as live

        if last_tick_at is not None:
            slate._last_tick_at = last_tick_at
        captured = {}
        original = slate.merged_events

        def spy(errors):
            captured["merged"] = original(errors)
            return captured["merged"]

        slate.merged_events = spy
        real = live.time
        live.time = clock
        try:
            slate.run(interval=5.0, duration=1.0, max_iterations=1, printer=lambda *_: None)
        finally:
            live.time = real
        return captured.get("merged")

    def test_run_records_a_polling_gap(self):
        clock, alerts = Clock(), CapturingAlerter()
        slate = build_slate(clock, alerter=alerts)
        self._run_once(slate, clock, last_tick_at=clock.t - 300.0)
        gaps = [kw for text, kw in alerts.infos if text.startswith("polling gap")]
        self.assertEqual(len(gaps), 1, alerts.infos)
        self.assertEqual(gaps[0]["polling_gap_s"], 300.0)

    def test_run_stamps_the_request_start_and_the_response_completion(self):
        clock = Clock()
        merged = self._run_once(build_slate(clock), clock)
        k = merged[KEY].quotes_by_venue["kalshi"][0]
        r = merged[KEY].quotes_by_venue["robinhood"][0]
        # Kalshi is fetched first (0.4 s), Robinhood after it (0.3 s more).
        self.assertEqual((k.meta["req_ts"], k.meta["obs_ts"]), (1_780_000_000.0, 1_780_000_000.4))
        self.assertEqual((r.meta["req_ts"], r.meta["obs_ts"]), (1_780_000_000.4, 1_780_000_000.7))
        for q in (k, r):
            self.assertLess(q.meta["req_ts"], q.meta["obs_ts"])
            self.assertEqual((q.meta["approx_time"], q.meta["refreshed"]), (False, True))

    def test_run_does_not_stamp_a_cached_price_as_freshly_observed(self):
        clock = Clock()
        cached_at = clock.t - 1800.0
        merged = self._run_once(build_slate(clock, robinhood_ts=cached_at), clock)
        r = merged[KEY].quotes_by_venue["robinhood"][0]
        self.assertEqual((r.meta["req_ts"], r.meta["obs_ts"]), (cached_at, cached_at))
        self.assertEqual((r.meta["approx_time"], r.meta["refreshed"]), (True, False))

    def test_run_decides_each_game_after_the_fetch_not_at_the_tick_start(self):
        clock = Clock()
        slate = build_slate(clock)
        seen = []
        original = slate.market_signals
        slate.market_signals = lambda me, view, out, now: seen.append(now) or original(me, view, out, now)
        self._run_once(slate, clock)
        self.assertTrue(seen)
        self.assertGreaterEqual(min(seen), 1_780_000_000.7)   # after both adapter fetches


class StuckLiveGameTests(unittest.TestCase):
    def test_an_empty_scoreboard_clears_the_fast_lane(self):
        clock = Clock()
        store = Store(tmp_db())
        self.addCleanup(store.close)

        class Client:
            def get(self, path, params=None):
                if path == "/markets":
                    return {"markets": [{"ticker": "KXT-KC", "yes_bid_dollars": ".52",
                                         "yes_ask_dollars": ".54"}]}
                return {"trades": [], "cursor": ""}

        slate = build_slate(clock, store=store, fast=1.0)
        slate.fastlane.kalshi, slate.fastlane.robinhood = Client(), None
        slate.fastlane.clock = lambda: clock.t
        slate.tick(clock.t)
        self.assertEqual(sorted(slate._live_priced), [KEY])

        slate.feed.games_list.clear()          # the game ended and left the scoreboard
        clock.advance(600.0)
        empty = slate.tick(clock.t)
        self.assertEqual(empty.games, 0)
        self.assertEqual(slate._live_priced, {})
        self.assertEqual(slate.fastlane.last, {})

        clock.advance(1.0)
        after = slate.fast_step(clock.t)
        self.assertEqual(after.games, 0)
        self.assertEqual([r["live"] for r in store.tick_rows() if r["source"] == "fast"], [])


class FastLaneStepTests(unittest.TestCase):
    def test_a_slow_venue_cannot_hold_the_lane_past_its_timeout(self):
        release = threading.Event()
        self.addCleanup(release.set)

        class SlowClient:
            def get(self, path, params=None):
                release.wait(5)
                return {"markets": []}

        lane = FastLane(kalshi_client=SlowClient(), timeout=0.2)
        lane.seed({KEY: {"kalshi": [quote()]}})
        started = time.monotonic()
        by, errors = lane.step()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertTrue(any("fastlane kalshi" in e for e in errors), errors)
        # The unrefreshed quote keeps its own time and is not claimed as fresh.
        self.assertEqual(by[KEY]["kalshi"][0].meta["refreshed"], False)

    def test_one_budget_covers_the_whole_step_not_one_per_venue(self):
        release = threading.Event()
        self.addCleanup(release.set)

        class Slow:
            def get(self, *a, **kw):
                release.wait(5)
                return {"markets": []}

            def quotes(self, ids):
                release.wait(5)
                return {}

        lane = FastLane(kalshi_client=Slow(), robinhood=Slow(), timeout=0.2)
        lane.seed({KEY: {"kalshi": [quote()],
                         "robinhood": [replace(quote("R-A", "DEN"), venue="robinhood")]}})
        started = time.monotonic()
        lane.step()
        self.assertLess(time.monotonic() - started, 1.0)   # not 2 x timeout, and not 2 x 5 s

    def test_the_fast_loop_stamps_the_real_response_time(self):
        clock = Clock()
        store = Store(tmp_db())
        self.addCleanup(store.close)
        real = {"t": 1_780_000_000.0}
        completions = []

        class Client:
            def get(self, path, params=None):
                real["t"] += 0.4
                completions.append(real["t"])
                return {"markets": [{"ticker": "KXT-KC", "yes_bid_dollars": ".52",
                                     "yes_ask_dollars": ".54", "yes_ask_size_fp": "120",
                                     "yes_bid_size_fp": "100"}]}

        slate = build_slate(clock, store=store, fast=0.01)
        slate.fastlane.kalshi, slate.fastlane.robinhood = Client(), None
        slate.fastlane.clock = lambda: real["t"]
        slate.tick(clock.t)
        completions.clear()

        start = real["t"]
        slate._stop.clear()
        thread = threading.Thread(target=slate._fast_loop, args=(lambda *_: None,), daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not completions and time.monotonic() < deadline:
            time.sleep(0.01)
        slate._stop.set()
        thread.join(timeout=5)
        self.assertTrue(completions)

        import json
        fast_rows = [r for r in store.tick_rows() if r["source"] == "fast"]
        self.assertTrue(fast_rows)
        row = next(r for r in json.loads(fast_rows[0]["l1_json"])["rows"] if r["venue"] == "kalshi")
        self.assertEqual(row["req_ts"], start)
        self.assertEqual(row["obs_ts"], completions[0])
        self.assertLess(row["req_ts"], row["obs_ts"])
        self.assertLessEqual(row["obs_ts"], fast_rows[0]["ts"])


class TradePollerPaginationTests(unittest.TestCase):
    def lane(self, client, clock=None):
        ln = FastLane(kalshi_client=client, clock=clock or (lambda: 1_780_000_000.0))
        ln.seed({KEY: {"kalshi": [quote()]}})
        return ln

    def store(self):
        st = Store(tmp_db())
        self.addCleanup(st.close)
        return st

    def test_a_repeating_cursor_is_abandoned_instead_of_looping_forever(self):
        calls = {"n": 0}

        class LoopClient:
            def get(self, path, params=None):
                calls["n"] += 1
                return {"trades": [trade_row("loop-1", 1_780_000_000)], "cursor": "SAME"}

        lane, store = self.lane(LoopClient()), self.store()
        for _ in range(4):
            lane.poll_trades(store, max_pages=5, cadence_s=0.0)
        self.assertEqual(lane._trade_pending, {})          # no unbounded buffer
        self.assertEqual(lane._trade_page_cursor, {})      # not left permanently backlogged
        self.assertFalse(lane.trade_poll_status()["T-A"]["backlog"])
        self.assertEqual(len(lane.trade_errors), 4)
        self.assertIn("repeated", lane.trade_errors[0])
        self.assertLessEqual(calls["n"], 8)                # 2 pages per poll, not 5
        status = lane.trade_poll_status()["T-A"]
        self.assertEqual(status["failures"], 4)            # a failing tape is not "no prints"
        self.assertIsNone(status["last_complete_ts"])

    def test_a_page_without_its_trades_list_is_a_failed_read(self):
        class NoTrades:
            def get(self, path, params=None):
                return {"cursor": ""}

        lane, store = self.lane(NoTrades()), self.store()
        lane.poll_trades(store, max_pages=3, cadence_s=0.0)
        self.assertTrue(any("without its 'trades' list" in e for e in lane.trade_errors),
                        lane.trade_errors)

    def test_a_malformed_page_never_advances_the_watermark_past_unfetched_prints(self):
        class TruncatedByMalformed:
            def __init__(self):
                self.i = 0

            def get(self, path, params=None):
                self.i += 1
                if self.i == 1:
                    return {"trades": [trade_row("new-2", 1_780_000_020),
                                       trade_row("new-1", 1_780_000_019)], "cursor": "page2"}
                return {"cursor": None}

        lane, store = self.lane(TruncatedByMalformed()), self.store()
        lane.poll_trades(store, max_pages=5, cadence_s=0.0)
        self.assertTrue(lane.trade_errors)
        # Nothing was committed, so the next poll re-reads the whole newest-first walk.
        self.assertIsNone(store.latest_trade_ts("T-A"))
        self.assertIsNone(lane.trade_cursor.get("T-A"))

    def test_overlapping_pages_and_same_second_prints_are_stored_exactly_once(self):
        pages = [
            {"trades": [trade_row("t3", 1_780_000_010), trade_row("t2", 1_780_000_010)], "cursor": "p2"},
            {"trades": [trade_row("t2", 1_780_000_010), trade_row("t1", 1_780_000_009)], "cursor": ""},
            {"trades": [trade_row("t4", 1_780_000_010), trade_row("t3", 1_780_000_010),
                        trade_row("t2", 1_780_000_010)], "cursor": ""},
        ]
        sent = []

        class Paged:
            def __init__(self):
                self.i = 0

            def get(self, path, params=None):
                sent.append(dict(params or {}))
                page = pages[min(self.i, len(pages) - 1)]
                self.i += 1
                return page

        lane, store = self.lane(Paged()), self.store()
        lane.poll_trades(store, max_pages=5, cadence_s=0.0)
        self.assertEqual(lane.trade_cursor["T-A"], 1_780_000_010)   # inclusive, not +1
        lane.poll_trades(store, max_pages=5, cadence_s=0.0)
        self.assertEqual(sent[-1]["min_ts"], 1_780_000_010)
        ids = sorted(r[0] for r in store.conn.execute("SELECT trade_id FROM trade_prints"))
        self.assertEqual(ids, ["t1", "t2", "t3", "t4"])

    def test_a_transient_error_mid_walk_loses_nothing_on_the_next_poll(self):
        state = {"fail": True}

        class Flaky:
            def get(self, path, params=None):
                if state["fail"]:
                    state["fail"] = False
                    raise OSError("connection reset")
                return {"trades": [trade_row("t1", 1_780_000_009)], "cursor": ""}

        lane, store = self.lane(Flaky()), self.store()
        self.assertEqual(lane.poll_trades(store, max_pages=5, cadence_s=0.0), 0)
        self.assertTrue(lane.trade_errors)
        self.assertEqual(lane.poll_trades(store, max_pages=5, cadence_s=0.0), 1)
        self.assertEqual([r[0] for r in store.conn.execute("SELECT trade_id FROM trade_prints")],
                         ["t1"])

    def test_restart_resumes_inclusively_from_the_stored_watermark(self):
        path = tmp_db()

        class Client:
            def __init__(self, page):
                self.page, self.params = page, []

            def get(self, p, params=None):
                self.params.append(dict(params or {}))
                return self.page

        first = Store(path)
        self.lane(Client({"trades": [trade_row("t2", 1_780_000_010),
                                     trade_row("t1", 1_780_000_009)], "cursor": ""})).poll_trades(
            first, max_pages=5, cadence_s=0.0)
        first.close()

        second = Store(path)
        self.addCleanup(second.close)
        client = Client({"trades": [trade_row("t3", 1_780_000_011),
                                    trade_row("t2", 1_780_000_010)], "cursor": ""})
        self.lane(client).poll_trades(second, max_pages=5, cadence_s=0.0)
        self.assertEqual([p.get("min_ts") for p in client.params], [1_780_000_010])
        ids = sorted(r[0] for r in second.conn.execute("SELECT trade_id FROM trade_prints"))
        self.assertEqual(ids, ["t1", "t2", "t3"])

    def test_a_backlog_beyond_the_bound_is_abandoned_and_re_read(self):
        class Flood:
            def __init__(self):
                self.n = 0

            def get(self, path, params=None):
                rows = [trade_row(f"f{self.n}-{i}", 1_780_000_000 + i) for i in range(100)]
                self.n += 1
                return {"trades": rows, "cursor": f"c{self.n}"}

        lane, store = self.lane(Flood()), self.store()
        lane.MAX_PENDING_TRADES = 150
        lane.poll_trades(store, max_pages=5, cadence_s=0.0)
        self.assertTrue(any("backlog bound" in e for e in lane.trade_errors), lane.trade_errors)
        self.assertEqual(lane._trade_pending, {})
        self.assertIsNone(store.latest_trade_ts("T-A"))


class TimestampPlausibilityTests(unittest.TestCase):
    def test_a_never_quoted_contract_has_no_venue_timestamp(self):
        class Adapter:
            def quotes(self, ids):
                return {"c1": {"yes_ask_price": "0.57", "yes_bid_price": "0.54",
                               "ask_size": "90", "bid_size": "70",
                               "ask_venue_timestamp": GO_ZERO_TIME, "state": "open"}}

        q = replace(quote("c1", "KC", ts=1_780_000_000.0, contract_id="c1"), venue="robinhood")
        ticks = iter([1_780_000_000.0, 1_780_000_000.4])
        rows = refresh_robinhood(Adapter(), [q], clock=lambda: next(ticks))
        self.assertIsNone(rows[0].quote_time)
        self.assertEqual(rows[0].meta["obs_ts"], 1_780_000_000.4)   # the receipt is still exact

    def test_non_finite_venue_timestamps_are_read_as_missing(self):
        from arb_engine.strategy.fastlane import _epoch

        for bad in (float("nan"), float("inf"), float("-inf"), GO_ZERO_TIME,
                    "0001-01-01T00:00:00Z", "garbage", None):
            self.assertIsNone(_epoch(bad), bad)
        self.assertEqual(_epoch(1_780_000_000), 1_780_000_000.0)
        self.assertEqual(_epoch(1_780_000_000_123), 1_780_000_000.123)

    def test_trade_id_deduplication_is_deterministic(self):
        store = Store(tmp_db())
        self.addCleanup(store.close)
        first = trade_row("t1", 1_780_000_009, price=0.55, count=10)
        # The same id coming back with different content (a retry that crossed a correction)
        # must not rewrite what was recorded: the primary key decides, first answer wins.
        conflicting = trade_row("t1", 1_780_000_009, price=0.99, count=999)
        self.assertEqual(store.record_trade_prints([first]), 1)
        for _ in range(3):
            self.assertEqual(store.record_trade_prints([conflicting, first]), 0)
        rows = [tuple(r) for r in store.conn.execute("SELECT trade_id, price, count FROM trade_prints")]
        self.assertEqual(rows, [("t1", 0.55, 10.0)])

    def test_unreadable_prints_are_rejected_and_counted(self):
        store = Store(tmp_db())
        self.addCleanup(store.close)
        for tid, ts in (("nan-1", float("nan")), ("inf-1", float("inf")),
                        ("go-1", float(GO_ZERO_TIME)), ("ms-1", 1_780_000_000_000.0)):
            self.assertEqual(store.record_trade_prints([trade_row(tid, ts)]), 0, tid)
            self.assertEqual(store.last_trade_print_rejects, 1, tid)
        self.assertEqual(store.record_trade_prints([trade_row("ok-1", 1_780_000_000.0)]), 1)
        self.assertEqual(store.last_trade_print_rejects, 0)
        self.assertEqual([r[0] for r in store.conn.execute("SELECT trade_id FROM trade_prints")],
                         ["ok-1"])

    def test_the_poller_reports_rejected_prints_instead_of_a_short_tape(self):
        class Client:
            def get(self, path, params=None):
                return {"trades": [trade_row("bad", float("inf")),
                                   trade_row("good", 1_780_000_000)], "cursor": ""}

        lane = FastLane(kalshi_client=Client(), clock=lambda: 1_780_000_000.0)
        lane.seed({KEY: {"kalshi": [quote()]}})
        store = Store(tmp_db())
        self.addCleanup(store.close)
        self.assertEqual(lane.poll_trades(store, cadence_s=0.0), 1)
        self.assertTrue(any("unreadable print" in e for e in lane.trade_errors), lane.trade_errors)

    def test_a_legacy_non_finite_print_cannot_break_the_restart_cursor(self):
        store = Store(tmp_db())
        self.addCleanup(store.close)
        # A file written before those were rejected: int(inf) raised here, and every later
        # poll of that ticker then failed before recording anything.
        store.conn.execute("INSERT INTO trade_prints (trade_id,ticker,ts,price,count) "
                           "VALUES ('legacy-inf','T-A',9e999,0.5,1)")
        store.conn.commit()
        self.assertIsNone(store.latest_trade_ts("T-A"))
        store.record_trade_prints([trade_row("t1", 1_780_000_009)])
        self.assertEqual(store.latest_trade_ts("T-A"), 1_780_000_009)


class KalshiTradeAdapterTests(unittest.TestCase):
    """``KalshiClient.trades`` is the single-page public-print adapter."""

    def client(self, payload):
        from arb_engine.venues.kalshi import KalshiClient

        c = KalshiClient.__new__(KalshiClient)
        c.get = lambda path, params=None, **kw: (self.seen.append((path, dict(params or {}))), payload)[1]
        self.seen = []
        return c

    def test_min_ts_is_passed_through_inclusively(self):
        rows = [trade_row("t1", "2026-09-21T02:00:00Z")]
        got = self.client({"trades": rows}).trades("T-A", limit=50, min_ts=1_780_000_010)
        self.assertEqual(got, rows)
        self.assertEqual(self.seen[0][1]["min_ts"], 1_780_000_010)   # never min_ts + 1

    def test_a_page_without_its_trades_list_is_a_failed_read(self):
        for payload in ({"cursor": ""}, {"trades": None}, {"trades": {"a": 1}}, None):
            with self.assertRaises(ValueError, msg=payload):
                self.client(payload).trades("T-A")

    def test_an_empty_page_is_an_empty_list_not_an_error(self):
        self.assertEqual(self.client({"trades": []}).trades("T-A"), [])


class TickRowContractTests(unittest.TestCase):
    def store(self):
        st = Store(tmp_db())
        self.addCleanup(st.close)
        return st

    def test_the_tick_timestamp_is_never_earlier_than_any_observation_in_it(self):
        store = self.store()
        rows = {"kalshi": [quote("K-1", "KC", ts=110.0, obs_ts=110.0, req_ts=109.0, refreshed=True),
                           # an approximate row stamped after the tick must lift it too
                           quote("K-2", "DEN", ts=120.0, obs_ts=120.0, approx_time=True, refreshed=False)]}
        store.record_l1(100.0, KEY, rows, home="KC", away="DEN")
        stored = store.tick_rows()[0]
        self.assertEqual(stored["ts"], 120.0)
        import json
        for r in json.loads(stored["l1_json"])["rows"]:
            self.assertLessEqual(r["obs_ts"], stored["ts"])

    def test_yes_and_no_rows_for_one_outcome_both_survive_with_full_identity(self):
        store = self.store()
        yes = OutcomeQuote("robinhood", "c1", KEY, "KC", ask=.55, bid=.53, ask_size=10, bid_size=9,
                           fee_params={"exchange": "rothera"}, ts=100.0, book_id="rothera",
                           quote_time=99.0,
                           meta={"side": "yes", "contract_id": "c1", "tie_payout": 0.0,
                                 "exchange": "rothera", "obs_ts": 100.0, "req_ts": 99.5,
                                 "refreshed": True})
        no = OutcomeQuote("robinhood", "c2#no", KEY, "KC", ask=.47, bid=.45, ask_size=8, bid_size=7,
                          fee_params={"exchange": "rothera"}, ts=100.0, book_id="rothera",
                          quote_time=99.0,
                          meta={"side": "no", "contract_id": "c2", "no_of": "DEN",
                                "mirror_of": "c1", "tie_payout": 1.0, "exchange": "rothera",
                                "obs_ts": 100.0, "req_ts": 99.5, "refreshed": True})
        l1 = store.l1_from_quotes({"robinhood": [yes, no]})
        sides = sorted(r["side"] for r in l1["rows"])
        self.assertEqual(sides, ["no", "yes"])
        by_side = {r["side"]: r for r in l1["rows"]}
        self.assertEqual(by_side["no"]["no_of"], "DEN")
        self.assertEqual(by_side["no"]["mirror_of"], "c1")
        self.assertEqual((by_side["yes"]["tie_payout"], by_side["no"]["tie_payout"]), (0.0, 1.0))
        for row in l1["rows"]:
            self.assertEqual(row["venue"], "robinhood")
            self.assertEqual(row["book_id"], "rothera")
            self.assertEqual(row["exchange"], "rothera")
            self.assertEqual(row["fee_params"], {"exchange": "rothera"})
            self.assertEqual((row["req_ts"], row["obs_ts"]), (99.5, 100.0))
            self.assertEqual(row["contract_id"], "c1" if row["side"] == "yes" else "c2")
        # The compatibility map can only hold one of them; the lossless list keeps both.
        self.assertEqual(l1["robinhood"]["KC"]["side"], "yes")

    def test_a_non_finite_time_on_a_quote_is_recorded_as_unknown(self):
        store = self.store()
        q = quote("K-1", "KC", ts=100.0, obs_ts=float("nan"), req_ts=float("inf"), refreshed=True)
        q = replace(q, quote_time=float("nan"))
        rows = store.l1_from_quotes({"kalshi": [q]}, req_ts=100.0, obs_ts=100.0)["rows"]
        self.assertIsNone(rows[0]["quote_time"])
        self.assertEqual((rows[0]["req_ts"], rows[0]["obs_ts"]), (100.0, 100.0))
        self.assertEqual(rows[0]["approx_time"], 1)

    def test_an_l1_size_change_never_becomes_a_trade(self):
        """The only writer of ``trade_prints`` is ``record_trade_prints`` from
        ``/markets/trades``. Recording L1 that shows a book shrinking (a fill *or* a
        cancellation - they are indistinguishable at L1) must add no print at all."""
        store = self.store()
        big = quote("K-1", "KC", ts=100.0, obs_ts=100.0, req_ts=99.0, refreshed=True)
        small = replace(big, ask_size=1.0, bid_size=1.0, ts=101.0,
                        meta={**big.meta, "obs_ts": 101.0, "req_ts": 100.0})
        store.record_l1(100.0, KEY, {"kalshi": [big]}, home="KC", away="DEN")
        store.record_l1(101.0, KEY, {"kalshi": [small]}, home="KC", away="DEN")
        self.assertEqual(len(store.tick_rows()), 2)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM trade_prints").fetchone()[0], 0)


class ShutdownTests(unittest.TestCase):
    def test_run_stops_its_workers_on_every_exit_path(self):
        clock = Clock()
        store = Store(tmp_db())
        self.addCleanup(store.close)

        class Client:
            def get(self, path, params=None):
                if path == "/markets":
                    return {"markets": [{"ticker": "KXT-KC", "yes_bid_dollars": ".52",
                                         "yes_ask_dollars": ".54"}]}
                time.sleep(0.3)
                return {"trades": [], "cursor": ""}

        slate = build_slate(clock, store=store, fast=0.05)
        slate.fastlane.kalshi, slate.fastlane.robinhood = Client(), None
        real_tick = slate.tick

        def boom(now=None):
            real_tick(now)
            raise KeyboardInterrupt("operator stopped the recorder")

        slate.tick = boom
        import arb_engine.strategy.live as live
        original = live.time
        live.time = clock
        try:
            with self.assertRaises(KeyboardInterrupt):
                slate.run(interval=0.05, duration=5.0, max_iterations=5, printer=lambda *_: None)
        finally:
            live.time = original
        self.assertTrue(slate._stop.is_set())
        alive = [t.name for t in threading.enumerate()
                 if t.name in ("fastlane", "kalshi-trade-prints")]
        self.assertEqual(alive, [])

    def test_close_reports_a_page_walk_that_did_not_finish(self):
        release = threading.Event()
        self.addCleanup(release.set)

        class Blocking:
            def get(self, path, params=None):
                release.wait(10)
                return {"trades": [], "cursor": ""}

        clock = Clock()
        store = Store(tmp_db())
        slate = build_slate(clock, store=store, fast=0.0)
        slate.fastlane.kalshi = Blocking()
        slate.fastlane.seed({KEY: {"kalshi": [quote()]}})
        slate.fastlane.poll_trades(store, background=True, cadence_s=0.0)
        lines = []
        self.assertFalse(slate.close(timeout=0.2, printer=lines.append))
        self.assertTrue(any("must not be closed yet" in l for l in lines), lines)
        release.set()
        self.assertTrue(slate.fastlane.wait_for_trade_polls(timeout=5))
        store.close()

    def test_close_is_idempotent(self):
        slate = build_slate(Clock(), fast=0.0)
        self.assertTrue(slate.close(timeout=0.1, printer=lambda *_: None))
        self.assertTrue(slate.close(timeout=0.1, printer=lambda *_: None))


if __name__ == "__main__":
    unittest.main()

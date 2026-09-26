"""Adversarial timing tests for the recorder and Kalshi public-print poller."""

import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from arb_engine.models import EventInfo, OutcomeQuote, VenueSnapshot
from arb_engine.store import Store
from arb_engine.strategy.fastlane import FastLane, refresh_kalshi
from arb_engine.strategy.live import LiveSlate


KEY = "nfl:DEN|KC:2026-09-21"


def quote(ticker="T-A", outcome="KC", ts=90.0, **meta):
    data = {"ticker": ticker, "side": "yes"}
    data.update(meta)
    return OutcomeQuote("kalshi", ticker, KEY, outcome, bid=.49, ask=.51,
                        bid_size=20, ask_size=30, ts=ts, meta=data)


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


if __name__ == "__main__":
    unittest.main()

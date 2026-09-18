"""Maker runner: discovery, pricing, reconcile, paper fills and hedge alerts — offline."""

import os
import unittest
from unittest import mock

from arb_engine.fees import KalshiFees, RobinhoodFees
from arb_engine.matching.matcher import MergedEvent, merge_snapshots
from arb_engine.models import EventInfo, OutcomeQuote
from arb_engine.quant.arbitrage import Leg, evaluate
from arb_engine.strategy.alerts import Alerter
from arb_engine.strategy.broker import KalshiBroker, PaperBroker
from arb_engine.strategy.maker import MakerConfig, MakerRunner, Watch
from arb_engine.venues.kalshi import KalshiClient

from .helpers import FakeHttp, load


def _event(kalshi_yes=(0.08, 0.09), kalshi_no=(0.91, 0.92), rh_yes=(0.03, 0.04), rh_no=(0.96, 0.97), rh_size=5000.0, k_size=200.0):
    """CAR @ ATL total 65.5: Kalshi over/under + Robinhood/Rothera over/under."""
    key = "nfl:ATL|CAR:2026-09-20:total:65.5"
    info = EventInfo(event_key=key, sport="nfl", market_type="total", outcomes=["over", "under"], labels={"over": "Over 65.5", "under": "Under 65.5"}, line=65.5, tie_rule="no_push", in_play=False)
    kfee = {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}
    t = "KXNFLTOTAL-26SEP20CARATL-66"
    kq = [
        OutcomeQuote("kalshi", t, key, "over", "Over 65.5", ask=kalshi_yes[1], bid=kalshi_yes[0], ask_size=6000, fee_params=kfee, meta={"ticker": t, "side": "yes", "exchange_index": 0}),
        OutcomeQuote("kalshi", t + "#no", key, "under", "Under 65.5", ask=kalshi_no[1], bid=kalshi_no[0], ask_size=k_size, fee_params=kfee, meta={"ticker": t, "side": "no", "exchange_index": 0}),
    ]
    cid = "c-65"
    rq = [
        OutcomeQuote("robinhood", cid, key, "over", "Over 65.5", ask=rh_yes[1], bid=rh_yes[0], ask_size=rh_size, fee_params={"exchange": "rothera"}, url="https://robinhood.com/x", meta={"side": "yes", "exchange": "rothera"}, book_id="rothera"),
        OutcomeQuote("robinhood", cid + "#no", key, "under", "Under 65.5", ask=rh_no[1], bid=rh_no[0], ask_size=500, fee_params={"exchange": "rothera"}, url="https://robinhood.com/x", meta={"side": "no", "exchange": "rothera"}, book_id="rothera"),
    ]
    return MergedEvent(event_key=key, info=info, quotes_by_venue={"kalshi": kq, "robinhood": rq})


class FakeFeed:
    def __init__(self):
        self.kalshi = {"KXNFLTOTAL-26SEP20CARATL-66": {"yes_bid": 0.08, "yes_ask": 0.09, "no_bid": 0.91, "no_ask": 0.92, "status": "active"}}
        self.rh = {"c-65": {"yes_ask": 0.04, "yes_bid": 0.03, "no_ask": 0.97, "no_bid": 0.96, "yes_ask_size": 5000, "no_ask_size": 500}}

    def kalshi_markets(self, tickers):
        return {t: dict(self.kalshi[t]) for t in tickers if t in self.kalshi}

    def robinhood_quotes(self, ids):
        return {i: dict(self.rh[i]) for i in ids if i in self.rh}

    def polymarket_asks(self, slugs):
        return {}


def _runner(cfg=None, feed=None, tmpdir=None):
    cfg = cfg or MakerConfig(size=100, min_margin=0.005, max_orders=4, max_notional=1000, interval=0, rescan=1e9)
    feed = feed or FakeFeed()
    journal = os.path.join(tmpdir or os.environ.get("TMPDIR", "/tmp"), "maker_test_journal.jsonl")
    r = MakerRunner(cfg, feed, PaperBroker(), Alerter(journal_path=journal, quiet=True, desktop=False, webhook=""), settings={}, scan_fn=lambda: [_event()])
    return r


class DiscoveryTests(unittest.TestCase):
    def test_both_directions_and_pricing(self):
        r = _runner()
        ws = {w.key: w for w in r.discover([_event()])}
        self.assertEqual(set(ws), {"nfl:ATL|CAR:2026-09-20:total:65.5|kalshi:over", "nfl:ATL|CAR:2026-09-20:total:65.5|kalshi:under"})
        under = ws["nfl:ATL|CAR:2026-09-20:total:65.5|kalshi:under"]
        self.assertEqual((under.kalshi_side, under.hedge_venue, under.hedge_id, under.hedge_side), ("no", "robinhood", "c-65", "yes"))
        # Hedge = RH over at 0.04 -> all-in 0.054 -> maker max ~0.94; ask is 0.92 so a taker arb exists;
        # we rest one tick under the ask.
        self.assertTrue(under.taker_arb)
        self.assertEqual(under.desired_price, 0.91)
        self.assertGreater(under.margin_if_filled, 0.02)
        # The other direction (rest Kalshi over vs RH under at 0.97) cannot lock a margin.
        over = ws["nfl:ATL|CAR:2026-09-20:total:65.5|kalshi:over"]
        self.assertIsNone(over.desired_price)
        self.assertTrue(over.reason)

    def test_queue_ahead_filter(self):
        ev = _event(kalshi_no=(0.93, 0.95))  # bid 0.93, ask 0.95; maker max for under ≈ 0.94 -> ok (>= bid)
        r = _runner(MakerConfig(size=100, min_margin=0.0, max_orders=4, max_notional=1000, interval=0, rescan=1e9))
        w = next(x for x in r.discover([ev]) if x.kalshi_side == "no")
        self.assertEqual(w.desired_price, 0.94)
        ev = _event(rh_yes=(0.07, 0.08), kalshi_no=(0.93, 0.95))  # hedge dearer -> maker max ~0.90 < bid 0.93
        w = next(x for x in r.discover([ev]) if x.kalshi_side == "no")
        self.assertIsNone(w.desired_price)
        self.assertIn("behind best bid", w.reason)
        r2 = _runner(MakerConfig(size=100, min_margin=0.0, queue_ahead=False, interval=0, rescan=1e9))
        w = next(x for x in r2.discover([ev]) if x.kalshi_side == "no")
        self.assertEqual(w.desired_price, 0.90)

    def test_in_play_and_thin_hedge_skipped(self):
        ev = _event()
        ev.info.in_play = True
        self.assertEqual(_runner().discover([ev]), [])
        ev = _event(rh_size=0.5)
        w = next(x for x in _runner().discover([ev]) if x.kalshi_side == "no")
        self.assertIsNone(w.desired_price)
        self.assertIn("hedge size", w.reason)


class LifecycleTests(unittest.TestCase):
    def test_rest_fill_hedge_alert(self):
        r = _runner()
        r.step()  # rescan + refresh + reconcile
        resting = [o for o in r.orders if o.status == "resting"]
        self.assertEqual(len(resting), 1)
        o = resting[0]
        self.assertEqual((o.ticker, o.side, o.price, o.count), ("KXNFLTOTAL-26SEP20CARATL-66", "no", 0.91, 100))
        self.assertEqual(o.payload["side"], "ask")          # buy NO = sell YES leg at 1 - 0.91
        self.assertEqual(o.payload["price"], "0.0900")
        self.assertTrue(o.payload["post_only"])
        # Someone sells the under down to our price: paper fill (book now 0.90 / 0.91).
        r.feed.kalshi["KXNFLTOTAL-26SEP20CARATL-66"].update({"no_ask": 0.91, "no_bid": 0.90})
        r.step()
        self.assertEqual(o.status, "filled")
        self.assertEqual(len(r.fills), 1)
        fill = r.fills[0]
        self.assertEqual(fill["hedge_venue"], "robinhood")
        # Max hedge price: 100 contracts, Kalshi leg 0.91 with maker fee 0.15 -> budget 100 - 91.15 = 8.85 -> RH over at 0.06 costs 6 + 2.00... 0.06: 6 + (ceil(0.10*0.06*0.94*100)=0.57)+1.00 = 7.57 <= 8.85 ok; 0.07: 7 + 0.66 + 1 = 8.66 ok; 0.08: 8+0.74+1 = 9.74 no -> 0.07
        self.assertEqual(fill["hedge_max_price"], 0.07)
        self.assertAlmostEqual(fill["margin_if_hedged_now"], evaluate([Leg("under", "kalshi", 0.91, KalshiFees(maker_fees=True), role="maker"), Leg("over", "robinhood", 0.04, RobinhoodFees())], 100).margin)
        alerts = [e for e in r.alerts.events if e["kind"] == "alert" and e["title"] == "HEDGE NOW"]
        self.assertEqual(len(alerts), 1)
        self.assertIn("HEDGE NOW: buy 100 x Over 65.5 on robinhood at ≤ 0.07", alerts[0]["msg"])
        # A filled watch gets a fresh order on the next reconcile (still priceable).
        self.assertEqual(len([x for x in r.orders if x.status == "resting"]), 1)

    def test_reprice_and_cancel_when_hedge_moves(self):
        r = _runner(MakerConfig(size=100, min_margin=0.0, max_orders=4, max_notional=1000, interval=0, rescan=1e9))
        r.step()
        o = next(x for x in r.orders if x.status == "resting")
        self.assertEqual(o.price, 0.91)
        # Hedge gets dearer (RH over ask 0.04 -> 0.06): still a taker arb at 0.92, nothing changes.
        r.feed.rh["c-65"]["yes_ask"] = 0.06
        r.step()
        self.assertEqual(o.status, "resting")
        # 0.08: maker max falls to 0.90; with the Kalshi bid at 0.89 that is still ahead of the queue -> re-price.
        r.feed.rh["c-65"]["yes_ask"] = 0.08
        r.feed.kalshi["KXNFLTOTAL-26SEP20CARATL-66"]["no_bid"] = 0.89
        r.step()
        self.assertEqual(o.status, "canceled")
        o2 = next(x for x in r.orders if x.status == "resting")
        self.assertEqual(o2.price, 0.90)
        # Hedge disappears: cancel and do not re-place.
        r.feed.rh["c-65"]["yes_ask"] = None
        r.step()
        self.assertEqual(o2.status, "canceled")
        self.assertEqual([x for x in r.orders if x.status == "resting"], [])
        w = r.watches["nfl:ATL|CAR:2026-09-20:total:65.5|kalshi:under"]
        self.assertEqual(w.reason, "no hedge ask")

    def test_limits(self):
        cfg = MakerConfig(size=100, min_margin=0.0, max_orders=1, max_notional=50, interval=0, rescan=1e9)
        r = _runner(cfg)
        r.step()
        self.assertEqual([x for x in r.orders if x.status == "resting"], [])  # 0.91 * 100 = $91 > $50 notional cap
        r.cfg.max_notional = 1000
        r.reconcile()
        self.assertEqual(len([x for x in r.orders if x.status == "resting"]), 1)

    def test_shutdown_cancels(self):
        r = _runner()
        r.step()
        r.shutdown()
        self.assertTrue(all(o.status != "resting" for o in r.orders))
        self.assertTrue(any(e["kind"] == "info" and "shutdown cancel" in e["msg"] for e in r.alerts.events))

    def test_run_loop_iterations(self):
        r = _runner()
        r.run(duration=10, max_iterations=2)
        self.assertTrue(any(e["kind"] == "info" and "maker runner stop" in e["msg"] for e in r.alerts.events))


class PolymarketHedgeTests(unittest.TestCase):
    def test_refresh_uses_the_right_outcome_token(self):
        ev = _event()
        # Replace the Robinhood hedge with a Polymarket one: outcomes [Over, Under], hedge = Over = index 0.
        key = ev.event_key
        ev.quotes_by_venue = {"kalshi": ev.quotes_by_venue["kalshi"], "polymarket": [
            OutcomeQuote("polymarket", "tok0", key, "over", "Over 65.5", ask=0.04, bid=0.03, fee_params={"feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True}}, meta={"slug": "nfl-car-atl-2026-09-20-total-65pt5", "outcome_index": 0}),
            OutcomeQuote("polymarket", "tok1", key, "under", "Under 65.5", ask=0.97, bid=0.96, fee_params={"feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True}}, meta={"slug": "nfl-car-atl-2026-09-20-total-65pt5", "outcome_index": 1}),
        ]}

        class Feed(FakeFeed):
            def polymarket_asks(self, slugs):
                return {"nfl-car-atl-2026-09-20-total-65pt5": {"ask0": 0.05, "bid0": 0.03, "ask1": 0.97}}

        r = _runner(feed=Feed())
        r.scan_fn = lambda: [ev]
        r.rescan()
        under = r.watches["nfl:ATL|CAR:2026-09-20:total:65.5|kalshi:under"]
        self.assertEqual((under.hedge_venue, under.hedge_side), ("polymarket", "0"))
        over = r.watches["nfl:ATL|CAR:2026-09-20:total:65.5|kalshi:over"]
        self.assertEqual(over.hedge_side, "1")
        r.refresh()
        self.assertEqual(under.hedge_ask, 0.05)   # outcome 0's ask
        self.assertEqual(over.hedge_ask, 0.97)    # outcome 1's ask = 1 - bid0


class BrokerGateTests(unittest.TestCase):
    def test_kalshi_broker_requires_creds_and_confirm(self):
        with self.assertRaises(RuntimeError):
            KalshiBroker(KalshiClient(env="demo", api_key=None, private_key_path=None), confirm=True)
        with self.assertRaises(RuntimeError):
            KalshiBroker(KalshiClient(env="demo", api_key="k", private_key_path="/x.pem"), confirm=False)
        with mock.patch.dict(os.environ, {"ARB_LIVE_TRADING": "0"}):
            with self.assertRaises(RuntimeError):
                KalshiBroker(KalshiClient(env="prod", api_key="k", private_key_path="/x.pem"), confirm=True)
        b = KalshiBroker(KalshiClient(env="demo", api_key="k", private_key_path="/x.pem"), confirm=True)
        self.assertEqual(b.name, "kalshi")

    def test_kalshi_broker_place_and_poll(self):
        http = FakeHttp({"/portfolio/events/orders/abc": {}, "/portfolio/events/orders": {"order": {"order_id": "abc", "status": "resting"}}, "/portfolio/orders/abc": {"order": {"order_id": "abc", "status": "resting", "fill_count_fp": "40"}}})
        client = KalshiClient(env="demo", api_key="k", private_key_path="/x.pem", http=http)
        client._auth_headers = lambda m, p: {}  # skip signing in the test
        b = KalshiBroker(client, confirm=True)
        o = b.place("KXT-1", "no", 0.91, 100, watch_key="w")
        self.assertEqual((o.order_id, o.status), ("abc", "resting"))
        fills = b.poll([o], {})
        self.assertEqual(fills, [(o, 40.0, 0.91)])
        self.assertEqual(o.filled, 40.0)
        b.cancel(o)
        self.assertEqual(o.status, "canceled")
        self.assertTrue(any("/portfolio/events/orders/abc" in c for c in http.calls))


class ScannerIntegrationTests(unittest.TestCase):
    def test_discover_from_recorded_scan(self):
        """The recorded DET-BUF fixtures (moneyline + lines on three venues) produce watches."""
        from tests.test_scanner import _adapters

        snaps = [a.fetch("nfl") for a in _adapters()]
        merged = [me for me in merge_snapshots(snaps).values() if len(me.quotes_by_venue) >= 2]
        r = _runner(MakerConfig(size=100, min_margin=-1.0, queue_ahead=False, interval=0, rescan=1e9))
        ws = r.discover(merged)
        self.assertGreater(len(ws), 4)
        self.assertTrue(all(w.hedge_venue in ("robinhood", "polymarket") for w in ws))
        self.assertTrue(all(w.kalshi_ticker.startswith("KXNFL") for w in ws))
        priced = [w for w in ws if w.desired_price is not None]
        self.assertTrue(priced)
        for w in priced:
            self.assertLess(w.desired_price, w.kalshi_ask)


if __name__ == "__main__":
    unittest.main()

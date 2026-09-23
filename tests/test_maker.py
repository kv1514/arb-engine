"""Maker runner: discovery, pricing, reconcile, paper fills, hedge alerts, venue eligibility,
hedge-cash / balance caps, exchange-status pause and broker contracts — offline."""

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


def _event(kalshi_yes=(0.08, 0.09), kalshi_no=(0.91, 0.92), rh_yes=(0.03, 0.04), rh_no=(0.96, 0.97), rh_size=5000.0, k_size=200.0, key="nfl:ATL|CAR:2026-09-20:total:65.5", t="KXNFLTOTAL-26SEP20CARATL-66", cid="c-65"):
    """CAR @ ATL total 65.5: Kalshi over/under + Robinhood/Rothera over/under."""
    info = EventInfo(event_key=key, sport="nfl", market_type="total", outcomes=["over", "under"], labels={"over": "Over 65.5", "under": "Under 65.5"}, line=65.5, tie_rule="no_push", in_play=False)
    kfee = {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}
    kq = [
        OutcomeQuote("kalshi", t, key, "over", "Over 65.5", ask=kalshi_yes[1], bid=kalshi_yes[0], ask_size=6000, fee_params=kfee, meta={"ticker": t, "side": "yes", "exchange_index": 0}),
        OutcomeQuote("kalshi", t + "#no", key, "under", "Under 65.5", ask=kalshi_no[1], bid=kalshi_no[0], ask_size=k_size, fee_params=kfee, meta={"ticker": t, "side": "no", "exchange_index": 0}),
    ]
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
        # The alert is an order ticket: sport, the Kalshi fill, then the hedge order with the
        # cash and the fee Robinhood charges for those 100 contracts.
        msg = alerts[0]["msg"]
        self.assertTrue(msg.startswith("NFL - "), msg)
        self.assertIn("filled 100 x Under 65.5 @ 0.91 on KALSHI", msg)
        self.assertIn("ROBINHOOD buy 100 x Over 65.5 @ <= 0.07", msg)
        self.assertIn("ask now 0.04 -> $4.00 + $1.39 fee = $5.39", msg)
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
        self.assertTrue(any(e["kind"] == "info" and e.get("reason") == "shutdown" and e["msg"].startswith("cancel ") for e in r.alerts.events))

    def test_run_loop_iterations(self):
        r = _runner()
        r.run(duration=10, max_iterations=2)
        self.assertTrue(any(e["kind"] == "info" and "maker runner stop" in e["msg"] for e in r.alerts.events))


def _polymarket_event(restricted=False):
    """The fixture event with the Robinhood hedge replaced by a Polymarket one:
    outcomes [Over, Under], hedge for the under rest = Over = token index 0."""
    ev = _event()
    key = ev.event_key
    meta = {"slug": "nfl-car-atl-2026-09-20-total-65pt5"}
    if restricted:
        meta["restricted"] = True
    fee = {"feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True}}
    ev.quotes_by_venue = {"kalshi": ev.quotes_by_venue["kalshi"], "polymarket": [
        OutcomeQuote("polymarket", "tok0", key, "over", "Over 65.5", ask=0.04, bid=0.03, fee_params=fee, meta={**meta, "outcome_index": 0}),
        OutcomeQuote("polymarket", "tok1", key, "under", "Under 65.5", ask=0.97, bid=0.96, fee_params=fee, meta={**meta, "outcome_index": 1}),
    ]}
    return ev


class PMFeed(FakeFeed):
    def polymarket_asks(self, slugs):
        return {"nfl-car-atl-2026-09-20-total-65pt5": {"ask0": 0.05, "bid0": 0.03, "ask1": 0.97}}


class HedgeEligibilityTests(unittest.TestCase):
    """Global Polymarket is not executable for US persons: never a hedge unless opted in."""

    def test_default_hedge_venues_exclude_polymarket(self):
        self.assertEqual(MakerConfig().hedge_venues, ("robinhood",))
        r = _runner()
        self.assertEqual(r.hedge_venues, ("robinhood",))
        self.assertEqual(r.ineligible_hedges, {})
        self.assertEqual(r.discover([_polymarket_event()]), [])
        self.assertEqual(r.skipped_ineligible, {"polymarket": 2})
        r.scan_fn = lambda: [_polymarket_event()]
        r.step()
        self.assertEqual(r.orders, [])   # zero rests with an ineligible hedge venue
        self.assertTrue(any("hedge quotes skipped as ineligible: polymarket=2" in e.get("msg", "") for e in r.alerts.events))

    def test_opt_in_restores_polymarket_and_alerts_say_so(self):
        cfg = MakerConfig(size=100, min_margin=0.005, max_orders=4, max_notional=1000, interval=0, rescan=1e9, hedge_venues=("robinhood", "polymarket"))
        r = _runner(cfg, feed=PMFeed())
        self.assertIn("polymarket", r.ineligible_hedges)
        self.assertIn("not executable for US persons", r.ineligible_hedges["polymarket"])
        r.scan_fn = lambda: [_polymarket_event()]
        r.step()
        o = next(x for x in r.orders if x.status == "resting")
        rest = next(e for e in r.alerts.events if e.get("msg", "").startswith("rest paper"))
        self.assertIn("NOT EXECUTABLE", rest["msg"])
        # Fill -> the HEDGE NOW alert states the venue's eligibility.
        r.feed.kalshi["KXNFLTOTAL-26SEP20CARATL-66"].update({"no_ask": o.price, "no_bid": o.price - 0.01})
        r.step()
        hedge = next(e for e in r.alerts.events if e["kind"] == "alert" and e["title"] == "HEDGE NOW")
        self.assertIn("polymarket: NOT EXECUTABLE", hedge["msg"])
        self.assertIn("not executable for US persons", hedge["hedge_eligibility"])
        # run() shouts once at start when an opted-in venue is ineligible.
        r2 = _runner(cfg, feed=PMFeed())
        r2.scan_fn = lambda: []
        r2.run(duration=1, max_iterations=1)
        self.assertTrue(any(e["kind"] == "alert" and e["title"] == "HEDGE VENUE NOT EXECUTABLE" for e in r2.alerts.events))

    def test_executable_venues_setting_makes_polymarket_eligible(self):
        cfg = MakerConfig(size=100, min_margin=0.005, interval=0, rescan=1e9, hedge_venues=("robinhood", "polymarket"))
        r = MakerRunner(cfg, PMFeed(), PaperBroker(), Alerter(journal_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), "maker_test_journal.jsonl"), quiet=True, desktop=False, webhook=""), settings={"executable_venues": "kalshi,robinhood,polymarket"})
        self.assertEqual(r.ineligible_hedges, {})
        self.assertTrue(r.discover([_polymarket_event()]))

    def test_restricted_market_is_skipped_unless_opted_in(self):
        # Gamma flags every sports market restricted, so the flag cannot veto an explicit opt-in.
        cfg = MakerConfig(size=100, min_margin=0.005, interval=0, rescan=1e9)
        r = _runner(cfg, feed=PMFeed())
        self.assertEqual(r.discover([_polymarket_event(restricted=True)]), [])
        self.assertEqual(r.skipped_ineligible, {"polymarket": 2})
        cfg = MakerConfig(size=100, min_margin=0.005, interval=0, rescan=1e9, hedge_venues=("robinhood", "polymarket"))
        r = _runner(cfg, feed=PMFeed())
        self.assertTrue(r.discover([_polymarket_event(restricted=True)]))

    def test_robinhood_kalshi_routed_book_is_never_a_hedge(self):
        ev = _event()
        for q in ev.quotes_by_venue["robinhood"]:
            q.book_id = "kalshi"
        self.assertEqual(_runner().discover([ev]), [])


class HedgeCashTests(unittest.TestCase):
    def _three_events(self):
        # Three totals whose hedge (RH over) asks are 0.60, 0.55 and 0.50 -> hedge cash 60 + 55 + 50 = 165 if all rest.
        # Kalshi under bid/ask 0.35/0.40 leaves room to rest (maker max vs a 0.60 hedge ≈ 0.38).
        evs = []
        for i, (ask, tkr, cid) in enumerate(((0.60, "KXT-A", "c-a"), (0.55, "KXT-B", "c-b"), (0.50, "KXT-C", "c-c"))):
            evs.append(_event(kalshi_no=(0.30, 0.40), rh_yes=(ask - 0.01, ask), key=f"nfl:X{i}|Y{i}:2026-09-20:total:44.5", t=tkr, cid=cid))
        return evs

    def test_hedge_cash_caps_resting_exposure(self):
        evs = self._three_events()
        feed = FakeFeed()
        feed.kalshi = {t: {"yes_bid": 0.60, "yes_ask": 0.70, "no_bid": 0.30, "no_ask": 0.40, "status": "active"} for t in ("KXT-A", "KXT-B", "KXT-C")}
        feed.rh = {c: {"yes_ask": a, "yes_bid": a - 0.01, "no_ask": 0.99, "no_bid": 0.98, "yes_ask_size": 5000, "no_ask_size": 500} for c, a in (("c-a", 0.60), ("c-b", 0.55), ("c-c", 0.50))}
        cfg = MakerConfig(size=100, min_margin=0.0, max_orders=8, max_notional=10_000, interval=0, rescan=1e9, hedge_cash=150)
        r = _runner(cfg, feed=feed)
        r.scan_fn = lambda: evs
        r.step()
        resting = [o for o in r.orders if o.status == "resting"]
        self.assertEqual(len(resting), 2)
        self.assertEqual(r.hedge_exposure(), 105.0)   # 50 + 55; adding the 60 would breach 150
        # Ranked by margin: the cheapest hedges (0.50, 0.55) rest; the 0.60 one is the third and is refused.
        hedged = sorted(r.watches[o.watch_key].hedge_ask for o in resting)
        self.assertEqual(hedged, [0.50, 0.55])
        line = next(e for e in r.alerts.events if e.get("reason") == "hedge-cash")
        self.assertIn("hedge-cash: not resting KXT-A", line["msg"])
        # Lifting the cap rests the third one.
        r.cfg.hedge_cash = 0
        r.reconcile()
        self.assertEqual(len([o for o in r.orders if o.status == "resting"]), 3)

    def test_collateral_bounded_by_kalshi_balance(self):
        class Client:
            has_credentials = True
            calls = 0

            def balance(self):
                Client.calls += 1
                return {"balance": 5000}  # cents -> $50

        class Broker(PaperBroker):
            client = Client()

        cfg = MakerConfig(size=100, min_margin=0.0, max_orders=4, max_notional=1000, interval=0, rescan=1e9)
        r = MakerRunner(cfg, FakeFeed(), Broker(), Alerter(journal_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), "maker_test_journal.jsonl"), quiet=True, desktop=False, webhook=""), settings={}, scan_fn=lambda: [_event()])
        r.step()
        self.assertEqual(r.collateral_cap(), 1000)  # max_notional still bounds resting + new
        self.assertEqual(r.available_balance(), 50.0)  # the live balance bounds each NEW order alone
        self.assertEqual([o for o in r.orders if o.status == "resting"], [])  # $91 rest > $50 available
        r.step()
        self.assertEqual(Client.calls, 1)  # balance polled at most once a minute
        # Kalshi's balance already nets resting collateral: $300 resting + $200 available must
        # still admit a $50 order (the old min(cap, balance) check double-counted the $300).
        Client.calls = 0
        r2 = MakerRunner(MakerConfig(size=100, min_margin=0.0, max_orders=4, max_notional=1000, interval=0, rescan=1e9), FakeFeed(), Broker(), Alerter(journal_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), "maker_test_journal.jsonl"), quiet=True, desktop=False, webhook=""), settings={}, scan_fn=lambda: [_event()])
        r2.balance, r2.last_balance_poll = 200.0, 1e18
        self.assertEqual(r2.collateral_cap(), 1000)
        self.assertEqual(r2.available_balance(), 200.0)


class ExchangeStatusTests(unittest.TestCase):
    def _runner_with_status(self):
        now = [1_000_000.0]
        status = {"trading_active": True, "exchange_active": True}
        polls = []

        def exchange_status():
            polls.append(now[0])
            return dict(status)

        cfg = MakerConfig(size=100, min_margin=0.005, max_orders=4, max_notional=1000, interval=0, rescan=1e9)
        r = MakerRunner(cfg, FakeFeed(), PaperBroker(), Alerter(journal_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), "maker_test_journal.jsonl"), quiet=True, desktop=False, webhook=""), settings={}, scan_fn=lambda: [_event()], clock=lambda: now[0], exchange_status=exchange_status)
        return r, now, status, polls

    def test_pause_cancels_all_and_polls_every_30s(self):
        r, now, status, polls = self._runner_with_status()
        r.step()
        self.assertEqual(len(polls), 1)
        o = next(x for x in r.orders if x.status == "resting")
        for _ in range(5):   # 5 loops inside 30 s: no new poll
            now[0] += 5
            r.step()
        self.assertEqual(len(polls), 1)
        status["trading_active"] = False
        now[0] += 10   # 35 s since the first poll
        r.step()
        self.assertEqual(len(polls), 2)
        self.assertTrue(r.paused)
        self.assertEqual(o.status, "canceled")
        self.assertEqual([x for x in r.orders if x.status == "resting"], [])
        self.assertTrue(any(e["kind"] == "alert" and e["title"] == "EXCHANGE PAUSED" for e in r.alerts.events))
        self.assertTrue(any(e.get("reason") == "exchange not trading" and e["msg"].startswith("cancel ") for e in r.alerts.events))
        # While paused nothing is rested even though the watch is priceable.
        now[0] += 5
        r.step()
        self.assertEqual([x for x in r.orders if x.status == "resting"], [])
        # Trading resumes: next poll clears the pause and the watch is rested again.
        status["trading_active"] = True
        now[0] += 30
        r.step()
        self.assertFalse(r.paused)
        self.assertEqual(len([x for x in r.orders if x.status == "resting"]), 1)

    def test_no_status_source_means_no_pause(self):
        r = _runner()
        self.assertIsNone(r.exchange_status)   # FakeFeed.kalshi is a dict, not a client
        self.assertIsNone(r.poll_exchange_status())
        self.assertFalse(r.paused)


class BrokerContractTests(unittest.TestCase):
    def test_place_receives_resting_orders_on_the_same_ticker(self):
        seen = []

        class Broker(PaperBroker):
            def place(self, ticker, side, price, count, watch_key="", exchange_index=None, resting=None):
                seen.append(list(resting or []))
                return super().place(ticker, side, price, count, watch_key=watch_key, exchange_index=exchange_index)

        cfg = MakerConfig(size=100, min_margin=0.005, max_orders=4, max_notional=1000, interval=0, rescan=1e9)
        b = Broker()
        r = MakerRunner(cfg, FakeFeed(), b, Alerter(journal_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), "maker_test_journal.jsonl"), quiet=True, desktop=False, webhook=""), settings={}, scan_fn=lambda: [_event()])
        r.step()
        self.assertEqual(seen, [[]])
        # A second rest on the same ticker (other watch) sees the first one.
        first = r.orders[0]
        other = b.place("KXNFLTOTAL-26SEP20CARATL-66", "yes", 0.05, 100, watch_key="x|kalshi:over")
        r.orders.append(other)
        first.status = "canceled"
        r.watches["nfl:ATL|CAR:2026-09-20:total:65.5|kalshi:under"].order = None
        r.reconcile()
        self.assertEqual([o.order_id for o in seen[-1]], [other.order_id])
        # The stock PaperBroker now declares resting= (the self-match guard, P12); a broker
        # without the kwarg still works and simply is not told.
        r2 = _runner()
        self.assertTrue(r2._place_accepts_resting)
        r2.step()
        self.assertTrue(any(o.status == "resting" for o in r2.orders))

        class Plain(PaperBroker):
            def place(self, ticker, side, price, count, watch_key="", exchange_index=None):  # no resting kwarg, no **kw
                return super().place(ticker, side, price, count, watch_key=watch_key, exchange_index=exchange_index)
        r3 = MakerRunner(cfg, FakeFeed(), Plain(), Alerter(journal_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), "maker_test_journal.jsonl"), quiet=True, desktop=False, webhook=""), settings={}, scan_fn=lambda: [_event()])
        self.assertFalse(r3._place_accepts_resting)
        r3.step()
        self.assertTrue(any(o.status == "resting" for o in r3.orders))

    def test_shutdown_uses_batched_cancel_all_when_the_broker_has_it(self):
        calls = []

        class Broker(PaperBroker):
            def cancel_all(self, orders=None):
                calls.append(list(orders or []))
                for o in orders or []:
                    self.cancel(o)

        cfg = MakerConfig(size=100, min_margin=0.005, max_orders=4, max_notional=1000, interval=0, rescan=1e9)
        r = MakerRunner(cfg, FakeFeed(), Broker(), Alerter(journal_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), "maker_test_journal.jsonl"), quiet=True, desktop=False, webhook=""), settings={}, scan_fn=lambda: [_event()])
        r.step()
        r.shutdown()
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(o.status == "canceled" for o in r.orders))
        self.assertTrue(any(e.get("reason") == "shutdown" and "(batched)" in e["msg"] for e in r.alerts.events))


class CliPluginTests(unittest.TestCase):
    """cli_plugins/maker_flags: registers against any argparse subparser set and its maker
    handler carries hedge venues / hedge cash / allowed venues into MakerConfig."""

    def _parser(self):
        import argparse

        p = argparse.ArgumentParser(prog="arb-engine")
        sub = p.add_subparsers(dest="cmd")
        parsers = {}
        for name in ("scan", "live", "maker"):
            sp = sub.add_parser(name)
            sp.add_argument("--sport", default="nfl")
            sp.add_argument("--venues", default="kalshi,polymarket,robinhood")
            sp.add_argument("--markets", default="moneyline,spread,total")
            sp.add_argument("--target-margin", type=float, default=0.0)
            sp.add_argument("--gold", action="store_true")
            parsers[name] = sp
        mk = parsers["maker"]
        for flag, kw in (("--mode", {"default": "paper"}), ("--confirm", {"action": "store_true"}), ("--size", {"type": float, "default": 100}), ("--min-margin", {"type": float, "default": 0.01}), ("--max-orders", {"type": int, "default": 8}), ("--max-notional", {"type": float, "default": 500.0}), ("--max-per-event", {"type": int, "default": 1}), ("--deep-queue", {"action": "store_true"}), ("--interval", {"type": float, "default": 10.0}), ("--rescan", {"type": float, "default": 300.0}), ("--duration", {"type": float, "default": 3600.0}), ("--iterations", {"type": int, "default": None}), ("--journal", {"default": os.path.join(os.environ.get("TMPDIR", "/tmp"), "maker_plugin_journal.jsonl")}), ("--state", {"default": None})):
            mk.add_argument(flag, **kw)
        return p, sub, parsers

    def test_register_adds_flags_and_handlers(self):
        from arb_engine.cli_plugins import maker_flags

        p, sub, parsers = self._parser()
        handlers = maker_flags.register(sub, parsers)
        self.assertEqual(set(handlers), {"maker", "scan", "live"})
        a = p.parse_args(["maker", "--hedge-venues", "robinhood,polymarket", "--hedge-cash", "120"])
        self.assertEqual((a.hedge_venues, a.hedge_cash, a.allowed_venues), ("robinhood,polymarket", 120.0, "executable"))
        self.assertEqual(p.parse_args(["scan"]).allowed_venues, "all")
        # Idempotent: registering twice (loader quirk) does not raise on duplicate options.
        maker_flags.register(sub, parsers)
        # Works with only the subparsers action (choices) when no dict is passed.
        p2, sub2, _ = self._parser()
        self.assertIn("maker", maker_flags.register(sub2))

    def test_maker_handler_wires_config(self):
        import contextlib
        import io

        from arb_engine.cli_plugins import maker_flags

        seen = {}
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

        def factory(cfg, settings, venues):
            seen.update(cfg=cfg, settings=settings, venues=venues)
            r = _runner(cfg)
            r.scan_fn = lambda: []
            return r

        p, sub, parsers = self._parser()
        maker_flags.register(sub, parsers)
        with mock.patch.dict(os.environ, {"EXECUTABLE_VENUES": "", "MAKER_HEDGE_CASH": "", "MAKER_HEDGE_VENUES": ""}):
            a = p.parse_args(["maker", "--iterations", "1", "--duration", "1", "--interval", "0"])
            maker_flags.run_maker(a, {}, runner_factory=factory)
            self.assertEqual(seen["cfg"].hedge_venues, ("robinhood",))
            self.assertEqual(seen["cfg"].hedge_cash, 250.0)
            self.assertEqual(seen["venues"], ["kalshi", "robinhood"])   # polymarket not fetched: not executable
            a = p.parse_args(["maker", "--iterations", "1", "--duration", "1", "--interval", "0", "--hedge-venues", "robinhood,polymarket", "--hedge-cash", "0"])
            maker_flags.run_maker(a, {}, runner_factory=factory)
            self.assertEqual(seen["cfg"].hedge_venues, ("robinhood", "polymarket"))
            self.assertEqual(seen["cfg"].hedge_cash, 0.0)
            self.assertEqual(seen["venues"], ["kalshi", "polymarket", "robinhood"])   # opt-in fetches it
            a = p.parse_args(["maker", "--iterations", "1", "--duration", "1", "--interval", "0", "--allowed-venues", "all"])
            maker_flags.run_maker(a, {}, runner_factory=factory)
            self.assertEqual(seen["venues"], ["kalshi", "polymarket", "robinhood"])
        with mock.patch.dict(os.environ, {"MAKER_HEDGE_CASH": "75", "MAKER_HEDGE_VENUES": "robinhood"}):
            a = p.parse_args(["maker", "--iterations", "1", "--duration", "1", "--interval", "0"])
            maker_flags.run_maker(a, {}, runner_factory=factory)
            self.assertEqual((seen["cfg"].hedge_cash, seen["cfg"].hedge_venues), (75.0, ("robinhood",)))

    def test_allowed_venue_resolution(self):
        from arb_engine.cli_plugins import maker_flags

        with mock.patch.dict(os.environ, {"EXECUTABLE_VENUES": ""}):
            self.assertIsNone(maker_flags.allowed_venues("all"))
            self.assertEqual(maker_flags.allowed_venues("executable"), {"kalshi", "robinhood"})
            self.assertEqual(maker_flags.allowed_venues("kalshi, polymarket"), {"kalshi", "polymarket"})
            self.assertEqual(maker_flags.allowed_venues(None, {"executable_venues": "robinhood"}), {"robinhood"})

    def test_live_handler_narrows_venues_and_scan_handler_passes_allowed(self):
        from arb_engine import cli
        from arb_engine.cli_plugins import maker_flags

        calls = []
        with mock.patch.object(cli, "cmd_live", lambda args: calls.append(("live", args.venues)) or 0):
            a = mock.Mock(venues="kalshi,polymarket,robinhood", allowed_venues="executable")
            maker_flags.run_live(a, {})
            self.assertEqual(calls, [("live", "kalshi,robinhood")])
            a = mock.Mock(venues="polymarket", allowed_venues="executable")
            with self.assertRaises(SystemExit):
                maker_flags.run_live(a, {})
        seen = {}

        def fake_scan(*a, **kw):
            seen.update(kw)
            raise RuntimeError("stop")

        def fake_cmd_scan(args, settings):
            cli.scan("nfl", [], settings=settings)
            return 0

        with mock.patch.object(cli, "scan", fake_scan), mock.patch.object(cli, "cmd_scan", fake_cmd_scan):
            with self.assertRaises(RuntimeError):
                maker_flags.run_scan(mock.Mock(allowed_venues="executable"), {})
            self.assertEqual(seen["allowed_venues"], {"kalshi", "robinhood"})
            self.assertIs(cli.scan, fake_scan)   # restored after the call


class PolymarketHedgeTests(unittest.TestCase):
    def test_refresh_uses_the_right_outcome_token(self):
        ev = _polymarket_event()
        r = _runner(MakerConfig(size=100, min_margin=0.005, max_orders=4, max_notional=1000, interval=0, rescan=1e9, hedge_venues=("robinhood", "polymarket")), feed=PMFeed())
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
        self.assertTrue(all(w.hedge_venue == "robinhood" for w in ws))   # default: executable hedges only
        self.assertGreater(r.skipped_ineligible.get("polymarket", 0), 0)
        r_in = _runner(MakerConfig(size=100, min_margin=-1.0, queue_ahead=False, interval=0, rescan=1e9, hedge_venues=("robinhood", "polymarket")))
        ws_in = r_in.discover(merged)
        self.assertGreaterEqual(len(ws_in), len(ws))
        self.assertTrue(any(w.hedge_venue == "polymarket" for w in ws_in))
        self.assertTrue(all(w.kalshi_ticker.startswith("KXNFL") for w in ws))
        priced = [w for w in ws if w.desired_price is not None]
        self.assertTrue(priced)
        for w in priced:
            self.assertLess(w.desired_price, w.kalshi_ask)


if __name__ == "__main__":
    unittest.main()

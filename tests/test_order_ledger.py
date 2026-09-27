"""execution/ledger.py + strategy/lagexec.py: durable intents, restart-safe budgets, orders whose
outcome is unknown, and actual fill / fee accounting - against an in-memory Kalshi account."""

from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from unittest import mock

from arb_engine.execution.kalshi import KalshiExecutor
from arb_engine.execution.ledger import (Budget, LedgerError, OrderLedger, env_host_problem, fee_bound, host_env,
                                         worst_cost)
from arb_engine.fees.base import D
from arb_engine.fees.kalshi import KalshiFees
from arb_engine.models import OutcomeQuote
from arb_engine.strategy.lagexec import LagExecutor
from arb_engine.venues.http import HttpError

KEY = "nfl:DEN|KC:2026-09-21"
TICKER = "KXNFLGAME-26SEP21DENKC-KC"
DEMO_URL = "https://external-api.demo.kalshi.co/trade-api/v2"
PROD_URL = "https://external-api.kalshi.com/trade-api/v2"


def tmp(name="ledger.sqlite3"):
    return os.path.join(tempfile.mkdtemp(prefix="arb_test_"), name)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeKalshi:
    """A demo account in memory. ``create_order`` records the order the way the exchange
    would and can then lose the answer (``lose_answer``: timeout after acceptance), fail
    before recording anything (``fail_before``), or answer without an order id
    (``blank_answer``). Reads lag writes by ``read_lag`` seconds (404 / absent)."""

    env, base_url, has_credentials = "demo", DEMO_URL, True

    def __init__(self, clock, fill=lambda n: n, api_key="key-id-A1", account="comms-id-A"):
        self.clock, self.fill = clock, fill
        self.api_key, self.account = api_key, account   # the key id and GET /communications/id
        self.comms_error = None
        self.rows, self.fill_rows, self.creates, self.cancels = {}, [], [], []
        self.lose_answer = None      # an exception to raise after the order is on the book
        self.fail_before = None      # an exception to raise before anything is recorded
        self.blank_answer = False
        self.read_lag = 0.0
        self.truncated = False
        self.cost_fields = True
        self.extra_fill_fee = Decimal("0")
        self.multiplier = 1                          # the series' fee_multiplier (GET /series/{ticker})
        self.rounding = "centicent"                  # what Kalshi's demo charged on 2026-09-26
        self.series_error = None
        self.omit = set()                            # order-row fields a read leaves out (an incomplete answer)
        self.fills_truncated = False                 # GET /portfolio/fills says more pages exist
        self.fills_hidden = False                    # the fills listing trails the order row (nothing listed yet)
        self.extra_fills = []                        # rows the fills listing adds (duplicates, contradictions)

    def communications_id(self):
        if self.comms_error is not None:
            raise self.comms_error
        return self.account

    def series(self, ticker):
        if self.series_error is not None:
            raise self.series_error
        return {"ticker": ticker, "fee_type": "quadratic", "fee_multiplier": self.multiplier}

    def create_order(self, payload):
        self.creates.append(payload)
        if self.fail_before is not None:
            raise self.fail_before
        n = int(float(payload["count"]))
        yes = Decimal(payload["price"])
        price = yes if payload["side"] == "bid" else Decimal(1) - yes
        resting = payload.get("time_in_force") == "good_till_canceled"
        got = 0 if resting else self.fill(n)            # a post-only maker order rests; an IOC fills or is gone
        fee = KalshiFees(multiplier=D(self.multiplier), rounding=self.rounding).fee(price, got, "taker") if got else Decimal("0")
        oid = f"o{len(self.rows) + 1}"
        row = {"order_id": oid, "client_order_id": payload.get("client_order_id"), "ticker": payload["ticker"],
               "status": "resting" if resting else ("executed" if got == n else "canceled"),
               "outcome_side": "yes" if payload["side"] == "bid" else "no", "fill_count_fp": f"{got}.00",
               "remaining_count_fp": f"{n}.00" if resting else "0.00", "initial_count_fp": f"{n}.00", "created": self.clock(), "_price": price}
        if self.cost_fields:
            row.update(taker_fill_cost_dollars=str(price * got), maker_fill_cost_dollars="0.0000", taker_fees_dollars=str(fee), maker_fees_dollars="0.0000")
        self.rows[oid] = row
        if got:
            fid = f"f{len(self.fill_rows) + 1}"
            self.fill_rows.append({"fill_id": fid, "trade_id": "t" + fid, "order_id": oid, "count_fp": f"{got}.00",
                                   "fee_cost": str(fee + self.extra_fill_fee), "yes_price_dollars": str(yes)})
        if self.lose_answer is not None:
            raise self.lose_answer
        if self.blank_answer:
            return {}
        return {"order_id": oid, "client_order_id": payload.get("client_order_id"), "fill_count": f"{got}.00", "remaining_count": "0.00",
                **({"average_fill_price": str(price)} if got else {})}

    def fill_resting(self, oid, k):
        """Someone sells into our resting order: k contracts at its price, as maker (the order
        row and the fills listing both show it, as on the exchange)."""
        row = self.rows[oid]
        filled = int(float(row["fill_count_fp"])) + k
        left = int(float(row["remaining_count_fp"])) - k
        row.update(fill_count_fp=f"{filled}.00", remaining_count_fp=f"{left}.00", status="resting" if left else "executed",
                   maker_fill_cost_dollars=str(row["_price"] * filled), maker_fees_dollars="0.0000",
                   taker_fill_cost_dollars="0.0000", taker_fees_dollars="0.0000")
        fid = f"f{len(self.fill_rows) + 1}"
        self.fill_rows.append({"fill_id": fid, "trade_id": "t" + fid, "order_id": oid, "count_fp": f"{k}.00", "fee_cost": "0.0000",
                               "is_taker": False})

    def cancel_order(self, order_id, market_ticker=None, exchange_index=None, subaccount=None):
        row = self.rows.get(order_id)
        if row is None or row["status"] != "resting":
            raise HttpError(404, f"{DEMO_URL}/portfolio/events/orders/{order_id}", "not found")
        left = row["remaining_count_fp"]
        row.update(status="canceled", remaining_count_fp="0.00")
        self.cancels.append(order_id)
        return {"order_id": order_id, "reduced_by": left, "ts_ms": 0}

    def _visible(self, row):
        return self.clock() - row["created"] >= self.read_lag

    def _read(self, row):
        return {k: v for k, v in row.items() if k not in ("created", "_price") and k not in self.omit}

    def order(self, oid):
        row = self.rows.get(oid)
        if row is None or not self._visible(row):
            raise HttpError(404, f"{DEMO_URL}/portfolio/orders/{oid}", "not found")
        return self._read(row)

    def _fills(self, order_id):
        return [] if self.fills_hidden else [dict(f) for f in self.fill_rows + self.extra_fills if f["order_id"] == order_id]

    def paged(self, path, key, params):
        if path == "/portfolio/fills":
            assert key == "fills"
            return self._fills(params.get("order_id")), self.fills_truncated
        assert path == "/portfolio/orders" and key == "orders"
        rows = [self._read(r) for r in self.rows.values() if self._visible(r) and r["ticker"] == params.get("ticker", r["ticker"])]
        return rows, self.truncated

    def fills_v2(self, **params):
        return self._fills(params.get("order_id"))


def sig(ts=1000.0, ask=0.60, contracts=50, event=KEY):
    from arb_engine.strategy.leadlag import LagSignal

    return LagSignal(event_key=event, title="DEN @ KC", leader="robinhood", follower="kalshi", outcome="KC", label="Kansas City", lead_move=0.08,
                     follower_move=0.0, leader_mid=0.675, follower_ask=ask, follower_all_in=ask + 0.017, edge=0.058, depth=300,
                     suggested_contracts=contracts, lag_s=0.0, ts=ts)


def quotes(ticker=TICKER, event=KEY):
    return {"kalshi": [OutcomeQuote("kalshi", ticker, event, "KC", ask=0.60, bid=0.59, meta={"ticker": ticker, "side": "yes"})]}


def executor(client, clock, path=None, **kw):
    return LagExecutor(mode="demo", executor=KalshiExecutor(client), intents_path=tmp("lag.jsonl"), ledger_path=path or tmp(), clock=clock, **kw)


class FeeBoundTests(unittest.TestCase):
    def test_bound_covers_every_split_of_an_ioc_into_fills(self):
        fees = KalshiFees()
        for limit in ("0.03", "0.21", "0.49", "0.50", "0.61", "0.97"):
            for n in (1, 7, 50):
                bound = fee_bound(limit, n, 1)
                # Any fill prices at or under the limit, split any way: never above the bound.
                for price in (Decimal(limit), Decimal("0.50") if Decimal(limit) >= Decimal("0.5") else Decimal(limit), Decimal("0.01")):
                    for split in ([n], [1] * n, [n // 2, n - n // 2]):
                        charged = sum((fees.fee(price, k, "taker") for k in split if k), Decimal("0"))
                        self.assertLessEqual(charged, bound, (limit, n, price, split))
        self.assertEqual(worst_cost("0.60", 50, 1), Decimal("31.00"))   # 50 x 0.60 + 50 x 2c


def maker(client, clock, path=None, owner=None, owner_alive=None, **kw):
    """A KalshiBroker (the maker's) on its own ledger; ``owner`` names the process."""
    from arb_engine.strategy.broker import KalshiBroker

    led_kw = {k: v for k, v in (("owner", owner), ("owner_alive", owner_alive)) if v is not None}
    led = OrderLedger.for_client(client, path=path or tmp(), clock=clock, **led_kw)
    return KalshiBroker(client, confirm=True, ledger=led, clock=clock, **kw)


class MakerLedgerTests(unittest.TestCase):
    """The maker's resting orders go through the ledger too: an order whose answer was lost
    blocks new ones until it is found (and, untracked, cancelled); a restart cancels what a
    dead maker left resting; fills and fees are booked from the rows the maker reads."""

    W = KEY + "|kalshi:yes"

    def test_a_maker_create_whose_answer_was_lost_blocks_then_is_found_and_cancelled(self):
        from arb_engine.strategy.broker import OrderOutcomeUnknown, OrderRefused

        clock = Clock(1000.0)
        c = FakeKalshi(clock)
        c.lose_answer = HttpError(0, DEMO_URL + "/portfolio/events/orders", "curl: (28) Operation timed out")
        b = maker(c, clock)
        with self.assertRaises(OrderOutcomeUnknown):
            b.place(TICKER, "yes", 0.40, 10, watch_key=self.W)
        c.lose_answer = None
        self.assertEqual((len(c.creates), c.rows["o1"]["status"]), (1, "resting"))       # it is on the book all the same
        self.assertEqual(c.creates[0]["client_order_id"], b.ledger.rows()[0]["client_order_id"])
        with self.assertRaisesRegex(OrderRefused, "unknown outcome"):
            b.place(TICKER, "yes", 0.39, 10, watch_key=self.W)
        self.assertEqual(len(c.creates), 1)                                             # nothing new while it is unknown
        clock.t = 1003.0
        b.poll([], {})                                                                  # reconcile: found resting, untracked -> cancelled
        self.assertEqual((c.cancels, c.rows["o1"]["status"]), (["o1"], "canceled"))
        clock.t = 1010.0
        b.poll([], {})                                                                  # its final state booked
        row = b.ledger.rows()[0]
        self.assertEqual((row["state"], row["fill_count"]), ("done", "0.00"))
        self.assertIsNone(b.ledger.blocked())
        o = b.place(TICKER, "yes", 0.39, 10, watch_key=self.W)
        self.assertEqual((o.status, len(c.creates)), ("resting", 2))

    def test_a_request_that_never_reached_the_exchange_is_released_and_the_maker_goes_on(self):
        from arb_engine.strategy.broker import OrderOutcomeUnknown

        clock = Clock(1000.0)
        c = FakeKalshi(clock)
        c.fail_before = HttpError(503, DEMO_URL, "unavailable")
        b = maker(c, clock)
        with self.assertRaises(OrderOutcomeUnknown):
            b.place(TICKER, "yes", 0.40, 10, watch_key=self.W)
        c.fail_before = None
        for t in (1003.0, 1040.0):
            clock.t = t
            b.poll([], {})
        self.assertEqual(b.ledger.rows()[0]["state"], "rejected")
        self.assertEqual(b.place(TICKER, "yes", 0.40, 10, watch_key=self.W).status, "resting")

    def test_a_restarted_maker_cancels_what_its_dead_predecessor_left_resting(self):
        clock, path = Clock(1000.0), tmp()
        c = FakeKalshi(clock)
        a = maker(c, clock, path, owner="maker-host:101")
        o1, o2 = a.place(TICKER, "yes", 0.40, 10, watch_key=self.W), a.place("KXNFLGAME-26SEP21DENKC-DEN", "yes", 0.35, 10, watch_key=KEY + "|kalshi:den")
        c.fill_resting(o1.order_id, 4)                         # 4 filled before the process was killed (no shutdown cancel)
        clock.t = 1100.0
        b = maker(c, clock, path, owner="maker-host:202", owner_alive=lambda owner: owner != "maker-host:101")
        b.recover()
        self.assertEqual(sorted(c.cancels), sorted([o1.order_id, o2.order_id]))
        clock.t = 1110.0
        b.reconcile(force=True)
        rows = {r["order_id"]: r for r in b.ledger.rows()}
        self.assertEqual((rows[o1.order_id]["state"], rows[o1.order_id]["fill_count"], Decimal(rows[o1.order_id]["fill_cost"])), ("done", "4.00", Decimal("1.60")))
        self.assertEqual((rows[o2.order_id]["state"], rows[o2.order_id]["fill_count"]), ("done", "0.00"))
        self.assertEqual(b.ledger.exposure(strategy="maker"), Decimal("1.60"))

    def test_a_maker_that_is_still_running_keeps_its_orders(self):
        clock, path = Clock(1000.0), tmp()
        c = FakeKalshi(clock)
        a = maker(c, clock, path, owner="maker-host:101")
        a.place(TICKER, "yes", 0.40, 10, watch_key=self.W)
        clock.t = 1100.0
        b = maker(c, clock, path, owner="maker-host:202", owner_alive=lambda owner: True)
        b.recover()
        self.assertEqual(c.cancels, [])
        self.assertEqual(b.ledger.rows()[0]["state"], "accepted")

    def test_fills_of_a_resting_order_are_booked_and_finished_after_the_cancel(self):
        clock = Clock(1000.0)
        c = FakeKalshi(clock)
        b = maker(c, clock)
        o = b.place(TICKER, "yes", 0.40, 10, watch_key=self.W)
        c.fill_resting(o.order_id, 3)
        clock.t = 1003.0
        self.assertEqual(b.poll([o], {}), [(o, 3.0, 0.40)])
        self.assertEqual((b.ledger.get(o.intent_id)["state"], b.ledger.get(o.intent_id)["fill_count"]), ("accepted", "3.00"))
        b.cancel(o)
        clock.t = 1010.0
        b.poll([o], {})                                             # no longer resting: the reconcile books its end
        row = b.ledger.get(o.intent_id)
        self.assertEqual((row["state"], row["fill_count"], Decimal(row["fill_cost"])), ("done", "3.00", Decimal("1.20")))
        self.assertEqual(c.cancels, [o.order_id])                   # cancelled once, by the runner's own cancel

    def test_an_unknown_fee_multiplier_rests_nothing(self):
        from arb_engine.strategy.broker import OrderRefused

        clock = Clock(1000.0)
        c = FakeKalshi(clock)
        c.series_error = HttpError(503, DEMO_URL, "down")
        b = maker(c, clock)
        with self.assertRaisesRegex(OrderRefused, "fee multiplier"):
            b.place(TICKER, "yes", 0.40, 10, watch_key=self.W)
        self.assertEqual(c.creates, [])

    def test_the_runner_recovers_before_anything_rests(self):
        from arb_engine.strategy.alerts import Alerter
        from arb_engine.strategy.maker import MakerConfig, MakerRunner

        calls = []

        class Broker:
            name = "stub"

            def place(self, *a, **k):
                calls.append("place")
                raise AssertionError("nothing should rest in this test")

            def poll(self, orders, state):
                return []

            def cancel(self, o):
                pass

            def recover(self):
                calls.append("recover")
                return [{"ticker": TICKER, "before": "ambiguous", "after": "done", "note": "found"}]

        class Feed:
            kalshi = None

        alerts = Alerter(journal_path=tmp("maker.jsonl"), quiet=True, desktop=False, webhook="")
        MakerRunner(MakerConfig(interval=0), Feed(), Broker(), alerts, settings={}).run(duration=0.01, max_iterations=0)
        self.assertEqual(calls, ["recover"])
        self.assertTrue(any("maker recovery" in str(e.get("msg")) for e in alerts.events))


class AccountIdentityTests(unittest.TestCase):
    """A ledger belongs to one Kalshi account: its budgets and its reconciliation never mix
    accounts, a rotated key of the same account carries on, and no identifier is stored."""

    def _ambiguous(self, client, clock, path, fail=HttpError(503, DEMO_URL, "unavailable")):
        client.fail_before = fail                                    # never reached the exchange
        ex = executor(client, clock, path=path)
        rec = ex.on_signal(sig(ts=clock.t), quotes())
        client.fail_before = None
        self.assertEqual(rec["state"], "ambiguous")
        return ex, rec

    def test_a_ledger_refuses_a_client_of_another_account(self):
        clock, path = Clock(), tmp()
        a = FakeKalshi(clock)
        executor(a, clock, path=path).on_signal(sig(), quotes())
        b = FakeKalshi(clock, api_key="key-id-B1", account="comms-id-B")
        ex_b = executor(b, clock, path=path)
        self.assertIn("belongs to another Kalshi account", ex_b.blocked_reason)
        self.assertEqual(ex_b.on_signal(sig(ts=1001.0), quotes())["status"], "blocked")
        self.assertEqual(b.creates, [])
        with self.assertRaises(LedgerError):
            OrderLedger.for_client(b, path=path)

    def test_another_accounts_client_cannot_reconcile_or_release(self):
        clock, path = Clock(1000.0), tmp()
        a = FakeKalshi(clock)
        ex, rec = self._ambiguous(a, clock, path)
        b = FakeKalshi(clock, api_key="key-id-B1", account="comms-id-B")
        clock.t = 1100.0
        with self.assertRaises(LedgerError):
            ex.ledger.reconcile(b)                                    # B's listing says nothing about A's orders
        self.assertEqual(ex.ledger.get(rec["intent_id"])["state"], "ambiguous")

    def test_an_unverified_account_never_releases_what_it_cannot_see(self):
        clock, path = Clock(1000.0), tmp()
        a = FakeKalshi(clock)
        ex, rec = self._ambiguous(a, clock, path)
        # Another key whose account cannot be read: it may be A's, it may not.
        stranger = FakeKalshi(clock, api_key="key-id-X9")
        stranger.comms_error = HttpError(503, DEMO_URL, "down")
        for t in (1003.0, 1040.0, 1200.0):
            clock.t = t
            res = ex.ledger.reconcile(stranger)
        self.assertEqual(ex.ledger.get(rec["intent_id"])["state"], "ambiguous")
        self.assertIn("not released", res[0]["note"])
        # The key that sent it can: one key never spans two accounts.
        for t in (1203.0, 1240.0):
            clock.t = t
            ex.ledger.reconcile(a)
        self.assertEqual(ex.ledger.get(rec["intent_id"])["state"], "rejected")

    def test_a_rotated_key_of_the_same_account_carries_on(self):
        clock, path = Clock(1000.0), tmp()
        old_key = FakeKalshi(clock)
        ex, rec = self._ambiguous(old_key, clock, path)
        new_key = FakeKalshi(clock, api_key="key-id-A2")                 # same account, new key
        clock.t = 1003.0
        ex2 = executor(new_key, clock, path=path)                       # start-up reconciles with the new key
        self.assertIsNone(ex2.blocked_reason)
        clock.t = 1040.0
        ex2.reconcile(force=True)
        self.assertEqual(ex2.ledger.get(rec["intent_id"])["state"], "rejected")
        self.assertEqual(ex2.ledger.status()["keys_seen"], 2)

    def test_a_key_whose_account_was_unreadable_is_mapped_once_it_is_read(self):
        clock, path = Clock(1000.0), tmp()
        k1 = FakeKalshi(clock)
        k1.comms_error = HttpError(503, DEMO_URL, "down")               # account unknown when the order went out
        ex, rec = self._ambiguous(k1, clock, path)
        self.assertIsNone(ex.ledger.get(rec["intent_id"])["account_fp"])
        k1.comms_error = None
        OrderLedger.for_client(k1, path=path).close()                   # later the same key reads its account: mapped
        k2 = FakeKalshi(clock, api_key="key-id-A2")
        led = OrderLedger.for_client(k2, path=path, clock=clock)
        for t in (1003.0, 1040.0):
            clock.t = t
            led.reconcile(k2)
        self.assertEqual(led.get(rec["intent_id"])["state"], "rejected")
        led.close()

    def test_no_key_id_or_account_id_is_written_to_disk(self):
        clock, path = Clock(1000.0), tmp()
        a = FakeKalshi(clock, api_key="KEYID-7f3e-SECRETISH", account="COMMS-ID-91c2")
        ex = executor(a, clock, path=path)
        ex.on_signal(sig(), quotes())
        clock.t += 3
        ex.reconcile(force=True)
        ex.ledger.close()
        blob = b"".join(open(f, "rb").read() for f in (path, path + "-wal") if os.path.exists(f))
        self.assertNotIn(b"KEYID-7f3e-SECRETISH", blob)
        self.assertNotIn(b"COMMS-ID-91c2", blob)
        self.assertIn(b"account:", blob)                                 # the fingerprints are there

    def test_a_stuck_intent_can_be_released_by_hand_with_a_reason(self):
        from arb_engine.cli_plugins.kalshi_ops import run_kalshi

        clock = Clock(1000.0)
        path = tmp("kalshi_demo_ledger.sqlite3")                          # where the CLI looks under ARB_ORDER_LEDGER_DIR
        a = FakeKalshi(clock)
        ex, rec = self._ambiguous(a, clock, path)
        with self.assertRaises(LedgerError):
            ex.ledger.release(rec["intent_id"], " ")
        base = dict(action="release", no_account_env=True, intent_id=rec["intent_id"], reason="checked on kalshi.com: never placed",
                    confirm=False, status="resting")
        out = io.StringIO()
        with mock.patch("arb_engine.execution.kalshi.KalshiClient", return_value=a), \
                mock.patch.dict(os.environ, {"ARB_ORDER_LEDGER_DIR": os.path.dirname(path)}), redirect_stdout(out):
            self.assertEqual(run_kalshi(argparse.Namespace(**base)), 0)                     # dry run
        self.assertEqual(ex.ledger.get(rec["intent_id"])["state"], "ambiguous")
        out = io.StringIO()
        with mock.patch("arb_engine.execution.kalshi.KalshiClient", return_value=a), \
                mock.patch.dict(os.environ, {"ARB_ORDER_LEDGER_DIR": os.path.dirname(path)}), redirect_stdout(out):
            self.assertEqual(run_kalshi(argparse.Namespace(**{**base, "confirm": True})), 0)
        led = OrderLedger(path, "demo", DEMO_URL)
        row = led.get(rec["intent_id"])
        self.assertEqual(row["state"], "rejected")
        self.assertIn("released by hand: checked on kalshi.com", row["reason"])
        self.assertIsNone(led.blocked())
        led.close()


class FeeMultiplierTests(unittest.TestCase):
    """The reserved fee must cover the fee a market can actually charge, whatever its multiplier."""

    def test_the_bound_covers_any_multiplier_split_and_rounding(self):
        for m in ("0", "0.5", "1", "1.5", "2", "3"):
            for limit in ("0.03", "0.21", "0.49", "0.50", "0.61", "0.97"):
                for n in (1, 7, 50):
                    bound = fee_bound(limit, n, m)
                    for rounding in ("cent", "centicent"):
                        fees = KalshiFees(multiplier=D(m), rounding=rounding)
                        for price in sorted({D(limit), min(D(limit), D("0.5")), D("0.01")}):
                            for split in ([n], [1] * n, [n // 2, n - n // 2]):
                                charged = sum((fees.fee(price, k, "taker") for k in split if k), Decimal("0"))
                                self.assertLessEqual(charged, bound, (m, limit, n, rounding, price, split))

    def test_an_assumed_multiplier_of_one_under_reserves_a_higher_one(self):
        # Why the multiplier is resolved, never defaulted: at 2x, one order of 10 at $0.50
        # costs $0.35 in fees; a bound built on 1x reserved $0.20.
        charged = KalshiFees(multiplier=Decimal(2)).fee("0.50", 10, "taker")
        self.assertGreater(charged, fee_bound("0.50", 10, 1))
        self.assertLessEqual(charged, fee_bound("0.50", 10, 2))
        for bad in ("-1", "nan", "inf"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                fee_bound("0.50", 10, bad)

    def test_a_reservation_without_a_known_multiplier_is_refused(self):
        led = OrderLedger(tmp(), "demo", DEMO_URL)
        res = led.reserve(strategy="lag", ticker=TICKER, side="yes", count=10, limit_price="0.50")
        self.assertFalse(res.ok)
        self.assertIn("fee multiplier", res.reason)
        self.assertEqual(led.rows(), [])
        led.close()

    def test_the_executor_reserves_at_the_exchanges_multiplier_when_the_quote_says_less(self):
        clock = Clock()
        client = FakeKalshi(clock)
        client.multiplier = 2                                   # the series, read from the exchange the order goes to
        ex = executor(client, clock)
        stated_one = {"kalshi": [OutcomeQuote("kalshi", TICKER, KEY, "KC", ask=0.60, bid=0.59, fee_params={"fee_type": "quadratic", "fee_multiplier": 1},
                                              meta={"ticker": TICKER, "side": "yes"})]}
        rec = ex.on_signal(sig(), stated_one)
        self.assertEqual((rec["status"], rec["fee_multiplier"]), ("SUBMITTED", "2"))
        row = ex.ledger.get(rec["intent_id"])
        self.assertEqual(Decimal(row["max_cost"]), Decimal("32.00"))       # 50 x 0.60 + 50 x 4c (2 x 0.07 x 0.25 -> 0.035 -> 4c)
        clock.t += 3
        ex.reconcile(force=True)
        row = ex.ledger.get(rec["intent_id"])
        self.assertEqual(Decimal(row["fees"]), Decimal("1.68"))           # charged at 2x: 2 x 0.07 x 50 x 0.24
        self.assertLessEqual(Decimal(row["fill_cost"]) + Decimal(row["fees"]), Decimal("32.00"))

    def test_an_unknown_multiplier_sends_nothing(self):
        clock = Clock()
        client = FakeKalshi(clock)
        client.series_error = HttpError(503, DEMO_URL, "down")
        ex = executor(client, clock)
        no_params = {"kalshi": [OutcomeQuote("kalshi", TICKER, KEY, "KC", ask=0.60, bid=0.59, meta={"ticker": TICKER, "side": "yes"})]}
        rec = ex.on_signal(sig(), no_params)
        self.assertEqual(rec["status"], "skipped")
        self.assertIn("fee multiplier of KXNFLGAME unknown", rec["reason"])
        self.assertEqual(client.creates, [])
        # An assumed multiplier (the adapter could not read the series) is not a stated one either.
        assumed = {"kalshi": [OutcomeQuote("kalshi", TICKER, KEY, "KC", ask=0.60, bid=0.59, meta={"ticker": TICKER, "side": "yes"},
                                           fee_params={"fee_type": "quadratic", "fee_multiplier": 1, "fee_multiplier_assumed": True})]}
        self.assertEqual(ex.on_signal(sig(ts=1001.0), assumed)["status"], "skipped")
        self.assertEqual(client.creates, [])

    def test_the_adapter_marks_a_multiplier_it_only_assumed(self):
        from arb_engine.execution.ledger import quote_fee_multiplier
        from arb_engine.venues.kalshi import KalshiAdapter

        class Down:
            def series(self, t):
                raise HttpError(503, "u", "down")
        info = KalshiAdapter(client=Down()).series_info("KXNFLGAME")
        self.assertTrue(info["fee_multiplier_assumed"])
        self.assertIsNone(quote_fee_multiplier({"fee_type": info["fee_type"], "fee_multiplier": info["fee_multiplier"], "fee_multiplier_assumed": True}))
        self.assertEqual(quote_fee_multiplier({"fee_type": "quadratic", "fee_multiplier": 0.5}), Decimal("0.5"))
        self.assertEqual(quote_fee_multiplier({"fee_type": "none"}), Decimal("0"))
        self.assertIsNone(quote_fee_multiplier({}))

    def test_the_manual_cap_is_checked_again_at_the_markets_multiplier(self):
        from arb_engine.cli_plugins.kalshi_ops import run_kalshi

        client = FakeKalshi(Clock())
        client.multiplier = 2
        args = argparse.Namespace(action="order", no_account_env=True, ticker=TICKER, price=0.61, count=40, max_notional=25.5, side_action="buy",
                                  side="yes", post_only=False, exchange_index=None, time_in_force="immediate_or_cancel", confirm=True, status="resting")
        with mock.patch("arb_engine.execution.kalshi.KalshiClient", return_value=client), \
                mock.patch.dict(os.environ, {"ARB_ORDER_LEDGER_DIR": os.path.dirname(tmp())}), \
                self.assertRaisesRegex(SystemExit, r"\$26.00 \(fees included, fee multiplier 2\)"):
            run_kalshi(args)                                    # $25.20 at 1x fits under $25.50; $26.00 at 2x does not
        self.assertEqual(client.creates, [])
        client.series_error = HttpError(503, DEMO_URL, "down")
        out = io.StringIO()
        with mock.patch("arb_engine.execution.kalshi.KalshiClient", return_value=client), \
                mock.patch.dict(os.environ, {"ARB_ORDER_LEDGER_DIR": os.path.dirname(tmp())}), redirect_stdout(out):
            self.assertEqual(run_kalshi(argparse.Namespace(**{**vars(args), "count": 10})), 3)
        self.assertIn("cannot be bounded", json.loads(out.getvalue())["status"])
        self.assertEqual(client.creates, [])


class HostEnvTests(unittest.TestCase):
    def test_hosts_map_to_their_environment(self):
        self.assertEqual((host_env(DEMO_URL), host_env(PROD_URL), host_env("https://demo-api.kalshi.co/trade-api/v2"),
                          host_env("https://api.elections.kalshi.com/trade-api/v2"), host_env("https://proxy.local/x")),
                         ("demo", "prod", "demo", "prod", None))
        self.assertIsNone(env_host_problem("demo", DEMO_URL))
        self.assertIn("prod host", env_host_problem("demo", PROD_URL))
        self.assertIn("demo host", env_host_problem("prod", DEMO_URL))
        self.assertIn("not a known Kalshi host", env_host_problem("demo", "https://proxy.local/trade-api/v2"))

    def test_a_recognised_host_is_not_enough_the_whole_endpoint_is_checked(self):
        from arb_engine.execution.ledger import endpoint_problem
        from arb_engine.strategy.broker import KalshiBroker

        bad = {
            "http://external-api.demo.kalshi.co/trade-api/v2": "only https",
            "HTTP://external-api.demo.kalshi.co/trade-api/v2": "only https",
            "https://user:pw@external-api.demo.kalshi.co/trade-api/v2": "user-info",
            "https://@external-api.demo.kalshi.co/trade-api/v2": "user-info",
            "https://external-api.demo.kalshi.co:8443/trade-api/v2": "port 8443",
            "https://external-api.demo.kalshi.co:80/trade-api/v2": "port 80",
            "https://external-api.demo.kalshi.co/other": "path",
            "https://external-api.demo.kalshi.co/trade-api/v1": "path",
            "https://external-api.demo.kalshi.co/trade-api/v2/portfolio": "path",
            "https://external-api.demo.kalshi.co": "path",
            "https://external-api.demo.kalshi.co/trade-api/v2?next=https://evil": "query",
            "https://external-api.demo.kalshi.co/trade-api/v2#x": "fragment",
            "https://external-api.demo.kalshi.co/trade-api/v2?": "query",
            " https://external-api.demo.kalshi.co/trade-api/v2": "whitespace",
            "https://external-api.demo.kalshi.co/trade-api/v2\n": "whitespace",
            "https://external-api.demo.kalshi.co\\@evil.example/trade-api/v2": "backslash",
            "https://external-api.demo.kalshi.co.evil.example/trade-api/v2": "not a known Kalshi host",
            "https://external-api.demo.kalshi.co:99999/trade-api/v2": "unparseable",
            "": "empty",
        }
        for url, why in bad.items():
            with self.subTest(url=url):
                problem = env_host_problem("demo", url)
                self.assertIsNotNone(problem)
                self.assertIn(why, problem)
                self.assertIsNone(host_env(url))
                client = FakeKalshi(Clock())
                client.base_url = url
                ex = KalshiExecutor(client)
                self.assertTrue(ex.execute(ex.plan(TICKER, "buy", "yes", 1, 0.5), confirm=True)["status"].startswith("BLOCKED"))
                with self.assertRaises(RuntimeError):
                    KalshiBroker(client, confirm=True)
                with self.assertRaises(LedgerError):
                    OrderLedger.for_client(client, path=tmp())
                self.assertEqual(client.creates, [])
        for good in (DEMO_URL, DEMO_URL + "/", "https://EXTERNAL-API.demo.kalshi.co/trade-api/v2", "https://external-api.demo.kalshi.co:443/trade-api/v2",
                     "https://demo-api.kalshi.co/trade-api/v2"):
            with self.subTest(good=good):
                self.assertIsNone(endpoint_problem(good))
                self.assertIsNone(env_host_problem("demo", good))

    def test_signed_requests_are_never_sent_over_plain_http(self):
        from arb_engine.venues.kalshi import KalshiClient

        c = KalshiClient(env="demo", api_key="k", private_key_path="/nonexistent.pem", base_url="http://external-api.demo.kalshi.co/trade-api/v2")
        with self.assertRaisesRegex(RuntimeError, "non-HTTPS"):
            c._auth_headers("GET", "/portfolio/balance")      # refused before the key file is even read

    def test_demo_gates_never_reach_a_production_host(self):
        client = FakeKalshi(Clock())
        client.base_url = PROD_URL                      # KALSHI_BASE_URL moved the host, KALSHI_ENV still says demo
        ex = KalshiExecutor(client)
        res = ex.execute(ex.plan(TICKER, "buy", "yes", 1, 0.5), confirm=True)
        self.assertTrue(res["status"].startswith("BLOCKED"))
        self.assertEqual(client.creates, [])
        lag = executor(client, Clock())
        self.assertIn("prod host", lag.blocked_reason)
        self.assertEqual(lag.on_signal(sig(), quotes())["status"], "blocked")
        self.assertEqual(client.creates, [])

    def test_a_ledger_belongs_to_one_environment_and_a_mode_to_one_client(self):
        path = tmp()
        OrderLedger(path, "demo", DEMO_URL).close()
        with self.assertRaisesRegex(LedgerError, "demo ledger"):
            OrderLedger(path, "prod", PROD_URL)
        led = OrderLedger(path, "demo", DEMO_URL)
        prod = FakeKalshi(Clock())
        prod.env, prod.base_url = "prod", PROD_URL
        with self.assertRaises(LedgerError):
            led.reconcile(prod)
        with self.assertRaisesRegex(RuntimeError, "demo client"):
            LagExecutor(mode="demo", executor=KalshiExecutor(prod), ledger_path=path)


class UnknownOutcomeTests(unittest.TestCase):
    def test_accepted_but_timed_out_order_blocks_then_is_found_and_accounted(self):
        clock = Clock(1000.0)
        client = FakeKalshi(clock)
        client.lose_answer = HttpError(0, DEMO_URL + "/portfolio/events/orders", "curl: (28) Operation timed out")
        ex = executor(client, clock)
        rec = ex.on_signal(sig(ts=1000.0), quotes())
        self.assertEqual((rec["status"], rec["state"]), ("UNKNOWN", "ambiguous"))
        self.assertEqual(len(client.creates), 1)
        self.assertEqual(client.creates[0]["client_order_id"], rec["client_order_id"])
        # Worst case stays reserved; nothing new goes out while the outcome is unknown.
        self.assertAlmostEqual(ex.sent_notional, 31.0)
        client.lose_answer = None
        clock.t = 1001.0
        blocked = ex.on_signal(sig(ts=1001.0), quotes())
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("unknown outcome", blocked["reason"])
        self.assertEqual(len(client.creates), 1)
        # Reconciliation finds it by client_order_id and books what was really paid.
        clock.t = 1003.0
        res = ex.reconcile(force=True)
        self.assertEqual([(r["before"], r["after"]) for r in res], [("ambiguous", "done")])
        row = ex.ledger.get(rec["intent_id"])
        self.assertEqual((row["order_id"], Decimal(row["fill_count"]), Decimal(row["fill_cost"]), Decimal(row["fees"])),
                         ("o1", Decimal("50"), Decimal("30.00"), Decimal("0.84")))
        self.assertAlmostEqual(ex.sent_notional, 30.84)
        self.assertIsNone(ex.ledger.blocked())
        self.assertEqual(ex.on_signal(sig(ts=1003.0), quotes())["status"], "SUBMITTED")

    def test_a_409_conflict_is_unknown_not_a_refusal(self):
        clock = Clock()
        client = FakeKalshi(clock)
        client.lose_answer = HttpError(409, DEMO_URL, '{"error":{"code":"conflict"}}')
        ex = executor(client, clock)
        self.assertEqual(ex.on_signal(sig(), quotes())["state"], "ambiguous")
        clock.t += 3
        self.assertEqual(ex.reconcile(force=True)[0]["after"], "done")

    def test_a_response_without_an_order_id_is_unknown_until_the_listing_shows_it(self):
        clock = Clock()
        client = FakeKalshi(clock)
        client.blank_answer = True
        ex = executor(client, clock)
        rec = ex.on_signal(sig(), quotes())
        self.assertEqual((rec["status"], rec["state"]), ("UNKNOWN", "ambiguous"))
        self.assertTrue(ex.describe(rec).startswith("AUTO (demo): UNKNOWN"))
        clock.t += 3
        ex.reconcile(force=True)
        self.assertEqual(ex.ledger.get(rec["intent_id"])["state"], "done")

    def test_a_request_that_never_arrived_is_released_only_by_complete_listings(self):
        clock = Clock(1000.0)
        client = FakeKalshi(clock)
        client.fail_before = HttpError(503, DEMO_URL, "unavailable")
        ex = executor(client, clock)
        rec = ex.on_signal(sig(), quotes())
        self.assertEqual(rec["state"], "ambiguous")
        # A truncated listing proves nothing, however long it has been.
        client.truncated = True
        for t in (1003.0, 1020.0, 1100.0):
            clock.t = t
            ex.reconcile(force=True)
        self.assertEqual(ex.ledger.get(rec["intent_id"])["state"], "ambiguous")
        client.truncated = False
        clock.t = 1101.0
        ex.reconcile(force=True)                      # one complete miss
        self.assertEqual(ex.ledger.get(rec["intent_id"])["state"], "ambiguous")
        clock.t = 1104.0
        ex.reconcile(force=True)                      # second complete miss, 104 s after sending
        row = ex.ledger.get(rec["intent_id"])
        self.assertEqual(row["state"], "rejected")
        self.assertIn("never accepted", row["reason"])
        self.assertEqual(ex.sent_notional, 0.0)
        self.assertIsNone(ex.ledger.blocked())

    def test_a_plain_client_error_is_released_after_one_complete_listing(self):
        clock = Clock(1000.0)
        client = FakeKalshi(clock)
        client.fail_before = HttpError(400, DEMO_URL, '{"error":{"code":"invalid_parameters"}}')
        ex = executor(client, clock)
        rec = ex.on_signal(sig(), quotes())
        clock.t = 1001.0
        ex.reconcile(force=True)                      # inside the settle time: not looked at yet
        self.assertEqual(ex.ledger.get(rec["intent_id"])["state"], "ambiguous")
        clock.t = 1003.0
        ex.reconcile(force=True)
        self.assertEqual(ex.ledger.get(rec["intent_id"])["state"], "rejected")

    def test_an_order_read_that_lags_the_write_is_retried_not_guessed(self):
        clock = Clock(1000.0)
        client = FakeKalshi(clock, fill=lambda n: 12)
        client.read_lag = 5.0
        ex = executor(client, clock)
        rec = ex.on_signal(sig(), quotes())
        self.assertEqual((rec["status"], rec["fill_count"]), ("SUBMITTED", "12.00"))
        self.assertAlmostEqual(ex.sent_notional, 12 * 0.62)            # provisional: fills at the limit + fee bound
        clock.t = 1003.0
        self.assertEqual(ex.reconcile(force=True)[0]["note"], "order not visible yet")
        clock.t = 1006.0
        ex.reconcile(force=True)
        row = ex.ledger.get(rec["intent_id"])
        self.assertEqual((row["state"], Decimal(row["fill_cost"]), Decimal(row["fees"])), ("done", Decimal("7.20"), Decimal("0.2016")))
        self.assertAlmostEqual(ex.sent_notional, 7.4016)                 # actual: 12 x 0.60 + 0.07 x 12 x 0.24 (centicent, as demo charges)


class AccountingTests(unittest.TestCase):
    def test_fills_that_disagree_with_the_order_row_keep_the_larger_fee(self):
        clock = Clock()
        client = FakeKalshi(clock)
        client.extra_fill_fee = Decimal("0.05")
        ex = executor(client, clock)
        rec = ex.on_signal(sig(), quotes())
        clock.t += 3
        ex.reconcile(force=True)
        self.assertEqual(Decimal(ex.ledger.get(rec["intent_id"])["fees"]), Decimal("0.89"))
        done = [e for e in ex.ledger.events(rec["intent_id"]) if e["kind"] == "done"][0]
        self.assertTrue(json.loads(done["detail"])["fills_mismatch"])

    def test_an_order_row_without_cost_fields_keeps_the_fills_at_the_limit_plus_the_fee_bound(self):
        clock = Clock()
        client = FakeKalshi(clock)
        client.cost_fields = False
        ex = executor(client, clock)
        rec = ex.on_signal(sig(), quotes())
        clock.t += 3
        ex.reconcile(force=True)
        row = ex.ledger.get(rec["intent_id"])
        # What the fills cost is not established: not done, the fills stay at the limit + the fee bound.
        self.assertEqual((row["state"], row["fill_count"], row["fill_cost"], row["fees"]), ("accepted", "50.00", None, None))
        self.assertIn("no taker_fill_cost_dollars", row["reason"])
        self.assertEqual(ex.ledger.exposure(), Decimal("31.00"))
        # The exchange reports them later: booked at what was paid.
        client.rows["o1"].update(taker_fill_cost_dollars="30.000000", maker_fill_cost_dollars="0.000000",
                                 taker_fees_dollars="0.840000", maker_fees_dollars="0.000000")
        clock.t += 3
        ex.reconcile(force=True)
        row = ex.ledger.get(rec["intent_id"])
        self.assertEqual((row["state"], Decimal(row["fill_cost"]), Decimal(row["fees"])), ("done", Decimal("30.00"), Decimal("0.84")))
        self.assertEqual(ex.ledger.exposure(), Decimal("30.84"))

    def test_an_ioc_that_filled_nothing_releases_its_budget(self):
        clock = Clock()
        client = FakeKalshi(clock, fill=lambda n: 0)
        ex = executor(client, clock, max_notional_per_game=31.0)
        self.assertEqual(ex.on_signal(sig(ts=1.0), quotes())["fill_count"], "0.00")
        self.assertEqual(ex.sent_notional, 0.0)
        self.assertEqual(ex.on_signal(sig(ts=2.0), quotes())["count"], 50)   # the whole game budget is still there


def order_row(oid="o1", **fields):
    """A final GET /portfolio/orders/{id} row of an IOC buy of 1 at $0.50, shaped like the demo
    rows recorded on 2026-09-26; ``field=None`` leaves that field out."""
    row = {"order_id": oid, "status": "executed", "fill_count_fp": "1.00", "remaining_count_fp": "0.00", "initial_count_fp": "1.00",
           "taker_fill_cost_dollars": "0.500000", "maker_fill_cost_dollars": "0.000000",
           "taker_fees_dollars": "0.017500", "maker_fees_dollars": "0.000000"}
    row.update(fields)
    return {k: v for k, v in row.items() if v is not None}


def fill_row(fid="f1", oid="o1", count="1.00", fee="0.017500", **fields):
    row = {"fill_id": fid, "trade_id": "t" + fid, "order_id": oid, "count_fp": count, "fee_cost": fee, "yes_price_dollars": "0.5000", "is_taker": True}
    row.update(fields)
    return {k: v for k, v in row.items() if v is not None}


class Fills:
    """Only ``GET /portfolio/fills`` (``apply_row`` is handed the order row itself)."""

    env, base_url = "demo", DEMO_URL

    def __init__(self, rows=(), truncated=False, error=None):
        self.rows, self.truncated, self.error = list(rows), truncated, error

    def paged(self, path, key, params):
        assert (path, key) == ("/portfolio/fills", "fills")
        if self.error is not None:
            raise self.error
        return [dict(r) for r in self.rows if r.get("order_id") == params["order_id"]], self.truncated


class EvidenceTests(unittest.TestCase):
    """Only adequate evidence releases a reservation: a final order row (a terminal status, an
    explicit zero remaining quantity, a fill count within the order) with its cost, and fees
    that the row or a complete, de-duplicated fills listing states. An absent fee is unknown,
    not zero. Anything less keeps the intent open, and late fills and fees still land."""

    BOUND = Decimal("0.52")         # 1 x $0.50 + the fee bound (0.07 x 0.25 rounded up to the cent)
    PAID = Decimal("0.5175")        # 1 x $0.50 + the fee demo charges (centicent)

    def open_ioc(self, count=1, limit="0.50", final_answer=False, clock=None, budget=None):
        """An IOC buy recorded and sent. ``final_answer``: its create answer said it filled in
        full with nothing remaining (the fills are booked); otherwise it said nothing final
        (no fills, no remaining quantity: the whole worst case stays)."""
        led = OrderLedger(tmp(), "demo", DEMO_URL, clock=clock or Clock())
        res = led.reserve(strategy="lag", ticker=TICKER, side="yes", count=count, limit_price=limit, event_key=KEY, game_key=KEY,
                          fee_multiplier=1, budget=budget)
        answer = {"order_id": "o1", "fill_count": f"{count}.00", "remaining_count": "0.00"} if final_answer else {"order_id": "o1", "fill_count": "0.00"}
        self.assertEqual(led.accepted(res.intent_id, answer), "accepted")
        return led, res.intent_id

    def test_a_row_without_status_or_remaining_quantity_releases_nothing(self):
        # Reported: order_id and fill_count=0, no status, no remaining quantity -> was "done", budget freed.
        maker = OrderLedger(tmp(), "demo", DEMO_URL, clock=Clock())
        res = maker.reserve(strategy="maker", ticker=TICKER, side="yes", count=10, limit_price="0.60", tif="good_till_canceled", fee_multiplier=1)
        maker.accepted(res.intent_id, {"order_id": "o1", "fill_count": "0.00", "remaining_count": "10.00"})
        self.assertEqual(maker.exposure(), Decimal("6.20"))
        note = maker.apply_row(res.intent_id, {"order_id": "o1", "fill_count": "0"})
        row = maker.get(res.intent_id)
        self.assertEqual(row["state"], "accepted")
        self.assertEqual(maker.exposure(), Decimal("6.20"))              # a resting order may still fill: nothing released
        self.assertIn("no status", note)
        self.assertIn("no remaining quantity", row["reason"])
        ioc, iid = self.open_ioc()
        self.assertEqual(ioc.exposure(), self.BOUND)
        ioc.apply_row(iid, {"order_id": "o1", "fill_count": "0"})
        self.assertEqual((ioc.get(iid)["state"], ioc.exposure()), ("accepted", self.BOUND))

    def test_an_unknown_or_open_status_is_not_final(self):
        for status in ("", "pending", "resting", "partially_filled", "EXECUTED?", "open"):
            with self.subTest(status=status):
                led, iid = self.open_ioc()
                led.apply_row(iid, order_row(status=status or None))
                self.assertEqual((led.get(iid)["state"], led.exposure()), ("accepted", self.BOUND))
        led, iid = self.open_ioc()
        led.apply_row(iid, order_row(status="Executed"), client=Fills([fill_row()]))   # a terminal status, however spelt
        self.assertEqual((led.get(iid)["state"], led.exposure()), ("done", self.PAID))

    def test_a_missing_or_open_remaining_quantity_is_not_final(self):
        for fields in ({"remaining_count_fp": None}, {"remaining_count_fp": "1.00", "status": "canceled"},
                       {"remaining_count_fp": "n/a"}, {"remaining_count_fp": None, "remaining_count": "0.5"}):
            with self.subTest(fields=fields):
                led, iid = self.open_ioc()
                note = led.apply_row(iid, order_row(**fields))
                self.assertEqual((led.get(iid)["state"], led.exposure()), ("accepted", self.BOUND), note)
        led, iid = self.open_ioc()                                         # the legacy spelling is read too
        led.apply_row(iid, order_row(remaining_count_fp=None, remaining_count=0), client=Fills([fill_row()]))
        self.assertEqual(led.get(iid)["state"], "done")

    def test_a_create_answer_books_an_iocs_fills_only_when_nothing_can_remain(self):
        def answered(**answer):
            led = OrderLedger(tmp(), "demo", DEMO_URL, clock=Clock())
            iid = led.reserve(strategy="lag", ticker=TICKER, side="yes", count=10, limit_price="0.50", fee_multiplier=1).intent_id
            led.accepted(iid, {"order_id": "o9", **answer})
            return led, led.get(iid)

        led, row = answered(fill_count="0.00", remaining_count="0.00")          # provably filled nothing: its budget is free
        self.assertEqual((row["fill_count"], led.exposure()), ("0.00", Decimal("0")))
        led, row = answered(fill_count="4.00", remaining_count="0.00")          # 4 filled, the rest provably gone
        self.assertEqual((row["fill_count"], led.exposure()), ("4.00", Decimal("2.08")))
        led, row = answered(fill_count="10.00")                                 # all 10 filled: nothing can remain
        self.assertEqual((row["fill_count"], led.exposure()), ("10.00", Decimal("5.20")))
        for answer in ({"fill_count": "0.00"}, {"fill_count": "4.00"}, {"fill_count": "4.00", "remaining_count": "6.00"},
                       {"fill_count": "4.00", "remaining_count": "0.00", "status": "resting"}, {"remaining_count": "0.00"},
                       {"fill_count": "11.00", "remaining_count": "0.00"}):
            with self.subTest(answer=answer):
                led, row = answered(**answer)
                self.assertIsNone(row["fill_count"])
                self.assertIn("create answer not final", row["reason"])
                self.assertEqual(led.exposure(), Decimal("5.20"))           # 10 x 0.50 + 10 x 2c: nothing released

    def test_absent_fees_keep_the_fee_bound_until_they_are_reported(self):
        # Reported: executed, 1 filled, taker cost $0.50, no fee fields -> was "done" with $0 fees.
        led, iid = self.open_ioc(final_answer=True)
        note = led.apply_row(iid, order_row(taker_fees_dollars=None, maker_fees_dollars=None), client=Fills([fill_row(fee=None)]))
        row = led.get(iid)
        self.assertEqual((row["state"], row["fill_count"], row["fees"]), ("accepted", "1.00", None))
        self.assertIn("no taker_fees_dollars", note)
        self.assertEqual(led.exposure(), self.BOUND)                       # not $0.50: the fee is unknown, so it is bounded
        # The fees arrive on a later read: booked at what was charged.
        self.assertTrue(led.apply_row(iid, order_row(), client=Fills([fill_row()])).startswith("done"))
        self.assertEqual((Decimal(led.get(iid)["fees"]), led.exposure()), (Decimal("0.0175"), self.PAID))

    def test_explicitly_reported_zero_fees_are_final(self):
        led, iid = self.open_ioc(final_answer=True)
        led.apply_row(iid, order_row(taker_fees_dollars="0.000000"), client=Fills([fill_row(fee="0.000000")]))   # stated, not absent
        self.assertEqual((led.get(iid)["state"], led.exposure()), ("done", Decimal("0.5")))
        led, iid = self.open_ioc(final_answer=True)
        led.apply_row(iid, order_row(taker_fees_dollars=""), client=Fills([fill_row(fee=None)]))                 # blank is absent
        self.assertEqual(led.get(iid)["state"], "accepted")
        # Nothing filled: nothing paid, whether or not the row repeats the zero dollar fields.
        for fields in ({}, {k: None for k in ("taker_fill_cost_dollars", "maker_fill_cost_dollars", "taker_fees_dollars", "maker_fees_dollars")}):
            with self.subTest(fields=fields):
                led, iid = self.open_ioc()
                led.apply_row(iid, order_row(status="canceled", fill_count_fp="0.00", **{**{"taker_fill_cost_dollars": "0.000000",
                                                                                             "taker_fees_dollars": "0.000000"}, **fields}),
                              client=Fills([]))                                                  # ... and its complete fills listing is empty
                self.assertEqual((led.get(iid)["state"], led.exposure()), ("done", Decimal("0")))
        led, iid = self.open_ioc()                                         # ... but a row billing an order with no fills contradicts itself
        led.apply_row(iid, order_row(status="canceled", fill_count_fp="0.00", taker_fees_dollars="0.017500"))
        self.assertEqual((led.get(iid)["state"], led.exposure()), ("accepted", self.BOUND))

    def test_fees_come_from_a_complete_fills_listing_when_the_row_has_none(self):
        led, iid = self.open_ioc(final_answer=True)
        no_fees = order_row(taker_fees_dollars=None, maker_fees_dollars=None)
        led.apply_row(iid, no_fees, client=Fills([]))                      # the fills trail the row: nothing to read yet
        self.assertEqual((led.get(iid)["state"], led.exposure()), ("accepted", self.BOUND))
        led.apply_row(iid, no_fees, client=Fills([fill_row(fee=None)]))     # a fill without its fee_cost establishes nothing
        self.assertEqual(led.get(iid)["state"], "accepted")
        led.apply_row(iid, no_fees, client=Fills([fill_row()]))             # late fills with their fees: booked
        self.assertEqual((led.get(iid)["state"], led.exposure()), ("done", self.PAID))
        done = [e for e in led.events(iid) if e["kind"] == "done"][0]
        self.assertEqual(json.loads(done["detail"])["source"], "order + fills (fees)")

    def test_a_truncated_fills_listing_establishes_nothing(self):
        led, iid = self.open_ioc(final_answer=True)
        no_fees = order_row(taker_fees_dollars=None, maker_fees_dollars=None)
        note = led.apply_row(iid, no_fees, client=Fills([fill_row()], truncated=True))
        self.assertEqual((led.get(iid)["state"], led.exposure()), ("accepted", self.BOUND), note)
        held = [e for e in led.events(iid) if e["kind"] == "held"][-1]
        self.assertTrue(json.loads(held["detail"])["fills_truncated"])
        led.apply_row(iid, no_fees, client=Fills(error=HttpError(500, DEMO_URL, "internal")))   # a failed read neither
        self.assertEqual(led.get(iid)["state"], "accepted")
        # Even a truncated page that already lists more contracts than the row contradicts it.
        led2, iid2 = self.open_ioc(final_answer=True)
        led2.apply_row(iid2, order_row(), client=Fills([fill_row("f1"), fill_row("f2")], truncated=True))
        self.assertEqual((led2.get(iid2)["state"], led2.get(iid2)["fill_count"]), ("accepted", None))
        # The complete listing settles the first one.
        led.apply_row(iid, no_fees, client=Fills([fill_row()]))
        self.assertEqual((led.get(iid)["state"], led.exposure()), ("done", self.PAID))

    def test_duplicate_fills_are_counted_once(self):
        led, iid = self.open_ioc(count=2, final_answer=True)
        row = order_row(fill_count_fp="2.00", initial_count_fp="2.00", taker_fill_cost_dollars="1.000000", taker_fees_dollars=None, maker_fees_dollars=None)
        led.apply_row(iid, row, client=Fills([fill_row("f1"), fill_row("f2"), fill_row("f1")]))   # f1 listed twice (overlapping pages)
        got = led.get(iid)
        self.assertEqual((got["state"], Decimal(got["fees"])), ("done", Decimal("0.035")))           # not 0.0525
        done = json.loads([e for e in led.events(iid) if e["kind"] == "done"][0]["detail"])
        self.assertEqual((done["fills"], done["fills_duplicates"]), (2, 1))
        # One fill id with two different contents is a contradiction, not a duplicate.
        led, iid = self.open_ioc(count=2, final_answer=True)
        led.apply_row(iid, row, client=Fills([fill_row("f1"), fill_row("f2"), fill_row("f1", fee="0.035000")]))
        self.assertEqual((led.get(iid)["state"], led.get(iid)["fill_count"], led.exposure()), ("accepted", None, Decimal("1.04")))

    def test_order_and_fill_totals_that_contradict_release_nothing(self):
        led, iid = self.open_ioc(final_answer=True)
        note = led.apply_row(iid, order_row(), client=Fills([fill_row("f1"), fill_row("f2")]))     # 2 fills, the row says 1
        row = led.get(iid)
        self.assertIn("disagree", note)
        self.assertEqual((row["state"], row["fill_count"]), ("accepted", None))                    # the fill is unverified again
        self.assertEqual(led.exposure(), self.BOUND)                                               # the whole worst case counts
        self.assertEqual(led.get(iid)["fill_state"], "contradicted")
        lock = led.reserve(strategy="lag-lock", ticker=TICKER.replace("-KC", "-DEN"), side="yes", count=1, limit_price="0.40",
                           parent_id=iid, fee_multiplier=1)
        self.assertFalse(lock.ok)
        self.assertIn("the entry's fill evidence is contradicted", lock.reason)                    # no lock leg sized on it
        # Fills that merely trail the row (fewer listed so far) do not contradict it - nor do they
        # confirm it: the intent waits, unverified, and the whole worst case still counts.
        note = led.apply_row(iid, order_row(), client=Fills([]))
        self.assertIn("trails the row", note)
        self.assertEqual((led.get(iid)["state"], led.get(iid)["fill_state"], led.exposure()), ("accepted", "provisional", self.BOUND))
        led.apply_row(iid, order_row(), client=Fills([fill_row()]))                                # the listing catches up
        self.assertEqual((led.get(iid)["state"], led.get(iid)["fill_state"], led.exposure()), ("done", "verified", self.PAID))

    def test_incomplete_answers_never_free_budget(self):
        """A per-game budget sized so that a second order fits after a complete answer
        (0.5175 + 0.50 <= 1.018) but not while any evidence is missing (0.52 + 0.50 > 1.018)
        - and would have, had a missing fee been booked as zero (0.50 + 0.50)."""
        budget = Budget(per_game=Decimal("1.018"))
        incomplete = {
            "no status": {"status": None}, "unknown status": {"status": "pending"}, "resting IOC": {"status": "resting"},
            "no remaining quantity": {"remaining_count_fp": None}, "remaining open": {"remaining_count_fp": "1.00"},
            "no fill count": {"fill_count_fp": None}, "fill count not a number": {"fill_count_fp": "?"},
            "more filled than ordered": {"fill_count_fp": "2.00"}, "executed short": {"fill_count_fp": "0.50"},
            "other initial count": {"initial_count_fp": "3.00"}, "no taker cost": {"taker_fill_cost_dollars": None},
            "zero cost for a fill": {"taker_fill_cost_dollars": "0.000000"}, "no taker fees": {"taker_fees_dollars": None},
            "fees not a number": {"taker_fees_dollars": "NaN"}, "negative fees": {"taker_fees_dollars": "-0.01"},
        }
        for name, fields in incomplete.items():
            for final_answer in (False, True):
                with self.subTest(name=name, final_answer=final_answer):
                    led, iid = self.open_ioc(final_answer=final_answer, budget=budget)
                    led.apply_row(iid, order_row(**fields), client=Fills([]))
                    self.assertNotEqual(led.get(iid)["state"], "done")
                    self.assertGreaterEqual(led.exposure(), self.BOUND)
                    second = led.reserve(strategy="lag", ticker=TICKER, side="yes", count=1, limit_price="0.48", event_key=KEY, game_key=KEY,
                                         fee_multiplier=1, budget=budget)
                    self.assertFalse(second.ok, "incomplete evidence freed the budget")
        led, iid = self.open_ioc(final_answer=True, budget=budget)
        led.apply_row(iid, order_row(), client=Fills([fill_row()]))
        self.assertTrue(led.reserve(strategy="lag", ticker=TICKER, side="yes", count=1, limit_price="0.48", event_key=KEY, game_key=KEY,
                                    fee_multiplier=1, budget=budget).ok)

    def test_late_fills_and_fees_are_reconciled_even_after_polling_gave_up(self):
        clock = Clock(1000.0)
        client = FakeKalshi(clock)
        client.omit = {"taker_fees_dollars", "maker_fees_dollars"}          # the row reports no fees yet
        client.fills_hidden = True                                          # and the fills listing has nothing yet
        ex = executor(client, clock)
        ex.ledger.max_checks = 3
        rec = ex.on_signal(sig(), quotes())
        for _ in range(5):
            clock.t += 3
            ex.reconcile(force=True)
        row = ex.ledger.get(rec["intent_id"])
        self.assertEqual((row["state"], row["checks"]), ("accepted", 3))    # polled 3 times, then left at its bound
        self.assertEqual(ex.ledger.exposure(), Decimal("31.00"))
        client.fills_hidden = False                                         # the fills (with their fees) arrive late
        clock.t += 3
        self.assertEqual(ex.ledger.reconcile(client), [])                   # automatic polling has given up on it ...
        res = ex.ledger.reconcile(client, recheck_exhausted=True)           # ... `kalshi reconcile` reads it again
        self.assertEqual([(r["before"], r["after"]) for r in res], [("accepted", "done")])
        self.assertEqual(ex.ledger.exposure(), Decimal("30.84"))

    def test_the_manual_reconcile_reads_exhausted_orders_again(self):
        from arb_engine.cli_plugins import kalshi_ops

        calls = []

        class Led:
            def reconcile(self, client, **kw):
                calls.append(kw)
                return []

            def status(self):
                return {"blocked": None}

            def rows(self, states):
                return []

        args = argparse.Namespace(action="reconcile", no_account_env=True)
        with mock.patch("arb_engine.execution.kalshi.KalshiExecutor"), \
                mock.patch("arb_engine.execution.ledger.OrderLedger.for_client", return_value=Led()), redirect_stdout(io.StringIO()):
            self.assertEqual(kalshi_ops.run_kalshi(args), 0)
        self.assertEqual(calls, [{"recheck_exhausted": True}])

    def test_real_demo_rows_are_final(self):
        """The recorded demo answer, order row and fill of 2026-09-26 still reconcile to done."""
        def load(name):
            with open(os.path.join(os.path.dirname(__file__), "fixtures", "kalshi_orders", name + ".json"), encoding="utf-8") as f:
                return json.load(f)

        od, fills = load("order_filled")["order"], load("fills_v2")["fills"]
        led = OrderLedger(tmp(), "demo", DEMO_URL, clock=Clock())
        res = led.reserve(strategy="lag", ticker=od["ticker"], side="yes", count=1, limit_price="0.56", fee_multiplier=1)
        led.accepted(res.intent_id, load("create_order_fill"))
        self.assertEqual(led.get(res.intent_id)["fill_count"], "1.00")      # the real create answer is final (remaining "0.00")
        # The recorded row is of the demo run's own order: as the row of *this* order it carries
        # this intent's client_order_id (a row naming another is not this order's evidence).
        self.assertIn("another client_order_id", led.apply_row(res.intent_id, od, client=Fills(fills)))
        self.assertEqual(led.get(res.intent_id)["fill_state"], "contradicted")
        od = dict(od, client_order_id=res.client_order_id)
        led.apply_row(res.intent_id, od, client=Fills(fills))
        row = led.get(res.intent_id)
        self.assertEqual((row["state"], Decimal(row["fill_cost"]), Decimal(row["fees"])), ("done", Decimal("0.56"), Decimal("0.0173")))


class RestartTests(unittest.TestCase):
    def test_budgets_survive_a_restart_and_span_processes(self):
        clock = Clock(1000.0)
        client = FakeKalshi(clock)
        path = tmp()
        a = executor(client, clock, path=path, max_notional_per_game=45.0, daily_notional=100.0)
        self.assertEqual(a.on_signal(sig(ts=1.0), quotes())["count"], 50)                 # $31.00 committed
        # A restarted process (or the college slate, on the same ledger) sees it.
        b = executor(client, clock, path=path, max_notional_per_game=45.0, daily_notional=100.0)
        self.assertAlmostEqual(b.game_notional(KEY), 31.0)
        self.assertEqual(b.on_signal(sig(ts=2.0), quotes())["count"], 22)                 # $14 left of the game's $45
        self.assertIn("duplicate", b.on_signal(sig(ts=1.0), quotes())["reason"])          # A already sent that signal

    def test_an_order_left_in_flight_by_a_dead_process_is_recovered_at_start(self):
        clock = Clock(1000.0)
        client = FakeKalshi(clock)
        path = tmp()
        # The previous process wrote its intent, sent the order, and died before recording
        # the answer: the ledger says pending, the exchange has the order.
        old = OrderLedger(path, "demo", DEMO_URL, clock=clock, owner="elsewhere:1")
        res = old.reserve(strategy="lag", ticker=TICKER, side="yes", count=50, limit_price="0.60", event_key=KEY, game_key=KEY,
                          budget=Budget(daily=Decimal("500")), fee_multiplier=1)
        ex = KalshiExecutor(client)
        ex.execute(ex.plan(TICKER, "buy", "yes", 50, 0.60, time_in_force="immediate_or_cancel", client_order_id=res.client_order_id), confirm=True)
        old.close()
        clock.t = 1400.0                               # past pending_stale_s: nobody is still sending it
        fresh = OrderLedger(path, "demo", DEMO_URL, clock=clock)
        self.assertIn("unknown outcome", fresh.blocked())
        fresh.close()
        new = executor(client, clock, path=path)       # start-up reconciles it
        row = new.ledger.get(res.intent_id)
        self.assertEqual((row["state"], row["order_id"], Decimal(row["fill_cost"])), ("done", "o1", Decimal("30.00")))
        self.assertIsNone(new.ledger.blocked())

    def test_a_pending_intent_of_a_live_process_is_not_treated_as_lost(self):
        clock = Clock(1000.0)
        path = tmp()
        mine = OrderLedger(path, "demo", DEMO_URL, clock=clock)
        mine.reserve(strategy="lag", ticker=TICKER, side="yes", count=5, limit_price="0.60", fee_multiplier=1)
        clock.t = 1010.0
        other = OrderLedger(path, "demo", DEMO_URL, clock=clock, owner="elsewhere:2")
        self.assertIsNone(other.blocked())                # in flight: its worst case is reserved, nothing is unknown
        self.assertEqual(other.exposure(strategy="lag"), Decimal("3.10"))


class FailureTests(unittest.TestCase):
    def test_an_unusable_ledger_sends_nothing(self):
        clock = Clock()
        client = FakeKalshi(clock)
        ex = executor(client, clock, path="/dev/null/ledger.sqlite3")
        self.assertIn("order ledger unusable", ex.blocked_reason)
        rec = ex.on_signal(sig(), quotes())
        self.assertEqual(rec["status"], "blocked")
        self.assertEqual(client.creates, [])

    def test_a_journal_that_cannot_be_written_is_counted_and_alerted_and_orders_stay_recorded(self):
        clock = Clock()
        client = FakeKalshi(clock)
        pushed = []

        class Alerts:
            def alert(self, kind, msg, **kw):
                pushed.append((kind, msg))

            def info(self, *a, **k):
                pass
        ex = LagExecutor(mode="demo", executor=KalshiExecutor(client), intents_path="/dev/null/lag.jsonl", ledger_path=tmp(), clock=clock, alerter=Alerts())
        before = ex.journal_errors                         # the start-up recovery note already failed once
        rec = ex.on_signal(sig(), quotes())
        self.assertEqual(rec["status"], "SUBMITTED")
        self.assertEqual(ex.journal_errors - before, 1)
        self.assertEqual(ex.ledger.get(rec["intent_id"])["state"], "accepted")
        self.assertEqual([k for k, _ in pushed], ["EXEC ERROR"])
        self.assertIn("not writable", pushed[0][1])
        ex.on_signal(sig(ts=1001.0), quotes())
        self.assertEqual(len(pushed), 1)                  # alerted once per 10 minutes, not per order

    def test_a_ledger_write_that_fails_after_sending_blocks_the_process(self):
        clock = Clock()
        client = FakeKalshi(clock)
        ex = executor(client, clock)
        with mock.patch.object(ex.ledger, "accepted", side_effect=LedgerError("disk full")):
            rec = ex.on_signal(sig(), quotes())
        self.assertEqual(rec["status"], "UNKNOWN")
        self.assertIn("disk full", ex.blocked_reason)
        self.assertEqual(ex.on_signal(sig(ts=1001.0), quotes())["status"], "blocked")
        self.assertEqual(len(client.creates), 1)


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_writers_never_exceed_the_budget(self):
        path = tmp()
        OrderLedger(path, "demo", DEMO_URL).close()
        start = threading.Barrier(8)
        results = []

        def worker(i):
            led = OrderLedger(path, "demo", DEMO_URL, owner=f"w:{i}")   # its own connection, like another process
            start.wait()
            for j in range(5):
                r = led.reserve(strategy="lag", ticker=TICKER, side="yes", count=10, limit_price="0.50", game_key=KEY,
                                dedupe_key=f"w{i}-{j}", budget=Budget(daily=Decimal("20")), fee_multiplier=1)
                results.append(r)
            led.close()
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        led = OrderLedger(path, "demo", DEMO_URL)
        total = led.exposure(strategy="lag")
        self.assertLessEqual(total, Decimal("20"))
        self.assertEqual(total, sum((r.max_cost for r in results if r.ok), Decimal("0")))
        self.assertEqual(sum(r.count for r in results if r.ok), 38)          # $20 / $0.52 worst case per contract
        led.close()


class ManualOrderTests(unittest.TestCase):
    def args(self, **kw):
        base = dict(action="order", no_account_env=True, ticker=TICKER, price=0.40, count=10, max_notional=25.0, side_action="buy",
                    side="yes", post_only=False, exchange_index=None, time_in_force="immediate_or_cancel", confirm=True, status="resting")
        base.update(kw)
        return argparse.Namespace(**base)

    def run_cli(self, args, client):
        from arb_engine.cli_plugins.kalshi_ops import run_kalshi

        out = io.StringIO()
        with mock.patch("arb_engine.execution.kalshi.KalshiClient", return_value=client), \
                mock.patch.dict(os.environ, {"ARB_ORDER_LEDGER_DIR": os.path.dirname(tmp())}), redirect_stdout(out):
            code = run_kalshi(args)
        return code, json.loads(out.getvalue()) if out.getvalue().strip() else None

    def test_non_finite_numbers_are_refused_before_any_client(self):
        from arb_engine.cli_plugins.kalshi_ops import run_kalshi

        for kw in ({"count": float("nan")}, {"count": float("inf")}, {"price": float("nan")}, {"max_notional": float("inf")}, {"max_notional": float("nan")}):
            with self.subTest(kw=kw), mock.patch("arb_engine.execution.kalshi.KalshiExecutor") as constructor:
                with self.assertRaisesRegex(SystemExit, "finite"):
                    run_kalshi(self.args(**kw))
                constructor.assert_not_called()

    def test_the_cap_includes_fees(self):
        from arb_engine.cli_plugins.kalshi_ops import _order_max_loss, run_kalshi

        self.assertEqual(_order_max_loss("buy", 40, 0.61, 1), Decimal("25.20"))       # 24.40 + 40 x 2c
        self.assertEqual(_order_max_loss("sell", 10, 0.90, 1), Decimal("1.10"))       # short 10 x 0.10 + 10 x 1c
        with self.assertRaisesRegex(SystemExit, r"\$25.20 \(fees included\)"):
            run_kalshi(self.args(count=40, price=0.61))

    def test_a_confirmed_demo_order_is_recorded_before_it_is_sent(self):
        client = FakeKalshi(Clock())
        code, out = self.run_cli(self.args(), client)
        self.assertEqual((code, out["status"], out["ledger_state"]), (0, "SUBMITTED", "accepted"))
        self.assertEqual(client.creates[0]["client_order_id"], out["plan"]["client_order_id"])

    def test_a_blocked_or_unknown_order_exits_nonzero(self):
        client = FakeKalshi(Clock())
        client.env, client.base_url = "prod", PROD_URL
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARB_LIVE_TRADING", None)
            code, out = self.run_cli(self.args(), client)
        self.assertEqual(code, 3)
        self.assertIn("ARB_LIVE_TRADING", out["status"])
        self.assertEqual(client.creates, [])
        demo = FakeKalshi(Clock())
        demo.lose_answer = HttpError(0, DEMO_URL, "timeout")
        code, out = self.run_cli(self.args(), demo)
        self.assertEqual((code, out["status"]), (4, "UNKNOWN"))
        self.assertIn("kalshi reconcile", out["next"])

    def test_listings_say_when_they_are_truncated(self):
        client = FakeKalshi(Clock())
        client.paged = lambda path, key, params: ([{"order_id": "o1"}], True)
        code, out = self.run_cli(self.args(action="orders"), client)
        self.assertEqual((code, out["count"], out["truncated"]), (0, 1, True))
        self.assertIn("not the whole list", out["note"])


if __name__ == "__main__":
    unittest.main()

"""execution/ledger.py, strategy/broker.py, strategy/lagexec.py: the open defects of the
audit of 6b81375, each as the reproduction that found it.

* **H6** reconciliation asked for orders from ``local created_ts - 60`` and Kalshi filters on
  its own clock, so a local clock a couple of minutes ahead hid the very order being looked
  for and it was released as never accepted.
* **H10** ``KalshiBroker.poll`` read a missing fill count as zero and stopped tracking the
  order, so a resting maker order that filled reported no fills and no hedge was ever raised.
* **H11** an order row's fill cost was booked with no bound: ``$0.0001`` for 10 contracts at
  a $0.50 limit replaced the reservation.
* **H13** an accepted manual IOC *sell* counted ``count x price``; a sell is short
  ``count x (1 - price)``.
* **H14** the daily budget was scoped to the local calendar day, so at midnight an order from
  yesterday that was still open stopped counting and the open worst case could double.
* **H16** fills were keyed by ``fill_id`` before ``trade_id``, so one trade listed under two
  fill ids counted twice - which contradicts the order row, and a contradicted entry can
  never be hedged.
* **s15** ``buy_lock`` capped a lock leg at the account's whole position in the market, which
  two entries on one ticker share.
* **s17** the remaining-inventory query counted a *lock leg of another entry* as this entry's
  exit, so the second entry stayed unhedged.
* **fail-open sweep**: ``reserve`` raised ``OverflowError`` (not ``LedgerError``) on an
  infinite count; ``rejected`` could release an intent that had shown fills when a caller
  passed ``from_states``; ``list_orders`` ignored a non-paging client's truncation flag.

Everything is offline: fake exchanges, temporary ledgers, injected clocks.  No order is sent,
no credential is read, nothing reaches the network.
"""
from __future__ import annotations

import unittest
from decimal import Decimal

from arb_engine.execution.kalshi import KalshiExecutor
from arb_engine.execution.ledger import (ACCEPTED, CONTRADICTED, DONE, REJECTED, Budget, Identity, LedgerError,
                                         OrderLedger, fills_evidence, list_orders, settlement_identity)
from arb_engine.strategy.lagexec import LagExecutor
from arb_engine.venues.http import HttpError
from tests.test_lock_identity import kq, settle
from tests.test_order_ledger import DEMO_URL, KEY, TICKER, Clock, FakeKalshi, Fills, executor, fill_row, maker, order_row, quotes, sig, tmp

DEN = TICKER.replace("-KC", "-DEN")
KC_SD = settlement_identity("kalshi", KEY, "KC", "yes", "0.5", True)
DEN_SD = settlement_identity("kalshi", KEY, "DEN", "yes", "0.5", True)
ACCOUNT = Identity(key_fp="key:hardening", account_fp="account:hardening")


def ledger(clock=None, path=None, identity=ACCOUNT):
    led = OrderLedger(path or tmp(), "demo", DEMO_URL, clock=clock or Clock())
    if identity is not None:
        led.bind(identity)
    return led


def entry(led, count=10, price="0.50", ticker=TICKER, side="yes", settlement=KC_SD, action="buy", **kw):
    res = led.reserve(strategy=kw.pop("strategy", "lag"), ticker=ticker, side=side, action=action, count=count, limit_price=price,
                      event_key=KEY, game_key=KEY, fee_multiplier=1, settlement=settlement, **kw)
    assert res.ok, res.reason
    return res


def verified(led, count=10, oid="e1", fill=None, ticker=TICKER, side="yes", price="0.50"):
    """An entry whose fill a final row and a complete fills listing confirm."""
    fill = f"{count}.00" if fill is None else fill
    res = entry(led, count=count, ticker=ticker, side=side, price=price)
    led.accepted(res.intent_id, {"order_id": oid, "fill_count": fill})
    settle(led, res.intent_id, oid, fill, count, price, ticker, side=side)
    assert led.get(res.intent_id)["fill_state"] == "verified", led.get(res.intent_id)
    return res


class SkewedClockKalshi(FakeKalshi):
    """A Kalshi that honours ``min_ts`` on *its own* clock, ``skew`` seconds behind ours."""

    skew = 120.0

    def paged(self, path, key, params):
        rows, truncated = super().paged(path, key, params)
        if path != "/portfolio/orders" or params.get("min_ts") is None:
            return rows, truncated
        self.filtered = getattr(self, "filtered", 0) + 1
        return [r for r in rows if self.rows[r["order_id"]]["created"] - self.skew >= float(params["min_ts"])], truncated


class ClockSkewTests(unittest.TestCase):
    """H6: an order the exchange has is never released because our clock runs ahead of its."""

    def test_a_local_clock_two_minutes_ahead_does_not_release_a_filled_order(self):
        clock = Clock(1000.0)
        client = SkewedClockKalshi(clock)
        ex = executor(client, clock)
        client.lose_answer = HttpError(0, DEMO_URL, "curl: (28) Operation timed out")
        rec = ex.on_signal(sig(ts=clock.t, contracts=10), quotes(), now=clock.t)
        self.assertEqual(rec["status"], "UNKNOWN")
        client.lose_answer = None
        oid = next(iter(client.rows))
        self.assertEqual(client.rows[oid]["fill_count_fp"], "10.00")        # the exchange has it, filled
        for t in (1005.0, 1040.0, 1080.0):
            clock.t = t
            ex.reconcile(now=t, force=True)
        row = ex.ledger.rows()[0]
        self.assertNotEqual(row["state"], REJECTED)
        self.assertEqual((row["state"], row["order_id"]), (DONE, oid))
        self.assertEqual(row["fill_count"], "10.00")
        self.assertGreater(client.filtered, 0)                             # the filter really did run

    def test_a_listing_still_finds_the_order_without_the_extra_read_when_the_clocks_agree(self):
        clock = Clock(1000.0)
        client = SkewedClockKalshi(clock)
        client.skew = 0.0
        ex = executor(client, clock)
        client.lose_answer = HttpError(0, DEMO_URL, "timeout")
        ex.on_signal(sig(ts=clock.t, contracts=10), quotes(), now=clock.t)
        client.lose_answer = None
        clock.t = 1005.0
        ex.reconcile(now=clock.t, force=True)
        self.assertEqual(ex.ledger.rows()[0]["state"], DONE)

    def test_an_order_that_was_never_accepted_is_still_released(self):
        """The release path must keep working: nothing on the exchange, nothing to hold."""
        clock = Clock(1000.0)
        client = FakeKalshi(clock)
        client.fail_before = HttpError(0, DEMO_URL, "timeout")        # nothing ever recorded
        ex = executor(client, clock)
        ex.on_signal(sig(ts=clock.t, contracts=10), quotes(), now=clock.t)
        for t in (1005.0, 1040.0, 1080.0):
            clock.t = t
            ex.reconcile(now=t, force=True)
        row = ex.ledger.rows()[0]
        self.assertEqual(row["state"], REJECTED)
        self.assertIn("never accepted", row["reason"])
        self.assertEqual(ex.ledger.exposure(), Decimal(0))

    def test_a_truncated_unfiltered_listing_releases_nothing(self):
        clock = Clock(1000.0)
        client = FakeKalshi(clock)
        client.fail_before = HttpError(0, DEMO_URL, "timeout")
        ex = executor(client, clock)
        ex.on_signal(sig(ts=clock.t, contracts=10), quotes(), now=clock.t)
        client.truncated = True
        for t in (1005.0, 1040.0, 1080.0):
            clock.t = t
            ex.reconcile(now=t, force=True)
        self.assertEqual(ex.ledger.rows()[0]["state"], "ambiguous")


class MakerPollTests(unittest.TestCase):
    """H10: a resting maker order whose row omits the fill count."""

    W = KEY + "|kalshi:yes"

    def test_a_row_without_a_fill_count_does_not_report_zero_fills(self):
        clock = Clock(1000.0)
        client = FakeKalshi(clock)
        broker = maker(client, clock)
        order = broker.place(TICKER, "yes", 0.40, 10, watch_key=self.W)
        client.fill_resting(order.order_id, 10)
        client.omit = {"fill_count_fp", "fill_count"}          # the answer leaves the count out
        clock.t = 1010.0
        fills = broker.poll([order], {})
        self.assertEqual([(o.order_id, n, p) for o, n, p in fills], [(order.order_id, 10.0, 0.40)])
        self.assertEqual(order.filled, 10.0)

    def test_a_row_with_neither_count_nor_remaining_keeps_the_order_tracked(self):
        clock = Clock(1000.0)
        client = FakeKalshi(clock)
        broker = maker(client, clock)
        order = broker.place(TICKER, "yes", 0.40, 10, watch_key=self.W)
        client.fill_resting(order.order_id, 4)
        client.omit = {"fill_count_fp", "fill_count", "remaining_count_fp", "remaining_count"}
        clock.t = 1010.0
        self.assertEqual(broker.poll([order], {}), [])
        self.assertEqual(order.status, "resting")              # still ours to watch, not written off
        client.omit = set()
        clock.t = 1020.0
        fills = broker.poll([order], {})
        self.assertEqual([(n, p) for _, n, p in fills], [(4.0, 0.40)])

    def test_a_genuine_zero_fill_cancellation_still_ends_the_tracking(self):
        clock = Clock(1000.0)
        client = FakeKalshi(clock)
        broker = maker(client, clock)
        order = broker.place(TICKER, "yes", 0.40, 10, watch_key=self.W)
        client.cancel_order(order.order_id, market_ticker=TICKER)
        clock.t = 1010.0
        self.assertEqual(broker.poll([order], {}), [])
        self.assertEqual(order.status, "canceled")


class FillCostBoundTests(unittest.TestCase):
    """H11: what an order row may claim it paid."""

    def setUp(self):
        self.clock = Clock(1000.0)
        self.led = ledger(self.clock)
        self.res = entry(self.led, count=10, price="0.50")
        self.led.accepted(self.res.intent_id, {"order_id": "o1", "fill_count": "10.00", "remaining_count": "0.00"})
        # 10 x $0.50 + the fee bound (10 x ceil(0.07 x 0.25) = 10 x 2c) = $5.20.
        self.assertEqual(self.led.exposure(), Decimal("5.20"))

    def row(self, **fields):
        base = dict(status="executed", fill_count_fp="10.00", remaining_count_fp="0.00", initial_count_fp="10.00",
                    taker_fill_cost_dollars="5.000000", taker_fees_dollars="0.175000")
        base.update(fields)
        return order_row("o1", **base)

    def fills(self, count="10.00", fee="0.175000", price="0.5000"):
        return Fills([fill_row("f1", "o1", count=count, fee=fee, yes_price_dollars=price)])

    def test_a_cost_below_a_cent_a_contract_releases_nothing(self):
        """The listing states no price either, so nothing establishes what was paid."""
        blind = Fills([fill_row("f1", "o1", count="10.00", fee="0.175000", yes_price_dollars=None)])
        note = self.led.apply_row(self.res.intent_id, self.row(taker_fill_cost_dollars="0.0001"), client=blind)
        row = self.led.get(self.res.intent_id)
        self.assertEqual(row["state"], ACCEPTED)
        self.assertIn("under a cent a contract", note)
        # The fills are verified, so the IOC counts at 10 x the limit plus the fee bound.
        self.assertEqual(self.led.exposure(), Decimal("5.20"))
        self.assertEqual(row["fill_count"], "10.00")

    def test_a_cost_the_listing_can_price_is_booked_from_the_listing(self):
        note = self.led.apply_row(self.res.intent_id, self.row(taker_fill_cost_dollars="0.0001"), client=self.fills())
        row = self.led.get(self.res.intent_id)
        self.assertEqual((row["state"], Decimal(row["fill_cost"])), (DONE, Decimal("5.0000")))
        self.assertIn("done", note)

    def test_a_cost_above_the_limit_contradicts_the_row(self):
        note = self.led.apply_row(self.res.intent_id, self.row(taker_fill_cost_dollars="9.000000"), client=self.fills())
        row = self.led.get(self.res.intent_id)
        self.assertEqual(row["fill_state"], CONTRADICTED)
        self.assertIn("more than", note)
        self.assertEqual(self.led.exposure(), Decimal("5.20"))       # the whole worst case

    def test_the_fills_listing_own_prices_keep_the_larger_cost(self):
        # The row under-reports; the listing prices the same 10 contracts at $0.50 each.
        self.led.apply_row(self.res.intent_id, self.row(taker_fill_cost_dollars="0.500000"), client=self.fills())
        row = self.led.get(self.res.intent_id)
        self.assertEqual(row["state"], DONE)
        self.assertEqual(Decimal(row["fill_cost"]), Decimal("5.0000"))
        self.assertEqual(self.led.exposure(), Decimal("5.0000") + Decimal("0.175000"))

    def test_an_honest_row_is_booked_exactly(self):
        self.led.apply_row(self.res.intent_id, self.row(), client=self.fills())
        row = self.led.get(self.res.intent_id)
        self.assertEqual((row["state"], row["fill_cost"], row["fees"]), (DONE, "5.000000", "0.175000"))
        self.assertEqual(self.led.exposure(), Decimal("5.175000"))

    def test_a_listing_without_prices_still_books_the_row(self):
        self.led.apply_row(self.res.intent_id, self.row(), client=Fills([fill_row("f1", "o1", count="10.00", fee="0.175000",
                                                                                  yes_price_dollars=None)]))
        self.assertEqual(self.led.get(self.res.intent_id)["state"], DONE)


class SellExposureTests(unittest.TestCase):
    """H13: a sell is short ``1 - price`` a contract, not ``price``."""

    def setUp(self):
        self.led = ledger(Clock(1000.0))

    def sell(self, count=10, price="0.10", per=None):
        # `kalshi order --side-action sell --side yes --price 0.10 --count 10`: short 10 x $0.90
        # plus the fee bound at the mirrored price 0.90, whose dearest fee price is 0.50
        # (10 x ceil(0.07 x 0.50 x 0.50) = 10 x 2c = $0.20) -> $9.20, i.e. $0.92 a contract.
        per = per if per is not None else (Decimal("0.90") * count + Decimal("0.20")) / count
        return entry(self.led, count=count, price=price, action="sell", strategy="manual", max_cost_per_contract=per)

    def test_a_partly_filled_sell_counts_its_short_side(self):
        res = self.sell()
        self.led.accepted(res.intent_id, {"order_id": "o1", "fill_count": "1.00", "remaining_count": "0.00"})
        # 1 contract short of $0.90 plus the 2c fee bound.
        self.assertEqual(self.led.exposure(), Decimal("0.92"))

    def test_a_fully_filled_sell_never_exceeds_its_reservation(self):
        res = self.sell()
        self.led.accepted(res.intent_id, {"order_id": "o1", "fill_count": "10.00", "remaining_count": "0.00"})
        self.assertEqual(self.led.exposure(), Decimal(res.max_cost))

    def test_a_finished_sell_is_bounded_by_what_it_is_short(self):
        res = self.sell(count=10, price="0.70", per=Decimal("0.32"))
        self.led.accepted(res.intent_id, {"order_id": "s1", "fill_count": "7.00"})
        settle(self.led, res.intent_id, "s1", "7.00", 10, "0.70", TICKER, action="sell", side="yes")
        self.assertEqual(self.led.get(res.intent_id)["state"], DONE)
        # 7 short of $0.30 plus the $0.14 of fee the row states = $2.24, never the $4.90 of proceeds.
        self.assertEqual(self.led.exposure(), Decimal("7") * Decimal("0.30") + Decimal("0.14"))

    def test_a_buy_is_unchanged(self):
        res = entry(self.led, count=10, price="0.50")
        self.led.accepted(res.intent_id, {"order_id": "o2", "fill_count": "4.00", "remaining_count": "0.00"})
        self.assertEqual(self.led.exposure(), Decimal("4") * Decimal("0.50") + Decimal("0.08"))

    def test_a_sell_reserved_without_a_per_contract_cap_is_still_sized_to_its_short_side(self):
        res = self.led.reserve(strategy="manual", ticker=TICKER, side="yes", action="sell", count=10, limit_price="0.10",
                               fee_multiplier=1)
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.max_cost, Decimal("0.90") * 10 + Decimal("0.20"))


class DailyBudgetTests(unittest.TestCase):
    """H14: an order still open from an earlier day keeps counting."""

    def setUp(self):
        self.clock = Clock(1000.0)
        self.led = ledger(self.clock)
        self.budget = Budget(daily=Decimal("11.00"))

    def tomorrow(self):
        t = self.clock.t + 86400.0
        while self.led.day_of(t) == self.led.day_of(self.clock.t):
            t += 3600.0
        return t

    def open_order(self):
        res = self.led.reserve(strategy="lag", ticker=TICKER, side="yes", count=10, limit_price="0.99", event_key=KEY,
                               game_key=KEY, fee_multiplier=1, budget=self.budget, settlement=KC_SD)
        self.assertTrue(res.ok, res.reason)
        # accepted, fills not final: the whole worst case stays reserved and it does not block.
        self.led.accepted(res.intent_id, {"order_id": "o1", "fill_count": "3.00"})
        self.assertIsNone(self.led.blocked())
        return res

    def test_an_order_open_from_yesterday_still_counts_today(self):
        self.open_order()
        again = self.led.reserve(strategy="lag", ticker=TICKER, side="yes", count=10, limit_price="0.99", event_key=KEY,
                                 game_key=KEY, fee_multiplier=1, budget=self.budget, settlement=KC_SD, now=self.tomorrow())
        self.assertFalse(again.ok)
        self.assertIn("budget", again.reason)
        self.assertEqual(self.led.exposure(), Decimal("10.10"))    # 10 x 0.99 + 10 x 2c of fee bound

    def test_a_finished_order_from_yesterday_frees_today(self):
        res = self.open_order()
        settle(self.led, res.intent_id, "o1", "3.00", 10, "0.99", TICKER)
        self.assertEqual(self.led.get(res.intent_id)["state"], DONE)
        again = self.led.reserve(strategy="lag", ticker=TICKER, side="yes", count=10, limit_price="0.99", event_key=KEY,
                                 game_key=KEY, fee_multiplier=1, budget=self.budget, settlement=KC_SD, now=self.tomorrow())
        self.assertTrue(again.ok, again.reason)
        self.assertEqual(again.count, 10)

    def test_today_is_still_capped_on_its_own(self):
        self.open_order()
        again = self.led.reserve(strategy="lag", ticker=TICKER, side="yes", count=10, limit_price="0.99", event_key=KEY,
                                 game_key=KEY, fee_multiplier=1, budget=self.budget, settlement=KC_SD)
        self.assertFalse(again.ok)


class TradeIdDedupeTests(unittest.TestCase):
    """H16: one trade listed under two fill ids is one fill."""

    def test_one_trade_under_two_fill_ids_counts_once(self):
        rows = [fill_row("f1", "o1", count="1.00"), fill_row("f2", "o1", count="1.00", trade_id="tf1")]
        ev = fills_evidence(rows, False, None, order_id="o1")
        self.assertEqual(ev.count, Decimal("1.00"))
        self.assertTrue(ev.conclusive)
        self.assertEqual(ev.fees, Decimal("0.017500"))

    def test_the_same_trade_with_different_contents_is_a_conflict(self):
        rows = [fill_row("f1", "o1", count="1.00"), fill_row("f2", "o1", count="9.00", trade_id="tf1")]
        ev = fills_evidence(rows, False, None, order_id="o1")
        self.assertFalse(ev.conclusive)
        self.assertEqual(ev.conflicts, 1)

    def test_two_real_fills_of_one_order_still_count_twice(self):
        rows = [fill_row("f1", "o1", count="1.00"), fill_row("f2", "o1", count="2.00")]
        ev = fills_evidence(rows, False, None, order_id="o1")
        self.assertEqual(ev.count, Decimal("3.00"))
        self.assertTrue(ev.conclusive)

    def test_a_row_with_only_a_trade_id_is_still_de_duplicated(self):
        rows = [fill_row("f1", "o1", count="1.00", fill_id=None), fill_row("f2", "o1", count="1.00", fill_id=None, trade_id="tf1")]
        ev = fills_evidence(rows, False, None, order_id="o1")
        self.assertEqual(ev.count, Decimal("1.00"))

    def test_a_duplicated_trade_does_not_contradict_the_order(self):
        clock = Clock(1000.0)
        led = ledger(clock)
        res = entry(led, count=10, price="0.50")
        led.accepted(res.intent_id, {"order_id": "o1", "fill_count": "1.00", "remaining_count": "0.00"})
        rows = [fill_row("f1", "o1", count="1.00"), fill_row("f2", "o1", count="1.00", trade_id="tf1")]
        note = led.apply_row(res.intent_id, order_row("o1", status="canceled", fill_count_fp="1.00", initial_count_fp="10.00"),
                             client=Fills(rows))
        row = led.get(res.intent_id)
        self.assertEqual((row["state"], row["fill_state"]), (DONE, "verified"))
        self.assertIn("done", note)
        self.assertEqual(led.exposure(), Decimal("0.500000") + Decimal("0.017500"))


class LockPositionCapTests(unittest.TestCase):
    """s15: two entries on one ticker share one Kalshi market position."""

    def setUp(self):
        from tests.test_lock_identity import FakeKalshi as LockKalshi

        self.clock = Clock(1000.0)
        self.client = LockKalshi(holding="20.00")
        self.ex = LagExecutor(mode="demo", executor=KalshiExecutor(self.client), intents_path=tmp("lag.jsonl"),
                              ledger_path=tmp(), clock=self.clock)
        self.a = verified(self.ex.ledger, count=10, oid="e1")
        self.b = verified(self.ex.ledger, count=10, oid="e2")
        self.clock.t = 1001.0

    def lock(self, parent, count=10):
        return self.ex.buy_lock(kq(DEN, "DEN", 0.40, self.clock.t), count, 0.40, KEY, self.clock.t, parent_id=parent.intent_id)

    def test_a_position_that_covers_both_entries_hedges_the_first_in_full(self):
        rec = self.lock(self.a)
        self.assertEqual(rec["status"], "SUBMITTED")
        self.assertEqual(rec["count"], 10)

    def test_a_position_short_of_both_entries_hedges_neither_blindly(self):
        self.client.holding = "10.00"                     # one entry's contracts were sold by hand
        rec = self.lock(self.a)
        self.assertEqual(rec["status"], "skipped")
        self.assertIn("other order", rec["reason"])
        self.assertEqual(self.client.sent, [])            # nothing was sent

    def test_one_entry_alone_is_capped_by_the_position(self):
        led = OrderLedger(tmp(), "demo", DEMO_URL, clock=self.clock)
        from tests.test_lock_identity import FakeKalshi as LockKalshi

        client = LockKalshi(holding="4.00")
        ex = LagExecutor(mode="demo", executor=KalshiExecutor(client), intents_path=tmp("lag.jsonl"), ledger=led, clock=self.clock)
        led.bind(Identity(key_fp="key:lock-caller", account_fp="account:lock-caller"))
        a = verified(led, count=10, oid="e9")
        self.clock.t += 1
        rec = ex.buy_lock(kq(DEN, "DEN", 0.40, self.clock.t), 10, 0.40, KEY, self.clock.t, parent_id=a.intent_id)
        self.assertEqual((rec["status"], rec["count"]), ("SUBMITTED", 4))


class LockExitsTests(unittest.TestCase):
    """s17: a lock leg of another entry is that entry's hedge, never this entry's exit."""

    def setUp(self):
        self.clock = Clock(1000.0)
        self.led = ledger(self.clock)
        self.a = verified(self.led, count=10, oid="e1")
        self.b = verified(self.led, count=10, oid="e2")

    def lock(self, parent, ticker=TICKER, side="no", count=10, now=None):
        now = self.clock.t if now is None else now
        return self.led.reserve(strategy="lock", ticker=ticker, side=side, count=count, limit_price="0.40", event_key=KEY,
                                game_key=KEY, parent_id=parent.intent_id, fee_multiplier=1, settlement=DEN_SD,
                                quote_ts=now, now=now)

    def test_each_entry_hedges_its_own_contracts_on_the_same_ticker(self):
        first = self.lock(self.a, now=1001.0)
        self.assertTrue(first.ok, first.reason)
        self.assertEqual(first.count, 10)
        second = self.lock(self.b, now=1002.0)
        self.assertTrue(second.ok, second.reason)
        self.assertEqual(second.count, 10)

    def test_a_real_exit_still_reduces_the_inventory(self):
        sale = self.led.reserve(strategy="manual", ticker=TICKER, side="no", action="buy", count=6, limit_price="0.40",
                                event_key=KEY, fee_multiplier=1, settlement=DEN_SD, now=1001.0)
        self.assertTrue(sale.ok, sale.reason)
        self.led.accepted(sale.intent_id, {"order_id": "x1", "fill_count": "6.00"})
        settle(self.led, sale.intent_id, "x1", "6.00", 6, "0.40", TICKER, side="no")
        res = self.lock(self.a, now=1002.0)
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.count, 4)                     # 10 filled - 6 bought back by hand

    def test_a_lock_leg_of_this_entry_still_counts_as_hedged(self):
        first = self.lock(self.a, count=4, now=1001.0)
        self.assertTrue(first.ok, first.reason)
        self.led.accepted(first.intent_id, {"order_id": "l1", "fill_count": "4.00"})
        settle(self.led, first.intent_id, "l1", "4.00", 4, "0.40", TICKER, side="no")
        second = self.lock(self.a, now=1002.0)
        self.assertTrue(second.ok, second.reason)
        self.assertEqual(second.count, 6)


class FailOpenTests(unittest.TestCase):
    """Public entry points must refuse, not raise something their callers do not catch, and
    must not release what an exchange answer showed filled."""

    def test_reserve_refuses_an_infinite_count_instead_of_raising(self):
        led = ledger()
        for count in (float("inf"), float("-inf"), 1e30):
            with self.subTest(count=count):
                res = led.reserve(strategy="lag", ticker=TICKER, side="yes", count=count, limit_price="0.50",
                                  event_key=KEY, fee_multiplier=1)
                self.assertFalse(res.ok)

    def test_rejected_never_releases_an_intent_that_showed_fills(self):
        led = ledger()
        res = entry(led, count=10)
        led.accepted(res.intent_id, {"order_id": "o1", "fill_count": "4.00"})
        applied = led.rejected(res.intent_id, "a caller tried to release it", from_states=(ACCEPTED,))
        self.assertFalse(applied)
        row = led.get(res.intent_id)
        self.assertEqual(row["state"], ACCEPTED)
        self.assertEqual(led.exposure(), Decimal("5.20"))
        self.assertEqual([e["kind"] for e in led.events(res.intent_id)][-1], "stale-write-ignored")

    def test_rejected_still_releases_a_pending_intent_that_filled_nothing(self):
        led = ledger()
        res = entry(led, count=10)
        self.assertTrue(led.rejected(res.intent_id, "the plan was refused: nothing sent"))
        self.assertEqual(led.exposure(), Decimal(0))

    def test_done_refuses_an_intent_the_exchange_never_acknowledged(self):
        """`done` is the low-level primitive `_apply_order` and `accept_correction` use; on
        its own it must not book an order the exchange never gave an id for."""
        led = ledger()
        res = entry(led, count=10)
        self.assertFalse(led.done(res.intent_id, Decimal(0), Decimal(0), Decimal(0)))
        self.assertEqual(led.get(res.intent_id)["state"], "pending")
        self.assertEqual(led.exposure(), Decimal("5.20"))
        led.accepted(res.intent_id, {"order_id": "o1", "fill_count": "0.00", "remaining_count": "0.00"})
        self.assertTrue(led.done(res.intent_id, Decimal(0), Decimal(0), Decimal(0)))
        self.assertEqual(led.exposure(), Decimal(0))

    def test_apply_row_without_a_client_can_never_finish_an_intent(self):
        led = ledger()
        res = entry(led, count=1, price="0.50")
        led.accepted(res.intent_id, {"order_id": "o1", "fill_count": "1.00", "remaining_count": "0.00"})
        note = led.apply_row(res.intent_id, order_row("o1"))          # no fills client at all
        self.assertEqual(led.get(res.intent_id)["state"], ACCEPTED)
        self.assertIn("not complete", note)
        self.assertEqual(led.exposure(), Decimal("0.52"))

    def test_list_orders_reports_a_non_paging_client_s_truncation(self):
        class OldClient:
            last_truncated = True

            def orders_v2(self, **params):
                return [{"order_id": "o1"}]

        rows, truncated = list_orders(OldClient(), ticker=TICKER)
        self.assertEqual(len(rows), 1)
        self.assertTrue(truncated)


if __name__ == "__main__":
    unittest.main()

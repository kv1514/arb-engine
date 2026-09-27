"""execution/ledger.py: fill evidence only grows, and only complete, correctly scoped evidence
finishes an order (the audit of bf7d2d5).

The reported defect: after a create answer reporting one fill, a final order row reporting
none closed the intent as "done: 0 filled, $0" - its exposure and budget released - and the
fills listing (which still showed the fill) was never read. Now every count an exchange
answer shows is kept (``fill_seen``), a lower one is a contradiction that releases nothing, an
order finishes only when a final row and a complete, correctly scoped fills listing agree, a
lock leg is sized only on a verified fill, an exchange correction is booked only by an
operator (``accept_correction`` / ``kalshi correct``), and a non-finite time never reaches an
age check. Everything here is offline: fake exchanges, no network, no orders.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import random
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from unittest import mock

from arb_engine.execution.kalshi import KalshiExecutor
from arb_engine.execution.ledger import (CONTRADICTED, CORRECTED, PROVISIONAL, VERIFIED, Budget, Identity, LedgerError, OrderLedger, book_side,
                                         fills_evidence, fingerprint, list_fills, refusal_hint, settlement_identity, worst_cost)
from arb_engine.strategy.lagexec import LagExecutor
from arb_engine.venues.http import HttpClient, HttpError
from arb_engine.venues.kalshi import KalshiClient
from tests.test_lock_identity import kq
from tests.test_order_ledger import DEMO_URL, KEY, TICKER, Clock, FakeKalshi, Fills, executor, fill_row, maker, order_row, quotes, sig, tmp

DEN = TICKER.replace("-KC", "-DEN")
BOUND = Decimal("0.52")          # 1 x $0.50 + the fee bound (0.07 x 0.25 rounded up to the cent)
PAID = Decimal("0.5175")         # 1 x $0.50 + the fee demo charges (centicent)


def one_lot(led=None, final_fill="1", clock=None):
    """A 1-contract LAG IOC at $0.50, recorded and answered with ``final_fill`` filled."""
    led = led or OrderLedger(tmp(), "demo", DEMO_URL, clock=clock or Clock())
    res = led.reserve(strategy="lag", ticker=TICKER, side="yes", count=1, limit_price="0.50", event_key=KEY, game_key=KEY, fee_multiplier=1,
                      settlement=settlement_identity("kalshi", KEY, "KC", "yes", "0.5", True))
    assert res.ok, res.reason
    led.accepted(res.intent_id, {"order_id": "o1", "fill_count": final_fill, "remaining_count": "0.00"})
    return led, res.intent_id


# An account the correction tests' exchange provably is (fingerprints of its key id and
# communications id, as OrderLedger.for_client would bind them).
ACCOUNT = Identity(fingerprint("key", "demo", "corr-key"), fingerprint("account", "demo", "corr-account"))


def bound(identity=ACCOUNT):
    led = OrderLedger(tmp(), "demo", DEMO_URL, clock=Clock())
    led.bind(identity)
    return led


class Unfiltered(Fills):
    """A fills listing that ignored its ``order_id`` filter: every row of the account."""

    def paged(self, path, key, params):
        return [dict(r) for r in self.rows], self.truncated


class FillsV2:
    """The audit's fake client: only ``fills_v2`` (no ``paged``), counting its calls."""

    env, base_url = "demo", DEMO_URL

    def __init__(self, rows):
        self.rows, self.calls = list(rows), 0

    def fills_v2(self, **params):
        self.calls += 1
        return [dict(r) for r in self.rows if r.get("order_id") == params.get("order_id")]


class Alerts:
    """The executor's alerter: records EXEC ERROR pushes."""

    def __init__(self):
        self.sent = []

    def alert(self, title, text, **kw):
        self.sent.append((title, text))

    def info(self, *a, **kw):
        pass


class ReportedDefectTests(unittest.TestCase):
    def test_a_zero_fill_row_after_a_reported_fill_erases_nothing(self):
        """The reproduction, step by step: reserve 1, accepted(fill_count 1), then a canceled
        row with fill_count_fp 0 / remaining 0, with a fills_v2 that returns one fill for o1.
        Was: "done: 0 filled, $0 + $0 fees", fills_v2 never called."""
        led, iid = one_lot()
        self.assertEqual(led.exposure(), BOUND)
        client = FillsV2([fill_row("f1", "o1")])
        note = led.apply_row(iid, {"order_id": "o1", "status": "canceled", "fill_count_fp": "0", "remaining_count_fp": "0"}, client=client)
        row = led.get(iid)
        self.assertEqual(client.calls, 1)                                  # the fills listing is read
        self.assertNotIn("done", note)
        self.assertIn("cumulative fills went down: the create answer showed 1 filled, the order row now says 0", note)
        self.assertEqual((row["state"], row["fill_state"], row["fill_count"], row["fill_seen"]), ("accepted", CONTRADICTED, None, "1"))
        self.assertEqual(led.exposure(), BOUND)                            # the whole worst case: nothing released
        # The budget is still spent: a per-game cap with room for one order has none.
        again = led.reserve(strategy="lag", ticker=TICKER, side="yes", count=1, limit_price="0.50", event_key=KEY, game_key=KEY,
                            fee_multiplier=1, budget=Budget(per_game=Decimal("0.60")))
        self.assertFalse(again.ok)
        self.assertIn("budget", again.reason)
        # Its events keep every answer.
        kinds = [e["kind"] for e in led.events(iid)]
        self.assertEqual(kinds[:3], ["reserved", "accepted", "contradicted"])


class DecreasingCountTests(unittest.TestCase):
    def test_a_lower_final_count_never_wins_whatever_the_fills_say(self):
        for fills in ([], [fill_row("f1", count="4.00")], [fill_row("f1", count="10.00")]):
            with self.subTest(fills=[f["count_fp"] for f in fills]):
                led = OrderLedger(tmp(), "demo", DEMO_URL, clock=Clock())
                res = led.reserve(strategy="lag", ticker=TICKER, side="yes", count=10, limit_price="0.50", fee_multiplier=1)
                led.accepted(res.intent_id, {"order_id": "o1", "fill_count": "10.00", "remaining_count": "0.00"})
                full = led.exposure()
                note = led.apply_row(res.intent_id, order_row(fill_count_fp="4.00", initial_count_fp="10.00", status="canceled",
                                                              taker_fill_cost_dollars="2.000000", taker_fees_dollars="0.070000"),
                                     client=Fills(fills))
                row = led.get(res.intent_id)
                self.assertIn("cumulative fills went down: the create answer showed 10.00 filled, the order row now says 4.00", note)
                self.assertEqual((row["state"], row["fill_state"], row["fill_seen"]), ("accepted", CONTRADICTED, "10.00"))
                self.assertGreaterEqual(led.exposure(), full)              # never lower than before the read

    def test_the_highest_count_is_kept_when_later_reads_go_down_and_up(self):
        led = OrderLedger(tmp(), "demo", DEMO_URL, clock=Clock())
        res = led.reserve(strategy="lag", ticker=TICKER, side="yes", count=10, limit_price="0.50", fee_multiplier=1)
        led.accepted(res.intent_id, {"order_id": "o1", "fill_count": "3.00"})             # not final: 3 is a lower bound
        self.assertEqual((led.get(res.intent_id)["fill_seen"], led.get(res.intent_id)["fill_state"]), ("3.00", PROVISIONAL))
        led.apply_row(res.intent_id, order_row(status="resting", fill_count_fp="6.00", remaining_count_fp="4.00", initial_count_fp="10.00"))
        self.assertEqual(led.get(res.intent_id)["fill_seen"], "6.00")
        led.apply_row(res.intent_id, order_row(status="resting", fill_count_fp="5.00", remaining_count_fp="5.00", initial_count_fp="10.00"))
        self.assertEqual((led.get(res.intent_id)["fill_state"], led.get(res.intent_id)["fill_seen"]), (CONTRADICTED, "6.00"))
        # The exchange's answers agree again at the highest count or above: verified, finished.
        row = order_row(status="canceled", fill_count_fp="6.00", initial_count_fp="10.00", taker_fill_cost_dollars="3.000000", taker_fees_dollars="0.105000")
        self.assertTrue(led.apply_row(res.intent_id, row, client=Fills([fill_row("f1", count="6.00", fee="0.105000")])).startswith("done"))
        got = led.get(res.intent_id)
        self.assertEqual((got["state"], got["fill_state"], got["fill_count"], Decimal(got["fees"])), ("done", VERIFIED, "6.00", Decimal("0.105")))

    def test_a_resting_order_whose_fills_go_down_keeps_the_higher_count(self):
        clock = Clock()
        client = FakeKalshi(clock)
        kb = maker(client, clock)
        order = kb.place(TICKER, "yes", 0.40, 10, watch_key=f"{KEY}|kalshi:KC", event_key=KEY, game_key=KEY)
        client.fill_resting(order.order_id, 3)
        kb.poll([order], {})
        self.assertEqual((kb.ledger.get(order.intent_id)["fill_count"], kb.ledger.get(order.intent_id)["fill_seen"]), ("3.00", "3.00"))
        client.rows[order.order_id].update(fill_count_fp="1.00", remaining_count_fp="9.00")        # a read that went backwards
        order.filled = 0.0                                                                      # (so the broker's own poll reads it again)
        kb.poll([order], {})
        row = kb.ledger.get(order.intent_id)
        self.assertEqual((row["fill_count"], row["fill_seen"], row["fill_state"]), ("3.00", "3.00", CONTRADICTED))
        self.assertEqual(kb.ledger.exposure(), Decimal(row["max_cost"]))                        # a resting order: its whole worst case


class ZeroAfterPositiveTests(unittest.TestCase):
    def test_zero_after_positive_is_a_contradiction_with_or_without_the_fill_listed(self):
        for listed in (True, False):
            with self.subTest(fill_still_listed=listed):
                led, iid = one_lot()
                led.apply_row(iid, order_row(status="canceled", fill_count_fp="0.00", taker_fill_cost_dollars="0.000000", taker_fees_dollars="0.000000"),
                              client=Fills([fill_row()] if listed else []))
                self.assertEqual((led.get(iid)["state"], led.get(iid)["fill_state"], led.exposure()), ("accepted", CONTRADICTED, BOUND))

    def test_the_contradiction_holds_across_many_reads_and_no_lock_is_sized_on_it(self):
        led, iid = one_lot()
        zero = order_row(status="canceled", fill_count_fp="0.00", taker_fill_cost_dollars="0.000000", taker_fees_dollars="0.000000")
        for _ in range(5):
            led.apply_row(iid, zero, client=Fills([]))
        self.assertEqual((led.get(iid)["state"], led.exposure()), ("accepted", BOUND))
        lock = led.reserve(strategy="lock", ticker=DEN, side="yes", count=1, limit_price="0.40", parent_id=iid, fee_multiplier=1,
                           settlement=settlement_identity("kalshi", KEY, "DEN", "yes", "0.5", True), quote_ts=1000.0)
        self.assertFalse(lock.ok)
        self.assertIn("the entry's fill evidence is contradicted", lock.reason)
        self.assertIn(led.get(iid)["intent_id"], [c["intent_id"] for c in led.status()["contradicted"]])


class DuplicateFillTests(unittest.TestCase):
    def test_identical_duplicates_count_once_even_across_pages(self):
        two = order_row(fill_count_fp="2.00", initial_count_fp="2.00", taker_fill_cost_dollars="1.000000", taker_fees_dollars="0.035000")
        led2 = OrderLedger(tmp(), "demo", DEMO_URL, clock=Clock())
        res = led2.reserve(strategy="lag", ticker=TICKER, side="yes", count=2, limit_price="0.50", fee_multiplier=1)
        led2.accepted(res.intent_id, {"order_id": "o1", "fill_count": "2.00", "remaining_count": "0.00"})
        pages = Fills([fill_row("f1"), fill_row("f2"), fill_row("f1"), fill_row("f2")])     # every fill twice (overlapping pages)
        self.assertTrue(led2.apply_row(res.intent_id, two, client=pages).startswith("done"))
        self.assertEqual(Decimal(led2.get(res.intent_id)["fees"]), Decimal("0.035"))

    def test_one_fill_id_with_two_contents_or_a_fill_without_an_id_proves_nothing(self):
        led, iid = one_lot()
        led.apply_row(iid, order_row(), client=Fills([fill_row("f1"), fill_row("f1", count="2.00")]))
        self.assertEqual((led.get(iid)["fill_state"], led.exposure()), (CONTRADICTED, BOUND))
        led, iid = one_lot()
        no_id = {k: v for k, v in fill_row("f1").items() if k not in ("fill_id", "trade_id")}
        note = led.apply_row(iid, order_row(), client=Fills([no_id]))
        self.assertIn("without a fill id", note)                            # cannot be told from a repeat: not counted
        self.assertEqual((led.get(iid)["state"], led.get(iid)["fill_state"]), ("accepted", PROVISIONAL))
        ev = fills_evidence([no_id, no_id], False, None, "o1")
        self.assertEqual((ev.count, ev.conclusive), (Decimal(0), False))    # two id-less rows are not two fills


class WrongOrderFillTests(unittest.TestCase):
    def test_fills_of_another_order_never_count_and_make_the_listing_inconclusive(self):
        led, iid = one_lot()
        other = [fill_row("f9", "o2")]                                     # the listing ignored its filter
        note = led.apply_row(iid, order_row(), client=Unfiltered(other + [fill_row("f1")]))
        self.assertIn("row(s) of other orders", note)
        self.assertEqual((led.get(iid)["state"], led.get(iid)["fill_state"]), ("accepted", PROVISIONAL))
        ev = fills_evidence(other, False, None, "o1")
        self.assertEqual((ev.count, ev.conclusive, ev.notes["fills_foreign"]), (Decimal(0), False, 1))

    def test_a_fill_or_row_of_this_order_on_another_market_or_book_side_contradicts_it(self):
        for bad in ({"ticker": DEN}, {"market_ticker": DEN}, {"book_side": "ask"}):
            with self.subTest(fill=bad):
                led, iid = one_lot()
                led.apply_row(iid, order_row(), client=Fills([fill_row(**bad)]))
                self.assertEqual((led.get(iid)["fill_state"], led.exposure()), (CONTRADICTED, BOUND))
        for bad in ({"ticker": DEN}, {"book_side": "ask"}, {"client_order_id": "someone-else"}):
            with self.subTest(row=bad):
                led, iid = one_lot()
                led.apply_row(iid, order_row(**bad), client=Fills([fill_row()]))
                self.assertEqual((led.get(iid)["fill_state"], led.exposure()), (CONTRADICTED, BOUND))

    def test_a_yes_sells_fills_are_in_scope(self):
        """How demo shows a YES sell (2026-09-27, GET only): the order row says side yes,
        outcome_side no, book_side ask; each fill says side no, outcome_side no, book_side ask.
        Only book_side names the order's direction - the legacy fields must not be compared."""
        self.assertEqual((book_side("buy", "yes"), book_side("sell", "yes"), book_side("buy", "no"), book_side("sell", "no")),
                         ("bid", "ask", "ask", "bid"))
        led = OrderLedger(tmp(), "demo", DEMO_URL, clock=Clock())
        res = led.reserve(strategy="manual", ticker=TICKER, side="yes", action="sell", count=1, limit_price="0.55", max_cost_per_contract="0.47")
        led.accepted(res.intent_id, {"order_id": "s1", "fill_count": "1.00", "remaining_count": "0.00"})
        row = order_row("s1", ticker=TICKER, action="sell", side="yes", outcome_side="no", book_side="ask", taker_fill_cost_dollars="0.440000",
                        taker_fees_dollars="0.017400")
        fill = fill_row("fs1", "s1", fee="0.017400", ticker=TICKER, action="sell", side="no", outcome_side="no", book_side="ask")
        self.assertTrue(led.apply_row(res.intent_id, row, client=Fills([fill])).startswith("done"), led.get(res.intent_id)["reason"])


class PaginationFailureTests(unittest.TestCase):
    def _client(self, pages):
        """A real KalshiClient whose fills listing answers ``pages`` in turn (dicts, or
        exceptions to raise); signing is covered elsewhere."""
        from tests.test_kalshi_client import _client

        return _client({"GET /portfolio/fills": list(pages)})

    def test_a_page_that_fails_mid_listing_proves_nothing(self):
        pages = lambda: [{"fills": [fill_row("f1")], "cursor": "c2"}, HttpError(500, DEMO_URL, "internal")]     # page 2 fails
        rows, truncated, error = list_fills(self._client(pages()), "o1")
        self.assertIn("500", error)
        led, iid = one_lot()
        note = led.apply_row(iid, order_row(), client=self._client(pages()))
        self.assertIn("fills listing is not complete (read failed", note)
        self.assertEqual((led.get(iid)["state"], led.get(iid)["fill_state"], led.exposure()), ("accepted", PROVISIONAL, BOUND))
        self.assertTrue(led.apply_row(iid, order_row(), client=self._client([{"fills": [fill_row("f1")], "cursor": ""}])).startswith("done"))

    def test_a_page_without_its_rows_or_a_truncated_listing_proves_nothing(self):
        for pages in ([{"cursor": ""}], [{"fills": None, "cursor": ""}], [{"fills": [fill_row()], "cursor": "more"}] * 25):
            with self.subTest(first=pages[0]):
                client = self._client(pages)
                rows, truncated, error = list_fills(client, "o1")
                self.assertTrue(error is not None or truncated)
                led, iid = one_lot()
                led.apply_row(iid, order_row(), client=self._client(pages))
                self.assertEqual((led.get(iid)["state"], led.get(iid)["fill_state"], led.exposure()), ("accepted", PROVISIONAL, BOUND))

    def test_an_orders_page_without_its_rows_never_releases_an_unknown_order(self):
        from tests.test_kalshi_client import _client

        for page, released in (({"cursor": ""}, False), ({"orders": [], "cursor": ""}, True)):   # a 200 without "orders"; a real empty listing
            with self.subTest(page=page):
                clock = Clock()
                client = _client({"GET /portfolio/orders": page, "GET /communications/id": {"communications_id": "acct-1"}})
                led = OrderLedger.for_client(client, path=tmp(), clock=clock)
                res = led.reserve(strategy="lag", ticker=TICKER, side="yes", count=1, limit_price="0.50", fee_multiplier=1)
                led.ambiguous(res.intent_id, "timeout")
                for _ in range(3):
                    clock.t += 40
                    led.reconcile(client)
                self.assertEqual(led.get(res.intent_id)["state"], "rejected" if released else "ambiguous")
                self.assertEqual(led.blocked() is None, released)


class RestartTests(unittest.TestCase):
    def test_the_evidence_survives_a_restart(self):
        clock = Clock(1000.0)
        path = tmp()
        client = FakeKalshi(clock)
        ex = executor(client, clock, path=path)
        rec = ex.on_signal(sig(contracts=5), quotes())
        self.assertEqual(ex.ledger.get(rec["intent_id"])["fill_seen"], "5.00")
        ex.ledger.close()
        # The exchange's row now says fewer (a correction, or a bad read); a new process starts.
        client.rows["o1"].update(fill_count_fp="2.00", status="canceled", taker_fill_cost_dollars="1.200000")
        clock.t += 10
        alerts = Alerts()
        ex2 = executor(client, clock, path=path, alerter=alerts)           # recover() reconciles first
        row = ex2.ledger.get(rec["intent_id"])
        self.assertEqual((row["state"], row["fill_state"], row["fill_seen"]), ("accepted", CONTRADICTED, "5.00"))
        pushed = [t for t in alerts.sent if t[0] == "EXEC ERROR" and "disagree" in t[1]]
        self.assertEqual(len(pushed), 1)                                   # one EXEC ERROR per contradicted order ...
        self.assertIn("kalshi correct", pushed[0][1])
        clock.t += 6
        ex2.reconcile(force=True)
        self.assertEqual(len([t for t in alerts.sent if "disagree" in t[1]]), 1)   # ... not one per read
        self.assertEqual(ex2.ledger.exposure(), Decimal(row["max_cost"]))
        wait = ex2.buy_lock(kq(DEN, "DEN", 0.36, clock()), 5, 0.36, KEY, clock(), parent_id=rec["intent_id"])
        self.assertIn("contradicted", wait["reason"])

    def test_an_order_whose_answer_died_with_its_process_is_verified_by_the_next(self):
        clock = Clock(1000.0)
        path = tmp()
        client = FakeKalshi(clock)
        client.lose_answer = HttpError(0, DEMO_URL, "curl: (28) Operation timed out")
        ex = executor(client, clock, path=path)
        rec = ex.on_signal(sig(contracts=5), quotes())
        self.assertEqual(rec["status"], "UNKNOWN")
        ex.ledger.close()
        client.lose_answer = None
        clock.t += 10
        ex2 = executor(client, clock, path=path)
        row = ex2.ledger.get(rec["intent_id"])
        self.assertEqual((row["state"], row["fill_state"], row["fill_count"]), ("done", VERIFIED, "5.00"))
        self.assertIsNone(ex2.ledger.blocked())


class NonFiniteTimeTests(unittest.TestCase):
    def setUp(self):
        self.led, self.parent = one_lot(bound())
        self.led.apply_row(self.parent, order_row(), client=Fills([fill_row()]))       # verified: hedgeable

    def lock(self, **kw):
        base = dict(strategy="lock", ticker=DEN, side="yes", count=1, limit_price="0.40", parent_id=self.parent, fee_multiplier=1,
                    settlement=settlement_identity("kalshi", KEY, "DEN", "yes", "0.5", True), quote_ts=1000.0)
        base.update(kw)
        return self.led.reserve(**base)

    def test_non_finite_quote_times_are_refused_at_the_boundary(self):
        for bad in (float("nan"), float("inf"), float("-inf"), Decimal("NaN"), "1000.0", True, [1000.0]):
            with self.subTest(quote_ts=bad):
                r = self.lock(quote_ts=bad)
                self.assertFalse(r.ok)
                self.assertIn("not a finite timestamp", r.reason)
        self.assertEqual(len(self.led.rows()), 1)                          # nothing reserved
        self.assertTrue(self.lock().ok)                                    # a finite, fresh one passes (the entry is hedgeable)

    def test_non_finite_decision_and_request_times_are_refused(self):
        r = self.led.reserve(strategy="lag", ticker=TICKER, side="yes", count=1, limit_price="0.50", fee_multiplier=1, now=float("nan"))
        self.assertFalse(r.ok)
        self.assertIn("not a finite timestamp", r.reason)
        for call in (lambda: self.led.accepted(self.parent, {"order_id": "o1"}, now=float("nan")),
                     lambda: self.led.ambiguous(self.parent, "x", req_ts=float("inf")),
                     lambda: self.led.blocked(float("nan")),
                     lambda: self.led.reconcile(Fills([]), now=float("-inf"))):
            with self.assertRaises(LedgerError):
                call()
        broken = OrderLedger(tmp(), "demo", DEMO_URL, clock=lambda: math.nan)          # a clock that returns NaN
        self.assertIn("not a finite timestamp", broken.reserve(strategy="lag", ticker=TICKER, side="yes", count=1, limit_price="0.5",
                                                             fee_multiplier=1).reason)

    def test_the_caller_refuses_a_quote_whose_time_is_not_finite(self):
        from tests.test_lock_identity import FakeKalshi as LockFake

        clock = Clock()
        client = LockFake()
        ex = LagExecutor(mode="demo", executor=KalshiExecutor(client), intents_path=tmp("lag.jsonl"), ledger_path=tmp(), clock=clock)
        entry = ex.on_signal(sig(contracts=10), {"kalshi": [kq(TICKER, "KC", 0.60, clock())]})
        clock.t += 3
        ex.reconcile(force=True)
        for bad in (float("nan"), float("inf")):
            q = kq(DEN, "DEN", 0.36, bad)
            rec = ex.buy_lock(q, 10, 0.36, KEY, clock(), parent_id=entry["intent_id"])
            self.assertEqual(rec["status"], "skipped")
            self.assertIn("the quote has no time", rec["reason"])


class ZeroFillCancellationTests(unittest.TestCase):
    """Legitimate zero-fill cancellations still resolve normally - once their (empty) fills
    listing is read complete."""

    def test_an_ioc_that_filled_nothing_is_done_at_zero_and_frees_its_budget(self):
        clock = Clock()
        client = FakeKalshi(clock, fill=lambda n: 0)
        ex = executor(client, clock, max_notional_per_game=31.0)
        rec = ex.on_signal(sig(ts=1.0), quotes())
        self.assertEqual(ex.sent_notional, 0.0)                            # the final create answer: nothing filled
        clock.t += 3
        ex.reconcile(force=True)
        row = ex.ledger.get(rec["intent_id"])
        self.assertEqual((row["state"], row["fill_state"], row["fill_count"], Decimal(row["fill_cost"]), Decimal(row["fees"])),
                         ("done", VERIFIED, "0.00", Decimal(0), Decimal(0)))
        self.assertEqual(ex.on_signal(sig(ts=2.0), quotes())["count"], 50)  # the whole game budget is still there

    def test_a_cancelled_maker_order_that_never_filled_is_done_at_zero(self):
        clock = Clock()
        client = FakeKalshi(clock)
        kb = maker(client, clock)
        order = kb.place(TICKER, "yes", 0.40, 10, watch_key=f"{KEY}|kalshi:KC", event_key=KEY, game_key=KEY)
        kb.cancel(order)
        clock.t += 3
        kb.reconcile(force=True)
        row = kb.ledger.get(order.intent_id)
        self.assertEqual((row["state"], row["fill_state"], row["fill_count"], kb.ledger.exposure()), ("done", VERIFIED, "0.00", Decimal("0")))

    def test_a_lost_answer_of_an_order_that_filled_nothing_is_found_and_done(self):
        clock = Clock()
        client = FakeKalshi(clock, fill=lambda n: 0)
        client.lose_answer = HttpError(0, DEMO_URL, "timeout")
        ex = executor(client, clock)
        rec = ex.on_signal(sig(), quotes())
        self.assertIsNotNone(ex.ledger.blocked())
        clock.t += 3
        ex.reconcile(force=True)
        self.assertEqual(ex.ledger.get(rec["intent_id"])["state"], "done")
        self.assertIsNone(ex.ledger.blocked())

    def test_a_zero_fill_cancellation_whose_listing_fails_waits_without_holding_budget(self):
        led, iid = one_lot(final_fill="0")                                # final create answer: nothing filled
        self.assertEqual(led.exposure(), Decimal("0"))
        zero = order_row(status="canceled", fill_count_fp="0.00", taker_fill_cost_dollars="0.000000", taker_fees_dollars="0.000000")
        led.apply_row(iid, zero, client=Fills(error=HttpError(500, DEMO_URL, "internal")))
        self.assertEqual((led.get(iid)["state"], led.exposure()), ("accepted", Decimal("0")))
        led.apply_row(iid, zero, client=Fills([]))
        self.assertEqual((led.get(iid)["state"], led.get(iid)["fill_state"]), ("done", VERIFIED))


class LockOnResolvedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.led = bound()
        res = self.led.reserve(strategy="lag", ticker=TICKER, side="yes", count=10, limit_price="0.60", event_key=KEY, fee_multiplier=1,
                               settlement=settlement_identity("kalshi", KEY, "KC", "yes", "0.5", True))
        self.iid = res.intent_id
        self.led.accepted(self.iid, {"order_id": "o1", "fill_count": "10.00", "remaining_count": "0.00"})

    def lock(self, count=10):
        return self.led.reserve(strategy="lock", ticker=DEN, side="yes", count=count, limit_price="0.36", parent_id=self.iid, fee_multiplier=1,
                                settlement=settlement_identity("kalshi", KEY, "DEN", "yes", "0.5", True), quote_ts=1000.0)

    def final(self, n):
        return order_row(status="executed" if n == 10 else "canceled", fill_count_fp=f"{n}.00", initial_count_fp="10.00",
                         taker_fill_cost_dollars=str(Decimal(n) * Decimal("0.60")), taker_fees_dollars=str(Decimal(n) * Decimal("0.02")))

    def test_a_create_answer_alone_sizes_no_lock_leg(self):
        r = self.lock()
        self.assertFalse(r.ok)
        self.assertIn("the entry's fill is not verified (an answer said 10.00", r.reason)
        self.led.apply_row(self.iid, self.final(10), client=Fills([fill_row(count="10.00", fee="0.20")]))
        self.assertEqual(self.lock().count, 10)

    def test_an_order_row_alone_or_a_trailing_listing_sizes_no_lock_leg(self):
        self.led.apply_row(self.iid, self.final(10))                      # no client: the listing is missing
        self.assertIn("not verified", self.lock().reason)
        self.led.apply_row(self.iid, self.final(10), client=Fills([fill_row(count="4.00")]))   # trails the row
        self.assertIn("not verified", self.lock().reason)
        self.led.apply_row(self.iid, self.final(10), client=Fills([fill_row(count="10.00")], truncated=True))
        self.assertIn("not verified", self.lock().reason)

    def test_an_operator_corrected_entry_is_hedged_on_the_corrected_count(self):
        client = CorrectionExchange(self.final(6), [fill_row(count="6.00", fee="0.12")])     # the create answer said 10; the exchange now 6
        self.led.apply_row(self.iid, client.row, client=client)
        self.assertIn("contradicted", self.lock().reason)
        self.led.accept_correction(self.iid, client, "Kalshi busted 4 contracts (support ticket 123)")
        self.assertEqual(self.led.get(self.iid)["fill_state"], CORRECTED)
        self.assertEqual(self.lock().count, 6)


class CorrectionExchange:
    """An exchange whose order row and fills listing can change (a busted trade); its client
    provably is ``ACCOUNT`` (the same key id)."""

    env, base_url, api_key = "demo", DEMO_URL, "corr-key"

    def communications_id(self):
        return "corr-account"

    def __init__(self, row, fills, filtered=True):
        self.row, self.fills, self.filtered = row, list(fills), filtered

    def order(self, oid):
        return dict(self.row)

    def paged(self, path, key, params):
        return [dict(f) for f in self.fills if not self.filtered or f["order_id"] == params["order_id"]], False


class ExchangeCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.led, self.iid = one_lot(bound())
        self.zero = order_row(status="canceled", fill_count_fp="0.00", taker_fill_cost_dollars="0.000000", taker_fees_dollars="0.000000")

    def test_a_correction_is_booked_only_by_hand_and_only_on_complete_agreeing_evidence(self):
        ex = CorrectionExchange(self.zero, [])                              # the exchange busted the fill: row 0, listing empty
        self.led.apply_row(self.iid, ex.row, client=ex)
        self.assertEqual(self.led.get(self.iid)["fill_state"], CONTRADICTED)  # never taken on its own
        with self.assertRaises(LedgerError):
            self.led.accept_correction(self.iid, ex, "   ")                # a reason is required
        ex.fills = [fill_row()]                                            # the listing still shows the fill: no
        with self.assertRaisesRegex(LedgerError, "fills listing shows 1.00 contracts, the order row 0.00"):
            self.led.accept_correction(self.iid, ex, "checked")
        ex.fills = []
        dry = self.led.accept_correction(self.iid, ex, "checked on kalshi.com: fill busted", apply=False)
        self.assertEqual((dry["seen_before"], dry["filled_now"], dry["applied"]), ("1", "0.00", False))
        self.assertEqual(self.led.get(self.iid)["state"], "accepted")      # a dry run books nothing
        self.led.accept_correction(self.iid, ex, "checked on kalshi.com: fill busted")
        row = self.led.get(self.iid)
        self.assertEqual((row["state"], row["fill_state"], row["fill_count"], row["fill_seen"]), ("done", CORRECTED, "0.00", "1"))
        done = json.loads([e for e in self.led.events(self.iid) if e["kind"] == "done"][-1]["detail"])
        self.assertEqual((done["overrides"], done["operator_reason"]), ("1", "checked on kalshi.com: fill busted"))

    def test_a_correction_is_refused_unless_the_intent_is_contradicted_and_the_count_lower(self):
        ex = CorrectionExchange(order_row(), [fill_row()])
        with self.assertRaisesRegex(LedgerError, "not contradicted"):
            self.led.accept_correction(self.iid, ex, "x")
        self.led.apply_row(self.iid, self.zero, client=CorrectionExchange(self.zero, [fill_row()]))    # contradicted
        with self.assertRaisesRegex(LedgerError, "not fewer than the 1 seen"):
            self.led.accept_correction(self.iid, ex, "x")                  # the exchange shows 1 again: reconcile finishes it
        with self.assertRaisesRegex(LedgerError, "not complete"):
            self.led.accept_correction(self.iid, CorrectionExchange(self.zero, [fill_row("f2", "o2")], filtered=False), "x")

    def test_a_manual_release_refuses_an_intent_that_showed_fills(self):
        with self.assertRaisesRegex(LedgerError, "showed 1 filled"):
            self.led.release(self.iid, "I checked")
        led, iid = one_lot(final_fill="0")                                 # nothing seen filled: releasable
        self.assertEqual(led.release(iid, "checked: never on the exchange")["state"], "rejected")

    def test_the_kalshi_correct_command_dry_runs_without_confirm(self):
        from arb_engine.cli_plugins import kalshi_ops

        calls = []

        class Led:
            def accept_correction(self, iid, client, reason, apply=True):
                calls.append((iid, reason, apply))
                return {"intent_id": iid, "applied": apply}

        for confirm, status in ((False, "DRY_RUN"), (True, "CORRECTED")):
            args = argparse.Namespace(action="correct", no_account_env=True, intent_id="i1", reason="busted by Kalshi", confirm=confirm)
            out = io.StringIO()
            with mock.patch("arb_engine.execution.kalshi.KalshiExecutor"), \
                    mock.patch("arb_engine.execution.ledger.OrderLedger.for_client", return_value=Led()), redirect_stdout(out):
                self.assertEqual(kalshi_ops.run_kalshi(args), 0)
            self.assertTrue(json.loads(out.getvalue())["status"].startswith(status))
        self.assertEqual(calls, [("i1", "busted by Kalshi", False), ("i1", "busted by Kalshi", True)])


class StaleWriteTests(unittest.TestCase):
    """Decisions read before a write never overwrite what another reader recorded since."""

    def test_a_stale_release_never_overwrites_a_finished_intent(self):
        led, iid = one_lot()
        led.apply_row(iid, order_row(), client=Fills([fill_row()]))
        self.assertEqual(led.get(iid)["state"], "done")
        self.assertFalse(led.rejected(iid, "not on the exchange: never accepted"))
        self.assertEqual((led.get(iid)["state"], led.exposure()), ("done", PAID))
        self.assertEqual(led.events(iid)[-1]["kind"], "stale-write-ignored")

    def test_a_finish_below_a_count_recorded_meanwhile_becomes_a_contradiction(self):
        led, iid = one_lot()
        led._write(lambda c: c.execute("UPDATE intents SET fill_seen='1', fill_count=NULL WHERE intent_id=?", (iid,)))
        self.assertFalse(led.done(iid, Decimal("0"), Decimal("0"), Decimal("0")))  # a reader decided on an older snapshot
        self.assertEqual((led.get(iid)["state"], led.get(iid)["fill_state"]), ("accepted", CONTRADICTED))

    def test_ambiguous_never_regresses_a_found_order_and_a_late_answer_reopens_a_release(self):
        led = OrderLedger(tmp(), "demo", DEMO_URL, clock=Clock())
        res = led.reserve(strategy="lag", ticker=TICKER, side="yes", count=1, limit_price="0.50", fee_multiplier=1)
        led.accepted(res.intent_id, {"order_id": "o1", "fill_count": "1.00", "remaining_count": "0.00"})
        led.ambiguous(res.intent_id, "a timeout noticed late")
        self.assertEqual(led.get(res.intent_id)["state"], "accepted")
        res2 = led.reserve(strategy="lag", ticker=TICKER, side="yes", count=1, limit_price="0.50", fee_multiplier=1)
        led.ambiguous(res2.intent_id, "timeout")
        self.assertTrue(led.rejected(res2.intent_id, "not on the exchange 40s after sending: never accepted"))
        led.accepted(res2.intent_id, {"order_id": "o2", "fill_count": "1.00", "remaining_count": "0.00"})    # the answer arrives after all
        row = led.get(res2.intent_id)
        self.assertEqual((row["state"], row["fill_seen"]), ("accepted", "1.00"))
        self.assertIn("arrived after the intent was released", row["reason"])
        self.assertEqual(led.exposure(), BOUND + BOUND)

    def test_a_4xx_answering_a_resent_post_is_not_a_definitive_refusal(self):
        one = HttpError(400, DEMO_URL, "bad request")
        again = HttpError(400, DEMO_URL, "duplicate client_order_id")
        again.attempts = 2
        self.assertEqual((refusal_hint(one), refusal_hint(again), refusal_hint(HttpError(0, DEMO_URL, "timeout")) == 0),
                         (400, None, True))

        class Flaky(HttpClient):                                           # 503, then 400 on the re-send
            def __init__(self):
                super().__init__(retries=2, transport="urllib")
                self.sent = 0

            def _via_urllib(self, method, url, hdrs, data):
                self.sent += 1
                raise HttpError(503 if self.sent == 1 else 400, url, "x")

        http = Flaky()
        with mock.patch("arb_engine.venues.http.time.sleep"), self.assertRaises(HttpError) as cm:
            http.request("POST", DEMO_URL + "/portfolio/events/orders", json_body={})
        self.assertEqual((cm.exception.status, cm.exception.attempts, refusal_hint(cm.exception)), (400, 2, None))
        # The ledger keeps such an order unknown for the full not-found window (reads trail writes).
        clock = Clock()
        client = FakeKalshi(clock)
        client.read_lag = 5.0
        client.lose_answer = cm.exception                                  # accepted, then the re-send's 400 came back
        ex = executor(client, clock)
        rec = ex.on_signal(sig(), quotes())
        self.assertEqual(rec["status"], "UNKNOWN")
        clock.t += 3
        ex.reconcile(force=True)                                           # not listed yet: not released on one listing
        self.assertEqual(ex.ledger.get(rec["intent_id"])["state"], "ambiguous")
        clock.t += 3
        ex.reconcile(force=True)
        self.assertEqual(ex.ledger.get(rec["intent_id"])["state"], "done")  # found: booked at what filled


class PropertyTests(unittest.TestCase):
    """Random answer sequences (seeded): fill evidence never shrinks, exposure never falls
    below the highest count seen (at the limit plus the fee bound) before a finish, and an
    order finishes only on a final row and a complete listing agreeing at or above it."""

    def test_random_answer_sequences_keep_the_invariants(self):
        rnd = random.Random(20260927)
        for case in range(300):
            ordered = rnd.randint(1, 6)
            led = OrderLedger(tmp(), "demo", DEMO_URL, clock=Clock())
            res = led.reserve(strategy="lag", ticker=TICKER, side="yes", count=ordered, limit_price="0.50", fee_multiplier=1)
            first = rnd.randint(0, ordered)
            led.accepted(res.intent_id, {"order_id": "o1", "fill_count": f"{first}.00", **({"remaining_count": "0.00"} if rnd.random() < 0.7 else {})})
            top = Decimal(first)
            for step in range(rnd.randint(1, 5)):
                n = rnd.randint(0, ordered)
                listed = rnd.choice([n, n, max(n - 1, 0), min(n + 1, ordered), 0])
                fills = [fill_row(f"f{i}", count="1.00", fee="0.017500") for i in range(listed)]
                kind = rnd.choice(["ok", "ok", "ok", "truncated", "error", "none"])
                client = None if kind == "none" else Fills(fills, truncated=kind == "truncated",
                                                           error=HttpError(500, DEMO_URL, "x") if kind == "error" else None)
                row = order_row(fill_count_fp=f"{n}.00", initial_count_fp=f"{ordered}.00", status="executed" if n == ordered else "canceled",
                                taker_fill_cost_dollars=f"{Decimal(n) * Decimal('0.5'):.6f}", taker_fees_dollars=f"{Decimal(n) * Decimal('0.0175'):.6f}")
                before = led.get(res.intent_id)
                if before["state"] == "done":
                    break
                led.apply_row(res.intent_id, row, client=client)
                after = led.get(res.intent_id)
                seen_before = Decimal(before["fill_seen"]) if before["fill_seen"] is not None else None
                seen_after = Decimal(after["fill_seen"]) if after["fill_seen"] is not None else None
                if seen_before is not None:
                    self.assertGreaterEqual(seen_after, seen_before, f"case {case}: fill evidence shrank")
                top = max(top, seen_after or Decimal(0))
                if after["state"] == "done":
                    self.assertGreaterEqual(Decimal(after["fill_count"]), top, f"case {case}: finished below a count seen")
                    self.assertEqual((kind, listed, n), ("ok", n, int(Decimal(after["fill_count"]))), f"case {case}: finished without agreeing evidence")
                else:
                    self.assertGreaterEqual(led.exposure(), worst_cost("0.50", top, 1), f"case {case}: exposure below the highest count seen")


if __name__ == "__main__":
    unittest.main()

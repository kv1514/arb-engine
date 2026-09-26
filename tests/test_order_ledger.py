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

    def __init__(self, clock, fill=lambda n: n):
        self.clock, self.fill = clock, fill
        self.rows, self.fill_rows, self.creates = {}, [], []
        self.lose_answer = None      # an exception to raise after the order is on the book
        self.fail_before = None      # an exception to raise before anything is recorded
        self.blank_answer = False
        self.read_lag = 0.0
        self.truncated = False
        self.cost_fields = True
        self.extra_fill_fee = Decimal("0")

    def create_order(self, payload):
        self.creates.append(payload)
        if self.fail_before is not None:
            raise self.fail_before
        n = int(float(payload["count"]))
        yes = Decimal(payload["price"])
        price = yes if payload["side"] == "bid" else Decimal(1) - yes
        got = self.fill(n)
        fee = KalshiFees().fee(price, got, "taker") if got else Decimal("0")
        oid = f"o{len(self.rows) + 1}"
        row = {"order_id": oid, "client_order_id": payload.get("client_order_id"), "ticker": payload["ticker"], "status": "executed" if got == n else "canceled",
               "outcome_side": "yes" if payload["side"] == "bid" else "no", "fill_count_fp": f"{got}.00", "remaining_count_fp": "0.00",
               "initial_count_fp": f"{n}.00", "created": self.clock()}
        if self.cost_fields:
            row.update(taker_fill_cost_dollars=str(price * got), maker_fill_cost_dollars="0.0000", taker_fees_dollars=str(fee), maker_fees_dollars="0.0000")
        self.rows[oid] = row
        if got:
            self.fill_rows.append({"order_id": oid, "count_fp": f"{got}.00", "fee_cost": str(fee + self.extra_fill_fee), "yes_price_dollars": str(yes)})
        if self.lose_answer is not None:
            raise self.lose_answer
        if self.blank_answer:
            return {}
        return {"order_id": oid, "client_order_id": payload.get("client_order_id"), "fill_count": f"{got}.00", "remaining_count": "0.00",
                **({"average_fill_price": str(price)} if got else {})}

    def _visible(self, row):
        return self.clock() - row["created"] >= self.read_lag

    def order(self, oid):
        row = self.rows.get(oid)
        if row is None or not self._visible(row):
            raise HttpError(404, f"{DEMO_URL}/portfolio/orders/{oid}", "not found")
        return {k: v for k, v in row.items() if k != "created"}

    def paged(self, path, key, params):
        assert path == "/portfolio/orders" and key == "orders"
        rows = [{k: v for k, v in r.items() if k != "created"} for r in self.rows.values()
                if self._visible(r) and r["ticker"] == params.get("ticker", r["ticker"])]
        return rows, self.truncated

    def fills_v2(self, **params):
        return [f for f in self.fill_rows if f["order_id"] == params.get("order_id")]


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
                bound = fee_bound(limit, n)
                # Any fill prices at or under the limit, split any way: never above the bound.
                for price in (Decimal(limit), Decimal("0.50") if Decimal(limit) >= Decimal("0.5") else Decimal(limit), Decimal("0.01")):
                    for split in ([n], [1] * n, [n // 2, n - n // 2]):
                        charged = sum((fees.fee(price, k, "taker") for k in split if k), Decimal("0"))
                        self.assertLessEqual(charged, bound, (limit, n, price, split))
        self.assertEqual(worst_cost("0.60", 50), Decimal("31.00"))   # 50 x 0.60 + 50 x 2c


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
        self.assertEqual((row["state"], Decimal(row["fill_cost"]), Decimal(row["fees"])), ("done", Decimal("7.20"), Decimal("0.21")))
        self.assertAlmostEqual(ex.sent_notional, 7.41)                   # actual: 12 x 0.60 + ceil(0.07 x 12 x 0.24)


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

    def test_an_order_row_without_cost_fields_is_priced_at_the_limit_plus_the_fee_bound(self):
        clock = Clock()
        client = FakeKalshi(clock)
        client.cost_fields = False
        ex = executor(client, clock)
        rec = ex.on_signal(sig(), quotes())
        clock.t += 3
        ex.reconcile(force=True)
        row = ex.ledger.get(rec["intent_id"])
        self.assertEqual((Decimal(row["fill_cost"]), Decimal(row["fees"])), (Decimal("30.00"), Decimal("1.00")))

    def test_an_ioc_that_filled_nothing_releases_its_budget(self):
        clock = Clock()
        client = FakeKalshi(clock, fill=lambda n: 0)
        ex = executor(client, clock, max_notional_per_game=31.0)
        self.assertEqual(ex.on_signal(sig(ts=1.0), quotes())["fill_count"], "0.00")
        self.assertEqual(ex.sent_notional, 0.0)
        self.assertEqual(ex.on_signal(sig(ts=2.0), quotes())["count"], 50)   # the whole game budget is still there


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
                          budget=Budget(daily=Decimal("500")))
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
        mine.reserve(strategy="lag", ticker=TICKER, side="yes", count=5, limit_price="0.60")
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
                                dedupe_key=f"w{i}-{j}", budget=Budget(daily=Decimal("20")))
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

        self.assertEqual(_order_max_loss("buy", 40, 0.61), Decimal("25.20"))       # 24.40 + 40 x 2c
        self.assertEqual(_order_max_loss("sell", 10, 0.90), Decimal("1.10"))       # short 10 x 0.10 + 10 x 1c
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

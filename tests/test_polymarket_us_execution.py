"""Adversarial offline IOC/accounting tests; no real credentials or network."""
import base64
import copy
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arb_engine.cli_plugins.us_ioc_ops import run
from arb_engine.execution.ledger import LedgerError, OrderLedger
from arb_engine.execution.polymarket_us_ioc import PolymarketUSExecutor, USOrderLedger, USOrderPlan, fee_bound
from arb_engine.execution.shared_limits import exposure
from arb_engine.venues.polymarket_us_trading import API, NoRedirect, PolymarketUSTradingClient, USAPIError

FIXTURE = json.loads((Path(__file__).parent / "fixtures/polymarket_us/orders_schema.json").read_text())
SLUG = FIXTURE["partial"]["marketSlug"]
NOW = datetime(2026, 9, 30, 18, tzinfo=timezone.utc).timestamp()
KEY = "0" * 64


def row(plan, filled=None, state="ORDER_STATE_FILLED", left=0, oid="us-order-1"):
    payload = plan.payload()
    return {"id": oid, **{k: payload[k] for k in ("marketSlug", "intent", "type", "price", "quantity", "tif")},
            "cumQuantity": plan.count if filled is None else filled, "leavesQuantity": left, "state": state,
            "avgPx": dict(payload["price"]),
            "commissionNotionalTotalCollected": {"currency": "USD", "value": str(fee_bound(plan.limit_price, plan.count if filled is None else filled, NOW))}}


class FakeUS:
    base_url, fingerprint = API, KEY

    def __init__(self, record=None):
        self.record = copy.deepcopy(record or FIXTURE["partial_final"])
        self.sent, self.cancelled = [], []
        self.fail_create = False
        self.fail_read = False
        self.power = 100
        self.check_reservation = lambda: None
        self.clock_hook = lambda: None

    def balances(self):
        self.clock_hook()
        return {"balances": [{"currency": "USD", "buyingPower": self.power}]}

    def _create(self, payload, confirm=False, not_after=None):
        assert confirm
        self.check_reservation()
        self.sent.append(payload)
        if self.fail_create:
            raise TimeoutError("lost response")
        return {"id": self.record["id"], "executions": [{"order": copy.deepcopy(self.record)}]}

    def order(self, oid):
        if self.fail_read:
            raise TimeoutError()
        return {"order": copy.deepcopy(self.record)}

    def _cancel(self, oid, slug, confirm=False):
        assert confirm
        self.cancelled.append((oid, slug))
        self.record["leavesQuantity"] = 0
        self.record["state"] = "ORDER_STATE_CANCELED"
        return {}  # cancellation ACK alone contains no fill evidence


class USExecutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "prod.sqlite3")
        self.now = NOW
        self.clock = lambda: self.now
        self.ledger = USOrderLedger(self.path, clock=self.clock)
        self.addCleanup(self.ledger.close)
        self.client = FakeUS()
        self.plan = USOrderPlan(SLUG, "yes", 5, D(".4"), "test-request")
        self.q = SimpleNamespace(venue="polymarket_us", book_id="polymarket_us", ask=.38, bid=.37, ask_size=2,
                                 meta={"market_slug": SLUG, "side": "yes", "obs_ts": NOW, "req_ts": NOW-1,
                                       "quote_time": NOW, "refreshed": True, "approx_time": False, "tick_size": ".01", "min_order_size": 1})
        self.info = SimpleNamespace(sport="nfl", market_type="moneyline", in_play=False,
                                    start_time=datetime.fromtimestamp(NOW+86400, timezone.utc))
        self.ex = PolymarketUSExecutor(self.client, ledger=self.ledger, clock=self.clock, quote_provider=lambda _: (self.q, self.info))
        self.flags = mock.patch.dict(os.environ, {"ARB_LIVE_TRADING": "1", "POLYMARKET_US_LIVE_TRADING": "1"})
        self.flags.start()
        self.addCleanup(self.flags.stop)

    def test_dry_run_no_credentials_reads_writes_or_sends(self):
        ex = PolymarketUSExecutor(clock=self.clock, quote_provider=mock.Mock(side_effect=AssertionError()))
        self.assertEqual(ex.execute(self.plan)["status"], "DRY_RUN")
        self.assertEqual(self.ledger.status()["intents"], [])
        self.assertEqual(self.client.sent, [])

    def test_each_live_flag_and_confirmation_required(self):
        for env in ({"ARB_LIVE_TRADING": "0"}, {"POLYMARKET_US_LIVE_TRADING": "0"}):
            with mock.patch.dict(os.environ, env):
                self.assertEqual(self.ex.execute(self.plan, confirm=True)["status"], "BLOCKED")
        self.assertEqual(self.ex.execute(self.plan, confirm=1)["status"], "DRY_RUN")
        self.assertEqual(self.client.sent, [])

    def test_host_cannot_move_gates_to_another_product(self):
        self.client.base_url = "https://clob.polymarket.com"
        self.assertEqual(self.ex.execute(self.plan, confirm=True)["status"], "BLOCKED")
        self.assertEqual(self.client.sent, [])

    def test_decimal_fees_and_durable_intent_before_send(self):
        self.assertEqual(self.plan.worst_cost(NOW), D("2.08"))
        def before_send():
            other = USOrderLedger(self.path, clock=self.clock)
            try:
                self.assertEqual(other.status()["shared_exposure"], "2.08")
                self.assertEqual(other.status()["intents"][0]["state"], "pending")
            finally:
                other.close()
        self.client.check_reservation = before_send
        result = self.ex.execute(self.plan, confirm=True)
        self.assertEqual((result["status"], result["filled"], result["charge"]), ("DONE", "2", "0.79"))
        self.assertEqual(self.ledger.status()["shared_exposure"], "0.79")

    def test_no_purchase_uses_long_price_and_actual_no_fill_cost(self):
        plan = USOrderPlan(SLUG, "no", 5, D(".4"))
        self.q.meta["side"] = "no"
        record = copy.deepcopy(FIXTURE["partial_final"])
        record.update(intent="ORDER_INTENT_BUY_SHORT", price={"value": ".60", "currency": "USD"}, avgPx={"value": ".62", "currency": "USD"})
        self.client.record = record
        result = self.ex.execute(plan, confirm=True)
        self.assertEqual(self.client.sent[0]["price"]["value"], "0.6")
        self.assertEqual(result["charge"], "0.79")

    def test_fee_bound_uses_peak_if_price_improves(self):
        self.assertEqual(fee_bound(".9", 100, NOW), D("1.74"))
        self.assertEqual(USOrderPlan(SLUG, "yes", 100, D(".9")).worst_cost(NOW), D("91.74"))

    def test_raw_25_dollars_is_blocked_when_fees_take_it_over(self):
        plan = USOrderPlan(SLUG, "yes", 50, D(".5"))
        self.assertEqual(self.ex.execute(plan)["status"], "BLOCKED")
        self.assertEqual(self.ex.execute(plan, confirm=True)["status"], "BLOCKED")
        self.assertEqual(self.client.sent, [])

    def test_cancel_partial_remainder_then_read_inventory(self):
        self.client.record = copy.deepcopy(FIXTURE["partial"])
        result = self.ex.execute(self.plan, confirm=True)
        self.assertEqual(self.client.cancelled, [("us-order-1", SLUG)])
        self.assertEqual((result["status"], result["filled"], result["charge"]), ("DONE", "2", "0.79"))
        self.assertEqual(len(self.client.sent), 1)

    def test_missing_fill_releases_only_final_zero_evidence(self):
        self.client.record = copy.deepcopy(FIXTURE["missed"])
        result = self.ex.execute(self.plan, confirm=True)
        self.assertEqual((result["status"], result["charge"]), ("MISSED", "0.00"))

    def test_unknown_create_blocks_restart_and_kalshi(self):
        self.client.fail_create = True
        result = self.ex.execute(self.plan, confirm=True)
        self.assertEqual(result["status"], "UNKNOWN")
        restarted = USOrderLedger(self.path, clock=self.clock)
        self.addCleanup(restarted.close)
        with self.assertRaises(LedgerError):
            restarted.reserve(USOrderPlan(SLUG, "yes", 1, D(".4")), KEY)
        kalshi = restarted.store.reserve(strategy="manual", ticker="KXNFLGAME-TEST", side="yes", count=1, limit_price=".4", fee_multiplier=1)
        self.assertFalse(kalshi.ok)
        self.assertIn("unresolved", kalshi.reason)
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.ex.recover(result["intent_id"])["status"], "UNKNOWN")

    def test_response_without_id_is_unknown_not_a_missed_fill(self):
        self.client._create = mock.Mock(return_value={})
        result = self.ex.execute(self.plan, confirm=True)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(self.ledger.status()["shared_exposure"], "2.08")

    def test_missing_fees_never_zero_then_later_evidence_recovers(self):
        del self.client.record["commissionNotionalTotalCollected"]
        result = self.ex.execute(self.plan, confirm=True)
        self.assertEqual(result["status"], "OPEN")
        self.assertEqual(result["charge"], "2.08")
        self.client.record = copy.deepcopy(FIXTURE["partial_final"])
        self.assertEqual(self.ex.recover(result["intent_id"])["charge"], "0.79")

    def test_regressing_fills_sticky_even_after_good_read(self):
        iid = self.ledger.reserve(self.plan, KEY)
        self.ledger.accepted(iid, {"id": "us-order-1", "executions": [{"order": copy.deepcopy(FIXTURE["partial"])}]})
        self.client.record = copy.deepcopy(FIXTURE["missed"])
        result = self.ex.recover(iid)
        self.assertEqual((result["status"], result["charge"]), ("CONTRADICTED", "2.08"))
        self.client.record = copy.deepcopy(FIXTURE["partial_final"])
        self.assertEqual(self.ex.recover(iid)["status"], "CONTRADICTED")

    def test_create_money_lower_bound_survives_restart_and_lower_final_row(self):
        iid = self.ledger.reserve(self.plan, KEY)
        record = copy.deepcopy(FIXTURE["partial_final"])
        record["commissionNotionalTotalCollected"]["value"] = ".04"
        self.ledger.accepted(iid, {"id": "us-order-1", "executions": [{"order": record}]})
        restarted = USOrderLedger(self.path, clock=self.clock)
        self.addCleanup(restarted.close)
        restarted.observe(iid, copy.deepcopy(FIXTURE["partial_final"]))
        self.assertEqual(restarted.get(iid)["state"], "contradicted")
        self.assertEqual(restarted.get(iid)["charge"], "2.08")

    def test_create_batch_permutation_keeps_all_cumulative_fill_evidence(self):
        for index, reverse in enumerate((False, True)):
            path = os.path.join(self.directory.name, f"permutation-{index}.db")
            ledger = USOrderLedger(path, clock=self.clock)
            try:
                iid = ledger.reserve(self.plan, KEY)
                a, b = copy.deepcopy(FIXTURE["partial"]), copy.deepcopy(FIXTURE["partial"])
                a.update(cumQuantity=1, leavesQuantity=4)
                a["commissionNotionalTotalCollected"]["value"] = ".02"
                snapshots = [a, b]
                if reverse:
                    snapshots.reverse()
                ledger.accepted(iid, {"id": "us-order-1", "executions": [{"order": r} for r in snapshots]})
                self.assertEqual(ledger.get(iid)["fill_seen"], "2")
                self.assertEqual(ledger.get(iid)["fee_seen"], "0.03")
                self.assertTrue(ledger.observe(iid, copy.deepcopy(FIXTURE["partial_final"])))
            finally:
                ledger.close()

    def test_missing_remaining_or_unknown_state_never_releases(self):
        iid = self.ledger.reserve(self.plan, KEY)
        self.ledger.accepted(iid, {"id": "us-order-1"})
        record = copy.deepcopy(FIXTURE["partial_final"])
        del record["leavesQuantity"]
        self.assertFalse(self.ledger.observe(iid, record))
        self.assertEqual(self.ledger.get(iid)["charge"], "2.08")
        record = copy.deepcopy(FIXTURE["partial_final"])
        record["state"] = "unknown-future-state"
        self.assertFalse(self.ledger.observe(iid, record))
        self.assertEqual(self.ledger.get(iid)["charge"], "2.08")

    def test_fill_count_on_incomplete_row_is_not_forgotten_before_zero_read(self):
        iid = self.ledger.reserve(self.plan, KEY)
        self.ledger.accepted(iid, {"id": "us-order-1"})
        record = copy.deepcopy(FIXTURE["partial_final"])
        del record["leavesQuantity"]
        self.ledger.observe(iid, record)
        self.assertEqual(self.ledger.get(iid)["fill_seen"], "2")
        self.ledger.observe(iid, copy.deepcopy(FIXTURE["missed"]))
        self.assertEqual(self.ledger.get(iid)["state"], "contradicted")
        self.assertEqual(self.ledger.get(iid)["charge"], "2.08")

    def test_actual_quote_time_attribute_is_checked_not_just_meta(self):
        self.q.quote_time = NOW-11
        self.assertEqual(self.ex.execute(self.plan, confirm=True)["status"], "BLOCKED")
        self.assertEqual(self.client.sent, [])

    def test_overlimit_notional_remains_accounted_as_a_breach(self):
        record = row(self.plan, filled=5)
        record["avgPx"]["value"] = ".9"
        self.client.record = record
        result = self.ex.execute(self.plan, confirm=True)
        self.assertEqual(result["status"], "CONTRADICTED")
        self.assertEqual(D(result["charge"]), D("4.58"))

    def test_wrong_order_scope_cannot_free_budget_or_cancel(self):
        iid = self.ledger.reserve(self.plan, KEY)
        self.ledger.accepted(iid, {"id": "us-order-1"})
        self.client.record["marketSlug"] = "other-market"
        result = self.ex.recover(iid, confirm=True)
        self.assertEqual(result["status"], "CONTRADICTED")
        self.assertEqual(self.client.cancelled, [])
        self.assertEqual(result["charge"], "2.08")

    def test_read_failure_never_releases_a_partial_order(self):
        self.client.fail_read = True
        result = self.ex.execute(self.plan, confirm=True)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(self.ledger.status()["shared_exposure"], "2.08")

    def test_read_only_recovery_does_not_cancel_or_need_live_flags(self):
        self.client.record = copy.deepcopy(FIXTURE["partial"])
        iid = self.ledger.reserve(self.plan, KEY)
        self.ledger.accepted(iid, {"id": "us-order-1"})
        with mock.patch.dict(os.environ, {"ARB_LIVE_TRADING": "0", "POLYMARKET_US_LIVE_TRADING": "0"}):
            self.assertEqual(self.ex.recover(iid)["status"], "OPEN")
        self.assertEqual(self.client.cancelled, [])

    def test_changed_key_cannot_reconcile_or_add_exposure(self):
        iid = self.ledger.reserve(self.plan, KEY)
        self.client.fingerprint = "1" * 64
        with self.assertRaises(LedgerError):
            self.ex.recover(iid)
        with self.assertRaises(LedgerError):
            self.ledger.reserve(USOrderPlan(SLUG, "yes", 1, D(".4")), self.client.fingerprint)

    def test_duplicate_request_id_never_sends_twice(self):
        self.assertEqual(self.ex.execute(self.plan, confirm=True)["status"], "DONE")
        self.assertEqual(self.ex.execute(self.plan, confirm=True)["status"], "BLOCKED")
        self.assertEqual(len(self.client.sent), 1)

    def test_carried_stale_future_approx_and_bad_grid_block(self):
        changes = [("refreshed", False), ("obs_ts", NOW+1), ("obs_ts", NOW-7), ("approx_time", True),
                   ("quote_time", NOW-11), ("req_ts", NOW+1), ("tick_size", ".03"), ("min_order_size", 2)]
        for name, value in changes:
            original = self.q.meta[name]
            self.q.meta[name] = value
            with self.subTest(name=name, value=value):
                self.assertEqual(self.ex.execute(self.plan, confirm=True)["status"], "BLOCKED")
            self.q.meta[name] = original
        self.assertEqual(self.client.sent, [])

    def test_slow_balance_read_expiring_book_blocks(self):
        self.client.clock_hook = lambda: setattr(self, "now", NOW+7)
        self.assertEqual(self.ex.execute(self.plan, confirm=True)["status"], "BLOCKED")
        self.assertEqual(self.client.sent, [])

    def test_slow_reservation_expires_book_before_send_and_releases_only_unsent(self):
        reserve = self.ledger.reserve
        def delayed(*args):
            iid = reserve(*args)
            self.now = NOW+7
            return iid
        with mock.patch.object(self.ledger, "reserve", side_effect=delayed):
            result = self.ex.execute(self.plan, confirm=True)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(self.client.sent, [])
        self.assertEqual(D(self.ledger.status()["shared_exposure"]), 0)

    def test_claimed_us_intent_cannot_be_claimed_twice_or_abandoned(self):
        iid = self.ledger.reserve(self.plan, KEY)
        self.ledger.claim_send(iid, KEY)
        with self.assertRaises(LedgerError):
            self.ledger.claim_send(iid, KEY)
        with self.assertRaises(LedgerError):
            self.ledger._abandon_unsent(iid)
        self.assertEqual(self.ledger.get(iid)["charge"], "2.08")

    def test_slow_send_claim_expires_book_without_sending_or_freeing_claimed_cash(self):
        claim = self.ledger.claim_send
        def delayed(*args):
            claim(*args)
            self.now = NOW+7
        with mock.patch.object(self.ledger, "claim_send", side_effect=delayed):
            result = self.ex.execute(self.plan, confirm=True)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(self.client.sent, [])
        self.assertEqual(D(self.ledger.status()["shared_exposure"]), D("2.08"))
        with self.assertRaises(LedgerError):
            self.ledger._abandon_unsent(result["intent_id"])

    def test_inplay_or_insufficient_buying_power_blocks(self):
        self.info.in_play = True
        self.assertEqual(self.ex.execute(self.plan, confirm=True)["status"], "BLOCKED")
        self.info.in_play = False
        self.client.power = 0
        self.assertEqual(self.ex.execute(self.plan, confirm=True)["status"], "BLOCKED")

    def test_shared_cap_counts_existing_kalshi_and_done_us_inventory(self):
        kalshi = self.ledger.store.reserve(strategy="manual", ticker="KXNFLGAME-TEST", side="yes", count=48, limit_price=".5", fee_multiplier=1)
        self.assertTrue(kalshi.ok)
        self.assertLessEqual(kalshi.max_cost, D("25"))
        # An unfinished Kalshi leg blocks US. Its reservation survives the next connection.
        with self.assertRaises(LedgerError):
            self.ledger.reserve(self.plan, KEY)
        self.ledger.store.rejected(kalshi.intent_id, "fixture never sent")
        plan = USOrderPlan(SLUG, "yes", 48, D(".5"))
        iid = self.ledger.reserve(plan, KEY)
        self.ledger.accepted(iid, {"id": "us-order-1"})
        self.ledger.observe(iid, row(plan))
        for number in range(2):
            got = self.ledger.store.reserve(strategy="other", ticker=f"KXNFLGAME-{number}", side="yes", count=48,
                                            limit_price=".5", fee_multiplier=1)
            if number == 0:
                self.assertTrue(got.ok)
            else:
                self.assertFalse(got.ok)
                self.assertIn("$50", got.reason)
        self.assertLessEqual(exposure(self.ledger.store.conn), D("50"))

    def test_kalshi_leg_cap_applies_even_without_us_orders(self):
        got = self.ledger.store.reserve(strategy="manual", ticker="KXNFLGAME-TEST", side="yes", count=50, limit_price=".5", fee_multiplier=1)
        self.assertFalse(got.ok)
        self.assertIn("$25", got.reason)

    def test_caller_fee_estimate_cannot_bypass_kalshi_cap(self):
        got = self.ledger.store.reserve(strategy="manual", ticker="KXNFLGAME-TEST", side="yes", count=50,
                                       limit_price=".5", fee_multiplier=1, max_cost_per_contract=".01")
        self.assertFalse(got.ok)
        self.assertIn("$25", got.reason)

    def test_raw_production_kalshi_executor_cannot_bypass_ledger_or_resend(self):
        from arb_engine.execution.kalshi import KalshiExecutor
        from tests.test_order_ledger import FakeKalshi, Clock, PROD_URL
        client = FakeKalshi(Clock(NOW))
        client.env, client.base_url = "prod", PROD_URL
        ex = KalshiExecutor(client)
        with mock.patch("arb_engine.execution.ledger.default_path", return_value=self.path):
            plan = ex.plan("KXNFLGAME-TEST", "buy", "yes", 1, .4, time_in_force="immediate_or_cancel")
            self.assertTrue(ex.execute(plan, confirm=True)["status"].startswith("BLOCKED"))
            bound = OrderLedger.for_client(client, self.path, clock=self.clock)
            try:
                got = bound.reserve(strategy="manual", ticker=plan.ticker, side="yes", count=1,
                                    limit_price=".4", fee_multiplier=1)
            finally:
                bound.close()
            self.assertTrue(got.ok)
            plan.client_order_id = got.client_order_id
            self.assertEqual(ex.execute(plan, confirm=True)["status"], "SUBMITTED")
            self.assertTrue(ex.execute(plan, confirm=True)["status"].startswith("BLOCKED"))
        self.assertEqual(len(client.creates), 1)

    def test_parallel_kalshi_reservations_share_one_cash_cap(self):
        barrier = threading.Barrier(3)
        results = []
        def reserve(number):
            ledger = OrderLedger(self.path, "prod", "https://external-api.kalshi.com/trade-api/v2", clock=self.clock)
            try:
                barrier.wait()
                results.append(ledger.reserve(strategy=f"test-{number}", ticker=f"KXNFLGAME-{number}", side="yes", count=48,
                                              limit_price=".5", fee_multiplier=1).ok)
            finally:
                ledger.close()
        threads = [threading.Thread(target=reserve, args=(i,)) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(results), [False, True, True])

    def test_invalid_plan_nonfinite_bool_fractional_and_path(self):
        for count, price, slug in [(True, ".4", SLUG), (1.1, ".4", SLUG), (1, "NaN", SLUG), (1, "Infinity", SLUG), (1, ".4", "../orders")]:
            with self.subTest(count=count, price=price), self.assertRaises(ValueError):
                USOrderPlan(slug, "yes", count, price).payload()

    def test_overlimit_fill_or_fee_breach_blocks_and_accounts_full_cost(self):
        self.client.record["commissionNotionalTotalCollected"]["value"] = "10"
        result = self.ex.execute(self.plan, confirm=True)
        self.assertEqual(result["status"], "CONTRADICTED")
        self.assertEqual(result["charge"], "10.76")

    def test_cli_dryrun_without_key_or_network(self):
        args = SimpleNamespace(action="order", market_slug=SLUG, side="no", count=1, limit=".4", request_id=None, confirm=False)
        with mock.patch("arb_engine.cli_plugins.us_ioc_ops._client", side_effect=AssertionError()), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run(args), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "DRY_RUN")

    def test_cli_reconcile_unknown_exits_nonzero_without_resending(self):
        iid = self.ledger.reserve(self.plan, KEY)
        self.ledger.unknown(iid, "offline lost response")
        args = SimpleNamespace(action="reconcile", intent_id=iid, confirm=False)
        with mock.patch("arb_engine.cli_plugins.us_ioc_ops._client", return_value=self.client), \
             mock.patch("arb_engine.cli_plugins.us_ioc_ops.USOrderLedger", return_value=self.ledger), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run(args), 4)
        self.assertEqual(json.loads(output.getvalue())["reconciled"][0]["status"], "UNKNOWN")
        self.assertEqual(self.client.sent, [])


class USTransportTests(unittest.TestCase):
    def client(self, transport):
        return PolymarketUSTradingClient({"POLYMARKET_KEY_ID": "00000000-0000-0000-0000-000000000001",
                                         "POLYMARKET_SECRET_KEY": base64.b64encode(bytes(range(32))).decode()},
                                        clock=lambda: 1.234, transport=transport, signer=lambda seed, message: message)

    def test_exact_us_signature_path_and_no_secret_transmission(self):
        requests = []
        client = self.client(lambda request: requests.append(request) or {"balances": []})
        client.balances()
        request = requests[0]
        signature = request.get_header("X-pm-signature")
        self.assertEqual(base64.b64decode(signature), b"1234GET/v1/account/balances")
        self.assertEqual(request.full_url, API+"/v1/account/balances")
        self.assertNotIn(base64.b64encode(bytes(range(32))).decode(), str(request.headers))

    def test_mutation_disabled_by_default_and_wrong_host_refused(self):
        send = mock.Mock()
        client = self.client(send)
        with self.assertRaises(USAPIError):
            client._create({}, confirm=False)
        client.base_url = "https://api.polymarket.us.evil.example"
        with self.assertRaises(USAPIError):
            client.balances()
        send.assert_not_called()

    def test_post_sent_once_never_retried_or_error_secret_echoed(self):
        send = mock.Mock(side_effect=TimeoutError("PRIVATE SECRET"))
        client = self.client(send)
        with mock.patch.dict(os.environ, {"ARB_LIVE_TRADING": "1", "POLYMARKET_US_LIVE_TRADING": "1"}):
            with self.assertRaises(USAPIError) as error:
                client._create({}, confirm=True)
        self.assertNotIn("PRIVATE SECRET", str(error.exception))
        self.assertEqual(send.call_count, 1)

    def test_redirect_always_refused(self):
        with self.assertRaises(USAPIError):
            NoRedirect().redirect_request(None, None, None, None, None, API)


if __name__ == "__main__":
    unittest.main()

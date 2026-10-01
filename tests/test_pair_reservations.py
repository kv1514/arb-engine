"""Atomic accounting staging only; no sender/network or actual account inventory."""
import json
import tempfile
import threading
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from arb_engine.execution.ledger import Identity, LedgerError
from arb_engine.execution.pair_reservations import PairReservationLedger
from arb_engine.execution.polymarket_us_ioc import USOrderLedger, USOrderPlan
from arb_engine.execution.shared_limits import exposure, problem
from arb_engine.execution.standing_approval import ApprovalStore, digest
from tests.test_standing_approval import BINDING, books
from tests.test_auto_arb import NOW, RULE


class PairReservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'prod.sqlite3'
        self.approval_path = Path(self.temp.name)/'approval.sqlite3'
        self.now = NOW
        self.clock = lambda: self.now
        self.us = USOrderLedger(self.path, clock=self.clock)
        self.addCleanup(self.us.close)
        self.us.store.bind(Identity(BINDING['kalshi_key'], BINDING['kalshi_account']))
        self.pairs = PairReservationLedger(self.us)
        self.approval = ApprovalStore(self.approval_path, clock=self.clock)
        self.addCleanup(self.approval.close)
        for patch in (mock.patch('arb_engine.quant.us_arbitrage.rule_for_quote', return_value=RULE),
                      mock.patch('arb_engine.quant.us_arbitrage.pair_flags', return_value=[]),
                      mock.patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden'))):
            patch.start()
            self.addCleanup(patch.stop)
        self.approval.arm(BINDING)
        self.grant = self.approval.approve(books(), BINDING, contracts=10)
        self.consumed = self.approval.consume(self.grant['permit_id'], BINDING, self.grant['plan_digest'])

    def admit(self):
        return self.pairs.admit(self.approval, self.consumed, BINDING)

    def test_both_child_terms_and_contingency_cash_commit_atomically(self):
        p = self.admit()
        self.assertEqual(p['state'], 'staged')
        self.assertEqual({(r['venue'], r['role']) for r in p['children']}, {('polymarket_us', 'entry'), ('kalshi', 'hedge')})
        # 10 US at .50, entry .17 + two exit fee bounds .17; Kal at .40
        # plus ten conservatively split 1-contract fees of .02.
        self.assertEqual((D(p['us_cash']), D(p['kalshi_cash'])), (D('5.51'), D('4.20')))
        self.assertEqual(exposure(self.us.store.conn), D('9.71'))
        self.assertEqual(self.us.store.conn.execute('SELECT count(*) FROM intents').fetchone()[0], 0)
        self.assertEqual(self.us.store.conn.execute('SELECT count(*) FROM pm_us_intents').fetchone()[0], 0)
        self.assertEqual(self.approval.conn.execute('SELECT dispatch_claimed FROM permits').fetchone()[0], 0)

    def test_no_existing_executor_exemption_or_auto_send(self):
        self.admit()
        why = problem(self.us.store.conn, D(1))
        self.assertIn('pair staged or unresolved', why)
        self.assertEqual(self.pairs.status()['status'], 'ACCOUNTING_ONLY')
        self.assertEqual(self.pairs.status()['orders_submitted'], 0)
        result = self.us.store.reserve(strategy='manual', ticker='KXNFLGAME-TEST', side='yes', count=1, limit_price='.4', fee_multiplier=1)
        self.assertFalse(result.ok)
        with self.assertRaises(LedgerError):
            self.us.reserve(USOrderPlan('other', 'yes', 1, D('.4')), BINDING['polymarket_us_key'])

    def test_shared_holds_survive_reopen_on_same_production_file(self):
        p = self.admit()
        other = USOrderLedger(self.path, clock=self.clock)
        try:
            again = PairReservationLedger(other)
            self.assertEqual(exposure(other.store.conn), D('9.71'))
            self.assertEqual(again.get(p['pair_id'])['children'], p['children'])
        finally:
            other.close()

    def test_duplicate_parent_and_rearm_never_restore_room(self):
        self.admit()
        with self.assertRaises(LedgerError):
            self.admit()
        self.approval.arm(BINDING)
        self.assertEqual(exposure(self.us.store.conn), D('9.71'))
        with self.assertRaises(ValueError):
            self.admit()
        self.assertEqual(self.us.store.conn.execute('SELECT count(*) FROM pair_reservations').fetchone()[0], 1)

    def test_revocation_after_staging_still_blocks_actual_dispatch(self):
        self.admit()
        self.approval.revoke()
        with self.assertRaises(ValueError), self.approval.dispatch_guard(self.grant['permit_id'], BINDING, self.grant['plan_digest']):
            self.fail('revoked staged pair dispatched')
        self.assertEqual(exposure(self.us.store.conn), D('9.71'))

    def test_admission_does_not_replace_next_dispatch_guard(self):
        self.admit()
        with self.approval.dispatch_guard(self.grant['permit_id'], BINDING, self.grant['plan_digest']) as proof:
            self.assertEqual(proof['plan_digest'], self.grant['plan_digest'])

    def test_no_unbound_or_changed_account(self):
        self.us.store.identity = Identity()
        with self.assertRaises(LedgerError):
            self.admit()
        self.assertEqual(exposure(self.us.store.conn), D(0))

    def test_stored_account_change_refused(self):
        self.us.store.conn.execute("UPDATE meta SET value=? WHERE key='account_fp'", ('account:'+'d'*32,))
        with self.assertRaises(LedgerError):
            self.admit()

    def test_changed_us_key_refused(self):
        with self.us.store._tx() as c:
            self.us._bind(c, 'd'*64)
        with self.assertRaises(LedgerError):
            self.admit()

    def test_unresolved_kalshi_blocks_both_staged_children(self):
        r = self.us.store.reserve(strategy='manual', ticker='KXNFLGAME-TEST', side='yes', count=1, limit_price='.4', fee_multiplier=1)
        self.assertTrue(r.ok)
        with self.assertRaises(LedgerError):
            self.admit()
        self.assertEqual(self.us.store.conn.execute('SELECT count(*) FROM pair_reserved_children').fetchone()[0], 0)

    def test_unresolved_us_blocks_both_children(self):
        self.us.reserve(USOrderPlan('other', 'yes', 1, D('.4')), BINDING['polymarket_us_key'])
        with self.assertRaises(LedgerError):
            self.admit()

    def test_parent_includes_existing_finished_us_cash(self):
        iid = self.us.reserve(USOrderPlan('other', 'yes', 1, D('.4')), BINDING['polymarket_us_key'])
        # Fixture accounting migration: not a live final fill claim.
        self.us.store.conn.execute("UPDATE pm_us_intents SET state='done',charge='.42' WHERE intent_id=?", (iid,))
        self.admit()
        self.assertEqual(exposure(self.us.store.conn), D('10.13'))

    def test_aggregate_cap_not_just_sum_of_pair_children(self):
        iid = self.us.reserve(USOrderPlan('other', 'yes', 1, D('.4')), BINDING['polymarket_us_key'])
        self.us.store.conn.execute("UPDATE pm_us_intents SET state='done',charge='41' WHERE intent_id=?", (iid,))
        with self.assertRaises(LedgerError):
            self.admit()
        self.assertEqual(exposure(self.us.store.conn), D(41))

    def test_expiry_while_waiting_for_production_transaction(self):
        original = self.us.store._tx
        from contextlib import contextmanager
        @contextmanager
        def delayed():
            self.now += 6
            with original() as connection:
                yield connection
        with mock.patch.object(self.us.store, '_tx', delayed), self.assertRaises(LedgerError):
            self.admit()
        self.assertEqual(exposure(self.us.store.conn), D(0))

    def test_failed_insert_rolls_back_both_children_and_cash(self):
        self.us.store.conn.execute("CREATE TRIGGER reject_hedge BEFORE INSERT ON pair_reserved_children WHEN NEW.role='hedge' BEGIN SELECT RAISE(ABORT,'fixture crash'); END")
        with self.assertRaises(Exception):
            self.admit()
        self.assertEqual(self.us.store.conn.execute('SELECT count(*) FROM pair_reservations').fetchone()[0], 0)
        self.assertEqual(self.us.store.conn.execute('SELECT count(*) FROM pair_reserved_children').fetchone()[0], 0)
        self.assertEqual(exposure(self.us.store.conn), D(0))

    def test_later_policy_failure_does_not_erase_committed_production_cash(self):
        original = self.pairs._costs
        def costs(plan, now):
            result = original(plan, now)
            # Production clock remains valid; policy clock expires on exit.
            self.approval.clock = lambda: NOW+6
            return result
        with mock.patch.object(self.pairs, '_costs', costs), self.assertRaises(ValueError):
            self.admit()
        self.assertEqual(exposure(self.us.store.conn), D('9.71'))
        self.approval.clock = self.clock
        with self.assertRaises(LedgerError):
            self.admit()

    def test_cash_is_validated_individually_not_negative_offsets(self):
        self.admit()
        self.us.store.conn.execute("UPDATE pair_reservations SET us_cash='-1',kalshi_cash='10'")
        with self.assertRaises(ValueError):
            exposure(self.us.store.conn)

    def test_robinhood_same_book_and_unknown_tie_plans_refused(self):
        plan = self.grant['plan']
        for field, value in [('venue', 'robinhood'), ('book', 'kalshi')]:
            changed = json.loads(json.dumps(plan))
            changed['legs'][0][field] = value
            with self.subTest(field=field), self.assertRaises(LedgerError):
                self.pairs._costs(changed, NOW)
        changed = {**plan, 'tie_payout': None}
        with self.assertRaises(ValueError):
            self.pairs._costs(changed, NOW)

    def test_lower_quote_fee_multiplier_cannot_bypass_production_bound(self):
        changed = json.loads(json.dumps(self.grant['plan']))
        changed['legs'][1]['fee_params']['fee_multiplier'] = 0
        us, kal = self.pairs._costs(changed, NOW)
        self.assertEqual(kal, D('4.20'))

    def test_lower_quote_us_fee_does_not_bypass_current_schedule(self):
        changed = json.loads(json.dumps(self.grant['plan']))
        changed['legs'][0]['fee_params']['taker_theta'] = '.01'
        us, kal = self.pairs._costs(changed, NOW)
        self.assertEqual(us, D('5.51'))

    def test_parallel_process_style_admission_creates_one_parent(self):
        barrier = threading.Barrier(2)
        results = []
        def admit():
            us = USOrderLedger(self.path, clock=lambda: NOW)
            policy = ApprovalStore(self.approval_path, clock=lambda: NOW)
            try:
                us.store.bind(Identity(BINDING['kalshi_key'], BINDING['kalshi_account']))
                pairs = PairReservationLedger(us)
                barrier.wait(5)
                pairs.admit(policy, self.consumed, BINDING)
                results.append('reserved')
            except LedgerError:
                results.append('blocked')
            finally:
                policy.close()
                us.close()
        threads = [threading.Thread(target=admit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(results), ['blocked', 'reserved'])
        self.assertEqual(exposure(self.us.store.conn), D('9.71'))

    def test_only_expired_never_sendable_staging_releases_cash(self):
        p = self.admit()
        with self.assertRaises(LedgerError):
            self.pairs.abandon_staged(p['pair_id'])
        self.now += 6
        released = self.pairs.abandon_staged(p['pair_id'])
        self.assertEqual(released['state'], 'missed')
        self.assertEqual(exposure(self.us.store.conn), D(0))
        self.assertEqual(self.us.store.conn.execute('SELECT count(*) FROM pair_reservations').fetchone()[0], 1)
        self.assertEqual(self.approval.conn.execute('SELECT count(*) FROM permits').fetchone()[0], 1)

    def test_claimed_child_never_released_even_after_expiry(self):
        p = self.admit()
        self.us.store.conn.execute("UPDATE pair_reserved_children SET state='claimed' WHERE role='entry'")
        self.now += 6
        with self.assertRaises(LedgerError):
            self.pairs.abandon_staged(p['pair_id'])
        self.assertEqual(exposure(self.us.store.conn), D('9.71'))

    def test_corrupt_or_missing_child_does_not_prove_never_sent(self):
        p = self.admit()
        self.us.store.conn.execute("DELETE FROM pair_reserved_children WHERE role='hedge'")
        self.now += 6
        with self.assertRaises(LedgerError):
            self.pairs.abandon_staged(p['pair_id'])
        self.assertEqual(exposure(self.us.store.conn), D('9.71'))

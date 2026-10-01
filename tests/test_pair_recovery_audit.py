"""Adversarial receipt/evidence tests; synthetic accounting, never live orders."""
import copy
import json
import unittest
from decimal import Decimal as D
from unittest import mock

from arb_engine.execution.ledger import LedgerError
from arb_engine.execution.shared_limits import exposure
from arb_engine.execution.us_pair_orders import InventoryEvidence, USPairOrder
from tests import test_pair_recovery as fixtures
from tests.test_standing_approval import BINDING


class RecoveryAuditTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.RecoveryTests('runTest')
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()

    def sale_inputs(self):
        f = self.f
        leg = json.loads(f.parent['plan'])['legs'][0]
        inv = InventoryEvidence(BINDING['polymarket_us_key'], leg['market'].split('#', 1)[0], 'yes',
                                D(10), D(10), f.now, f.now, f.now+6)
        quote = {'market': leg['market'], 'book': 'polymarket_us', 'side': 'yes',
                 'refreshed': True, 'approx_time': False, 'req_ts': f.now, 'obs_ts': f.now,
                 'bid': '.49', 'size': 20}
        return inv, quote

    def missed_hedge(self):
        _, hedge = self.f.hedge()
        self.f.verify_kal(hedge, 0)

    def test_incomplete_increase_cannot_retain_final_inventory_verification(self):
        f = self.f
        entry = f.entry()
        f.verify_us(entry, 3, fees='.05')
        raw = f.us_row(entry, 4, fees='.07')
        raw.pop('leavesQuantity')
        self.assertFalse(f.recovery.observe(entry['id'], raw, BINDING))
        self.assertEqual(f.status()['commands'][0]['state'], 'contradicted')
        with self.assertRaises(LedgerError):
            f.recovery.claim_hedge(f.pid, BINDING)

    def test_incomplete_money_increase_cannot_retain_final_verification(self):
        f = self.f
        entry = f.entry()
        raw = f.verify_us(entry, 3, fees='.05')
        raw = copy.deepcopy(raw)
        raw['commissionNotionalTotalCollected']['value'] = '.06'
        raw.pop('leavesQuantity')
        self.assertFalse(f.recovery.observe(entry['id'], raw, BINDING))
        self.assertEqual(f.status()['commands'][0]['state'], 'contradicted')

    def test_money_without_cumulative_count_remains_a_lower_bound(self):
        f = self.f
        entry = f.entry()
        raw = f.us_row(entry, 3, fees='.05')
        raw.pop('cumQuantity')
        f.recovery.accepted(entry['id'], raw['id'], BINDING)
        self.assertFalse(f.recovery.observe(entry['id'], raw, BINDING))
        self.assertEqual(D(f.status()['commands'][0]['fee_seen']), D('.05'))
        self.assertFalse(f.recovery.observe(entry['id'], f.us_row(entry, 3, fees='.04'), BINDING))
        self.assertEqual(f.status()['commands'][0]['state'], 'contradicted')

    def test_incomplete_evidence_receipt_fences_later_clock_regression(self):
        f = self.f
        entry = f.entry()
        raw = f.us_row(entry, 3, fees='.05')
        f.recovery.accepted(entry['id'], raw['id'], BINDING)
        raw.pop('leavesQuantity')
        f.now += 2
        self.assertFalse(f.recovery.observe(entry['id'], raw, BINDING))
        f.now -= 1
        with self.assertRaises(LedgerError):
            f.status()

    def test_slow_sale_preparation_cannot_outlive_inventory_receipt(self):
        self.missed_hedge()
        f = self.f
        inv, quote = self.sale_inputs()
        # Inventory is newer than final fills, but expires before the book.
        f.now += 5
        quote.update(req_ts=f.now, obs_ts=f.now)
        original = USPairOrder.payload
        def slow(plan):
            f.now += 2
            return original(plan)
        with mock.patch.object(USPairOrder, 'payload', slow), self.assertRaises(LedgerError):
            f.recovery.claim_unwind(f.pid, BINDING, inv, quote)
        self.assertEqual(len(f.status()['commands']), 2)

    def test_sale_command_deadline_is_earliest_inventory_or_book_receipt(self):
        self.missed_hedge()
        f = self.f
        inv, quote = self.sale_inputs()
        f.now += 2
        quote.update(req_ts=f.now, obs_ts=f.now)
        command = f.recovery.claim_unwind(f.pid, BINDING, inv, quote)
        self.assertEqual(command['deadline'], inv.deadline)

    def test_exchange_stale_or_future_sale_quote_is_rejected(self):
        self.missed_hedge()
        inv, quote = self.sale_inputs()
        for qt in (self.f.now-11, self.f.now+1, float('nan')):
            with self.subTest(quote_time=qt), self.assertRaises((LedgerError, ValueError)):
                self.f.recovery.claim_unwind(self.f.pid, BINDING, inv, {**quote, 'quote_time': qt})
        self.assertEqual(len(self.f.status()['commands']), 2)

    def test_observed_exit_fee_overrun_is_in_shared_cash_total(self):
        self.missed_hedge()
        f = self.f
        sale = f.unwind()
        raw = f.us_row(sale, 10, fees='.50')
        f.recovery.accepted(sale['id'], raw['id'], BINDING)
        self.assertFalse(f.recovery.observe(sale['id'], raw, BINDING))
        self.assertEqual(exposure(f.us.store.conn), D('9.87'))
        self.assertEqual(f.status()['state'], 'unresolved')

    def test_incomplete_fee_overrun_is_charged_and_blocks_verification(self):
        f = self.f
        entry = f.entry()
        raw = f.us_row(entry, 10, fees='1')
        raw.pop('leavesQuantity')
        f.recovery.accepted(entry['id'], raw['id'], BINDING)
        self.assertFalse(f.recovery.observe(entry['id'], raw, BINDING))
        self.assertEqual(exposure(f.us.store.conn), D('10.20'))
        self.assertEqual(f.status()['commands'][0]['state'], 'contradicted')

    def test_over_cap_actual_cash_can_still_be_reported_and_reconciled(self):
        f = self.f
        entry = f.entry()
        raw = f.us_row(entry, 10, fees='51')
        f.recovery.accepted(entry['id'], raw['id'], BINDING)
        self.assertFalse(f.recovery.observe(entry['id'], raw, BINDING))
        self.assertEqual(D(f.status()['shared_exposure']), D('60.20'))
        self.assertFalse(f.recovery.observe(entry['id'], raw, BINDING))
        with self.assertRaises(LedgerError):
            f.recovery.claim_hedge(f.pid, BINDING)

    def test_negative_lower_bound_cannot_hide_behind_larger_reservation(self):
        f = self.f
        entry = f.entry()
        f.us.store.conn.execute('UPDATE pair_recovery_orders SET fee_seen=? WHERE id=?', ('-1', entry['id']))
        with self.assertRaises(ValueError):
            exposure(f.us.store.conn)


if __name__ == '__main__':
    unittest.main()

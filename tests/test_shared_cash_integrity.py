"""Corrupt/legacy accounting never supplies cash headroom. Offline only."""
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from arb_engine.execution.polymarket_us_ioc import USOrderLedger, USOrderPlan
from arb_engine.execution.shared_limits import exposure, problem


class CashIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = USOrderLedger(Path(self.temp.name) / 'prod.sqlite3')
        self.addCleanup(self.ledger.close)
        self.c = self.ledger.store.conn
        self.kal = self.ledger.store.reserve(strategy='fixture', ticker='KXNFLGAME-TEST',
                                             side='yes', count=1, limit_price='.4', fee_multiplier=1)
        self.assertTrue(self.kal.ok)

    def update(self, **values):
        # Only named test-owned columns, never an external input to SQL.
        self.c.execute('UPDATE intents SET ' + ','.join(k+'=?' for k in values) + ' WHERE intent_id=?',
                       (*values.values(), self.kal.intent_id))

    def test_legacy_done_missing_fee_retains_full_hold(self):
        self.update(state='done', fill_cost='.2', fees=None)
        self.assertEqual(exposure(self.c), D('.42'))

    def test_legacy_done_missing_cost_retains_full_hold(self):
        self.update(state='done', fill_cost=None, fees='0')
        self.assertEqual(exposure(self.c), D('.42'))

    def test_explicit_zero_fill_cost_and_fee_are_zero(self):
        self.update(state='done', fill_cost='0', fees='0', fill_count='0', fill_state='verified')
        self.assertEqual(exposure(self.c), 0)

    def test_verified_paid_cash_is_counted(self):
        self.update(state='done', fill_cost='.4', fees='.01', fill_count='1', fill_state='verified')
        self.assertEqual(exposure(self.c), D('.41'))

    def test_contradicted_done_never_releases_bound(self):
        self.update(state='done', fill_cost='.2', fees='.01', fill_state='contradicted')
        self.assertEqual(exposure(self.c), D('.42'))

    def test_negative_bound_cannot_offset_positive_us_cash(self):
        self.update(state='rejected')
        iid = self.ledger.reserve(USOrderPlan('fixture-market', 'yes', 1, D('.4')), 'a'*64)
        # Synthetic finished row isolates the cash check from the separate
        # unresolved-order gate (which also correctly refuses a new order).
        self.c.execute("UPDATE pm_us_intents SET state='done' WHERE intent_id=?", (iid,))
        self.update(state='pending', max_cost='-.2')
        with self.assertRaises(ValueError):
            exposure(self.c)
        self.assertIn('invalid shared cash', problem(self.c, '.1'))

    def test_negative_done_fee_cannot_offset_paid_cost(self):
        self.update(state='done', fill_cost='.4', fees='-.1')
        with self.assertRaises(ValueError):
            exposure(self.c)

    def test_negative_done_cost_cannot_offset_positive_fee(self):
        self.update(state='done', fill_cost='-.1', fees='.2')
        with self.assertRaises(ValueError):
            exposure(self.c)

    def test_nonfinite_and_malformed_bound_block(self):
        for value in ('NaN', 'Infinity', 'sNaN', 'broken'):
            self.update(max_cost=value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                exposure(self.c)

    def test_nonfinite_and_negative_us_charge_block(self):
        self.update(state='rejected')
        iid = self.ledger.reserve(USOrderPlan('fixture-market', 'yes', 1, D('.4')), 'a'*64)
        for value in ('NaN', 'Infinity', '-1', 'broken'):
            self.c.execute('UPDATE pm_us_intents SET charge=? WHERE intent_id=?', (value, iid))
            with self.subTest(value=value), self.assertRaises(ValueError):
                exposure(self.c)

    def test_corrupt_finished_cash_refuses_new_reservation(self):
        self.update(state='done', fill_cost='.4', fees='-.1')
        result = self.ledger.store.reserve(strategy='fixture', ticker='KXNFLGAME-OTHER',
                                            side='no', count=1, limit_price='.4', fee_multiplier=1)
        self.assertFalse(result.ok)
        self.assertIn('invalid shared cash', result.reason)
        self.assertEqual(self.c.execute('SELECT count(*) FROM intents').fetchone()[0], 1)

    def test_sell_with_unknown_fee_or_inventory_retains_bound(self):
        self.update(state='done', action='sell', fill_count=None, fees='0')
        self.assertEqual(exposure(self.c), D('.42'))

    def test_each_sell_component_is_validated(self):
        self.update(state='done', action='sell', fill_count='-.2', fees='.2')
        with self.assertRaises(ValueError):
            exposure(self.c)

"""Offline recovery/outbox state tests, NOT live dispatch or fill evidence."""
import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from arb_engine.execution.ledger import Identity, LedgerError, book_side
from arb_engine.execution.pair_recovery import PairRecovery
from arb_engine.execution.pair_reservations import PairReservationLedger
from arb_engine.execution.polymarket_us_ioc import USOrderLedger
from arb_engine.execution.shared_limits import exposure, problem
from arb_engine.execution.standing_approval import ApprovalStore
from arb_engine.execution.us_pair_orders import InventoryEvidence
from tests.test_auto_arb import NOW, RULE
from tests.test_standing_approval import BINDING, books


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'prod.sqlite3'
        self.now = NOW
        self.clock = lambda: self.now
        self.us = USOrderLedger(self.path, clock=self.clock)
        self.addCleanup(self.us.close)
        self.us.store.bind(Identity(BINDING['kalshi_key'], BINDING['kalshi_account']))
        self.pairs = PairReservationLedger(self.us)
        self.recovery = PairRecovery(self.pairs)
        self.approval = ApprovalStore(Path(self.temp.name)/'approval.sqlite3', clock=self.clock)
        self.addCleanup(self.approval.close)
        for patch in (mock.patch('arb_engine.quant.us_arbitrage.rule_for_quote', return_value=RULE),
                      mock.patch('arb_engine.quant.us_arbitrage.pair_flags', return_value=[]),
                      mock.patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden'))):
            patch.start()
            self.addCleanup(patch.stop)
        self.approval.arm(BINDING)
        grant = self.approval.approve(books(), BINDING, contracts=10)
        consumed = self.approval.consume(grant['permit_id'], BINDING, grant['plan_digest'])
        self.parent = self.pairs.admit(self.approval, consumed, BINDING)
        self.pid = self.parent['pair_id']

    def entry(self):
        return self.recovery.claim_entry(self.pid, self.approval, BINDING)

    def us_row(self, command, qty, *, fees='.17', price=None):
        payload = copy.deepcopy(command['payload'])
        payload.update(id='us-'+command['id'], cumQuantity=qty, leavesQuantity=0,
                       avgPx={'value': str(price if price is not None else payload['price']['value']), 'currency': 'USD'},
                       commissionNotionalTotalCollected={'value': fees, 'currency': 'USD'},
                       state='ORDER_STATE_FILLED' if D(str(qty)) == D(str(payload['quantity'])) else 'ORDER_STATE_CANCELED')
        return payload

    def verify_us(self, command, qty, **kwargs):
        raw = self.us_row(command, qty, **kwargs)
        self.recovery.accepted(command['id'], raw['id'], BINDING)
        self.assertTrue(self.recovery.observe(command['id'], raw, BINDING))
        return raw

    def hedge(self, entry_qty=10):
        e = self.entry()
        self.verify_us(e, entry_qty)
        h = self.recovery.claim_hedge(self.pid, BINDING)
        return e, h

    def kal_row(self, command, qty):
        terms = command['plan']
        side = terms['side']
        limit = D(terms['limit_price'])
        row = {'order_id': 'kal-'+command['id'], 'client_order_id': terms['client_order_id'],
               'ticker': terms['ticker'], 'book_side': book_side('buy', side), 'action': 'buy',
               'fill_count_fp': str(qty), 'remaining_count_fp': '0', 'initial_count_fp': str(terms['count']),
               'status': 'executed' if qty == terms['count'] else 'canceled',
               'taker_fill_cost_dollars': str(D(qty)*limit), 'taker_fees_dollars': str(D(qty)*D('.02'))}
        fills = [] if qty == 0 else [{'order_id': row['order_id'], 'fill_id': 'fill-'+command['id'],
                                     'ticker': terms['ticker'], 'book_side': row['book_side'], 'count_fp': str(qty),
                                     'yes_price_dollars': str(limit if side == 'yes' else 1-limit),
                                     'no_price_dollars': str(limit if side == 'no' else 1-limit),
                                     'fee_cost': row['taker_fees_dollars']}]
        return row, fills

    def verify_kal(self, command, qty):
        row, fills = self.kal_row(command, qty)
        self.recovery.accepted(command['id'], row['order_id'], BINDING)
        self.assertTrue(self.recovery.observe(command['id'], row, BINDING, fills=fills, truncated=False))
        return row, fills

    def unwind(self, *, available=10, size=20, bid='.49', side=None, **change):
        leg = json.loads(self.parent['plan'])['legs'][0]
        side = side or leg['side']
        inv = InventoryEvidence(BINDING['polymarket_us_key'], leg['market'].split('#', 1)[0], side,
                                D(available) if side == 'yes' else -D(available), D(available), self.now, self.now, self.now+6)
        quote = {'market': leg['market'], 'book': 'polymarket_us', 'side': side, 'refreshed': True,
                 'req_ts': self.now, 'obs_ts': self.now, 'bid': bid, 'size': size, **change}
        return self.recovery.claim_unwind(self.pid, BINDING, inv, quote)

    def status(self):
        return self.recovery.status(self.pid, BINDING)

    def test_claim_is_durable_before_any_network_and_never_authorizes_send(self):
        command = self.entry()
        self.assertFalse(command['send_authorized'])
        self.assertEqual(command['payload']['manualOrderIndicator'], 'MANUAL_ORDER_INDICATOR_AUTOMATIC')
        self.assertEqual(self.status()['orders_submitted_by_coordinator'], 0)
        self.assertIn(command['id'], self.status()['reconciliation_required'])
        self.assertEqual(exposure(self.us.store.conn), D('9.71'))

    def test_restart_after_claim_never_offers_duplicate_entry(self):
        command = self.entry()
        other = USOrderLedger(self.path, clock=self.clock)
        self.addCleanup(other.close)
        other.store.bind(Identity(BINDING['kalshi_key'], BINDING['kalshi_account']))
        restarted = PairRecovery(PairReservationLedger(other))
        with self.assertRaises((ValueError, LedgerError)):
            restarted.claim_entry(self.pid, self.approval, BINDING)
        self.assertEqual(restarted.status(self.pid, BINDING)['commands'][0]['id'], command['id'])

    def test_revoked_after_staging_never_claims_entry(self):
        self.approval.revoke()
        with self.assertRaises(ValueError):
            self.entry()
        self.assertEqual(self.status()['commands'], [])

    def test_claim_expiry_while_waiting_leaves_no_new_command(self):
        self.now += 6
        with self.assertRaises(ValueError):
            self.entry()
        self.assertEqual(self.status()['commands'], [])

    def test_policy_failure_after_production_commit_keeps_permanent_claim(self):
        original = self.recovery._insert
        def late(*args):
            result = original(*args)
            self.approval.clock = lambda: NOW+6
            return result
        with mock.patch.object(self.recovery, '_insert', late), self.assertRaises(ValueError):
            self.entry()
        self.approval.clock = self.clock
        self.assertEqual(len(self.status()['commands']), 1)
        with self.assertRaises(LedgerError):
            self.entry()

    def test_create_snapshot_never_establishes_inventory_for_hedge(self):
        e = self.entry()
        raw = self.us_row(e, 10)
        self.recovery.accepted(e['id'], raw['id'], BINDING)
        self.assertFalse(self.recovery.observe(e['id'], raw, BINDING, final_read=False))
        with self.assertRaises(LedgerError):
            self.recovery.claim_hedge(self.pid, BINDING)

    def test_partial_entry_sizes_hedge_and_cancels_entry_remainder(self):
        _, hedge = self.hedge(3)
        self.assertEqual(hedge['plan']['count'], 3)
        self.assertEqual(D(hedge['plan']['limit_price']), D('.4'))
        self.verify_kal(hedge, 3)
        self.assertEqual(self.status()['state'], 'held')

    def test_missing_fee_blocks_hedge(self):
        e = self.entry()
        raw = self.us_row(e, 10)
        raw.pop('commissionNotionalTotalCollected')
        self.recovery.accepted(e['id'], raw['id'], BINDING)
        self.assertFalse(self.recovery.observe(e['id'], raw, BINDING))
        with self.assertRaises(LedgerError):
            self.recovery.claim_hedge(self.pid, BINDING)

    def test_missing_exchange_id_cannot_be_inferred_absent(self):
        e = self.entry()
        with self.assertRaises(LedgerError):
            self.recovery.accepted(e['id'], None, BINDING)
        with self.assertRaises(LedgerError):
            self.recovery.observe(e['id'], {}, BINDING)
        self.assertEqual(self.status()['state'], 'unresolved')

    def test_unknown_hedge_never_authorizes_excess_sale(self):
        self.hedge()
        with self.assertRaises(LedgerError):
            self.unwind()

    def test_fixed_hedge_deadline_is_not_shifted_by_late_entry_fill(self):
        e = self.entry()
        self.now += 6
        self.verify_us(e, 10)
        with self.assertRaises(LedgerError):
            self.recovery.claim_hedge(self.pid, BINDING)
        self.assertEqual(len(self.status()['commands']), 1)

    def test_hedge_miss_can_unwind_only_verified_pair_owned_excess(self):
        _, h = self.hedge()
        self.verify_kal(h, 0)
        u = self.unwind(available=100, size=100)
        self.assertEqual(u['plan']['count'], 10)
        self.assertEqual(u['payload']['intent'], 'ORDER_INTENT_SELL_LONG')
        self.verify_us(u, 10, fees='.17')
        self.assertEqual(self.status()['state'], 'held')
        self.assertEqual(self.status()['shared_exposure'], '9.71')

    def test_partial_hedge_unwinds_only_unhedged_four(self):
        _, h = self.hedge()
        self.verify_kal(h, 6)
        u = self.unwind(available=100, size=100)
        self.assertEqual(u['plan']['count'], 4)
        self.verify_us(u, 4, fees='.07')
        self.assertEqual(self.status()['state'], 'held')

    def test_partial_exit_requires_final_evidence_before_next_attempt(self):
        _, h = self.hedge()
        self.verify_kal(h, 6)
        u = self.unwind()
        with self.assertRaises(LedgerError):
            self.unwind()
        self.verify_us(u, 2, fees='.03')
        self.now += 1
        second = self.unwind()
        self.assertEqual(second['plan']['count'], 2)

    def test_duplicate_liquidity_and_decimal_alias_cannot_fill_remainder(self):
        _, h = self.hedge()
        self.verify_kal(h, 0)
        u = self.unwind(size=8)
        self.verify_us(u, 2, fees='.03')
        with self.assertRaises(LedgerError):
            self.unwind(size=8, bid='.4900')
        with self.assertRaises(LedgerError):
            self.unwind(size=100)

    def test_two_attempt_bound_leaves_unresolved_inventory(self):
        _, h = self.hedge()
        self.verify_kal(h, 0)
        for _ in range(2):
            u = self.unwind(size=8)
            self.verify_us(u, 2, fees='.03')
            self.now += 1
        with self.assertRaises(LedgerError):
            self.unwind()
        self.assertEqual(self.status()['state'], 'unresolved')
        self.assertEqual(D(self.status()['verified_unhedged_quantity']), D(6))
        self.assertIn('unresolved', problem(self.us.store.conn, D(1)))

    def test_loss_cap_includes_entry_exit_fees(self):
        _, h = self.hedge()
        self.verify_kal(h, 0)
        with self.assertRaises(LedgerError):
            self.unwind(bid='.40')
        self.assertEqual(len(self.status()['commands']), 2)

    def test_unclaimed_expired_hedge_can_be_abandoned_for_recovery(self):
        e = self.entry()
        self.verify_us(e, 10)
        with self.assertRaises(LedgerError):
            self.unwind()
        self.now += 6
        u = self.unwind()
        self.assertEqual(u['plan']['count'], 10)
        with self.assertRaises(LedgerError):
            self.recovery.claim_hedge(self.pid, BINDING)

    def test_future_carried_stale_and_wrong_book_exit_quotes_refused(self):
        _, h = self.hedge()
        self.verify_kal(h, 0)
        for change in ({'obs_ts': self.now+1}, {'refreshed': False}, {'obs_ts': self.now-7, 'req_ts': self.now-8},
                       {'book': 'kalshi'}, {'approx_time': True}):
            with self.subTest(change=change), self.assertRaises(LedgerError):
                self.unwind(**change)

    def test_wrong_inventory_side_refused(self):
        _, h = self.hedge()
        self.verify_kal(h, 0)
        with self.assertRaises(LedgerError):
            self.unwind(side='no')

    def test_no_side_sale_complements_long_price(self):
        e = self.entry()
        # Independently exercise the existing executable NO-sale helper, not
        # relabeling this YES pair's ownership or fabricated account inventory.
        from arb_engine.execution.us_pair_orders import USPairOrder
        p = USPairOrder(e['plan']['market_slug'], 'no', 2, D('.49'), action='sell').payload()
        self.assertEqual(p['intent'], 'ORDER_INTENT_SELL_SHORT')
        self.assertEqual(D(p['price']['value']), D('.51'))

    def test_quantity_regression_is_sticky(self):
        e = self.entry()
        raw = self.verify_us(e, 3)
        regressed = self.us_row(e, 2)
        self.assertFalse(self.recovery.observe(e['id'], regressed, BINDING))
        self.assertFalse(self.recovery.observe(e['id'], raw, BINDING))
        self.assertEqual(self.status()['commands'][0]['state'], 'contradicted')

    def test_money_regression_is_sticky(self):
        e = self.entry()
        raw = self.verify_us(e, 10)
        lower = copy.deepcopy(raw)
        lower['commissionNotionalTotalCollected']['value'] = '.16'
        self.assertFalse(self.recovery.observe(e['id'], lower, BINDING))
        self.assertFalse(self.recovery.observe(e['id'], raw, BINDING))

    def test_accepted_duplicate_receipt_cannot_undo_verified_state(self):
        e = self.entry()
        raw = self.verify_us(e, 10)
        self.recovery.accepted(e['id'], raw['id'], BINDING)
        self.assertEqual(self.status()['state'], 'entry-verified')

    def test_kalshi_truncated_or_missing_fills_never_verify(self):
        _, h = self.hedge()
        row, fills = self.kal_row(h, 6)
        self.recovery.accepted(h['id'], row['order_id'], BINDING)
        self.assertFalse(self.recovery.observe(h['id'], row, BINDING))
        self.assertFalse(self.recovery.observe(h['id'], row, BINDING, fills=fills, truncated=True))
        with self.assertRaises(LedgerError):
            self.unwind()

    def test_wrong_kalshi_book_scope_is_sticky(self):
        _, h = self.hedge()
        row, fills = self.kal_row(h, 6)
        self.recovery.accepted(h['id'], row['order_id'], BINDING)
        row['book_side'] = 'ask'
        self.assertFalse(self.recovery.observe(h['id'], row, BINDING, fills=fills, truncated=False))
        self.assertEqual(self.status()['commands'][-1]['state'], 'contradicted')

    def test_zero_fill_entry_never_creates_hedge_or_sale(self):
        e = self.entry()
        self.verify_us(e, 0, fees='0')
        self.assertEqual(self.status()['state'], 'missed')
        with self.assertRaises(LedgerError):
            self.recovery.claim_hedge(self.pid, BINDING)
        with self.assertRaises(LedgerError):
            self.unwind()

    def test_persisted_account_change_blocks_recovery(self):
        self.entry()
        self.us.store.conn.execute("UPDATE pm_us_meta SET value=? WHERE key='key_fp'", ('d'*64,))
        with self.assertRaises(LedgerError):
            self.status()

    def test_clock_regression_blocks_recovery(self):
        self.entry()
        self.now -= 1
        with self.assertRaises(LedgerError):
            self.status()

    def test_malformed_money_never_frees_parent_cash(self):
        e = self.entry()
        raw = self.us_row(e, 10)
        raw['commissionNotionalTotalCollected']['value'] = 'NaN'
        self.recovery.accepted(e['id'], raw['id'], BINDING)
        self.assertFalse(self.recovery.observe(e['id'], raw, BINDING))
        self.assertEqual(exposure(self.us.store.conn), D('9.71'))

    def test_failed_child_insert_rolls_back_claim_and_policy(self):
        self.us.store.conn.execute("CREATE TRIGGER no_claim BEFORE INSERT ON pair_recovery_orders BEGIN SELECT RAISE(ABORT,'fixture crash'); END")
        with self.assertRaises(Exception):
            self.entry()
        self.assertEqual(self.status()['commands'], [])
        self.assertEqual(self.approval.conn.execute('SELECT dispatch_claimed FROM permits').fetchone()[0], 0)

    def test_missing_leaves_does_not_erase_seen_fills(self):
        e = self.entry()
        raw = self.us_row(e, 3)
        self.recovery.accepted(e['id'], raw['id'], BINDING)
        raw.pop('leavesQuantity')
        self.assertFalse(self.recovery.observe(e['id'], raw, BINDING))
        self.assertEqual(D(self.status()['commands'][0]['seen']), D(3))
        self.assertFalse(self.recovery.observe(e['id'], self.us_row(e, 0, fees='0'), BINDING))
        self.assertEqual(self.status()['commands'][0]['state'], 'contradicted')

    def test_fractional_partial_fill_never_rounds_up_hedge(self):
        e = self.entry()
        self.verify_us(e, '.5', fees='.01')
        with self.assertRaises(LedgerError):
            self.recovery.claim_hedge(self.pid, BINDING)
        self.now += 6
        with self.assertRaises(LedgerError):
            self.unwind()
        self.assertEqual(self.status()['shared_exposure'], '9.71')

    def test_kalshi_fill_requires_explicit_market_book_scope(self):
        _, h = self.hedge()
        row, fills = self.kal_row(h, 6)
        self.recovery.accepted(h['id'], row['order_id'], BINDING)
        fills[0].pop('book_side')
        self.assertFalse(self.recovery.observe(h['id'], row, BINDING, fills=fills, truncated=False))
        self.assertEqual(self.status()['state'], 'unresolved')

    def test_inventory_from_before_final_fill_cannot_authorize_sale(self):
        _, h = self.hedge()
        self.now += 1
        self.verify_kal(h, 0)
        leg = json.loads(self.parent['plan'])['legs'][0]
        inv = InventoryEvidence(BINDING['polymarket_us_key'], leg['market'].split('#', 1)[0], 'yes',
                                D(10), D(10), NOW, NOW, NOW+6)
        q = {'market': leg['market'], 'book': 'polymarket_us', 'side': 'yes', 'refreshed': True,
             'req_ts': self.now, 'obs_ts': self.now, 'bid': '.49', 'size': 20}
        with self.assertRaises(LedgerError):
            self.recovery.claim_unwind(self.pid, BINDING, inv, q)

    def test_parent_local_expiry_cannot_release_a_claimed_entry(self):
        self.entry()
        self.now += 10
        with self.assertRaises(LedgerError):
            self.pairs.abandon_staged(self.pid)
        self.assertEqual(exposure(self.us.store.conn), D('9.71'))

    def test_outbox_payload_hedge_and_sale_limits_are_fixed(self):
        _, h = self.hedge()
        self.verify_kal(h, 0)
        u = self.unwind()
        before = self.status()['commands'][-1]['payload']
        self.now += 1
        self.assertEqual(self.status()['commands'][-1]['payload'], before)
        self.assertEqual(D(u['plan']['limit_price']), D('.49'))

    def test_concurrent_hedge_claims_create_only_one_command(self):
        e = self.entry()
        self.verify_us(e, 10)
        barrier = threading.Barrier(2)
        def claim():
            local = USOrderLedger(self.path, clock=self.clock)
            try:
                local.store.bind(Identity(BINDING['kalshi_key'], BINDING['kalshi_account']))
                recovery = PairRecovery(PairReservationLedger(local))
                barrier.wait(timeout=5)
                try:
                    recovery.claim_hedge(self.pid, BINDING)
                    return True
                except LedgerError:
                    return False
            finally:
                local.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(self.status()['commands']), 2)

    def test_command_order_is_role_stable_at_equal_timestamps(self):
        _, h = self.hedge()
        self.verify_kal(h, 0)
        self.unwind()
        self.assertEqual([r['role'] for r in self.status()['commands']], ['entry', 'hedge', 'unwind'])

    def test_missing_leaves_still_preserves_fee_lower_bound(self):
        e = self.entry()
        raw = self.us_row(e, 3, fees='.05')
        self.recovery.accepted(e['id'], raw['id'], BINDING)
        raw.pop('leavesQuantity')
        self.assertFalse(self.recovery.observe(e['id'], raw, BINDING))
        self.assertEqual(D(self.status()['commands'][0]['fee_seen']), D('.05'))
        self.assertFalse(self.recovery.observe(e['id'], self.us_row(e, 3, fees='.04'), BINDING))
        self.assertEqual(self.status()['commands'][0]['state'], 'contradicted')

    def test_kalshi_missing_remaining_still_preserves_money_lower_bounds(self):
        _, h = self.hedge()
        raw, fills = self.kal_row(h, 3)
        self.recovery.accepted(h['id'], raw['order_id'], BINDING)
        raw.pop('remaining_count_fp')
        self.assertFalse(self.recovery.observe(h['id'], raw, BINDING, fills=fills, truncated=False))
        self.assertEqual(D(self.status()['commands'][-1]['cash_seen']), D('1.2'))
        raw, fills = self.kal_row(h, 2)
        self.assertFalse(self.recovery.observe(h['id'], raw, BINDING, fills=fills, truncated=False))

    def test_conflicting_price_at_same_receipt_never_supplies_second_book(self):
        _, h = self.hedge()
        self.verify_kal(h, 0)
        u = self.unwind(size=8)
        self.verify_us(u, 2, fees='.03')
        with self.assertRaises(LedgerError):
            self.unwind(size=8, bid='.48')
        self.assertEqual(len(self.status()['commands']), 3)

    def test_excess_exit_commission_is_conflict_not_verified_release(self):
        _, h = self.hedge()
        self.verify_kal(h, 0)
        u = self.unwind()
        raw = self.us_row(u, 10, fees='.50')
        self.recovery.accepted(u['id'], raw['id'], BINDING)
        self.assertFalse(self.recovery.observe(u['id'], raw, BINDING))
        self.assertEqual(self.status()['commands'][-1]['state'], 'contradicted')
        self.assertEqual(self.status()['state'], 'unresolved')
        self.assertEqual(self.status()['shared_exposure'], '9.71')

    def test_slow_payload_creation_cannot_shift_hedge_deadline(self):
        e = self.entry()
        self.verify_us(e, 10)
        from arb_engine.execution.kalshi import OrderPlan
        original = OrderPlan.payload
        def slow(plan, *args, **kwargs):
            self.now += 6
            return original(plan, *args, **kwargs)
        with mock.patch.object(OrderPlan, 'payload', slow), self.assertRaises(LedgerError):
            self.recovery.claim_hedge(self.pid, BINDING)
        self.assertEqual(len(self.status()['commands']), 1)

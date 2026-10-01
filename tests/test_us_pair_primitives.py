"""Offline schema-shaped evidence, signing races and standing-claim fences.

Synthetic responses are NOT real account/fill evidence. No live orders/network.
"""
import base64
import copy
import json
import os
import tempfile
import threading
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from arb_engine.execution.standing_approval import ApprovalStore
from arb_engine.execution.us_pair_orders import USPairOrder, order_evidence, read_inventory
from arb_engine.execution.polymarket_us_ioc import MissingEvidence
from arb_engine.venues.polymarket_us_trading import API, PolymarketUSTradingClient, USAPIError
from tests.test_standing_approval import BINDING, books
from tests.test_auto_arb import NOW, RULE


SLUG = 'aec-nfl-chi-nyj-2026-09-30'


def order(plan, *, filled=2, price='.4', fees='.03', state='ORDER_STATE_CANCELED', left=0):
    p = plan.payload()
    return {**p, 'id': 'order-1', 'cumQuantity': filled, 'leavesQuantity': left, 'state': state,
            'avgPx': {'value': price, 'currency': 'USD'},
            'commissionNotionalTotalCollected': {'value': fees, 'currency': 'USD'}}


class PairOrderTests(unittest.TestCase):
    def plan(self, side='yes', action='buy', price='.4', count=5):
        return USPairOrder(SLUG, side, count, D(price), action=action)

    def test_four_intents_use_long_price_and_automatic_indicator(self):
        for side in ('yes', 'no'):
            for action in ('buy', 'sell'):
                p = self.plan(side, action).payload()
                self.assertEqual(p['intent'], 'ORDER_INTENT_' + action.upper() + ('_LONG' if side == 'yes' else '_SHORT'))
                self.assertEqual(D(p['price']['value']), D('.4') if side == 'yes' else D('.6'))
                self.assertEqual(p['manualOrderIndicator'], 'MANUAL_ORDER_INDICATOR_AUTOMATIC')
                self.assertEqual(p['tif'], 'TIME_IN_FORCE_IMMEDIATE_OR_CANCEL')

    def test_hand_calculated_entry_bound(self):
        self.assertEqual(self.plan().worst_cost(NOW), D('2.08'))

    def test_hand_calculated_sale_bound_no_proceeds_credit(self):
        self.assertEqual(self.plan(action='sell').worst_cost(NOW), D('.09'))

    def test_sale_fee_peak_is_over_improved_fill_interval(self):
        # 5*.0695*.5*.5=.086875 -> conservative rounded US fee .09.
        self.assertEqual(self.plan(action='sell', price='.1').fee_bound(NOW), D('.09'))
        self.assertLess(self.plan(price='.1').fee_bound(NOW), D('.09'))

    def test_sell_no_actual_proceeds_at_complement_price(self):
        p = self.plan('no', 'sell', '.4')
        evidence = order_evidence(p, order(p, price='.55'), 'order-1')
        self.assertEqual((evidence.cash, evidence.fees), (D('.90'), D('.03')))
        self.assertTrue(evidence.verified)

    def test_buy_no_actual_cost_at_complement_price(self):
        p = self.plan('no', price='.4')
        self.assertEqual(order_evidence(p, order(p, price='.65'), 'order-1').cash, D('.70'))

    def test_entry_and_exit_limit_violations_refused(self):
        for side, action, price in [('yes', 'buy', '.41'), ('yes', 'sell', '.39'),
                                    ('no', 'buy', '.59'), ('no', 'sell', '.61')]:
            p = self.plan(side, action)
            with self.subTest(side=side, action=action), self.assertRaises(ValueError):
                order_evidence(p, order(p, price=price), 'order-1')

    def test_create_or_nonterminal_snapshot_not_verified(self):
        p = self.plan()
        self.assertFalse(order_evidence(p, order(p), 'order-1', final_read=False).verified)
        self.assertFalse(order_evidence(p, order(p, state='ORDER_STATE_PENDING_CANCEL', left=3), 'order-1').verified)

    def test_missing_money_not_zero(self):
        p = self.plan()
        for field in ('avgPx', 'commissionNotionalTotalCollected'):
            raw = order(p)
            del raw[field]
            e = order_evidence(p, raw, 'order-1')
            self.assertFalse(e.verified)
            self.assertIsNone(e.cash if field == 'avgPx' else e.fees)

    def test_explicit_zero_fill_requires_explicit_zero_fee(self):
        p = self.plan()
        raw = order(p, filled=0, fees='0')
        del raw['avgPx']
        self.assertTrue(order_evidence(p, raw, 'order-1').verified)
        del raw['commissionNotionalTotalCollected']
        self.assertFalse(order_evidence(p, raw, 'order-1').verified)

    def test_changed_identity_terms_and_overriding_side_refused(self):
        p = self.plan()
        for field, value in [('id', 'other'), ('marketSlug', 'other'), ('quantity', 4),
                              ('intent', 'ORDER_INTENT_SELL_LONG'), ('tif', 'TIME_IN_FORCE_DAY'),
                              ('outcomeSide', 'OUTCOME_SIDE_NO'), ('action', 'ORDER_ACTION_SELL')]:
            raw = order(p)
            raw[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                order_evidence(p, raw, 'order-1')

    def test_impossible_counts_and_filled_partial_refused(self):
        p = self.plan()
        for qty, left, state in [(6, 0, 'ORDER_STATE_CANCELED'), (-1, 0, 'ORDER_STATE_CANCELED'),
                                  (2, 4, 'ORDER_STATE_CANCELED'), (2, 0, 'ORDER_STATE_FILLED')]:
            with self.subTest(qty=qty, left=left), self.assertRaises(ValueError):
                order_evidence(p, order(p, filled=qty, left=left, state=state), 'order-1')

    def test_currency_nonfinite_negative_and_zero_fill_fees_refused(self):
        p = self.plan()
        for price, fees in [('NaN', '.01'), ('.4', '-.01'), ('.4', 'Infinity')]:
            with self.subTest(price=price, fees=fees), self.assertRaises(ValueError):
                order_evidence(p, order(p, price=price, fees=fees), 'order-1')
        with self.assertRaises(ValueError):
            order_evidence(p, order(p, filled=0, fees='.01'), 'order-1')

    def test_invalid_actions_and_quantities_refused(self):
        for action, count in [('short', 5), ('buy', True), ('sell', 1.5)]:
            with self.subTest(action=action, count=count), self.assertRaises(ValueError):
                self.plan(action=action, count=count).payload()


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.pages = [{'positions': {SLUG: {'netPositionDecimal': '5', 'qtyAvailableDecimal': '3', 'expired': False,
                                           'marketMetadata': {'slug': SLUG}}}, 'eof': True}]
        self.client = mock.Mock(base_url=API, fingerprint='c'*64)
        self.client.positions_page.side_effect = lambda slug, cursor=None: copy.deepcopy(self.pages.pop(0))

    def read(self, side='yes', **kwargs):
        return read_inventory(self.client, SLUG, side, clock=lambda: self.now, **kwargs)

    def test_complete_long_uses_decimal_not_rounded_fields(self):
        self.pages[0]['positions'][SLUG].update(netPosition='50', qtyAvailable='50', netPositionDecimal='5.25')
        e = self.read()
        self.assertEqual((e.net, e.available, e.key_fp), (D('5.25'), D(3), 'c'*64))
        self.assertEqual((e.requested_at, e.observed_at, e.deadline), (NOW, NOW, NOW+6))

    def test_short_direction_from_signed_net_and_available_magnitude(self):
        for available in ('3', '-3'):
            self.setUp()
            self.pages[0]['positions'][SLUG].update(netPositionDecimal='-5', qtyAvailableDecimal=available)
            self.assertEqual(self.read('no').available, D(3))

    def test_empty_complete_map_proves_zero(self):
        self.pages = [{'positions': {}, 'eof': True}]
        self.assertEqual(self.read().available, D(0))

    def test_all_pages_required_and_duplicate_identical_allowed(self):
        self.pages = [{**copy.deepcopy(self.pages[0]), 'eof': False, 'nextCursor': 'cursor-1'}, self.pages[0]]
        self.assertEqual(self.read().available, D(3))
        self.assertEqual(self.client.positions_page.call_args_list, [mock.call(SLUG, cursor=None), mock.call(SLUG, cursor='cursor-1')])

    def test_conflicting_duplicate_position_refused(self):
        first = {**copy.deepcopy(self.pages[0]), 'eof': False, 'nextCursor': 'cursor-1'}
        self.pages[0]['positions'][SLUG]['qtyAvailableDecimal'] = '2'
        self.pages.insert(0, first)
        with self.assertRaises(ValueError):
            self.read()

    def test_missing_map_eof_wrong_shape_not_empty(self):
        for raw in ({'eof': True}, {'positions': {}}, {'positions': [], 'eof': True}, {'positions': {}, 'eof': 1}):
            self.pages = [raw]
            with self.subTest(raw=raw), self.assertRaises(MissingEvidence):
                self.read()

    def test_cycle_and_page_bound_refused(self):
        page = {'positions': {}, 'eof': False, 'nextCursor': 'loop'}
        self.pages = [page, page]
        with self.assertRaises(MissingEvidence):
            self.read()
        self.pages = [page]
        with self.assertRaises(MissingEvidence):
            self.read(max_pages=1)

    def test_partial_page_error_never_returns_inventory(self):
        self.client.positions_page.side_effect = [{'positions': {}, 'eof': False, 'nextCursor': '1'}, TimeoutError()]
        with self.assertRaises(TimeoutError):
            self.read()

    def test_opposite_direction_available_excess_or_wrong_scope_refused(self):
        for change in ({'netPositionDecimal': '-5'}, {'qtyAvailableDecimal': '6'},
                       {'marketMetadata': {'slug': 'other'}}):
            self.setUp()
            self.pages[0]['positions'][SLUG].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.read()
        self.pages = [{'positions': {'other': {}}, 'eof': True}]
        with self.assertRaises(ValueError):
            self.read()

    def test_missing_decimal_availability_expiry_not_inventory(self):
        for change in ({'qtyAvailableDecimal': None}, {'netPositionDecimal': None}, {'expired': True}, {'expired': None}):
            self.setUp()
            self.pages[0]['positions'][SLUG].update(change)
            with self.subTest(change=change), self.assertRaises(MissingEvidence):
                self.read()

    def test_slow_receipt_and_clock_regression_refused(self):
        for delta in (6, -1):
            self.setUp()
            original = self.client.positions_page.side_effect
            def page(slug, cursor=None):
                self.now += delta
                return original(slug, cursor)
            self.client.positions_page.side_effect = page
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                self.read()

    def test_account_rotation_during_read_refused(self):
        original = self.client.positions_page.side_effect
        def page(slug, cursor=None):
            self.client.fingerprint = 'd'*64
            return original(slug, cursor)
        self.client.positions_page.side_effect = page
        with self.assertRaises(ValueError):
            self.read()


class TransportDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.requests = []
        self.values = {'POLYMARKET_KEY_ID': '00000000-0000-0000-0000-000000000001',
                       'POLYMARKET_SECRET_KEY': base64.b64encode(bytes(32)).decode()}
        self.signer = mock.Mock(side_effect=lambda seed, message: message)
        self.client = PolymarketUSTradingClient(self.values, clock=lambda: self.now, signer=self.signer,
                                               transport=lambda request: self.requests.append(request) or {})
        self.flags = mock.patch.dict(os.environ, {'ARB_LIVE_TRADING': '1', 'POLYMARKET_US_LIVE_TRADING': '1'})
        self.flags.start()
        self.addCleanup(self.flags.stop)

    def test_expiry_during_signing_prevents_transport(self):
        def sign(seed, message):
            self.now += 6
            return message
        self.signer.side_effect = sign
        with self.assertRaises(USAPIError):
            self.client._create({}, confirm=True, not_after=NOW+6)
        self.assertEqual(self.requests, [])

    def test_clock_regression_during_signing_prevents_transport(self):
        self.signer.side_effect = lambda seed, message: setattr(self, 'now', NOW-1) or message
        with self.assertRaises(USAPIError):
            self.client._create({}, confirm=True, not_after=NOW+6)
        self.assertEqual(self.requests, [])

    def test_flag_removed_during_signing_prevents_transport(self):
        def sign(seed, message):
            os.environ.pop('ARB_LIVE_TRADING', None)
            return message
        self.signer.side_effect = sign
        with self.assertRaises(USAPIError):
            self.client._create({}, confirm=True, not_after=NOW+6)
        self.assertEqual(self.requests, [])

    def test_invalid_deadline_before_signing_refused(self):
        for deadline in (True, float('nan'), float('inf'), NOW, 'soon'):
            with self.subTest(deadline=deadline), self.assertRaises(USAPIError):
                self.client._create({}, confirm=True, not_after=deadline)
        self.signer.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_identity_changed_during_signing_prevents_transport(self):
        for attr, value in [('base_url', 'https://example.com'), ('_key', 'different'),
                             ('_seed', b'changed'), ('fingerprint', 'd'*64)]:
            original = getattr(self.client, attr)
            def sign(seed, message):
                setattr(self.client, attr, value)
                return message
            self.signer.side_effect = sign
            with self.subTest(attr=attr), self.assertRaises(USAPIError):
                self.client._create({}, confirm=True, not_after=NOW+6)
            self.assertEqual(self.requests, [])
            setattr(self.client, attr, original)

    def test_method_path_confusion_refused(self):
        for method, path in [('POST', '/v1/account/balances'), ('GET', '/v1/orders'),
                              ('GET', '/v1/order/id/cancel'), ('POST', '/v1/portfolio/positions')]:
            with self.subTest(method=method, path=path), self.assertRaises(USAPIError):
                self.client._request(method, path, confirm=True)
        self.assertEqual(self.requests, [])

    def test_positions_query_encoded_but_signature_uses_bare_path(self):
        self.client.positions_page(SLUG, cursor='a+b/=?')
        request = self.requests[0]
        self.assertIn('cursor=a%2Bb%2F%3D%3F', request.full_url)
        self.assertEqual(base64.b64decode(request.get_header('X-pm-signature')), f'{int(NOW*1000)}GET/v1/portfolio/positions'.encode())


class DispatchGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'approval.sqlite3'
        self.now = NOW
        self.store = ApprovalStore(self.path, clock=lambda: self.now)
        self.addCleanup(self.store.close)
        for patch in (mock.patch('arb_engine.quant.us_arbitrage.rule_for_quote', return_value=RULE),
                      mock.patch('arb_engine.quant.us_arbitrage.pair_flags', return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)
        self.store.arm(BINDING)
        self.grant = self.store.approve(books(), BINDING, contracts=10)

    def consume(self):
        return self.store.consume(self.grant['permit_id'], BINDING, self.grant['plan_digest'])

    def guard(self, binding=BINDING, digest=None):
        return self.store.dispatch_guard(self.grant['permit_id'], binding, digest or self.grant['plan_digest'])

    def test_unconsumed_permission_cannot_dispatch(self):
        with self.assertRaises(ValueError), self.guard():
            self.fail('guard must not enter')

    def test_single_use_dispatch_persists_restart(self):
        self.consume()
        with self.guard() as proof:
            self.assertEqual(proof['accounts'], BINDING)
            self.assertEqual(proof['plan'], self.grant['plan'])
        other = ApprovalStore(self.path, clock=lambda: self.now)
        try:
            with self.assertRaises(ValueError), other.dispatch_guard(self.grant['permit_id'], BINDING, self.grant['plan_digest']):
                self.fail('duplicate dispatch entered')
        finally:
            other.close()

    def test_revocation_after_consume_blocks_dispatch(self):
        self.consume()
        self.store.revoke()
        with self.assertRaises(ValueError), self.guard():
            self.fail('revoked dispatch entered')

    def test_rearm_invalidates_consumed_generation(self):
        self.consume()
        self.store.arm(BINDING)
        with self.assertRaises(ValueError), self.guard():
            self.fail('old generation entered')

    def test_wrong_account_digest_and_expiry_refused(self):
        self.consume()
        for binding, digest in [({**BINDING, 'polymarket_us_key': 'd'*64}, None), (BINDING, 'wrong')]:
            with self.subTest(binding=binding, digest=digest), self.assertRaises(ValueError), self.guard(binding, digest):
                self.fail('mismatching guard entered')
        self.now += 6
        with self.assertRaises(ValueError), self.guard():
            self.fail('expired dispatch entered')

    def test_guard_error_rolls_back_only_permission_claim(self):
        self.consume()
        with self.assertRaises(RuntimeError), self.guard():
            raise RuntimeError('production claim refused')
        self.assertEqual(self.store.conn.execute('SELECT dispatch_claimed FROM permits').fetchone()[0], 0)

    def test_revocation_serialized_behind_durable_claim(self):
        self.consume()
        started, finished = threading.Event(), threading.Event()
        errors = []
        def revoke():
            try:
                other = ApprovalStore(self.path, clock=lambda: NOW)
                started.set()
                other.revoke()
                other.close()
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()
        with self.guard():
            thread = threading.Thread(target=revoke)
            thread.start()
            # Constructor may wait for schema checks behind this transaction.
            self.assertFalse(finished.wait(.05))
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.store.conn.execute('SELECT active FROM approval').fetchone()[0], 0)
        self.assertEqual(self.store.conn.execute('SELECT dispatch_claimed FROM permits').fetchone()[0], 1)

    def test_expiry_inside_guard_does_not_commit_permission_marker(self):
        self.consume()
        with self.assertRaises(ValueError), self.guard():
            self.now += 6
        self.assertEqual(self.store.conn.execute('SELECT dispatch_claimed FROM permits').fetchone()[0], 0)

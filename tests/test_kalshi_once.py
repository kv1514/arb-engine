"""Single-attempt transport regressions; fake signatures/accounts only."""
import http.client
import json
import os
import tempfile
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from arb_engine.execution.kalshi import KalshiExecutor
from arb_engine.execution.kalshi_once import KalshiOnceError, _NoRedirect, _transport
from arb_engine.execution.ledger import OrderLedger, refusal_hint
from tests.test_order_ledger import Clock, FakeKalshi, PROD_URL, TICKER


class OnceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'ledger.sqlite3')
        self.clock = Clock()
        self.client = FakeKalshi(self.clock)
        self.client.env, self.client.base_url = 'prod', PROD_URL
        self.client._auth_headers = lambda *args: {'KALSHI-ACCESS-KEY': self.client.api_key,
                                                   'KALSHI-ACCESS-SIGNATURE': 'fake-secret'}
        self.calls = []
        self.reply = {'order_id': 'fixture-order'}
        self.transport = self.send
        self.ex = KalshiExecutor(self.client, ledger_path=self.path, clock=self.clock,
                                 transport=lambda request: self.transport(request))
        self.plan = self.ex.plan(TICKER, 'buy', 'no', 2, .4, time_in_force='immediate_or_cancel')
        self.env = mock.patch.dict(os.environ, {'ARB_LIVE_TRADING': '1'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.ledger = OrderLedger.for_client(self.client, self.path, clock=self.clock)
        self.addCleanup(self.ledger.close)
        self.reserve()

    def reserve(self):
        got = self.ledger.reserve(strategy='manual', ticker=TICKER, side='no', count=2,
                                  limit_price='.4', fee_multiplier=1,
                                  tif='immediate_or_cancel')
        self.assertTrue(got.ok, got.reason)
        self.iid = got.intent_id
        self.plan.client_order_id = got.client_order_id

    def send(self, request):
        self.calls.append(request)
        return self.reply

    def execute(self, **kw):
        return self.ex.execute(self.plan, confirm=True, one_send=True, not_after=1006, **kw)

    def test_dry_run_never_claims_or_signs(self):
        self.client._auth_headers = mock.Mock(side_effect=AssertionError('must not sign'))
        self.assertTrue(self.ex.execute(self.plan, one_send=True)['status'].startswith('DRY_RUN'))
        self.client._auth_headers.assert_not_called()
        self.assertFalse(self.calls)
        self.assertFalse(self.ledger.conn.execute("SELECT 1 FROM sqlite_master WHERE name='production_sends'").fetchone())

    def test_truthy_confirm_is_not_confirmation(self):
        for value in (1, 'true', [True]):
            self.assertTrue(self.ex.execute(self.plan, confirm=value, one_send=True,
                                            not_after=1006)['status'].startswith('DRY_RUN'))
        self.assertFalse(self.calls)

    def test_exact_endpoint_payload_and_never_legacy_create(self):
        self.client.create_order = mock.Mock(side_effect=AssertionError('legacy called'))
        result = self.execute()
        self.assertEqual(result['status'], 'SUBMITTED')
        self.assertEqual(self.ledger.get(self.iid)['state'], 'accepted')
        self.assertEqual(len(self.calls), 1)
        r = self.calls[0]
        self.assertEqual(r.full_url, PROD_URL + '/portfolio/events/orders')
        self.assertEqual(json.loads(r.data), self.plan.payload())
        self.assertEqual(json.loads(r.data)['price'], '0.6000')
        self.client.create_order.assert_not_called()

    def test_claim_is_durable_before_transport(self):
        def send(request):
            other = OrderLedger.for_client(self.client, self.path, clock=self.clock)
            try:
                self.assertTrue(other.conn.execute('SELECT 1 FROM production_sends WHERE client_order_id=?',
                                                   (self.plan.client_order_id,)).fetchone())
            finally:
                other.close()
            return self.reply
        self.transport = send
        self.assertEqual(self.execute()['status'], 'SUBMITTED')

    def test_restart_cannot_resend(self):
        self.execute()
        restarted = KalshiExecutor(self.client, ledger_path=self.path, clock=self.clock, transport=self.send)
        self.assertTrue(restarted.execute(self.plan, confirm=True, one_send=True,
                                          not_after=1006)['status'].startswith('BLOCKED'))
        self.assertEqual(len(self.calls), 1)

    def test_concurrent_claims_send_once(self):
        def attempt(_):
            return self.execute()['status']
        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(attempt, range(6)))
        self.assertEqual(statuses.count('SUBMITTED'), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(s == 'SUBMITTED' or s.startswith('BLOCKED') for s in statuses))

    def test_default_path_is_frozen_before_account_callback(self):
        self.ex.ledger_path = None
        alternative = str(Path(self.temp.name) / 'wrong.sqlite3')
        path = [self.path]
        def account():
            path[0] = alternative
            return self.client.account
        self.client.communications_id = account
        with mock.patch('arb_engine.execution.ledger.default_path', side_effect=lambda env: path[0]):
            self.assertEqual(self.execute()['status'], 'SUBMITTED')
        self.assertFalse(Path(alternative).exists())
        self.assertEqual(self.ledger.get(self.iid)['state'], 'accepted')

    def test_timeout_is_one_attempt_and_hold_survives(self):
        def fail(request):
            self.calls.append(request)
            raise TimeoutError('secret data')
        self.transport = fail
        with self.assertRaises(KalshiOnceError) as error:
            self.execute()
        self.assertNotIn('secret', str(error.exception))
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.execute()['status'].startswith('BLOCKED'))
        self.assertEqual(self.ledger.get(self.iid)['state'], 'ambiguous')

    def test_incomplete_read_does_not_fall_back(self):
        def fail(request):
            self.calls.append(request)
            raise http.client.IncompleteRead(b'partial')
        self.transport = fail
        with self.assertRaises(KalshiOnceError):
            self.execute()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.client.base_url, PROD_URL)

    def test_http_error_is_not_absence_release_evidence(self):
        def fail(request):
            self.calls.append(request)
            raise urllib.error.HTTPError(request.full_url, 409, 'secret', {}, None)
        self.transport = fail
        with self.assertRaises(KalshiOnceError) as error:
            self.execute()
        self.assertIsNone(refusal_hint(error.exception))
        self.assertEqual(len(self.calls), 1)

    def test_missing_invalid_or_expired_deadline_does_not_claim(self):
        for deadline in (None, True, float('nan'), float('inf'), 1000, 999):
            result = self.ex.execute(self.plan, confirm=True, one_send=True, not_after=deadline)
            self.assertTrue(result['status'].startswith('BLOCKED'))
        self.assertFalse(self.calls)
        self.assertFalse(self.ledger.conn.execute("SELECT 1 FROM sqlite_master WHERE name='production_sends'").fetchone())

    def test_expiry_during_account_read_refuses_transport(self):
        self.client.communications_id = lambda: (setattr(self.clock, 't', 1007) or self.client.account)
        with self.assertRaises(KalshiOnceError):
            self.execute()
        self.assertFalse(self.calls)

    def test_key_changed_during_account_read_refuses_transport(self):
        self.client.communications_id = lambda: (setattr(self.client, 'api_key', 'rotated') or self.client.account)
        with self.assertRaises(KalshiOnceError):
            self.execute()
        self.assertFalse(self.calls)

    def test_order_changed_during_account_read_refuses_transport(self):
        self.client.communications_id = lambda: (setattr(self.plan, 'note', 'different terms') or self.client.account)
        with self.assertRaises(KalshiOnceError):
            self.execute()
        self.assertFalse(self.calls)

    def test_expiry_during_signing_refuses_transport_and_resend(self):
        self.client._auth_headers = lambda *args: (setattr(self.clock, 't', 1007) or {})
        with self.assertRaises(KalshiOnceError):
            self.execute()
        self.assertFalse(self.calls)
        self.clock.t = 1001
        self.assertTrue(self.execute()['status'].startswith('BLOCKED'))

    def test_clock_regression_during_signing_refused(self):
        self.client._auth_headers = lambda *args: (setattr(self.clock, 't', 999) or {})
        with self.assertRaises(KalshiOnceError):
            self.execute()
        self.assertFalse(self.calls)

    def test_gate_removed_during_signing_refused(self):
        def sign(*args):
            os.environ.pop('ARB_LIVE_TRADING')
            return {}
        self.client._auth_headers = sign
        with self.assertRaises(KalshiOnceError):
            self.execute()
        self.assertFalse(self.calls)

    def changed_signer(self, attr, value):
        self.client._auth_headers = lambda *args: (setattr(self.client, attr, value) or {})
        with self.assertRaises(KalshiOnceError):
            self.execute()
        self.assertFalse(self.calls)
        self.assertEqual(self.ledger.get(self.iid)['state'], 'ambiguous')

    def test_key_change_during_signing_refused(self):
        self.changed_signer('api_key', 'different')

    def test_host_change_during_signing_refused(self):
        self.changed_signer('base_url', 'https://example.com/trade-api/v2')

    def test_key_path_change_during_signing_refused(self):
        self.changed_signer('private_key_path', 'different')

    def test_changed_reservation_terms_refused(self):
        self.plan.price = .41
        self.assertTrue(self.execute()['status'].startswith('BLOCKED'))
        self.assertFalse(self.calls)

    def test_unreserved_demo_order_also_refused(self):
        demo = FakeKalshi(self.clock)
        ex = KalshiExecutor(demo, ledger_path=str(Path(self.temp.name) / 'demo.sqlite3'),
                            clock=self.clock, transport=self.send)
        self.assertTrue(ex.execute(self.plan, confirm=True, one_send=True, not_after=1006)['status'].startswith('BLOCKED'))
        self.assertFalse(self.calls)

    def test_resting_order_not_accepted_by_once_transport(self):
        self.plan.time_in_force = 'good_till_canceled'
        self.assertTrue(self.execute()['status'].startswith('BLOCKED'))
        self.assertFalse(self.calls)

    def test_malformed_response_retains_claim(self):
        self.reply = []
        with self.assertRaises(KalshiOnceError):
            self.execute()
        self.assertTrue(self.execute()['status'].startswith('BLOCKED'))

    def test_missing_order_id_is_not_reported_as_accepted(self):
        self.reply = {}
        response = self.execute()
        self.assertTrue(response['status'].startswith('UNKNOWN'))
        self.assertEqual(response['ledger_state'], 'ambiguous')
        self.assertEqual(self.ledger.get(self.iid)['state'], 'ambiguous')
        self.assertTrue(self.execute()['status'].startswith('BLOCKED'))
        self.assertEqual(len(self.calls), 1)

    def test_acceptance_is_not_verified_fill_inventory(self):
        response = self.execute()
        self.assertEqual(response['ledger_state'], 'accepted')
        row = self.ledger.get(self.iid)
        self.assertNotIn(row['fill_state'], ('verified', 'corrected'))
        self.assertEqual(row['max_cost'], '0.84')

    def test_real_transport_refuses_redirect(self):
        with self.assertRaises(KalshiOnceError):
            _NoRedirect().redirect_request(None, None, 307, None, None, 'https://example.com')

    def test_real_transport_never_retries_incomplete_read(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.side_effect = http.client.IncompleteRead(b'x')
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch('arb_engine.execution.kalshi_once.urllib.request.build_opener', return_value=opener):
            with self.assertRaises(http.client.IncompleteRead):
                _transport(mock.Mock())
        self.assertEqual(opener.open.call_count, 1)

    def test_real_transport_bounded_response(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'x' * 1_000_001
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch('arb_engine.execution.kalshi_once.urllib.request.build_opener', return_value=opener):
            with self.assertRaises(KalshiOnceError):
                _transport(mock.Mock())
        response.__enter__.return_value.read.assert_called_once_with(1_000_001)

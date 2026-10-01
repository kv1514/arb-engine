"""Standing permission is account-bound policy, never an order transport.

All inputs are synthetic, all SQLite files are temporary, and account reads are
mocked. Successful permission checks do not establish executable/profitable arbs.
"""
import copy
import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stdout
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arb_engine.cli_plugins import trade_approval as cli
from arb_engine.execution import standing_approval as approval
from tests.test_auto_arb import EVENT, NOW, RULE, quote, snaps


BINDING = {'kalshi_key': 'key:' + 'a' * 32,
           'kalshi_account': 'account:' + 'b' * 32,
           'polymarket_us_key': 'c' * 64}


def books(*, event=EVENT, count=40):
    rows = snaps(quote('kalshi', size=count), quote('polymarket_us', size=count))
    if event != EVENT:
        for snapshot in rows:
            info = snapshot.events.pop(EVENT)
            info.event_key = event
            snapshot.events[event] = info
            for row in snapshot.quotes:
                row.event_key = event
    return rows


class StandingApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'approval.sqlite3'
        self.now = NOW
        self.store = approval.ApprovalStore(self.path, clock=lambda: self.now)
        self.addCleanup(lambda: self.store.close())
        self.rules = mock.patch('arb_engine.quant.us_arbitrage.rule_for_quote', return_value=RULE)
        self.flags = mock.patch('arb_engine.quant.us_arbitrage.pair_flags', return_value=[])
        self.rules.start()
        self.flags.start()
        self.addCleanup(self.rules.stop)
        self.addCleanup(self.flags.stop)
        # Any accidentally introduced account transport makes these offline tests fail.
        self.network = mock.patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def arm(self, **kwargs):
        return self.store.arm(BINDING, **kwargs)

    def grant(self, *, count=10, event=EVENT):
        return self.store.approve(books(event=event, count=max(40, count * 2)), BINDING, contracts=count)

    def test_unarmed_status_and_permission_do_not_enable_execution(self):
        status = approval.read_status(self.path, now=self.now)
        self.assertEqual(status['status'], 'UNARMED')
        self.assertFalse(status['approval_active'])
        self.assertFalse(status['execution_enabled'])
        with self.assertRaises(ValueError):
            self.grant()

    def test_arm_and_readonly_status_expose_no_account_identifiers(self):
        status = self.arm()
        self.assertEqual(status['status'], 'ARMED')
        self.assertTrue(status['approval_active'])
        self.assertFalse(status['execution_enabled'])
        self.assertEqual(status['expires'], NOW + 6 * 3600)
        self.assertEqual(approval.read_status(self.path, now=NOW), status)
        for value in BINDING.values():
            self.assertNotIn(value, json.dumps(status))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_revoke_stops_new_and_previously_issued_permission(self):
        self.arm()
        grant = self.grant()
        result = self.store.revoke()
        self.assertEqual(result['status'], 'REVOKED')
        self.assertIn('not cancellation', result['note'])
        self.assertEqual(approval.read_status(self.path, now=NOW)['status'], 'REVOKED')
        with self.assertRaises(ValueError):
            self.grant(event='nfl:BUF|DET:2026-09-30')
        with self.assertRaises(ValueError):
            self.store.consume(grant['permit_id'], BINDING, grant['plan_digest'])

    def test_exact_expiry_boundary_is_inactive(self):
        self.arm(hours='0.001')
        self.now = NOW + 3.6
        status = approval.read_status(self.path, now=self.now)
        self.assertEqual(status['status'], 'EXPIRED_OR_CLOCK_REGRESSED')
        with self.assertRaises(ValueError):
            self.grant()

    def test_invalid_duration_and_nonfinite_time_fail_closed(self):
        for hours in (0, -1, 25, True, 'NaN', 'Infinity'):
            with self.subTest(hours=hours), self.assertRaises((ValueError, TypeError)):
                self.arm(hours=hours)
        for now in (float('nan'), float('inf'), -1):
            with self.subTest(now=now), self.assertRaises((ValueError, TypeError)):
                approval.read_status(self.path, now=now)
        self.assertEqual(approval.read_status(self.path, now=NOW)['status'], 'UNARMED')

    def test_clock_regression_blocks_permission_and_rearm(self):
        self.arm()
        self.now -= 1
        self.assertEqual(approval.read_status(self.path, now=self.now)['status'], 'EXPIRED_OR_CLOCK_REGRESSED')
        with self.assertRaises(ValueError):
            self.grant()
        with self.assertRaises(ValueError):
            self.arm()

    def test_permission_updates_clock_high_water_mark(self):
        self.arm()
        self.now += 1
        self.assertEqual(self.grant()['status'], 'APPROVED')
        self.now -= .5
        with self.assertRaises(ValueError):
            self.grant(event='nfl:BUF|DET:2026-09-30')

    def test_authenticated_account_and_both_keys_must_match(self):
        self.arm()
        for key, replacement in (('kalshi_key', 'key:' + 'd' * 32),
                                 ('kalshi_account', 'account:' + 'e' * 32),
                                 ('polymarket_us_key', 'f' * 64)):
            binding = {**BINDING, key: replacement}
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.store.approve(books(), binding)
            with self.subTest(rearm=key), self.assertRaises(ValueError):
                self.store.arm(binding)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM permits').fetchone()[0], 0)

    def test_incomplete_malformed_or_raw_identifier_bindings_refused(self):
        invalid = [None, {}, {**BINDING, 'unexpected': 'x'},
                   {**BINDING, 'kalshi_account': 'actual-account-id'},
                   {**BINDING, 'polymarket_us_key': True},
                   {**BINDING, 'kalshi_key': 'key:' + 'A' * 32}]
        for binding in invalid:
            with self.subTest(binding=binding), self.assertRaises(ValueError):
                self.store.arm(binding)

    def test_changed_spec_invalidates_status_approve_and_consume(self):
        self.arm()
        grant = self.grant()
        with mock.patch.object(approval, 'spec_hash', return_value='changed'):
            self.assertEqual(approval.read_status(self.path, now=NOW)['status'], 'SPEC_CHANGED')
            with self.assertRaises(ValueError):
                self.grant(event='nfl:BUF|DET:2026-09-30')
            with self.assertRaises(ValueError):
                self.store.consume(grant['permit_id'], BINDING, grant['plan_digest'])

    def test_changed_profile_invalidates_permission(self):
        self.arm()
        with mock.patch.object(approval, 'PROFILE', {**approval.PROFILE, 'leg_cap_fees_included': '100'}):
            self.assertEqual(approval.read_status(self.path, now=NOW)['status'], 'SPEC_CHANGED')
            with self.assertRaises(ValueError):
                self.grant()

    def test_default_registry_blocks_conditional_pair_without_overrides(self):
        self.arm()
        self.rules.stop()
        self.flags.stop()
        result = self.grant()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['orders_submitted'], 0)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM permits').fetchone()[0], 0)

    def test_stale_future_carried_approx_and_failed_quotes_refused(self):
        self.arm()
        bad = [quote('polymarket_us', t=NOW-7), quote('polymarket_us', t=NOW+1),
               quote('polymarket_us', refreshed=0), quote('polymarket_us', approx_time=True),
               quote('polymarket_us', arb_ineligible='failed'),
               quote('polymarket_us', req_ts=NOW+1), quote('polymarket_us', obs_ts=NOW+1)]
        for row in bad:
            with self.subTest(meta=row.meta, ts=row.ts):
                result = self.store.approve(snaps(quote('kalshi'), row), BINDING)
                self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM permits').fetchone()[0], 0)

    def test_missing_depth_and_snapshot_failure_refused(self):
        self.arm()
        row = quote('polymarket_us')
        row.ask_size = None
        self.assertEqual(self.store.approve(snaps(quote('kalshi'), row), BINDING)['status'], 'BLOCKED')
        data = books()
        data[0].errors.append('unfinished book fetch')
        self.assertEqual(self.store.approve(data, BINDING)['status'], 'BLOCKED')

    def test_in_play_same_book_and_identity_disagreement_refused(self):
        self.arm()
        for kind in ('in_play', 'same_book', 'identity'):
            data = books()
            if kind == 'in_play':
                data[0].events[EVENT].in_play = True
            elif kind == 'same_book':
                data[1].quotes[0].book_id = 'kalshi'
            else:
                data[0].events[EVENT].market_type = 'spread'
            with self.subTest(kind=kind):
                self.assertEqual(self.store.approve(data, BINDING)['status'], 'BLOCKED')

    def test_fee_inclusive_permission_holds_both_entries_and_unwind_commission(self):
        self.arm()
        grant = self.grant(count=10)
        self.assertEqual(grant['status'], 'APPROVED')
        held = approval.read_status(self.path, now=NOW)['permission_cash_held']
        # US entry .5*10+.17 plus two unwind .17 bounds; Kalshi .4*10+.02*10.
        self.assertEqual(D(held['polymarket_us']), D('5.51'))
        self.assertEqual(D(held['kalshi']), D('4.20'))
        self.assertEqual(D(held['total']), D('9.71'))
        self.assertFalse(grant['execution_enabled'])
        self.assertEqual(grant['orders_submitted'], 0)

    def test_fees_reduce_contracts_below_raw_twentyfive_dollar_purchase(self):
        self.arm()
        grant = self.grant(count=100)
        self.assertEqual(grant['status'], 'APPROVED')
        self.assertEqual(grant['plan']['count'], 45)
        held = approval.read_status(self.path, now=NOW)['permission_cash_held']
        self.assertEqual(D(held['polymarket_us']), D('24.84'))
        self.assertLessEqual(D(held['kalshi']), D(25))
        self.assertLessEqual(D(held['total']), D(50))

    def test_permission_ceiling_counts_existing_events_not_just_current_leg(self):
        self.arm()
        self.assertEqual(self.grant(count=40)['status'], 'APPROVED')
        self.assertEqual(self.grant(count=6, event='nfl:BUF|DET:2026-09-30')['status'], 'BLOCKED')
        grant = self.grant(count=5, event='nfl:BUF|DET:2026-09-30')
        self.assertEqual(grant['status'], 'APPROVED')
        held = approval.read_status(self.path, now=NOW)['permission_cash_held']
        self.assertEqual(D(held['polymarket_us']), D('24.87'))
        self.assertLessEqual(D(held['total']), D(50))

    def test_duplicate_event_does_not_restore_or_add_permission_cash(self):
        self.arm()
        self.grant()
        before = approval.read_status(self.path, now=NOW)['permission_cash_held']
        self.assertEqual(self.grant()['status'], 'BLOCKED')
        self.assertEqual(approval.read_status(self.path, now=NOW)['permission_cash_held'], before)

    def test_rearm_does_not_reset_cash_or_make_old_generation_consumable(self):
        self.arm()
        grant = self.grant(count=40)
        held = approval.read_status(self.path, now=NOW)['permission_cash_held']
        self.store.revoke()
        self.arm()
        self.assertEqual(approval.read_status(self.path, now=NOW)['permission_cash_held'], held)
        self.assertEqual(self.grant(count=6, event='nfl:BUF|DET:2026-09-30')['status'], 'BLOCKED')
        with self.assertRaises(ValueError):
            self.store.consume(grant['permit_id'], BINDING, grant['plan_digest'])

    def test_restart_keeps_permission_cash_and_single_use_claim(self):
        self.arm()
        grant = self.grant()
        self.store.consume(grant['permit_id'], BINDING, grant['plan_digest'])
        held = approval.read_status(self.path, now=NOW)['permission_cash_held']
        self.store.close()
        self.store = approval.ApprovalStore(self.path, clock=lambda: self.now)
        self.assertEqual(approval.read_status(self.path, now=NOW)['permission_cash_held'], held)
        with self.assertRaises(ValueError):
            self.store.consume(grant['permit_id'], BINDING, grant['plan_digest'])

    def test_consume_is_exact_account_plan_and_single_use_without_orders(self):
        self.arm()
        grant = self.grant()
        with self.assertRaises(ValueError):
            self.store.consume(grant['permit_id'], BINDING, 'wrong-plan')
        with self.assertRaises(ValueError):
            self.store.consume(grant['permit_id'], {**BINDING, 'kalshi_key': 'key:' + 'd' * 32}, grant['plan_digest'])
        consumed = self.store.consume(grant['permit_id'], BINDING, grant['plan_digest'])
        self.assertEqual(consumed['status'], 'PERMISSION_CONSUMED')
        self.assertEqual(consumed['orders_submitted'], 0)
        self.assertEqual(approval.digest(consumed['plan']), grant['plan_digest'])
        with self.assertRaises(ValueError):
            self.store.consume(grant['permit_id'], BINDING, grant['plan_digest'])

    def test_consume_deadline_is_decision_time_not_later_consumer_time(self):
        self.arm()
        grant = self.grant()
        self.assertEqual(grant['deadline'], NOW+6)
        self.now += 6
        with self.assertRaises(ValueError):
            self.store.consume(grant['permit_id'], BINDING, grant['plan_digest'])

    def test_clock_sample_after_consume_lock_prevents_waiting_on_expired_permission(self):
        self.arm()
        grant = self.grant()
        original = self.store._tx

        @contextmanager
        def delayed_lock():
            with original():
                self.now += 7
                yield

        with mock.patch.object(self.store, '_tx', delayed_lock), self.assertRaises(ValueError):
            self.store.consume(grant['permit_id'], BINDING, grant['plan_digest'])
        self.assertEqual(self.store.conn.execute('SELECT claimed FROM permits').fetchone()[0], 0)

    def test_approve_clock_is_sampled_after_transaction_lock(self):
        self.arm()
        original = self.store._tx

        @contextmanager
        def delayed_lock():
            with original():
                self.now += 7
                yield

        with mock.patch.object(self.store, '_tx', delayed_lock):
            self.assertEqual(self.grant()['status'], 'BLOCKED')
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM permits').fetchone()[0], 0)

    def test_arm_lifetime_starts_after_lock_not_before_wait(self):
        original = self.store._tx

        @contextmanager
        def delayed_lock():
            with original():
                self.now += 60
                yield

        with mock.patch.object(self.store, '_tx', delayed_lock):
            status = self.arm()
        self.assertEqual(status['created'], NOW+60)
        self.assertEqual(status['expires'], NOW+60+6*3600)

    def test_older_fresh_quote_deadline_is_not_extended_to_decision_plus_six(self):
        self.arm()
        data = snaps(quote('kalshi', t=NOW-5), quote('polymarket_us'))
        grant = self.store.approve(data, BINDING, contracts=10)
        self.assertEqual(grant['status'], 'APPROVED')
        self.assertEqual(grant['deadline'], NOW+1)
        self.assertEqual(grant['plan']['legs'][1]['observed_at'], NOW-5)
        self.now += 1
        with self.assertRaises(ValueError):
            self.store.consume(grant['permit_id'], BINDING, grant['plan_digest'])

    def test_slow_planning_cannot_approve_expired_quotes(self):
        self.arm()
        original = approval.plans

        def slow_plans(*args, **kwargs):
            result = original(*args, **kwargs)
            self.now += 7
            return result

        with mock.patch.object(approval, 'plans', side_effect=slow_plans):
            self.assertEqual(self.grant()['status'], 'BLOCKED')
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM permits').fetchone()[0], 0)

    def test_slow_digest_cannot_commit_an_expired_permission(self):
        self.arm()
        original = approval.digest

        def slow_digest(value):
            self.now += 7
            return original(value)

        with mock.patch.object(approval, 'digest', side_effect=slow_digest), self.assertRaises(ValueError):
            self.grant()
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM permits').fetchone()[0], 0)

    def test_slow_consume_digest_cannot_claim_an_expired_permission(self):
        self.arm()
        grant = self.grant()
        original = approval.digest

        def slow_digest(value):
            self.now += 7
            return original(value)

        with mock.patch.object(approval, 'digest', side_effect=slow_digest), self.assertRaises(ValueError):
            self.store.consume(grant['permit_id'], BINDING, grant['plan_digest'])
        self.assertEqual(self.store.conn.execute('SELECT claimed FROM permits').fetchone()[0], 0)

    def test_slow_storage_serialization_cannot_create_late_permission(self):
        self.arm()
        original = approval._json
        calls = 0

        def slow_json(value):
            nonlocal calls
            if isinstance(value, dict) and 'legs' in value:
                calls += 1
                if calls == 2:  # digest first, then the exact body to be stored
                    self.now += 7
            return original(value)

        with mock.patch.object(approval, '_json', side_effect=slow_json), self.assertRaises(ValueError):
            self.grant()
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM permits').fetchone()[0], 0)

    def test_slow_final_spec_check_cannot_create_or_claim_late_permission(self):
        self.arm()
        original = self.store._check
        calls = 0

        def slow_check(*args):
            nonlocal calls
            calls += 1
            result = original(*args)
            if calls == 2:
                self.now += 7
            return result

        with mock.patch.object(self.store, '_check', side_effect=slow_check), self.assertRaises(ValueError):
            self.grant()
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM permits').fetchone()[0], 0)
        self.now = NOW
        grant = self.grant()
        calls = 0
        with mock.patch.object(self.store, '_check', side_effect=slow_check), self.assertRaises(ValueError):
            self.store.consume(grant['permit_id'], BINDING, grant['plan_digest'])
        self.assertEqual(self.store.conn.execute('SELECT claimed FROM permits').fetchone()[0], 0)

    def test_invalid_rule_evidence_cannot_arm_or_issue_permission(self):
        with mock.patch.object(approval, 'verify_settlements', return_value=['fixture SHA mismatch']), self.assertRaises(ValueError):
            self.arm()
        self.arm()
        with mock.patch.object(approval, 'verify_settlements', return_value=['fixture SHA mismatch']), self.assertRaises(ValueError):
            self.grant()
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM permits').fetchone()[0], 0)

    def test_concurrent_approval_can_hold_same_event_only_once(self):
        self.arm()

        def approve_once(_):
            store = approval.ApprovalStore(self.path, clock=lambda: NOW)
            try:
                return store.approve(books(), BINDING, contracts=10)['status']
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(approve_once, range(2)))
        self.assertCountEqual(results, ['APPROVED', 'BLOCKED'])
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM permits').fetchone()[0], 1)

    def test_concurrent_consume_has_one_success_and_no_double_claim(self):
        self.arm()
        grant = self.grant()

        def consume_once(_):
            store = approval.ApprovalStore(self.path, clock=lambda: NOW)
            try:
                try:
                    return store.consume(grant['permit_id'], BINDING, grant['plan_digest'])['status']
                except ValueError:
                    return 'REFUSED'
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(consume_once, range(2)))
        self.assertCountEqual(results, ['PERMISSION_CONSUMED', 'REFUSED'])
        self.assertEqual(self.store.conn.execute('SELECT claimed FROM permits').fetchone()[0], 1)

    def test_corrupted_plan_body_cannot_be_consumed(self):
        self.arm()
        grant = self.grant()
        modified = copy.deepcopy(grant['plan'])
        modified['count'] += 1
        self.store.conn.execute('UPDATE permits SET body=? WHERE id=?', (json.dumps(modified), grant['permit_id']))
        with self.assertRaises(ValueError):
            self.store.consume(grant['permit_id'], BINDING, grant['plan_digest'])

    def test_invalid_contract_limit_does_not_create_permission(self):
        self.arm()
        for contracts in (True, 0, -1, 10001, 1.5):
            with self.subTest(contracts=contracts), self.assertRaises(ValueError):
                self.store.approve(books(), BINDING, contracts=contracts)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM permits').fetchone()[0], 0)

    def test_frozen_settings_respect_operator_venue_restriction(self):
        with self.assertRaises(ValueError):
            self.arm(settings={'executable_venues': 'kalshi', 'polymarket_us_volume_rebate': 1})
        self.assertEqual(approval.read_status(self.path, now=NOW)['status'], 'UNARMED')
        self.arm(settings={'executable_venues': 'kalshi,polymarket_us', 'polymarket_us_volume_rebate': 1})
        body = json.loads(self.store.conn.execute('SELECT body FROM approval').fetchone()[0])
        self.assertEqual(body['settings']['polymarket_us_volume_rebate'], 0)
        self.assertEqual(body['settings']['executable_venues'], ['kalshi', 'polymarket_us'])

    def test_new_environment_cannot_rewrite_frozen_approved_venue_scope(self):
        self.arm()
        with mock.patch.dict(os.environ, {'EXECUTABLE_VENUES': 'kalshi'}):
            self.assertEqual(self.grant()['status'], 'APPROVED')

    def test_changed_running_process_rules_refuse_rearm_even_if_disk_hash_is_new(self):
        self.arm()
        with mock.patch.object(approval, 'PROCESS_SPEC_HASH', 'old-running-process'):
            self.assertEqual(approval.read_status(self.path, now=NOW)['status'], 'SPEC_CHANGED')
            with self.assertRaises(ValueError):
                self.arm()


class PrivateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_missing_status_does_not_create_file_or_directory(self):
        path = self.root/'missing'/'approval.sqlite3'
        self.assertEqual(approval.read_status(path, now=NOW)['status'], 'UNARMED')
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())

    def test_public_existing_file_refused_without_chmod(self):
        path = self.root/'approval.sqlite3'
        path.write_bytes(b'not a ledger')
        path.chmod(0o644)
        for action in (lambda: approval.ApprovalStore(path), lambda: approval.read_status(path, now=NOW)):
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(path.read_bytes(), b'not a ledger')

    def test_symlink_file_refused_without_touching_target(self):
        target = self.root/'target'
        target.write_bytes(b'private untouched data')
        target.chmod(0o600)
        path = self.root/'approval.sqlite3'
        path.symlink_to(target)
        for action in (lambda: approval.ApprovalStore(path), lambda: approval.read_status(path, now=NOW)):
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(target.read_bytes(), b'private untouched data')

    def test_publicly_writable_directory_refused_before_ledger_creation(self):
        parent = self.root/'public'
        parent.mkdir(mode=0o700)
        parent.chmod(0o777)
        path = parent/'approval.sqlite3'
        with self.assertRaises(ValueError):
            approval.ApprovalStore(path)
        self.assertFalse(path.exists())

    def test_directory_as_file_refused(self):
        path = self.root/'directory'
        path.mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            approval.ApprovalStore(path)

    def test_hardlinked_existing_file_refused(self):
        target = self.root/'target'
        target.write_bytes(b'private untouched data')
        target.chmod(0o600)
        path = self.root/'approval.sqlite3'
        os.link(target, path)
        with self.assertRaises(ValueError):
            approval.ApprovalStore(path)
        self.assertEqual(target.read_bytes(), b'private untouched data')

    def test_symlink_or_public_sqlite_sidecars_refused_before_open(self):
        for suffix in ('-wal', '-shm', '-journal'):
            path = self.root/f'approval{suffix}.sqlite3'
            target = self.root/f'target{suffix}'
            target.write_bytes(b'untouched')
            target.chmod(0o600)
            aux = Path(str(path)+suffix)
            aux.symlink_to(target)
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                approval.ApprovalStore(path)
            aux.unlink()
            aux.write_bytes(b'public sidecar')
            aux.chmod(0o644)
            with self.subTest(public=suffix), self.assertRaises(ValueError):
                approval.read_status(path, now=NOW)
            self.assertFalse(path.exists())

    def test_existing_status_creates_no_sidecars_or_other_files(self):
        path = self.root/'approval.sqlite3'
        store = approval.ApprovalStore(path, clock=lambda: NOW)
        try:
            store.arm(BINDING)
        finally:
            store.close()
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        self.assertEqual(approval.read_status(path, now=NOW)['status'], 'ARMED')
        after = {p.name: p.read_bytes() for p in self.root.iterdir()}
        self.assertEqual(after, before)

    def test_spec_hash_includes_referenced_rule_text(self):
        engine = self.root/'arb_engine'
        (engine/'data').mkdir(parents=True)
        fixtures = self.root/'tests'/'fixtures'/'rules'
        fixtures.mkdir(parents=True)
        registry = engine/'data'/'settlement_rules.json'
        registry.write_text(json.dumps({'rules': [{'source': {'fixture': 'rule.txt'}}]}))
        evidence = fixtures/'rule.txt'
        evidence.write_text('first captured rule text')
        with mock.patch.object(approval, 'ROOT', engine), \
                mock.patch.object(approval, 'SPEC_FILES', ['data/settlement_rules.json']):
            before = approval.spec_hash()
            evidence.write_text('changed captured rule text')
            self.assertNotEqual(approval.spec_hash(), before)

    def test_spec_hash_refuses_external_or_symlink_rule_evidence(self):
        engine = self.root/'arb_engine'
        (engine/'data').mkdir(parents=True)
        fixtures = self.root/'tests'/'fixtures'/'rules'
        fixtures.mkdir(parents=True)
        registry = engine/'data'/'settlement_rules.json'
        target = self.root/'private.txt'
        target.write_text('untouched private evidence')
        (fixtures/'rule.txt').symlink_to(target)
        with mock.patch.object(approval, 'ROOT', engine), \
                mock.patch.object(approval, 'SPEC_FILES', ['data/settlement_rules.json']):
            for name in ('../private.txt', str(target), 'rule.txt'):
                registry.write_text(json.dumps({'rules': [{'source': {'fixture': name}}]}))
                with self.subTest(name=name), self.assertRaises(ValueError):
                    approval.spec_hash()
        self.assertEqual(target.read_text(), 'untouched private evidence')

    def test_spec_pins_quote_producers_and_identity_helpers(self):
        required = {'venues/polymarket_us.py', 'venues/kalshi.py', 'cli_plugins/us_arbs.py',
                    'matching/normalize.py', 'matching/teams.py', 'data/nfl_teams.json'}
        self.assertTrue(required <= set(approval.SPEC_FILES))


class ApprovalCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'approvals'/'standing.sqlite3'
        self.original_path = cli._path
        self.path_patch = mock.patch.object(cli, '_path', return_value=self.path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.network = mock.patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def run_cli(self, action, *, confirm=False, **kwargs):
        args = SimpleNamespace(action=action, confirm=confirm, hours='6',
                               kalshi_env_file=self.root/'never-read.env',
                               us_env_file=self.root/'never-read-us.env')
        for key, value in kwargs.items():
            setattr(args, key, value)
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.run(args, {})
        return code, json.loads(output.getvalue())

    def test_default_arm_and_revoke_are_inert_dry_runs(self):
        with mock.patch.object(cli, '_binding', side_effect=AssertionError('account read forbidden')):
            for action in ('arm', 'revoke'):
                code, result = self.run_cli(action)
                self.assertEqual(code, 0)
                self.assertEqual(result['status'], 'DRY_RUN')
                self.assertFalse(result['execution_enabled'])
        self.assertFalse(self.path.parent.exists())

    def test_missing_status_and_confirmed_revoke_do_not_create_store(self):
        with mock.patch.object(cli, '_binding', side_effect=AssertionError('account read forbidden')):
            for action, confirm in (('status', False), ('revoke', True)):
                code, result = self.run_cli(action, confirm=confirm)
                self.assertEqual(code, 0)
                self.assertEqual(result['status'], 'UNARMED')
        self.assertFalse(self.path.parent.exists())

    def test_failed_authentication_creates_no_permission_store(self):
        with mock.patch.object(cli, '_binding', side_effect=ValueError('private secret must not leak')):
            code, result = self.run_cli('arm', confirm=True)
        self.assertEqual(code, 3)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertNotIn('private secret', json.dumps(result))
        self.assertFalse(self.path.parent.exists())

    def test_invalid_confirmed_lifetime_is_rejected_before_account_reads(self):
        with mock.patch.object(cli, '_binding') as binding:
            for hours in ('0', '-1', '25', 'NaN', 'Infinity'):
                with self.subTest(hours=hours):
                    code, result = self.run_cli('arm', confirm=True, hours=hours)
                    self.assertEqual(code, 3)
                    self.assertEqual(result['status'], 'BLOCKED')
            binding.assert_not_called()
        self.assertFalse(self.path.parent.exists())

    def test_cli_store_path_respects_explicit_ledger_directory(self):
        path = self.original_path({'order_ledger_dir': str(self.root/'chosen')})
        self.assertEqual(path, self.root/'chosen'/'standing_approval.sqlite3')
        self.assertFalse(path.parent.exists())

    def test_confirmed_authenticated_arm_uses_only_mocked_account_reads(self):
        pem = self.root/'dummy-signing.pem'
        pem.write_text('synthetic file; signer is mocked')
        pem.chmod(0o600)
        env = self.root/'dummy-prod.env'
        env.write_text('export KALSHI_ENV=prod\nexport KALSHI_API_KEY=fake-public-key\n'
                       f'export KALSHI_PRIVATE_KEY_PATH={pem}\n')
        env.chmod(0o600)
        us = mock.Mock(base_url=cli.API, fingerprint=BINDING['polymarket_us_key'])
        us.balances.return_value = {'balances': [{'currency': 'USD', 'buyingPower': '35'}]}
        kal = mock.Mock()
        identity = SimpleNamespace(error=None, key_fp=BINDING['kalshi_key'], account_fp=BINDING['kalshi_account'])
        with mock.patch.object(cli, 'KalshiClient', return_value=kal) as factory, \
                mock.patch.object(cli, 'client_identity', return_value=identity) as identity_read, \
                mock.patch.object(cli, 'load_credentials', return_value={'synthetic': 'credentials'}), \
                mock.patch.object(cli, 'PolymarketUSTradingClient', return_value=us):
            code, result = self.run_cli('arm', confirm=True, kalshi_env_file=env)
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'ARMED')
        self.assertFalse(result['execution_enabled'])
        self.assertEqual(factory.call_args.kwargs['env'], 'prod')
        self.assertEqual(factory.call_args.kwargs['base_url'], cli.ENV_REST_BASE['prod'])
        identity_read.assert_called_once_with(kal, 'prod')
        us.balances.assert_called_once_with()
        us._create.assert_not_called()
        us._cancel.assert_not_called()
        kal.create_order.assert_not_called()
        self.assertNotIn('fake-public-key', json.dumps(result))

    def test_confirmed_revoke_and_status_never_reauthenticate_or_send(self):
        with mock.patch.object(cli, '_binding', return_value=BINDING):
            self.assertEqual(self.run_cli('arm', confirm=True)[0], 0)
        with mock.patch.object(cli, '_binding', side_effect=AssertionError('account read forbidden')):
            self.assertEqual(self.run_cli('status')[1]['status'], 'ARMED')
            self.assertEqual(self.run_cli('revoke', confirm=True)[1]['status'], 'REVOKED')
            self.assertEqual(self.run_cli('status')[1]['status'], 'REVOKED')

    def test_readonly_account_transport_rejects_order_demo_and_foreign_urls(self):
        transport = cli.ReadOnlyAccounts()
        bad = ['https://external-api.kalshi.com/trade-api/v2/portfolio/events/orders',
               'https://external-api.demo.kalshi.co/trade-api/v2/portfolio/balance',
               'https://attacker.test/trade-api/v2/portfolio/balance',
               'http://external-api.kalshi.com/trade-api/v2/portfolio/balance',
               'https://external-api.kalshi.com:444/trade-api/v2/portfolio/balance',
               'https://user:password@external-api.kalshi.com/trade-api/v2/portfolio/balance',
               'https://external-api.kalshi.com/trade-api/v2/portfolio/balance?unexpected=1',
               'https://external-api.kalshi.com/trade-api/v2/portfolio/balance#fragment']
        for url in bad:
            with self.subTest(url=url), self.assertRaises(ValueError):
                transport.get(url)
        with self.assertRaises(ValueError):
            transport.get('https://external-api.kalshi.com/trade-api/v2/portfolio/balance', params={'any': 1})

    def test_auto_live_readiness_shows_permission_without_loading_accounts_or_enabling_orders(self):
        from arb_engine.cli_plugins import auto_arb
        with mock.patch.object(cli, '_binding', return_value=BINDING):
            self.assertEqual(self.run_cli('arm', confirm=True)[0], 0)
        args = SimpleNamespace(mode='live', every=0, contracts=10, status=False)
        output = io.StringIO()
        with mock.patch.object(cli, '_binding', side_effect=AssertionError('account read forbidden')), \
                mock.patch.object(auto_arb, 'PaperPairs', side_effect=AssertionError('paper ledger forbidden')), \
                redirect_stdout(output):
            self.assertEqual(auto_arb.run(args, {}), 3)
        result = json.loads(output.getvalue())
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertFalse(result['live_enabled'])
        self.assertTrue(result['standing_approval']['approval_active'])
        self.assertFalse(result['standing_approval']['execution_enabled'])


if __name__ == '__main__':
    unittest.main()

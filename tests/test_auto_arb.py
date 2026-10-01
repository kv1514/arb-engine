"""Synthetic books and hand-computed fees; no account or network access."""
import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arb_engine.cli_plugins.auto_arb import run
from arb_engine.execution.pairpaper import PaperPairs, plans, live_readiness
from arb_engine.models import EventInfo, OutcomeQuote, VenueSnapshot

NOW = 1790791200.
EVENT = 'nfl:CHI|NYJ:2026-09-30'
RULE = {'status': 'verbatim', 'tie': 'half', 'cancelled': '50-50',
        'postponed': 'open_until_complete', 'ot_included': True}


def quote(venue, *, t=NOW, ask=None, bid=None, size=40, side='yes', **meta):
    ask = (.5 if venue == 'polymarket_us' else .4) if ask is None else ask
    bid = float(D(str(ask)) - D('.01')) if bid is None else bid
    outcome = 'NYJ' if venue == 'polymarket_us' else 'CHI'
    return OutcomeQuote(venue, venue+outcome+'#'+side, EVENT, outcome, outcome,
                        ask=ask, bid=bid, ask_size=size, bid_size=size, ts=t, book_id=venue,
                        fee_params={'taker_theta': '.0695'} if venue == 'polymarket_us' else {'fee_multiplier': 1},
                        meta={'side': side, 'tie_payout': .5, 'req_ts': t-.1, 'obs_ts': t,
                              'refreshed': True, 'approx_time': False, 'tick_size': '.01', 'min_size': 1, **meta})


def snaps(*qs):
    return [VenueSnapshot(v, {EVENT: EventInfo(EVENT, 'nfl', 'moneyline', ['CHI', 'NYJ'],
               start_time=datetime.fromtimestamp(NOW+3600, timezone.utc), in_play=False)},
               [q for q in qs if q.venue == v], max(q.ts for q in qs if q.venue == v))
            for v in sorted({q.venue for q in qs})]


class PairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'paper.sqlite3'
        self.ledger = PaperPairs(self.path)
        self.addCleanup(lambda: self.ledger.close())
        self.addCleanup(self.temp.cleanup)
        self.rules = mock.patch('arb_engine.quant.us_arbitrage.rule_for_quote', return_value=RULE)
        self.flags = mock.patch('arb_engine.quant.us_arbitrage.pair_flags', return_value=[])
        self.rules.start()
        self.flags.start()
        self.addCleanup(self.rules.stop)
        self.addCleanup(self.flags.stop)

    def admit(self, count=10):
        return self.ledger.admit(snaps(quote('kalshi'), quote('polymarket_us')), now=NOW, contracts=count)

    def advance(self, t, *qs):
        self.ledger.advance(snaps(*qs) if qs else [], now=NOW+t)
        return self.ledger.status()['pairs'][0]

    def test_default_registry_never_admits_conditional_pair(self):
        self.rules.stop()
        self.flags.stop()
        self.assertIsNone(self.admit())
        self.assertEqual(self.ledger.status()['pairs'], [])

    def test_live_flags_cannot_enable_missing_paired_transport(self):
        with mock.patch.dict(os.environ, {'ARB_LIVE_TRADING': '1', 'POLYMARKET_US_LIVE_TRADING': '1', 'KALSHI_ENV': 'prod'}):
            result = live_readiness()
        self.assertFalse(result['live_enabled'])
        self.assertEqual(result['status'], 'BLOCKED')

    def test_both_actual_fees_and_hedge_at_fixed_limit(self):
        self.assertTrue(self.admit())
        p = self.advance(3, quote('polymarket_us', t=NOW+3))
        self.assertEqual(p['first'], 10)
        self.assertEqual(p['fills'][0]['fee'], '0.17')
        p = self.advance(6, quote('kalshi', t=NOW+6))
        self.assertEqual(p['phase'], 'locked')
        self.assertEqual(p['fills'][1]['fee'], '0.17')
        self.assertEqual(D(self.ledger.status()['held_paper_cash']), D('9.34'))

    def test_partial_first_leg_sizes_only_actual_hedge(self):
        self.admit()
        self.advance(3, quote('polymarket_us', t=NOW+3, size=6))
        p = self.advance(6, quote('kalshi', t=NOW+6))
        self.assertEqual((p['first'], p['hedge']), (3, 3))
        self.assertEqual(p['phase'], 'locked')

    def test_entry_price_move_cancels_everything(self):
        self.admit()
        p = self.advance(3, quote('polymarket_us', t=NOW+3, ask=.51))
        self.assertEqual(p['phase'], 'missed')
        self.assertEqual(p['fills'], [])
        self.assertEqual(self.ledger.status()['held_paper_cash'], '0')

    def test_missing_entry_window_not_silently_late_fill(self):
        self.admit()
        p = self.advance(6, quote('polymarket_us', t=NOW+6))
        self.assertEqual(p['phase'], 'missed')

    def test_future_rows_not_visible(self):
        self.admit()
        p = self.advance(3, quote('polymarket_us', t=NOW+4))
        self.assertEqual(p['phase'], 'entry')
        self.assertEqual(p['first'], 0)

    def test_stale_carried_approx_failed_and_negative_request_refused(self):
        for meta in ({'refreshed': 0}, {'approx_time': True}, {'arb_ineligible': 'failed'},
                     {'req_ts': -1}, {'obs_ts': NOW+4}):
            self.assertEqual(plans(snaps(quote('kalshi'), quote('polymarket_us', **meta)), now=NOW), [])
        self.assertEqual(plans(snaps(quote('kalshi', t=NOW-7), quote('polymarket_us')), now=NOW), [])

    def test_partial_hedge_unwinds_only_excess_with_both_fees(self):
        self.admit()
        self.advance(3, quote('polymarket_us', t=NOW+3))
        self.advance(6, quote('kalshi', t=NOW+6, size=8))
        p = self.advance(9, quote('polymarket_us', t=NOW+9, bid=.49))
        self.assertEqual((p['first'], p['hedge'], p['unwound']), (10, 4, 6))
        self.assertEqual(p['phase'], 'recovered')
        self.assertEqual(p['fills'][2]['fee'], '0.10')
        # Held cash excludes sale proceeds: 5+.17 + 1.60+.07 + .10.
        self.assertEqual(D(self.ledger.status()['held_paper_cash']), D('6.94'))

    def test_failed_hedge_has_latency_before_unwind(self):
        self.admit()
        self.advance(3, quote('polymarket_us', t=NOW+3))
        self.advance(6, quote('kalshi', t=NOW+6, ask=.45))
        p = self.advance(8, quote('polymarket_us', t=NOW+8))
        self.assertEqual(p['unwound'], 0)
        p = self.advance(9, quote('polymarket_us', t=NOW+9))
        self.assertEqual(p['unwound'], 10)

    def test_partial_unwind_rolls_at_most_twice_and_blocks_new_exposure(self):
        self.admit()
        reserved = self.ledger.status()['held_paper_cash']
        self.advance(3, quote('polymarket_us', t=NOW+3))
        self.advance(6, quote('kalshi', t=NOW+6, ask=.45))
        self.advance(9, quote('polymarket_us', t=NOW+9, size=4))
        p = self.advance(12, quote('polymarket_us', t=NOW+12, size=4))
        self.assertEqual((p['phase'], p['unwound'], p['rolls']), ('unresolved', 4, 2))
        self.assertEqual(self.ledger.status()['held_paper_cash'], reserved)
        self.assertIsNone(self.admit())

    def test_unwind_loss_cap_refuses_large_loss(self):
        self.admit()
        self.advance(3, quote('polymarket_us', t=NOW+3))
        self.advance(6, quote('kalshi', t=NOW+6, ask=.45))
        self.advance(9, quote('polymarket_us', t=NOW+9, bid=.1))
        p = self.advance(12, quote('polymarket_us', t=NOW+12, bid=.1))
        self.assertEqual(p['phase'], 'unresolved')
        self.assertEqual(p['reason'], 'unwind loss cap exceeded')

    def test_duplicate_marks_supply_size_once_and_resume_on_restart(self):
        self.admit()
        q = quote('polymarket_us', t=NOW+3, size=8)
        p = self.advance(3, q, copy.deepcopy(q))
        self.assertEqual(p['first'], 4)
        self.ledger.close()
        self.ledger = PaperPairs(self.path)
        self.advance(3, q)
        p = self.advance(6, quote('kalshi', t=NOW+6))
        self.assertEqual((p['first'], p['hedge'], len(p['fills'])), (4, 4, 2))
        self.assertIsNone(self.admit())

    def test_conflicting_equal_time_marks_cannot_pick_optimistic_price(self):
        self.admit()
        a = quote('polymarket_us', t=NOW+3)
        b = quote('polymarket_us', t=NOW+3, ask=.51)
        p = self.advance(3, a, b)
        self.assertEqual(p['phase'], 'entry')
        self.assertEqual(p['fills'], [])

    def test_decision_uses_latest_quote_not_cheaper_old_quote(self):
        p = quote('polymarket_us', t=NOW-1, ask=.2)
        latest = quote('polymarket_us', ask=.7)
        self.assertEqual(plans(snaps(quote('kalshi'), p, latest), now=NOW), [])
        failed = quote('polymarket_us', arb_ineligible='failed')
        self.assertEqual(plans(snaps(quote('kalshi'), p, failed), now=NOW), [])

    def test_arrival_uses_first_mark_not_later_better_price(self):
        self.admit()
        p = self.advance(5, quote('polymarket_us', t=NOW+3, ask=.51),
                         quote('polymarket_us', t=NOW+4, ask=.49))
        self.assertEqual(p['phase'], 'missed')

    def test_decision_conflicts_and_event_identity_disagreement_rejected(self):
        self.assertEqual(plans(snaps(quote('kalshi'), quote('polymarket_us'),
                                    quote('polymarket_us', ask=.49)), now=NOW), [])
        data = snaps(quote('kalshi'), quote('polymarket_us'))
        data[0].events[EVENT].in_play = True
        self.assertEqual(plans(data, now=NOW), [])

    def test_frozen_fee_changes_refuse_hedge(self):
        self.admit()
        self.advance(3, quote('polymarket_us', t=NOW+3))
        k = quote('kalshi', t=NOW+6)
        k.fee_params['fee_multiplier'] = 10
        p = self.advance(6, k)
        self.assertEqual((p['hedge'], p['phase']), (0, 'unwind'))
        self.assertIn('fee parameters changed', p['reason'])

    def test_venue_live_flag_blocks_new_entries_even_before_scheduled_kickoff(self):
        self.admit()
        data = snaps(quote('polymarket_us', t=NOW+3))
        data[0].events[EVENT].in_play = True
        self.ledger.advance(data, now=NOW+3)
        p = self.ledger.status()['pairs'][0]
        self.assertEqual((p['phase'], p['first']), ('missed', 0))

    def test_changed_paper_policy_preserves_reservation_and_blocks_resume(self):
        self.admit()
        held = self.ledger.status()['held_paper_cash']
        with mock.patch('arb_engine.execution.pairpaper.POLICY', {'version': 2}):
            p = self.advance(3, quote('polymarket_us', t=NOW+3))
        self.assertEqual(p['phase'], 'unresolved')
        self.assertEqual(self.ledger.status()['held_paper_cash'], held)

    def test_missing_exit_size_is_unresolved_not_free_liquidity(self):
        self.admit()
        self.advance(3, quote('polymarket_us', t=NOW+3))
        self.advance(6, quote('kalshi', t=NOW+6, ask=.45))
        q = quote('polymarket_us', t=NOW+9)
        q.bid_size = None
        self.advance(9, q)
        q.ts = q.meta['obs_ts'] = q.meta['req_ts'] = NOW+12
        p = self.advance(12, q)
        self.assertEqual((p['phase'], p['unwound']), ('unresolved', 0))

    def test_operator_restriction_is_respected(self):
        self.assertEqual(plans(snaps(quote('kalshi'), quote('polymarket_us')), now=NOW,
                               settings={'executable_venues': 'kalshi'}), [])

    def test_held_cash_survives_restart_and_does_not_reset_daily(self):
        self.assertTrue(self.ledger.admit(snaps(quote('kalshi', size=100), quote('polymarket_us', size=100)),
                                          now=NOW, contracts=40))
        self.advance(3, quote('polymarket_us', t=NOW+3, size=100))
        self.advance(6, quote('kalshi', t=NOW+6, size=100))
        held = D(self.ledger.status()['held_paper_cash'])
        self.assertGreater(held, D(36))
        self.ledger.close()
        self.ledger = PaperPairs(self.path)
        data = snaps(quote('kalshi', size=100), quote('polymarket_us', size=100))
        key = EVENT.replace('CHI|NYJ', 'BUF|DET')
        for s in data:
            info = s.events.pop(EVENT)
            info.event_key = key
            s.events[key] = info
            for q in s.quotes:
                q.event_key = key
        self.assertIsNone(self.ledger.admit(data, now=NOW, contracts=40))
        self.assertEqual(D(self.ledger.status()['held_paper_cash']), held)

    def test_short_purchase_consumes_real_complement_quote(self):
        data = snaps(quote('kalshi'), quote('polymarket_us', side='no'))
        self.assertTrue(self.ledger.admit(data, now=NOW, contracts=10))
        p = self.advance(3, quote('polymarket_us', t=NOW+3, side='yes'))
        self.assertEqual(p['first'], 0)
        p = self.advance(4, quote('polymarket_us', t=NOW+4, side='no', size=6))
        self.assertEqual(p['first'], 3)

    def test_simulated_crash_rolls_back_liquidity_and_fill_together(self):
        self.admit()
        fill = self.ledger._fill
        def crash(*args, **kwargs):
            fill(*args, **kwargs)
            raise RuntimeError('crash after liquidity consumption')
        with mock.patch.object(self.ledger, '_fill', side_effect=crash):
            with self.assertRaises(RuntimeError):
                self.advance(3, quote('polymarket_us', t=NOW+3))
        self.assertEqual(self.ledger.status()['pairs'][0]['phase'], 'entry')
        self.assertEqual(self.ledger.conn.execute('SELECT count(*) FROM liquidity').fetchone()[0], 0)
        p = self.advance(3, quote('polymarket_us', t=NOW+3))
        self.assertEqual(p['first'], 10)

    def test_fee_inclusive_caps_and_haircut(self):
        ps = plans(snaps(quote('kalshi', size=1000), quote('polymarket_us', size=1000)), now=NOW)
        # 45*.5 + .78 entry fee + 2*.78 exit reserves =24.84; 46 costs25.40.
        self.assertEqual(ps[0]['count'], 45)
        self.assertLessEqual(D(ps[0]['reservation']), D(50))
        self.assertEqual(self.ledger.status()['orders_submitted'], 0)

    def test_exact_quantity_and_long_price_grids(self):
        p = quote('polymarket_us', quantity_increment=3, min_size=3)
        ps = plans(snaps(quote('kalshi'), p), now=NOW, contracts=10)
        self.assertEqual(ps[0]['count'], 9)
        self.assertEqual(plans(snaps(quote('kalshi'), quote('polymarket_us', ask=.505)), now=NOW), [])
        self.assertTrue(plans(snaps(quote('kalshi'), quote('polymarket_us', side='no', ask=.5)), now=NOW))

    def test_same_book_unknown_fees_and_losing_ties_rejected(self):
        k = quote('kalshi')
        p = quote('polymarket_us')
        p.book_id = 'kalshi'
        self.assertEqual(plans(snaps(k, p), now=NOW), [])
        k.fee_params = {}
        self.assertEqual(plans(snaps(k, quote('polymarket_us')), now=NOW), [])
        self.assertEqual(plans(snaps(quote('kalshi'), quote('polymarket_us', tie_payout=0)), now=NOW), [])

    def test_errors_missing_depth_and_imminent_kickoff_rejected(self):
        data = snaps(quote('kalshi'), quote('polymarket_us'))
        data[0].errors.append('partial response')
        self.assertEqual(plans(data, now=NOW), [])
        self.assertEqual(plans(snaps(quote('kalshi'), quote('polymarket_us', size=None)), now=NOW), [])
        data = snaps(quote('kalshi'), quote('polymarket_us'))
        for s in data:
            s.events[EVENT].start_time = datetime.fromtimestamp(NOW+9, timezone.utc)
        self.assertEqual(plans(data, now=NOW), [])

    def test_permutation_and_future_append_invariance(self):
        k, p = quote('kalshi'), quote('polymarket_us')
        expected = plans(snaps(k, p), now=NOW)
        self.assertEqual(expected, plans(list(reversed(snaps(k, p))), now=NOW))
        self.assertEqual(expected, plans(snaps(k, p, quote('polymarket_us', t=NOW+10, ask=.2)), now=NOW))

    def test_sqlite_transactions_prevent_parallel_pair_reservations(self):
        other = PaperPairs(self.path)
        try:
            self.assertTrue(self.admit())
            self.assertIsNone(other.admit(snaps(quote('kalshi'), quote('polymarket_us')), now=NOW))
            self.assertEqual(len(other.status()['pairs']), 1)
        finally:
            other.close()

    def test_missing_hedge_and_missing_exits_leave_unresolved_inventory(self):
        self.admit()
        self.advance(3, quote('polymarket_us', t=NOW+3))
        self.advance(9)
        self.advance(15)
        p = self.advance(21)
        self.assertEqual(p['phase'], 'unresolved')
        self.assertEqual((p['first'], p['hedge'], p['unwound']), (10, 0, 0))

    def test_invalid_times_and_counts_refused(self):
        for now in (float('nan'), -1, True):
            with self.assertRaises(ValueError):
                plans([], now=now)
        for n in (True, 0, 1.5):
            with self.assertRaises(ValueError):
                plans([], now=NOW, contracts=n)


class CLITests(unittest.TestCase):
    def args(self, **kwargs):
        return SimpleNamespace(**{'mode': 'off', 'status': False, 'every': 0, 'contracts': 10, **kwargs})

    def call(self, args, settings=None):
        output = io.StringIO()
        with redirect_stdout(output):
            code = run(args, settings)
        return code, json.loads(output.getvalue())

    def test_default_off_does_not_load_books_or_accounts_or_create_ledger(self):
        with mock.patch('arb_engine.cli_plugins.auto_arb._default_adapters', side_effect=AssertionError), \
             mock.patch('arb_engine.cli_plugins.auto_arb.PaperPairs', side_effect=AssertionError):
            code, result = self.call(self.args(mode=None), {'auto_pair_mode': 'off'})
        self.assertEqual((code, result['status']), (0, 'OFF'))

    def test_live_switch_fails_closed_without_ledger_or_network(self):
        with mock.patch('arb_engine.cli_plugins.auto_arb._default_adapters', side_effect=AssertionError), \
             mock.patch('arb_engine.cli_plugins.auto_arb.PaperPairs', side_effect=AssertionError):
            code, result = self.call(self.args(mode='live'))
        self.assertEqual((code, result['status'], result['orders_submitted']), (3, 'BLOCKED', 0))

    def test_paper_scan_only_selects_two_public_adapters(self):
        adapters = [SimpleNamespace(venue=v) for v in ('kalshi', 'polymarket_us', 'robinhood')]
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch('arb_engine.cli_plugins.auto_arb._default_adapters', return_value=adapters), \
             mock.patch('arb_engine.cli_plugins.auto_arb.fetch_snapshots', return_value=[]) as fetch:
            code, result = self.call(self.args(mode='paper'), {'order_ledger_dir': temp})
        self.assertEqual(code, 0)
        self.assertEqual(result['mode'], 'paper')
        self.assertEqual([a.venue for a in fetch.call_args.kwargs['adapters']], ['kalshi', 'polymarket_us'])

    def test_missing_status_does_not_create_a_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            self.call(self.args(status=True), {'order_ledger_dir': temp})
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_invalid_env_mode_is_blocked(self):
        code, result = self.call(self.args(mode=None), {'auto_pair_mode': 'banana'})
        self.assertEqual((code, result['status']), (3, 'BLOCKED'))

"""Causal diagnostics and conservative lead-lag regression cases; no network."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from arb_engine.strategy.leadlag import LeadLagTracker
from arb_engine.strategy.momentum import MomentumTracker, replay
from tests.test_leadlag import KEY, OUT, LABELS, _book, _q


class DetectionIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tracker = LeadLagTracker(executable={'kalshi'})
        self.tracker.observe(KEY, '', OUT, LABELS, _book(0, (.59, .60), (.58, .61)), now=0)

    def observe(self, book, now=10, **kw):
        return self.tracker.observe(KEY, '', OUT, LABELS, book, now=now, **kw)

    def changed(self, t=10):
        return _book(t, (.59, .60), (.66, .69))

    def test_shared_book_cannot_lead_itself(self):
        book = self.changed()
        for q in book['robinhood']:
            q.book_id = 'kalshi'
        self.assertEqual(self.observe(book), [])
        self.assertEqual(self.observe(self.changed(15)), [])  # changed identity warms up

    def test_new_follower_is_not_stationary_history(self):
        self.tracker._hist.pop((KEY, 'kalshi'))
        self.assertEqual(self.observe(self.changed()), [])

    def test_stale_observation_without_exchange_timestamp(self):
        book = self.changed(20)
        for q in book['kalshi']:
            q.ts = 0
        self.assertEqual(self.observe(book, 20), [])

    def test_stale_away_only_quote_is_rejected(self):
        book = self.changed()
        book['robinhood'] = [book['robinhood'][1]]
        book['robinhood'][0].quote_time = -20
        self.assertEqual(self.observe(book), [])

    def test_future_timestamp_rejected(self):
        # A venue clock a little ahead of ours is tolerated (leadlag.CLOCK_SKEW_S = 2 s);
        # a timestamp well in the future is bogus and never evidence.
        book = self.changed()
        for q in book['robinhood']:
            q.quote_time = 13
        self.assertEqual(self.observe(book), [])

    def test_feed_gap_requires_new_history(self):
        self.assertEqual(self.observe(self.changed(60), 60), [])

    def test_wrong_event_and_crossed_nan_books_rejected(self):
        for value in (float('nan'), .9):
            with self.subTest(value=value):
                book = self.changed()
                for q in book['robinhood']:
                    q.bid = value
                self.assertEqual(self.observe(book), [])
        book = self.changed()
        for q in book['robinhood']:
            q.event_key = 'another-game'
        self.assertEqual(self.observe(book), [])

    def test_fee_failure_is_not_free(self):
        with patch('arb_engine.strategy.leadlag.fee_model_for_quote', side_effect=ValueError('unknown fee')):
            self.assertEqual(self.observe(self.changed(), bankroll=500), [])

    def test_missing_or_zero_depth_cannot_size(self):
        for depth in (None, 0, float('nan')):
            book = self.changed()
            for q in book['kalshi']:
                q.ask_size = depth
            self.assertEqual(self.observe(book, bankroll=500), [])

    def test_single_contract_rounding_is_used_for_edge(self):
        book = _book(10, (.59, .60), (.63, .645))  # mid .6375 - .60 - .02 < .02
        self.tracker.move = .03
        for q in book['kalshi']:
            q.ask_size = 1
        self.assertEqual(self.observe(book, bankroll=500), [])

    def test_duplicate_and_backwards_polls_do_not_create_impulse(self):
        self.assertEqual(self.observe(self.changed(0), 0), [])
        self.assertEqual(self.observe(self.changed(-1), 0), [])


class MomentumTests(unittest.TestCase):
    def feed(self, prices, times=None, tracker=None, **kwargs):
        tr = tracker or MomentumTracker()
        result = []
        for t, p in zip(times or range(0, len(prices)*5, 5), prices):
            q = _q('kalshi', 'KC', p-.01, p+.01, t)
            for k, v in kwargs.items():
                setattr(q, k, v)
            result = tr.observe(KEY, {'kalshi': [q]}, t)
        return tr, result

    def test_sustained_dip_is_watch_not_buy(self):
        _, result = self.feed([.6, .59, .58, .57, .56])
        m = result[0]
        self.assertEqual(m['status'], 'dip-watch')
        self.assertAlmostEqual(m['short_slope'], -.002)
        self.assertAlmostEqual(m['projected_mid'], .54)
        self.assertTrue(m['signal_only'])

    def test_flat_and_choppy_do_not_claim_trend(self):
        for prices in ([.5]*5, [.5, .52, .49, .52, .5]):
            _, result = self.feed(prices)
            self.assertEqual(result[0]['status'], 'flat')

    def test_rebound_needs_observed_recovery(self):
        _, result = self.feed([.6, .58, .56, .54, .52, .53, .54, .55, .56])
        self.assertEqual(result[0]['status'], 'rebound-watch')

    def test_spread_widening_cannot_confirm_direction(self):
        tr = MomentumTracker()
        for t in range(0, 25, 5):
            q = _q('kalshi', 'KC', .49, .50+t*.002, t)
            result = tr.observe(KEY, {'kalshi':[q]}, t)
        self.assertEqual(result[0]['status'], 'flat')
        self.assertEqual(result[0]['projected_mid'], result[0]['mid'])

    def test_warmup_and_gap_reset(self):
        tr, result = self.feed([.5, .51, .52, .53])
        self.assertEqual(result, [])
        _, result = self.feed([.6], times=[50], tracker=tr)
        self.assertEqual(result, [])

    def test_duplicate_snapshots_do_not_increase_samples(self):
        tr, result = self.feed([.5, .51, .52, .53, .54])
        _, again = self.feed([.54], times=[20], tracker=tr)
        self.assertEqual(result, again)
        self.assertEqual(len(next(iter(tr._history.values()))), 5)

    def test_same_book_is_not_independent_confirmation(self):
        tr = MomentumTracker()
        for t in range(0, 25, 5):
            q = _q('kalshi', 'KC', .5+t*.002, .52+t*.002, t)
            rh = _q('robinhood', 'KC', q.bid, q.ask, t)
            rh.book_id = 'kalshi'
            result = tr.observe(KEY, {'kalshi':[q], 'robinhood':[rh]}, t)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['samples'], 5)

    def test_irregular_polling_uses_elapsed_time(self):
        times = [0, 3, 8, 15, 20]
        _, result = self.feed([.5+t*.001 for t in times], times=times)
        self.assertAlmostEqual(result[0]['short_slope'], .001)

    def test_future_inputs_do_not_change_past_forecast(self):
        tr, before = self.feed([.5, .51, .52, .53, .54])
        snapshot = json.dumps(before)
        self.feed([.8, .1], times=[25,30], tracker=tr)
        self.assertEqual(json.dumps(before), snapshot)

    def test_replay_metrics_match_committed_fixture(self):
        root = Path(__file__).parent/'fixtures'
        data = json.loads((root/'ticks/synthetic_40.json').read_text())
        actual = replay(data)
        expected = json.loads((root/'results/momentum_synthetic_40.json').read_text())
        # Floats compared to 1e-12, not exactly: sum() is compensated from Python 3.12 on, so
        # 3.10/3.11 differ in the last digits (CI runs 3.10-3.13).
        self.assertEqual(set(actual), set(expected))
        for k, v in expected.items():
            if isinstance(v, float):
                self.assertAlmostEqual(actual[k], v, places=12, msg=k)
            else:
                self.assertEqual(actual[k], v, k)
        # Deliberately preserve a negative result, rather than select winning predictions.
        self.assertGreater(actual['mean_absolute_error'], actual['persistence_mean_absolute_error'])

    def test_replay_rejects_noncausal_order(self):
        with self.assertRaises(ValueError):
            replay({'event_key':KEY, 'ticks':[{'ts':2,'quotes':{}}, {'ts':1,'quotes':{}}]})


class StudyIntegrityTests(unittest.TestCase):
    def test_move_direction_compares_dollars_not_squared_dollars(self):
        from scripts.leadlag_study import move_study
        def row(t, p):
            return dict(ts=t, kalshi_home_bid=p-.01, kalshi_home_ask=p+.01,
                        model_p=None, home_score=0, away_score=0)
        for later, label in ((.57, 'continue'), (.53, 'revert')):
            result = move_study({KEY:[row(0,.49), row(5,.55), row(35,later)]})
            self.assertEqual(result['all']['horizons']['30'][label], 1)

    def test_move_study_does_not_substitute_a_much_later_mark(self):
        from scripts.leadlag_study import move_study

        def row(t, p):
            return dict(ts=t, kalshi_home_bid=p-.01, kalshi_home_ask=p+.01,
                        model_p=None, home_score=0, away_score=0)
        result = move_study({KEY:[row(0,.49), row(5,.55), row(50,.57)]})
        self.assertNotIn('30', result['all']['horizons'])

    def test_study_exit_fees_and_missing_horizon(self):
        import datetime
        import sqlite3
        import tempfile
        from dataclasses import asdict
        from scripts.leadlag_study import lag_replay
        base = datetime.datetime(2026, 9, 21).timestamp()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/'ticks.db')
            conn = sqlite3.connect(path)
            conn.execute('create table inplay_ticks (ts real, event_key text, l1_json text, game_line text, live integer)')
            conn.execute('create table espn_ticks (ts real, event_key text, status text, home_score int, away_score int, home text, away text)')
            for offset, prices in ((0,(.58,.61)), (5,(.58,.61)), (10,(.66,.69)), (40,(.66,.69)), (1000,(.66,.69))):
                books = _book(base+offset, (.59,.60) if offset<40 else (.65,.66), prices)
                l1 = {v:{q.outcome:{**asdict(q), 'exchange':q.meta.get('exchange')} for q in qs} for v,qs in books.items()}
                conn.execute('insert into inplay_ticks values (?, ?, ?, ?, 1)', (base+offset, KEY, json.dumps(l1), 'DEN @ KC'))
            conn.commit()
            conn.close()
            result = lag_replay(path, '2026-09-21', 'nfl', horizons=(30,60))
            self.assertEqual(result['signals'], 1)
            mark = result['exit_at_bid']['30']
            # Gross 5c minus both approximately 1.7c entry and 1.6c exit fees.
            self.assertLess(mark['mean_pnl_per_contract'], .02)
            self.assertGreater(mark['mean_pnl_per_contract'], .01)
            self.assertEqual(result['exit_at_bid']['60']['unknown'], 1)

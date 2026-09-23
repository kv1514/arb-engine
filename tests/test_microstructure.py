"""Causal microstructure samples, paper execution and frozen-fold discipline."""
import json
import tempfile
import unittest
from pathlib import Path

from arb_engine.fees.base import ZeroFees
from arb_engine.quant.microdata import features_at, lead_lag_label, trigger_events
from arb_engine.quant.paperexec import ioc, two_leg_arb
from scripts.microstructure_eval import game_block_bootstrap, guard_test_open, spec_hash, validate_manifest


def row(t, mid, book="kalshi", market="K", **kw):
    out = dict(obs_ts=float(t), req_ts=float(t), refreshed=1, in_play=True, source="fast",
               quote_time=float(t), venue=book, book_id=book, venue_market_id=market,
               outcome="HOME", side="yes", bid=mid-.01, ask=mid+.01, bid_size=100, ask_size=100)
    out.update(kw)
    return out


class MicrodataTests(unittest.TestCase):
    def test_future_append_does_not_change_past_features(self):
        past = [row(t, .40 + .001*t) for t in range(0, 31)]
        before = features_at(past, 30)
        with self.assertRaises(ValueError):
            features_at(past + [row(31, .9)], 30)
        self.assertEqual(before, features_at(past, 30))

    def test_trigger_is_confirmed_and_shared_book_collapses(self):
        rows = [row(t, .4 + .002*t) for t in range(31)]
        rows += [row(t, .4 + .002*t, book="kalshi", market="K") for t in range(31)]
        events = trigger_events(rows)
        self.assertEqual(len(events), 1)
        self.assertGreater(events[0]["dmid_30"], .05)

    def test_planted_leader_has_positive_h3_convergence(self):
        leader = {"t": 30, "book_id": "rothera", "contract": "R", "mid": .60}
        follower = {"t": 30, "book_id": "kalshi", "contract": "K", "mid": .50}
        rows = [row(60, .56)]
        label = lead_lag_label(rows, leader, follower, 30)
        self.assertIsNotNone(label)
        self.assertGreater(label["convergence"], 0)


class PaperExecutionTests(unittest.TestCase):
    """Hand-computed cases for every rule in quant/paperexec.py."""

    def test_partial_fill_exit_roll_and_settlement(self):
        # Ask 0.51 x 4 at t=1 (limit 0.51): 4 of 10 fill; bid 0.54 x 2 at t=31 sells 2, the
        # next observation's bid 0.55 x 1 sells 1, the last contract settles at $1.
        rows = [row(1, .50, ask=.51, ask_size=4), row(31, .55, bid=.54, bid_size=2), row(33, .56, bid=.55, bid_size=1)]
        t = ioc(rows, 0, .51, 10, ZeroFees(), latency_s=1, horizon_s=30, settlement=1)
        self.assertEqual((t.filled, [n for *_, n, _ in t.exits], t.settled, t.unresolved), (4, [2, 1], 1, 0))
        self.assertAlmostEqual(float(t.pnl), .54 * 2 + .55 + 1.0 - .51 * 4)

    def test_limit_is_respected_and_misses_are_counted(self):
        rows = [row(1, .55, ask=.56, ask_size=50)]
        self.assertEqual(ioc(rows, 0, .51, 10, ZeroFees()).reason, "ask moved above the limit")
        self.assertEqual(ioc([row(9, .5)], 0, .51, 10, ZeroFees(), latency_s=1).reason, "no quote when the order arrives")
        self.assertEqual(ioc([row(1, .5, ask=.51, ask_size=0)], 0, .51, 10, ZeroFees()).reason, "no displayed size")
        self.assertTrue(all(ioc(r, 0, .51, 10, ZeroFees()).pnl is None for r in ([row(1, .55, ask=.56)], [])))

    def test_unsold_contracts_without_a_settlement_are_unresolved_not_valued(self):
        rows = [row(1, .50, ask=.51, ask_size=10), row(31, .55, bid=.54, bid_size=3)]
        t = ioc(rows, 0, .51, 10, ZeroFees(), horizon_s=30, settlement=None)
        self.assertEqual((t.filled, t.unresolved), (10, 7))
        self.assertIsNone(t.pnl)      # excluded from P&L, never priced at an assumed value

    def test_fees_are_charged_per_order_at_the_real_count_on_both_sides(self):
        from arb_engine.fees.kalshi import KalshiFees

        fm = KalshiFees()
        rows = [row(1, .50, ask=.51, ask_size=10), row(31, .55, bid=.54, bid_size=10)]
        t = ioc(rows, 0, .51, 10, fm, horizon_s=30)
        self.assertEqual(t.entry_fee, fm.fee(.51, 10, "taker"))
        self.assertEqual(t.exit_fee, fm.fee(.54, 10, "taker"))
        self.assertAlmostEqual(float(t.pnl), (.54 - .51) * 10 - float(fm.fee(.51, 10, "taker")) - float(fm.fee(.54, 10, "taker")))

    def test_arb_legs_meet_their_own_books_after_their_own_latency(self):
        a = [row(1, .40, ask=.40, ask_size=10)]
        # Robinhood leg (a person, 15 s): the ask 0.50 x 10 at t=15 is gone by t=16 (0.58).
        b = [row(15, .50, book="robinhood", market="R", ask=.50, ask_size=10), row(16, .58, book="robinhood", market="R", ask=.58, ask_size=10)]
        fast = two_leg_arb(a, b, 0, .40, .50, 10, ZeroFees(), ZeroFees(), latency_a_s=1, latency_b_s=15)
        self.assertEqual((fast.matched, fast.leg_failure), (10, False))
        self.assertAlmostEqual(float(fast.pnl), 10 - 4.0 - 5.0)
        slow = two_leg_arb(a, b, 0, .40, .50, 10, ZeroFees(), ZeroFees(), latency_a_s=1, latency_b_s=16)
        self.assertEqual((slow.matched, slow.leg_failure), (0, True))

    def test_failed_leg_excess_is_unwound_at_the_bid_after_the_other_leg_is_known(self):
        a = [row(1, .40, ask=.40, ask_size=10), row(16, .36, bid=.35, bid_size=10)]
        b = [row(15, .50, book="robinhood", market="R", ask=.50, ask_size=4)]
        r = two_leg_arb(a, b, 0, .40, .50, 10, ZeroFees(), ZeroFees(), latency_a_s=1, latency_b_s=15, unwind_latency_s=1)
        self.assertEqual((r.matched, r.unwound, r.unresolved), (4, 6, 0))
        self.assertAlmostEqual(float(r.pnl), 4 * 1.0 + 6 * .35 - (10 * .40 + 4 * .50))

    def test_unknown_tie_payout_excludes_and_known_ones_price_the_tie_case(self):
        a = [row(1, .40, ask=.40, ask_size=10)]
        b = [row(1, .50, book="robinhood", market="R", ask=.50, ask_size=10)]
        self.assertEqual(two_leg_arb(a, b, 0, .40, .50, 10, ZeroFees(), ZeroFees(), latency_b_s=1, tie_payouts=(.5, None)).excluded, "unknown-tie")
        # Kalshi YES-A (0.50 on a tie) + Rothera YES-B (0 on a tie): wins +$1.00, loses on a tie.
        r = two_leg_arb(a, b, 0, .40, .50, 10, ZeroFees(), ZeroFees(), latency_b_s=1, tie_payouts=(.5, 0.0))
        self.assertAlmostEqual(float(r.pnl), 1.0)
        self.assertAlmostEqual(float(r.pnl_tie), 5.0 - 9.0)


class EvaluationDisciplineTests(unittest.TestCase):
    def test_fold_overlap_refused_and_bootstrap_deterministic(self):
        with self.assertRaises(ValueError):
            validate_manifest({"discovery": ["g"], "test": ["g"]})
        values = {"a": [1, 2], "b": [3]}
        self.assertEqual(game_block_bootstrap(values), game_block_bootstrap(values))

    def test_changed_open_test_spec_requires_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "eval.jsonl"
            log.write_text(json.dumps({"fold": "test", "spec_hash": spec_hash({"v": 1})}) + "\n")
            with self.assertRaises(RuntimeError):
                guard_test_open(log, spec_hash({"v": 2}))
            guard_test_open(log, spec_hash({"v": 2}), "pre-registered correction")

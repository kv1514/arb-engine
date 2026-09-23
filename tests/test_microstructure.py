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
    def test_partial_missed_and_settlement(self):
        rows = [row(1, .50, ask=.51, ask_size=4), row(31, .55, bid=.54, bid_size=2)]
        fill = ioc(rows, 0, 10, ZeroFees(), latency_s=1, horizon_s=30, settlement=1)
        self.assertEqual((fill.filled, fill.settled), (4, 2))
        self.assertFalse(fill.missed)
        self.assertTrue(ioc([], 0, 10, ZeroFees()).missed)

    def test_failed_arb_leg_unwinds_and_unknown_tie_excludes(self):
        a = {"ask": .4, "bid": .39, "ask_size": 10, "tie_payout": .5}
        b = {"ask": .5, "bid": .49, "ask_size": 0, "tie_payout": .5}
        result = two_leg_arb(a, b, 5, ZeroFees(), ZeroFees())
        self.assertTrue(result["leg_failure"])
        self.assertEqual(result["unwound"], 5)
        a["tie_payout"] = None
        self.assertEqual(two_leg_arb(a, b, 5, ZeroFees(), ZeroFees(), tie=True)["excluded"], "unknown-tie")


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

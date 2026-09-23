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


class BuilderTests(unittest.TestCase):
    """The streaming builder (quant/microdata.build): identity, causality, labels, speed."""

    def _ramp(self, book="kalshi", market="K", start=0, n=61, base=.40, slope=.002, **kw):
        return [row(start + t, base + slope * t, book=book, market=market, event_key="nfl:A|B:2026-10-11", outcome="A", **kw) for t in range(n)]

    def test_appending_the_future_changes_no_past_sample(self):
        from arb_engine.quant.microdata import build

        rows = self._ramp() + self._ramp(book="rothera", market="R", slope=.001)
        cut = 40
        past = [r for r in rows if r["obs_ts"] <= cut]
        full = build(rows, sample="all", horizons=())
        part = build(past, sample="all", horizons=())
        strip = lambda xs: [{k: v for k, v in x.items()} for x in xs if x["t"] <= cut]
        self.assertEqual(strip(full), strip(part))

    def test_yes_and_no_are_different_contracts_and_a_reseller_collapses(self):
        from arb_engine.quant.microdata import build

        yes = self._ramp()
        no = [dict(r, side="no", bid=round(1 - r["ask"], 4), ask=round(1 - r["bid"], 4)) for r in yes]
        # Robinhood showing the same Kalshi book (KX-routed): same identity, direct row wins.
        resold = [dict(r, venue="robinhood", venue_market_id="rh-uuid") for r in yes]
        out = build(yes + no + resold, sample="unconditional", horizons=())
        by_side = {}
        for s in out:
            by_side.setdefault((s["book_id"], s["side"]), []).append(s)
        self.assertEqual(set(by_side), {("kalshi", "yes"), ("kalshi", "no")})
        self.assertTrue(all(s["venue"] == "kalshi" for s in by_side[("kalshi", "yes")]))
        # a NO history never mixes with the YES one: its moves mirror, they do not jump 0.2
        self.assertTrue(all(abs(s["dmid_5"] or 0) < .02 for s in out))

    def test_planted_twenty_second_lead_is_convergence_on_the_follower(self):
        from arb_engine.quant.microdata import build

        # Rothera jumps 0.40 -> 0.48 at t=30; Kalshi follows at t=50.
        lead = [row(t, .40 if t < 30 else .48, book="rothera", market="R", venue="robinhood", event_key="g", outcome="A") for t in range(0, 90)]
        follow = [row(t, .40 if t < 50 else .48, book="kalshi", market="K", event_key="g", outcome="A") for t in range(0, 90)]
        out = build(lead + follow, sample="unconditional", horizons=(30,), fee_for_row=lambda r: None)
        k = next(s for s in out if s["book_id"] == "kalshi" and 35 <= s["t"] < 40)   # after the lead, before the catch-up
        self.assertEqual(k["leader_book"], "rothera")
        self.assertGreater(k["gap_leader"], .05)
        self.assertGreater(k["dmid_30_fwd"], .05)          # the follower converged within 30 s

    def test_missing_marks_stay_missing_and_carried_rows_are_not_observations(self):
        from arb_engine.quant.microdata import build

        rows = self._ramp(n=31) + [dict(r, refreshed=0) for r in self._ramp(start=31, n=60)]
        out = build(rows, sample="unconditional", horizons=(15,), fee_for_row=lambda r: None)
        late = [s for s in out if s["t"] >= 20]
        self.assertTrue(late and all(s["dmid_15_fwd"] is None for s in late))
        self.assertTrue(all(s["t"] <= 30 for s in out))

    def test_executable_label_uses_the_simulator_with_both_fees(self):
        from arb_engine.fees.kalshi import KalshiFees
        from arb_engine.quant.microdata import build

        rows = self._ramp(n=120, slope=.001)
        out = build(rows, sample="unconditional", horizons=(30,), fee_for_row=lambda r: KalshiFees(), ref_contracts=10)
        s = next(x for x in out if x["t"] == 10)
        # bought at the t=11 ask (limit = t=10 ask is exceeded by the rising ramp -> missed)
        self.assertIsNone(s["ret_long_30"])
        flat = [row(t, .50, event_key="g", outcome="A") for t in range(120)]
        s = next(x for x in build(flat, sample="unconditional", horizons=(30,), fee_for_row=lambda r: KalshiFees()) if x["t"] == 10)
        fm = KalshiFees()
        self.assertAlmostEqual(s["ret_long_30"], (.49 - .51) - float(fm.fee(.51, 10, "taker") + fm.fee(.49, 10, "taker")) / 10)

    def test_legacy_rows_load_with_approx_time_and_the_espn_and_prints_are_causal(self):
        from arb_engine.quant.microdata import build, rows_from_tick

        tick = {"ts": 100.0, "event_key": "nfl:A|B:2026-09-20", "live": 1,
                "l1_json": json.dumps({"kalshi": {"A": {"bid": .5, "ask": .52, "book_id": "kalshi", "venue_market_id": "K-A"}}})}
        r = rows_from_tick(tick)
        self.assertEqual((r[0]["obs_ts"], r[0]["approx_time"], r[0]["side"]), (100.0, 1, "yes"))
        rows = [dict(x, obs_ts=float(t), approx_time=1) for t in range(90, 131) for x in r]
        espn = [{"ts": 110.0, "event_key": "nfl:A|B:2026-09-20", "home": "A", "away": "B", "home_score": 0, "away_score": 0},
                {"ts": 125.0, "event_key": "nfl:A|B:2026-09-20", "home": "A", "away": "B", "home_score": 7, "away_score": 0}]
        prints = [{"ticker": "K-A", "ts": 118.5, "count": 10, "taker_side": "yes", "price": .6},
                  {"ticker": "K-A", "ts": 119.5, "count": 99, "taker_side": "yes", "price": .9}]
        out = {s["t"]: s for s in build(rows, espn=espn, prints=prints, sample="unconditional", horizons=())}
        self.assertEqual(out[120.0]["score_diff"], 0)       # the 7-0 row arrives at 125
        self.assertEqual(out[125.0]["score_diff"], 7)
        self.assertEqual(out[120.0]["flow_30"], 10)         # prints stamped <= t - 1: the 119.5 one is not visible yet
        self.assertAlmostEqual(out[120.0]["last_print_minus_mid"], .6 - .51)
        self.assertEqual(out[120.0]["approx_time"], 1)

    def test_a_full_game_day_builds_in_seconds(self):
        import time as _time
        from arb_engine.quant.microdata import build

        rows = []
        for g in range(8):
            for book, venue in (("kalshi", "kalshi"), ("rothera", "robinhood")):
                for o in ("A", "B"):
                    rows += [row(t, .4 + .05 * ((t // 97) % 3), book=book, market=f"{book}{g}{o}", venue=venue,
                                 event_key=f"g{g}", outcome=o) for t in range(0, 3600, 2)]
        t0 = _time.time()
        out = build(rows, sample="all", horizons=(5, 30), fee_for_row=lambda r: None)
        self.assertGreater(len(out), 1000)
        self.assertLess(_time.time() - t0, 60)


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

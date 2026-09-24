"""Causal microstructure samples, paper execution and frozen-fold discipline."""
import json
import tempfile
import unittest
from pathlib import Path

from arb_engine.fees.base import ZeroFees
from arb_engine.quant.microdata import features_at, lead_lag_label, trigger_events
from arb_engine.quant.paperexec import ioc, ioc_short_via_complement, two_leg_arb
from scripts.microstructure_eval import decision, fold_of, game_block_bootstrap, guard_test_open, holm, spec_hash, summarize, validate_manifest


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
        self.assertEqual((t.partial, t.cancelled), (True, 6))

    def test_exit_horizon_is_decision_plus_latency_not_late_fill_time(self):
        # Arrival at t=1, but the first usable observation is t=3 inside its tolerance. The
        # frozen +30 s mark remains t=31, rather than drifting to t=33.
        rows = [row(3, .50, ask=.51, ask_size=2), row(31, .55, bid=.54, bid_size=2), row(33, .70, bid=.69, bid_size=2)]
        t = ioc(rows, 0, .51, 2, ZeroFees(), latency_s=1, entry_tol_s=2, horizon_s=30)
        self.assertEqual(t.exits[0][0:3], (31.0, .54, 2))

    def test_duplicate_marks_cannot_reuse_the_same_displayed_depth(self):
        duplicate = row(33, .56, bid=.55, bid_size=2)
        rows = [row(1, .50, ask=.51, ask_size=5), row(31, .55, bid=.54, bid_size=0), duplicate, dict(duplicate)]
        t = ioc(rows, 0, .51, 5, ZeroFees(), horizon_s=30, settlement=1)
        self.assertEqual(([n for *_, n, _ in t.exits], t.settled), ([2], 3))

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
        self.assertFalse(r.guaranteed)

    def test_same_book_and_settlement_mismatch_are_not_guaranteed_arbs(self):
        a = [row(1, .40, ask=.40, ask_size=10, book="kalshi", market="K-A")]
        b = [row(1, .50, ask=.50, ask_size=10, book="kalshi", market="K-B", outcome="AWAY")]
        self.assertEqual(two_leg_arb(a, b, 0, .40, .50, 10, ZeroFees(), ZeroFees(), latency_b_s=1).excluded, "same-book")
        b[0]["book_id"] = "rothera"
        self.assertEqual(two_leg_arb(a, b, 0, .40, .50, 10, ZeroFees(), ZeroFees(), latency_b_s=1,
                                     settlement_compatible=False).excluded, "settlement-mismatch")

    def test_short_is_an_ioc_purchase_on_the_complement_book(self):
        complement = [row(1, .39, ask=.40, ask_size=3, outcome="AWAY")]
        t = ioc_short_via_complement(complement, 0, .40, 5, ZeroFees(), latency_s=1, settlement=1)
        self.assertEqual((t.filled, t.cancelled, t.entry_price), (3, 2, .40))


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

    def test_a_model_significantly_worse_than_unchanged_is_rejected(self):
        # Codex's rule only rejected a CI that straddled 0; a CI wholly below 0 slipped through.
        worse = {"skill_ci": {"lo": -.02, "hi": -.01}, "net_pnl_ci": {"lo": .01, "hi": .02}, "fill_rate": .9,
                 "test_games": 40, "deduped_triggers": 400, "top_game_share": .1,
                 "robust_l3_h05": {"net_pnl_ci": {"lo": .01}, "positive_game_share": .8}}
        self.assertEqual(decision(worse), "reject")
        good = dict(worse, skill_ci={"lo": .001, "hi": .01})
        self.assertEqual(decision(good), "alert-only")
        self.assertEqual(decision(dict(good, net_pnl_ci={"lo": -.03, "hi": -.01})), "shadow")   # P&L wholly negative
        self.assertEqual(decision(dict(good, test_games=12)), "shadow")

    def test_folds_are_whole_games_and_test_is_defined_by_date(self):
        m = {"discovery": [{"event_key": "nfl:A|B:2026-09-20"}], "test_from": "2026-10-08"}
        self.assertEqual(fold_of(m, "nfl:A|B:2026-09-20:spread:A-3.5"), "discovery")   # lines follow their game
        self.assertEqual(fold_of(m, "nfl:C|D:2026-09-27"), "validation")
        self.assertEqual(fold_of(m, "nfl:C|D:2026-10-08"), "test")
        with self.assertRaises(ValueError):
            validate_manifest({"discovery": [{"event_key": "nfl:C|D:2026-10-11"}], "test_from": "2026-10-08"})

    def test_holm_and_summary_metrics(self):
        self.assertEqual(holm({"a": .01, "b": .04, "c": .5}, alpha=.10), {"a": True, "b": True, "c": False})
        m = summarize({"g1": [.02, None, .04], "g2": [-.05], "g3": [None]}, {"g1": 3, "g2": 1, "g3": 1}, draws=200)
        self.assertEqual((m["attempts"], m["trades"], m["games"]), (5, 3, 2))
        self.assertAlmostEqual(m["fill_rate"], .6)
        self.assertAlmostEqual(m["top_game_share"], .06 / .11)
        self.assertAlmostEqual(m["max_drawdown_per_contract"], .05)
        self.assertAlmostEqual(m["positive_game_share"], .5)

    def test_end_to_end_on_a_recorded_database(self):
        import sqlite3
        from scripts import microstructure_eval as ev

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "h.db"
            con = sqlite3.connect(db)
            con.execute("create table inplay_ticks (ts real, event_key text, live integer, l1_json text, source text)")
            con.execute("create table espn_ticks (ts real, event_key text, status text, period integer, clock integer, home text, away text, home_score integer, away_score integer, last_play_type text)")
            for g in ("nfl:A|B:2026-09-27", "nfl:C|D:2026-09-27"):
                a, b = g.split(":")[1].split("|")
                for t in range(0, 400):
                    mid = .5 + .06 * ((t // 60) % 2)
                    rows = [{"venue": v, "book_id": bk, "outcome": o, "side": "yes", "obs_ts": 1000.0 + t, "refreshed": 1,
                             "venue_market_id": f"{bk}-{o}", "bid": round((mid if o == a else 1 - mid) - .01, 3),
                             "ask": round((mid if o == a else 1 - mid) + .01, 3), "bid_size": 50, "ask_size": 50,
                             "fee_params": {}, "exchange": "rothera" if bk == "rothera" else None}
                            for v, bk in (("kalshi", "kalshi"), ("robinhood", "rothera")) for o in (a, b)]
                    con.execute("insert into inplay_ticks values (?,?,?,?,?)", (1000.0 + t, g, 1, json.dumps({"rows": rows}), "fast"))
                con.execute("insert into espn_ticks values (?,?,?,?,?,?,?,?,?,?)", (1000.0, g, "in", 1, 900, a, b, 0, 0, None))
            con.commit()
            con.close()
            manifest = Path(tmp) / "m.json"
            manifest.write_text(json.dumps({"discovery": [], "test_from": "2026-10-08", "spec": {"horizons": [30], "bootstrap": 200, "seed": 1}}))
            out = Path(tmp) / "r.json"
            import contextlib
            import io

            with contextlib.redirect_stdout(io.StringIO()):
                rc = ev.main(["--db", str(db), "--fold", "validation", "--manifest", str(manifest), "--log", str(Path(tmp) / "log.jsonl"), "--results", str(out)])
            self.assertEqual(rc, 0)
            r = json.loads(out.read_text())
            self.assertEqual((r["games"], r["latency_s"], r["legacy_timestamps"]), (2, 1, False))
            h = r["horizons"]["30"]
            self.assertGreater(h["B1_buy_any"]["attempts"], 0)
            self.assertIn("skill_ci", h["B3_ridge_dmid30"])
            self.assertIn("H4_arb", r)
            self.assertEqual(set(r["H4_arb"]["by_margin"]), {"<1c", "1-3c", ">=3c"})
            self.assertEqual(set(h["H3_by_grade"]), {"hard", "soft", "agree>=1", "agree=0"})
            lk = r["H3_lock"]
            for k in ("entries_filled", "locked", "lock_conversion", "hold_no_lock", "locked_only"):
                self.assertIn(k, lk)
            self.assertEqual(json.loads((Path(tmp) / "log.jsonl").read_text().splitlines()[-1])["fold"], "validation")

    def test_h3_lock_locks_a_winner_and_holds_a_loser(self):
        """Replay of the lock watch: KC bought on Kalshi after a Rothera lead. In game g1 KC then
        rises and Kalshi's DEN falls far enough to lock; in g2 KC falls and nothing locks, so the
        position is sold to the bid at the end of the watch. The same entries held without
        locking are the baseline."""
        from arb_engine.fees.base import ZeroFees
        from scripts.microstructure_eval import h3_lock_trades

        rows, samples = [], []
        for g, later_kc in (("g1", .80), ("g2", .45)):
            ev = f"nfl:{g}:2026-09-27"
            for t in range(0, 700):
                kc = .60 if t < 100 else later_kc
                for book, venue, o, mid in (("kalshi", "kalshi", "KC", kc), ("kalshi", "kalshi", "DEN", 1 - kc)):
                    rows.append({"event_key": ev, "venue": venue, "book_id": book, "outcome": o, "side": "yes", "obs_ts": float(t), "refreshed": 1,
                                 "venue_market_id": f"{o}", "bid": round(mid - .01, 2), "ask": round(mid + .01, 2), "bid_size": 50, "ask_size": 50, "tie_payout": .5})
            samples.append({"kind": "unconditional", "t": 10.0, "event_key": ev, "book_id": "kalshi", "outcome": "KC", "side": "yes", "venue": "kalshi",
                            "ask": .61, "dmid_30": 0.0, "leader_dmid_30": .08, "gap_leader": .06})
        out = h3_lock_trades(samples, rows, lambda r: ZeroFees(), latency_s=1, watch_s=600, n=10)
        self.assertEqual((out["filled"], out["locked"]), (2, 1))
        self.assertAlmostEqual(out["locked_only"]["nfl:g1:2026-09-27"][0], 1 - .61 - .21)   # KC at 0.61 + DEN at 0.21 once KC is 0.80
        self.assertAlmostEqual(out["rets"]["nfl:g2:2026-09-27"][0], .44 - .61)              # never locked: sold to the 0.44 bid
        self.assertAlmostEqual(out["hold"]["nfl:g1:2026-09-27"][0], .79 - .61)              # held, not locked: the bid after the watch

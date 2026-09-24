"""Causal microstructure samples, paper execution and frozen-fold discipline."""
import json
import tempfile
import unittest
from pathlib import Path

from arb_engine.fees.base import ZeroFees
from arb_engine.quant.microdata import features_at, lead_lag_label, trigger_events
from arb_engine.quant.paperexec import ioc, two_leg_arb
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

        # Rothera jumps 0.40 -> 0.48 at t=30; Kalshi follows at t=50. NFL tie rules: a Kalshi YES
        # pays $0.50 on a tie, a Rothera YES $0.
        lead = [row(t, .40 if t < 30 else .48, book="rothera", market="R", venue="robinhood", event_key="nfl:A|B:2026-09-27", outcome="A", tie_payout=0.0) for t in range(0, 90)]
        follow = [row(t, .40 if t < 50 else .48, book="kalshi", market="K", event_key="nfl:A|B:2026-09-27", outcome="A", tie_payout=0.5) for t in range(0, 90)]
        out = build(lead + follow, sample="unconditional", horizons=(30,), fee_for_row=lambda r: None)
        k = next(s for s in out if s["book_id"] == "kalshi" and 35 <= s["t"] < 40)   # after the lead, before the catch-up
        self.assertEqual(k["leader_book"], "rothera")
        self.assertGreater(k["gap_leader"], .05)
        self.assertAlmostEqual(k["gap_leader"], k["cross"]["rothera"]["gap_raw"] + .004 * .5)   # the tie difference, priced
        self.assertTrue(k["cross"]["rothera"]["tie_mismatch"])
        self.assertGreater(k["dmid_30_fwd"], .05)          # the follower converged within 30 s
        # Adversarial: with no settlement rule on record for either book, no comparison is made.
        blind = build([dict(r, event_key="xyz:A|B:2026-09-27", tie_payout=None) for r in lead + follow], sample="unconditional",
                      horizons=(), fee_for_row=lambda r: None)
        self.assertTrue(all(s["leader_book"] is None and s["cross"] == {} for s in blind))

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
        self.assertAlmostEqual(m["resolved_rate"], .6)   # completed / attempted: a resolution rate, not a fill rate
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
            b1 = h["B1_buy_any"]
            self.assertGreater(b1["attempted_orders"], 0)
            self.assertEqual(b1["attempted_orders"], b1["missed_orders"] + b1["filled_orders"])
            self.assertEqual(b1["filled_orders"], b1["closed_positions"] + b1["settled_positions"] + b1["unresolved_positions"])
            self.assertAlmostEqual(b1["fill_rate"], b1["filled_orders"] / b1["attempted_orders"])
            for name in ("H1_momentum", "H2_dip", "H2_recovery", "H3_leadlag", "M_prototype", "B0_persistence"):
                self.assertIn(name, h)
            self.assertIn("skill_ci", h["B3_ridge_dmid30"])
            self.assertEqual(r["model_fit"]["from"], "earlier games of this fold")   # no discovery data here
            self.assertIn("H4_arb", r)
            self.assertEqual(set(r["H4_arb"]["by_margin"]), {"<1c", "1-3c", ">=3c"})
            self.assertIn("both_legs_fast", r["H4_arb"])
            self.assertEqual(set(h["H3_by_grade"]), {"hard", "soft", "agree>=1", "agree=0"})
            lk = r["H3_lock"]
            for k in ("entries_filled", "locked", "lock_conversion", "hold_no_lock", "locked_only"):
                self.assertIn(k, lk)
            self.assertIn("H3_leadlag@30", r["decisions"])
            self.assertEqual(r["primary"]["hypothesis"], "H3_leadlag@30")
            self.assertFalse(r["exploratory"])
            self.assertEqual(set(r["hashes"]), {"spec", "folds", "constants", "code", "frozen_models"})
            audit = json.loads((Path(tmp) / "log.jsonl").read_text().splitlines()[-1])
            self.assertEqual(audit["fold"], "validation")
            for k in ("utc", "git_head", "spec_hash", "hashes", "results_sha256", "exploratory", "argv"):
                self.assertIn(k, audit)
            import hashlib
            self.assertEqual(audit["results_sha256"], hashlib.sha256(out.read_bytes()).hexdigest())

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



def _db(tmp, games, n=400, kalshi_tie=.5, rothera_tie=0.0):
    """A small recorder database: Kalshi + Rothera YES rows for both teams, 1 s apart."""
    import sqlite3

    db = Path(tmp) / "h.db"
    con = sqlite3.connect(db)
    con.execute("create table inplay_ticks (ts real, event_key text, live integer, l1_json text, source text)")
    con.execute("create table espn_ticks (ts real, event_key text, status text, period integer, clock integer, home text, away text, home_score integer, away_score integer, last_play_type text)")
    for g in games:
        a, b = g.split(":")[1].split("|")
        for t in range(0, n):
            mid = .5 + .06 * ((t // 60) % 2)
            rows = [{"venue": v, "book_id": bk, "outcome": o, "side": "yes", "obs_ts": 1000.0 + t, "refreshed": 1,
                     "venue_market_id": f"{bk}-{o}", "bid": round((mid if o == a else 1 - mid) - .01, 3),
                     "ask": round((mid if o == a else 1 - mid) + .01, 3), "bid_size": 50, "ask_size": 50,
                     "fee_params": {}, "exchange": "rothera" if bk == "rothera" else None,
                     "tie_payout": rothera_tie if bk == "rothera" else kalshi_tie}
                    for v, bk in (("kalshi", "kalshi"), ("robinhood", "rothera")) for o in (a, b)]
            con.execute("insert into inplay_ticks values (?,?,?,?,?)", (1000.0 + t, g, 1, json.dumps({"rows": rows}), "fast"))
        con.execute("insert into espn_ticks values (?,?,?,?,?,?,?,?,?,?)", (1000.0, g, "in", 1, 900, a, b, 0, 0, None))
    con.commit()
    con.close()
    return db


def _run(argv):
    import contextlib
    import io
    from scripts import microstructure_eval as ev

    with contextlib.redirect_stdout(io.StringIO()):
        return ev.main(argv)


class SpecComplianceTests(unittest.TestCase):
    """Adversarial checks of the pre-registered spec (docs/MODEL.md, "Microstructure experiment")."""

    MAN = {"discovery": [{"event_key": "nfl:A|B:2026-09-20"}], "discovery_dates": ["2026-09-20", "2026-09-21"], "test_from": "2026-10-08",
           "test_kickoff": {"et_date": "2026-10-08", "utc": "2026-10-09T00:15:00Z"}}

    def test_discovery_is_by_date_even_when_a_game_is_missing_from_the_list(self):
        self.assertEqual(fold_of(self.MAN, "nfl:X|Y:2026-09-20"), "discovery")          # not enumerated
        self.assertEqual(fold_of(self.MAN, "nfl:X|Y:2026-09-21:total:44.5"), "discovery")
        self.assertEqual(fold_of(self.MAN, "nfl:X|Y:2026-09-22"), "validation")
        self.assertEqual(fold_of(self.MAN, "nfl:X|Y:2026-10-07"), "validation")
        self.assertEqual(fold_of(self.MAN, "nfl:X|Y:2026-10-08"), "test")
        with self.assertRaises(ValueError):
            validate_manifest({**self.MAN, "discovery_dates": ["2026-10-11"]})
        with self.assertRaises(ValueError):
            validate_manifest({**self.MAN, "test_kickoff": {"et_date": "2026-10-09"}})       # test_from must be the kickoff's date
        validate_manifest(json.loads((Path(__file__).parent / "fixtures/microstructure/manifest.json").read_text()))

    def test_the_forecast_models_never_score_the_data_they_were_fitted_on(self):
        from scripts.microstructure_eval import chrono_split, forecast_skill

        samples = [{"kind": "unconditional", "event_key": f"nfl:G{g}|H:2026-09-27", "t": 100.0 * g + i, "dmid_30": x, "dmid_30_fwd": 2 * x}
                   for g in range(6) for i, x in enumerate((-.2, -.1, .1, .2))]
        train, scored = chrono_split(samples, 2 / 3)
        self.assertEqual((train, scored), ([f"nfl:G{g}|H:2026-09-27" for g in range(4)], [f"nfl:G{g}|H:2026-09-27" for g in (4, 5)]))
        # Frozen coefficients are used as given: a zero model equals unchanged price even though
        # the scored data follow y = 2x exactly (refitting on them would score perfectly).
        m = forecast_skill(samples, 30, "dmid_30", 1, 200, coef=[0.0, 0.0])
        self.assertAlmostEqual(m["mae_model"], m["mae_persistence"])
        m = forecast_skill(samples, 30, "dmid_30", 1, 200, train=samples[:8])     # fitted on earlier games only
        self.assertAlmostEqual(m["coef"][1], 2.0, delta=.01)                  # ridge shrinks it a hair

    def test_the_test_fold_only_reads_frozen_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp, ["nfl:A|B:2026-10-11"], n=200)
            man = Path(tmp) / "m.json"
            man.write_text(json.dumps({**self.MAN, "spec": {"horizons": [30], "bootstrap": 50, "seed": 1}}))
            base = ["--db", str(db), "--manifest", str(man), "--log", str(Path(tmp) / "log.jsonl")]
            with self.assertRaises(SystemExit):
                _run(base + ["--fold", "test", "--frozen", str(Path(tmp) / "missing.json")])
            with self.assertRaises(SystemExit):
                _run(base + ["--fold", "test", "--freeze", str(Path(tmp) / "f.json")])
            self.assertFalse((Path(tmp) / "log.jsonl").exists())            # refused before anything was opened or logged

    def test_a_changed_test_spec_is_refused_then_visibly_exploratory(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp, ["nfl:A|B:2026-10-11", "nfl:C|D:2026-10-11"], n=200)
            man = Path(tmp) / "m.json"
            man.write_text(json.dumps({**self.MAN, "spec": {"horizons": [30], "bootstrap": 50, "seed": 1}}))
            frozen = Path(tmp) / "frozen.json"
            frozen.write_text(json.dumps({"coef": {"B3_ridge_dmid30": {"30": [0, 0]}, "B4_ridge_gap": {"30": [0, 0]}}, "trained_on": []}))
            out = Path(tmp) / "r.json"
            base = ["--db", str(db), "--manifest", str(man), "--log", str(Path(tmp) / "log.jsonl"), "--fold", "test", "--frozen", str(frozen), "--results", str(out)]
            self.assertEqual(_run(base), 0)
            first = json.loads(out.read_text())
            self.assertFalse(first["exploratory"])
            self.assertEqual(first["model_fit"]["from"], "frozen")
            frozen.write_text(json.dumps({"coef": {"B3_ridge_dmid30": {"30": [0, .1]}, "B4_ridge_gap": {"30": [0, 0]}}, "trained_on": []}))
            with self.assertRaises(RuntimeError):
                _run(base)                                                       # a different model after opening
            with self.assertRaises(ValueError):
                _run(base + ["--reopen-test", "  "])
            self.assertEqual(_run(base + ["--reopen-test", "coefficient typo found after opening"]), 0)
            again = json.loads(out.read_text())
            self.assertTrue(again["exploratory"])
            self.assertTrue(again["decisions"] and all(v.startswith("exploratory:") for v in again["decisions"].values()))
            log = [json.loads(x) for x in (Path(tmp) / "log.jsonl").read_text().splitlines()]
            self.assertEqual([x["exploratory"] for x in log], [False, True])
            self.assertEqual(log[1]["reopen_reason"], "coefficient typo found after opening")

    def test_the_spec_hash_covers_constants_folds_code_and_frozen_models(self):
        from arb_engine.quant import microdata
        from scripts.microstructure_eval import effective_spec

        base = spec_hash(effective_spec(self.MAN))
        self.assertNotEqual(base, spec_hash(effective_spec({**self.MAN, "discovery_dates": ["2026-09-20"]})))
        self.assertNotEqual(base, spec_hash(effective_spec(self.MAN, frozen={"coef": {}})))
        saved = microdata.TRIGGER_MOVE
        try:
            microdata.TRIGGER_MOVE = 0.04
            self.assertNotEqual(base, spec_hash(effective_spec(self.MAN)))
        finally:
            microdata.TRIGGER_MOVE = saved
        with tempfile.TemporaryDirectory() as tmp:
            from scripts.microstructure_eval import CODE_FILES
            for f in CODE_FILES:
                (Path(tmp) / f).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / f).write_text("x")
            a = spec_hash(effective_spec(self.MAN, root=Path(tmp)))
            (Path(tmp) / CODE_FILES[1]).write_text("y")                          # the paper executor changed
            self.assertNotEqual(a, spec_hash(effective_spec(self.MAN, root=Path(tmp))))

    def test_fills_are_not_resolutions(self):
        from scripts.microstructure_eval import candidate_metrics

        def s(t, status, filled, pnl, fwd):
            return {"t": t, "event_key": "nfl:A|B:2026-09-27", "exec_30": {"status": status, "requested": 10, "filled": filled, "fees": .1 if filled else 0.0, "pnl": pnl},
                    "dmid_30_fwd": fwd}
        sel = [s(1, "missed", 0, None, .01), s(2, "closed", 10, .5, .02), s(3, "unresolved", 10, None, None), s(4, "settled", 4, -.2, -.01)]
        m = candidate_metrics(sel, 30, 1, 100)
        self.assertEqual((m["attempted_orders"], m["filled_orders"], m["filled_contracts"], m["missed_orders"]), (4, 3, 24, 1))
        self.assertEqual((m["closed_positions"], m["settled_positions"], m["unresolved_positions"]), (1, 1, 1))
        self.assertAlmostEqual(m["fill_rate"], 3 / 4)                     # filled orders / attempted orders
        self.assertAlmostEqual(m["resolved_share_of_fills"], 2 / 3)
        self.assertEqual((m["labelled"], m["missing_labels"]), (3, 1))
        self.assertAlmostEqual(m["dollar_pnl"], .3)
        self.assertAlmostEqual(m["fees"], .2)                              # resolved positions only
        self.assertAlmostEqual(m["directional_hit"], 2 / 3)

    def test_drawdown_is_chronological_not_by_game(self):
        # Game b's loss happens between game a's two gains: by game the curve never dips below
        # its peak by more than 0.03; in time order it falls 0.05 from a peak of 0.02.
        m = summarize({"a": [.02, .03], "b": [-.05]}, {"a": 2, "b": 1}, draws=50, times={"a": [1.0, 3.0], "b": [2.0]})
        self.assertAlmostEqual(m["max_drawdown_per_contract"], .05)

    def test_decision_rules_edges(self):
        ok = {"skill_ci": {"lo": .001}, "net_pnl_ci": {"lo": .01}, "fill_rate": .9, "test_games": 40, "deduped_triggers": 400,
              "top_game_share": .1, "robust_l3_h05": {"net_pnl_ci": {"lo": .01}, "positive_game_share": .8}}
        self.assertEqual(decision(ok), "alert-only")
        self.assertEqual(decision(dict(ok, skill_ci={"lo": 0.0, "hi": .01})), "reject")        # includes zero
        self.assertEqual(decision(dict(ok, skill_ci={"lo": -.01, "hi": .01})), "reject")
        for bad in ({"net_pnl_ci": {"lo": -.001, "hi": .02}}, {"fill_rate": .59}, {"test_games": 29}, {"deduped_triggers": 199},
                    {"top_game_share": .41}, {"robust_l3_h05": {"net_pnl_ci": {"lo": .01}, "positive_game_share": .59}},
                    {"robust_l3_h05": {"net_pnl_ci": {"lo": -.01}, "positive_game_share": .9}}):
            self.assertEqual(decision(dict(ok, **bad)), "shadow", bad)

    def test_holm_family_is_the_pre_registered_secondary_list(self):
        self.assertEqual(holm({"H1_momentum@30": .001, "H4_arb": .2}, .10), {"H1_momentum@30": True, "H4_arb": False})
        spec = json.loads((Path(__file__).parent / "fixtures/microstructure/manifest.json").read_text())["spec"]
        self.assertEqual(spec["primary"], "H3@30s")
        self.assertNotIn("H3_leadlag@30", spec["secondary"])                   # the primary is tested alone
        self.assertIn("M_prototype@30", spec["secondary"])


class H2AndCausalityTests(unittest.TestCase):
    def _path(self, mids, bids_up=True):
        out = []
        for t, m in enumerate(mids):
            r = row(t, m, event_key="nfl:A|B:2026-09-27", outcome="A", tie_payout=.5)
            if not bids_up and t >= 40:                                     # the bounce is only the ask lifting
                r["bid"], r["ask"] = mids[39] - .01, m + .02
            out.append(r)
        return out

    def test_dip_and_recovery_are_different_decisions(self):
        from arb_engine.quant.microdata import build

        fall = [.60 - .003 * t for t in range(31)] + [.51] * 9               # falls 9c, then flat
        bounce = fall + [.51 + .005 * (t + 1) for t in range(10)]            # then comes back 5c
        dips = [s for s in build(self._path(fall), sample="all", horizons=()) if s["kind"] == "trigger"]
        self.assertTrue(dips and all(s["dmid_30"] < 0 for s in dips))
        self.assertEqual([s for s in build(self._path(fall), sample="all", horizons=()) if s["kind"] == "recovery"], [])   # no bounce yet
        rec = [s for s in build(self._path(bounce), sample="all", horizons=()) if s["kind"] == "recovery"]
        self.assertEqual(len(rec), 1)                                         # once per contract per 60 s
        self.assertGreaterEqual(rec[0]["t"], 41)
        self.assertGreaterEqual(rec[0]["drop_60"], .05)
        self.assertGreaterEqual(rec[0]["rebound"], .01)
        spread_only = [s for s in build(self._path(bounce, bids_up=False), sample="all", horizons=()) if s["kind"] == "recovery"]
        self.assertEqual(spread_only, [])                                     # an ask lifting alone is not a recovery

    def test_a_print_received_late_is_a_future_input(self):
        from arb_engine.quant.microdata import _Prints

        p = _Prints([{"ticker": "K", "ts": 95.0, "recv_ts": 103.0, "count": 10, "taker_side": "yes", "price": .5},
                     {"ticker": "K", "ts": 99.5, "count": 7, "taker_side": "yes", "price": .5}])
        self.assertEqual(p.at("K", 100.0, "yes", .5)["flow_30"], 0)          # stamped 95 but in hand only at 103; 99.5 not until 100.5
        self.assertEqual(p.at("K", 101.0, "yes", .5)["flow_30"], 7)
        self.assertEqual(p.at("K", 103.0, "yes", .5)["flow_30"], 17)

    def test_cross_book_freshness_by_venue(self):
        from arb_engine.quant.microdata import build

        ev = "nfl:A|B:2026-09-27"
        me = [row(t, .40, event_key=ev, outcome="A", tie_payout=.5) for t in range(0, 40)]
        rh = [row(29, .50, book="rothera", market="R", venue="robinhood", event_key=ev, outcome="A", tie_payout=0.0)]
        pm = [row(29, .50, book="polymarket", market="P", venue="polymarket", event_key=ev, outcome="A", tie_payout=.5)]
        out = {s["t"]: s for s in build(me + rh + pm, sample="unconditional", horizons=(), fee_for_row=lambda r: None) if s["book_id"] == "kalshi"}
        self.assertEqual(set(out[30.0]["cross"]), {"rothera", "polymarket"})   # both 1 s old
        self.assertEqual(set(out[35.0]["cross"]), {"polymarket"})            # 6 s old: > 2 s for the fast-lane Rothera, <= 6 s for Polymarket

    def test_a_persistent_lag_is_one_decision_per_minute(self):
        from scripts.microstructure_eval import _h3, select

        s = [{"kind": "unconditional", "t": float(t), "venue": "kalshi", "event_key": "nfl:A|B:2026-09-27", "book_id": "kalshi", "outcome": "A",
              "side": "yes", "leader_dmid_30": .08, "dmid_30": 0.0, "gap_leader": .05} for t in range(0, 120, 5)]
        self.assertEqual(len(select(s, _h3, 60.0)), 2)
        self.assertEqual(len(select(s, _h3, 0.0)), 24)

    def test_the_guaranteed_arb_counts_the_tie(self):
        from arb_engine.fees.base import ZeroFees
        from scripts.microstructure_eval import arb_scan

        ev = "nfl:A|B:2026-09-27"
        def rows(no_leg):
            out = []
            for t in range(0, 40):
                out.append(row(t, .44, event_key=ev, outcome="A", tie_payout=.5))                          # Kalshi YES A at .45
                if no_leg:   # Rothera NO of A, shown on B: pays $1 on a tie
                    out.append(row(t, .44, book="rothera", market="RA#no", venue="robinhood", event_key=ev, outcome="B", side="no", tie_payout=1.0))
                else:
                    out.append(row(t, .44, book="rothera", market="RB", venue="robinhood", event_key=ev, outcome="B", tie_payout=0.0))
            return out
        unsafe = arb_scan(rows(False), lambda r: ZeroFees(), 1, 5)[0]
        self.assertEqual(unsafe["tie_safe"], False)
        self.assertAlmostEqual(unsafe["pnl_win"], .10)
        self.assertAlmostEqual(unsafe["pnl_tie"], .5 - .90)
        self.assertAlmostEqual(unsafe["pnl_worst"], -.40)                     # a tie turns this "lock" into a loss
        safe = arb_scan(rows(True), lambda r: ZeroFees(), 1, 5)[0]
        self.assertEqual(safe["tie_safe"], True)
        self.assertAlmostEqual(safe["pnl_worst"], .10)

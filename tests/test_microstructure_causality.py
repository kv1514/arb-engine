"""Microdata / evaluator causality: what may be read at an instant, and what may not.

Companion to ``test_microstructure_instants.py`` (one instant, one decision).  Every case
here is synthetic - no recorded database, no network, no order - and pins one causal or
determinism property of the offline experiment:

* an equally strong leader that contradicts another is no leader, and no book's *name*
  decides a trade (audit M1);
* a paper exit, roll or settlement meets the book strictly after the fill (audit M4);
* prints that share a receipt time are ordered by content, not by arrival;
* rows at one observation time enter state together, duplicates count once, conflicts void
  the contract, a late receipt hides a print, appending the future changes nothing;
* a resale alias collapses into the book it resells;
* a missing future window is excluded *and* counted;
* a settlement mismatch, an unknown tie and a partial fill never become a guaranteed claim;
* the H3-lock entry is priced by the fee model of the decision's own row.

Nothing here measures profitability: a passing suite says the accounting is causal, not that
the strategy makes money, and says nothing at all about live safety.
"""
import json
import math
import unittest
from unittest import mock

from arb_engine.fees.base import ZeroFees

EV = "nfl:A|B:2026-09-27"
KALSHI_FEES = {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1, "series": "KXNFLGAME"}


def obs(t, book, outcome, mid=None, venue=None, side="yes", tie=0.5, size=100, bid=None, ask=None,
        event=EV, **kw):
    """One contract observation; ``mid`` is shorthand for a 2c book around it."""
    if bid is None or ask is None:
        bid, ask = round(mid - .01, 4), round(mid + .01, 4)
    r = {"event_key": event, "obs_ts": float(t), "req_ts": float(t), "quote_time": float(t), "refreshed": 1,
         "in_play": True, "source": "fast", "venue": venue or book, "book_id": book,
         "venue_market_id": f"{book}-{outcome}-{side}", "outcome": outcome, "side": side,
         "bid": bid, "ask": ask, "bid_size": size, "ask_size": size, "tie_payout": tie}
    r.update(kw)
    return r


def identical_settlement():
    return mock.patch("arb_engine.quant.microdata.settlement_relation", return_value="identical")


# ---------------------------------------------------------------------------------------
class LeaderChoiceTests(unittest.TestCase):
    """M1: the H3 leader of an instant is economics, never the books' names.

    The follower sits at .50 for a minute.  One other book rises 6c over the 30 s before
    t=40, another falls 6c over the same 30 s.  |dmid_30| is exactly equal, so the two
    leaders are equally strong and say the opposite thing.
    """

    FOLLOWER = "rothera"

    def rows(self, up, down, up_mid=(.47, .53), down_mid=(.53, .47)):
        out = []
        for t in range(0, 61):
            step = min(max(t - 10, 0), 30) / 30.0
            out.append(obs(t, self.FOLLOWER, "A", .50, venue="robinhood"))
            out.append(obs(t, self.FOLLOWER, "B", .50, venue="robinhood"))
            out.append(obs(t, up, "A", up_mid[0] + (up_mid[1] - up_mid[0]) * step, venue="kalshi"))
            out.append(obs(t, down, "A", down_mid[0] + (down_mid[1] - down_mid[0]) * step, venue="polymarket"))
        return out

    def build(self, rows):
        from arb_engine.quant.microdata import build

        with identical_settlement():
            return build(rows, sample="unconditional", horizons=(), fee_for_row=lambda r: None)

    def follower_at(self, samples, t=40.0):
        return next(s for s in samples if s["t"] == t and s["book_id"] == self.FOLLOWER and s["outcome"] == "A")

    def trades(self, samples):
        from scripts.microstructure_eval import _h3_direction, select_trades

        sel, _ = select_trades(samples, _h3_direction, 60.0)
        return [(s["t"], d, tuple(bc["key"])) for s, d, bc in sel]

    def test_equally_strong_leaders_that_contradict_are_no_leader(self):
        s = self.follower_at(self.build(self.rows("alpha", "zulu")))
        self.assertEqual(sorted(s["cross"]), ["alpha", "zulu"])     # both books are registered comparisons
        self.assertIsNone(s["leader_book"])
        self.assertIsNone(s["leader_dmid_30"])
        self.assertIsNone(s["gap_leader"])
        self.assertIsNone((s["leader_diag"] or {})["tie_matched"])
        self.assertIsNone((s["leader_diag"] or {})["any_settlement"])

    def test_renaming_tied_leaders_never_flips_the_trade(self):
        a = self.trades(self.build(self.rows("alpha", "zulu")))
        b = self.trades(self.build(self.rows("zulu", "alpha")))
        self.assertEqual(a, b)
        self.assertEqual(a, [])            # contradicting leaders decide nothing

    def test_a_stronger_leader_still_leads_whatever_it_is_called(self):
        for up, down in (("alpha", "zulu"), ("zulu", "alpha")):
            s = self.follower_at(self.build(self.rows(up, down, up_mid=(.44, .56))))   # +12c vs -6c
            self.assertEqual(s["leader_book"], up)
            self.assertAlmostEqual(s["leader_dmid_30"], .12, places=6)

    def test_tied_leaders_that_agree_register_the_weaker_signal(self):
        """Two books both up 6c, one 5c above the follower and one 2c above: the registered
        gap is the smaller one, whichever book carries it."""
        for near, far in (("alpha", "zulu"), ("zulu", "alpha")):
            rows = self.rows(near, far, up_mid=(.46, .52), down_mid=(.49, .55))
            s = self.follower_at(self.build(rows))
            self.assertAlmostEqual(s["leader_dmid_30"], .06, places=6)
            self.assertAlmostEqual(s["gap_leader"], .02, places=6)
            self.assertEqual(s["leader_book"], near)


# ---------------------------------------------------------------------------------------
class PaperExitCausalityTests(unittest.TestCase):
    """M4 (paperexec half): the exit, the rolls and the settlement meet the book strictly
    after the observation that filled the entry."""

    def row(self, t, bid, ask, size=100):
        return {"obs_ts": float(t), "refreshed": 1, "bid": bid, "ask": ask, "bid_size": size, "ask_size": size,
                "book_id": "kalshi", "venue_market_id": "K-A-yes", "side": "yes"}

    def test_the_exit_never_meets_the_observation_that_filled_the_entry(self):
        from arb_engine.quant.paperexec import ioc_round_trip

        # The one observation in the arrival window [1, 6] sits exactly on the 2 s horizon.
        tr = ioc_round_trip([self.row(3.0, .70, .50)], 0.0, .50, 10, ZeroFees(),
                            latency_s=1.0, horizon_s=2.0, entry_tol_s=5.0)
        self.assertEqual(tr.filled, 10)
        self.assertEqual(tr.exits, [])
        self.assertEqual(tr.unresolved, 10)
        self.assertIsNone(tr.pnl)

    def test_a_later_observation_still_closes_the_position(self):
        from arb_engine.quant.paperexec import ioc_round_trip

        tr = ioc_round_trip([self.row(3.0, .70, .50), self.row(3.5, .70, .72)], 0.0, .50, 10, ZeroFees(),
                            latency_s=1.0, horizon_s=2.0, entry_tol_s=5.0)
        self.assertEqual([(t, p, n) for t, p, n, _ in tr.exits], [(3.5, .70, 10)])
        self.assertAlmostEqual(tr.pnl_per_contract, .20, places=9)

    def test_settlement_only_follows_a_fill_it_could_not_sell(self):
        from arb_engine.quant.paperexec import ioc_round_trip

        tr = ioc_round_trip([self.row(3.0, .70, .50)], 0.0, .50, 10, ZeroFees(),
                            latency_s=1.0, horizon_s=2.0, entry_tol_s=5.0, settlement=1.0)
        self.assertEqual(tr.settled, 10)          # held to the result, never sold into its own snapshot
        self.assertEqual(tr.exits, [])
        self.assertAlmostEqual(tr.pnl_per_contract, .50, places=9)

    def test_the_exit_horizon_is_still_decision_plus_latency_not_the_fill_time(self):
        """The registered horizon must not drift with a late fill (existing contract)."""
        from arb_engine.quant.paperexec import ioc_round_trip

        rows = [self.row(2.5, .49, .50), self.row(31.5, .60, .61), self.row(33.5, .80, .81)]
        tr = ioc_round_trip(rows, 0.0, .50, 10, ZeroFees(), latency_s=1.0, horizon_s=30.0, entry_tol_s=2.0)
        self.assertEqual([t for t, _, _, _ in tr.exits], [31.5])     # decision + 1 + 30, not fill + 30


# ---------------------------------------------------------------------------------------
class PrintOrderTests(unittest.TestCase):
    """Prints that arrived in one response share a receipt time; the feature they produce
    must not depend on which order the page listed them in."""

    def prints(self):
        return [{"ticker": "K", "ts": 10.0, "price": .60, "count": 5, "taker_side": "yes", "obs_ts": 20.0, "trade_id": "a"},
                {"ticker": "K", "ts": 11.0, "price": .40, "count": 5, "taker_side": "no", "obs_ts": 20.0, "trade_id": "b"}]

    def test_prints_sharing_a_receipt_time_do_not_depend_on_arrival_order(self):
        from arb_engine.quant.microdata import _Prints

        rows = self.prints()
        a = _Prints(rows).at("K", 25.0, "yes", .5)
        b = _Prints(list(reversed(rows))).at("K", 25.0, "yes", .5)
        self.assertEqual(a, b)
        self.assertEqual(a["flow_30"], 0.0)                 # +5 yes and -5 no, whichever order

    def test_a_print_is_invisible_until_its_receipt_time(self):
        from arb_engine.quant.microdata import _Prints

        p = _Prints([{"ticker": "K", "ts": 10.0, "price": .60, "count": 5, "taker_side": "yes", "obs_ts": 40.0}])
        self.assertEqual(p.at("K", 39.9, "yes", .5).get("flow_60"), 0)
        self.assertEqual(p.at("K", 40.0, "yes", .5)["flow_60"], 5)


# ---------------------------------------------------------------------------------------
class InstantStateTests(unittest.TestCase):
    """Equal timestamps, duplicates, conflicts and resale aliases."""

    def base(self):
        return [obs(t, "kalshi", o, m, fee_params=dict(KALSHI_FEES))
                for t in range(0, 40) for o, m in (("A", .40), ("B", .60))]

    def test_rows_at_one_observation_time_enter_state_together(self):
        from arb_engine.quant.microdata import features_at

        rows = self.base() + [obs(39, "rothera", "A", .44, venue="robinhood")]
        a = features_at(rows, 39.0)
        b = features_at(list(reversed(rows)), 39.0)
        self.assertEqual(sorted((list(k), sorted(v.items())) for k, v in a.items()),
                         sorted((list(k), sorted(v.items())) for k, v in b.items()))
        self.assertIn((EV, "rothera", "A", "yes"), a)

    def test_features_at_refuses_an_input_from_the_future(self):
        from arb_engine.quant.microdata import features_at

        with self.assertRaises(ValueError):
            features_at(self.base() + [obs(60, "kalshi", "A", .40)], 39.0)

    def test_duplicated_rows_are_one_observation_and_one_fill(self):
        from arb_engine.quant.microdata import _dedupe, series_by_contract

        rows = self.base()
        doubled = rows + [dict(r) for r in rows]
        self.assertEqual(len(_dedupe(doubled)), len(_dedupe(rows)))
        self.assertEqual({k: len(v) for k, v in series_by_contract(doubled).items()},
                         {k: len(v) for k, v in series_by_contract(rows).items()})

    def test_a_resale_alias_collapses_into_the_book_it_resells(self):
        from arb_engine.quant.microdata import contract_key, resolve_instant

        direct = obs(5, "kalshi", "A", .40, venue="kalshi")
        resale = obs(5, "kalshi", "A", .30, venue="robinhood", venue_market_id="RH-KX-A")
        self.assertEqual(contract_key(direct), contract_key(resale))
        for order in ([(0, direct), (0, resale)], [(0, resale), (0, direct)]):
            got, why = resolve_instant(order)
            self.assertIsNone(why)
            self.assertEqual(got[1]["venue"], "kalshi")      # the reseller never replaces the book

    def test_conflicting_rows_void_the_contract_at_that_instant(self):
        from arb_engine.quant.microdata import build, observation_instants

        clash = [obs(20, "kalshi", "A", .40), obs(20, "kalshi", "A", .55)]
        rows = [r for r in self.base() if not (r["obs_ts"] == 20 and r["outcome"] == "A")] + clash
        t20 = next(x for x in observation_instants(rows) if x[0] == 20.0)
        self.assertIn((EV, "kalshi", "A", "yes"), t20[2])
        with identical_settlement():
            samples = build(rows, sample="unconditional", horizons=(), fee_for_row=lambda r: None)
        self.assertFalse([s for s in samples if s["t"] == 20.0 and s["outcome"] == "A"])
        b = next(s for s in samples if s["t"] == 20.0 and s["outcome"] == "B")
        self.assertIsNone(b["complement"])                   # a voided contract is no complement either
        self.assertEqual(b["complement_missing"], "ambiguous")


# ---------------------------------------------------------------------------------------
class LabelWindowTests(unittest.TestCase):
    def rows(self, extra=()):
        out = [obs(t, "kalshi", o, m, fee_params=dict(KALSHI_FEES))
               for t in range(0, 20) for o, m in (("A", .40), ("B", .60))]
        return out + list(extra)

    def build(self, rows, horizons=(5,)):
        from arb_engine.quant.microdata import build

        with identical_settlement():
            return build(rows, sample="unconditional", horizons=horizons, fee_for_row=lambda r: None)

    def test_a_missing_future_window_is_excluded_and_counted(self):
        from scripts.microstructure_eval import candidate_metrics

        samples = self.build(self.rows())
        late = [s for s in samples if s["t"] >= 15.0]
        self.assertTrue(late)
        self.assertTrue(all(s["dmid_5_fwd"] is None for s in late))   # nothing recorded at t+5
        m = candidate_metrics([dict(s) for s in samples if s["book_id"] == "kalshi" and s["outcome"] == "A"],
                              5, seed=1, draws=10)
        self.assertGreater(m["missing_labels"], 0)
        self.assertEqual(m["labelled"] + m["missing_labels"], m["attempted_orders"])

    def test_the_label_is_the_first_refreshed_mark_in_the_window(self):
        carried = [obs(5.5, "kalshi", "A", .90, refreshed=0)]
        s = next(x for x in self.build(self.rows(carried)) if x["t"] == 0.0 and x["outcome"] == "A")
        self.assertAlmostEqual(s["dmid_5_fwd"], 0.0, places=9)        # the carried .90 row is not a mark

    def test_appending_the_future_changes_no_prior_feature_or_label(self):
        base = self.build(self.rows())
        later = self.build(self.rows([obs(t, "kalshi", o, m, fee_params=dict(KALSHI_FEES))
                                      for t in range(40, 60) for o, m in (("A", .90), ("B", .10))]))
        keep = {(s["t"], s["outcome"]): s for s in later if s["t"] < 14.0}
        for s in base:
            if s["t"] < 14.0:
                self.assertEqual(json.dumps(s, sort_keys=True, default=str),
                                 json.dumps(keep[(s["t"], s["outcome"])], sort_keys=True, default=str))


# ---------------------------------------------------------------------------------------
class GuaranteedClaimTests(unittest.TestCase):
    """A guaranteed arbitrage needs two distinct books, verified settlement and known ties."""

    def legs(self, ask_a=.45, ask_b=.50):
        a = [obs(t, "kalshi", "A", None, bid=ask_a - .01, ask=ask_a) for t in range(0, 20)]
        b = [obs(t, "rothera", "B", None, bid=ask_b - .01, ask=ask_b, venue="robinhood") for t in range(0, 20)]
        return a, b

    def test_a_settlement_mismatch_is_excluded_not_guaranteed(self):
        from arb_engine.quant.paperexec import two_leg_arb

        a, b = self.legs()
        res = two_leg_arb(a, b, 0.0, .45, .50, 10, ZeroFees(), ZeroFees(), latency_a_s=1, latency_b_s=1,
                          tie_payouts=(.5, .5), settlement_compatible=False, book_id_a="kalshi", book_id_b="rothera")
        self.assertEqual(res.excluded, "settlement-mismatch")
        self.assertFalse(res.guaranteed)
        self.assertIsNone(res.pnl)

    def test_an_unknown_tie_is_excluded_not_guaranteed(self):
        from arb_engine.quant.paperexec import two_leg_arb

        a, b = self.legs()
        res = two_leg_arb(a, b, 0.0, .45, .50, 10, ZeroFees(), ZeroFees(), latency_a_s=1, latency_b_s=1,
                          tie_payouts=(.5, None), settlement_compatible=True, book_id_a="kalshi", book_id_b="rothera")
        self.assertEqual(res.excluded, "unknown-tie")
        self.assertFalse(res.guaranteed)

    def test_a_shared_book_is_never_an_arb(self):
        from arb_engine.quant.paperexec import two_leg_arb

        a, b = self.legs()
        res = two_leg_arb(a, b, 0.0, .45, .50, 10, ZeroFees(), ZeroFees(), latency_a_s=1, latency_b_s=1,
                          tie_payouts=(.5, .5), settlement_compatible=True, book_id_a="kalshi", book_id_b="kalshi")
        self.assertEqual(res.excluded, "same-book")
        self.assertFalse(res.guaranteed)

    def test_guaranteed_and_speculation_are_reported_apart(self):
        from scripts.microstructure_eval import h4_report

        recs = [{"game": "g", "t": 1.0, "margin": .05, "tie_safe": True, "settlement": "identical",
                 "class": "guaranteed-eligible", "guaranteed_result": True, "excluded": None, "legs_filled": 2,
                 "matched": 10, "unwound": 0, "unresolved": 0, "pnl_win": .05, "pnl_tie": .01, "pnl_worst": .01, "pnl_ev": .05},
                {"game": "g", "t": 2.0, "margin": .05, "tie_safe": False, "settlement": "unverified",
                 "class": "speculation", "guaranteed_result": False, "excluded": None, "legs_filled": 2,
                 "matched": 10, "unwound": 0, "unresolved": 0, "pnl_win": .40, "pnl_tie": -.60, "pnl_worst": -.60, "pnl_ev": .39}]
        rep = h4_report(recs, seed=1, draws=10)
        self.assertEqual(rep["guaranteed"]["trades"], 1)
        self.assertAlmostEqual(rep["guaranteed"]["mean_ret"]["point"], .01, places=9)
        self.assertEqual(rep["speculation"]["trades"], 1)
        self.assertAlmostEqual(rep["speculation"]["mean_ret"]["point"], .40, places=9)

    def test_a_partial_fill_is_counted_and_the_remainder_is_never_valued(self):
        from arb_engine.quant.microdata import exec_record
        from arb_engine.quant.paperexec import ioc_round_trip

        rows = [{"obs_ts": 1.0, "refreshed": 1, "bid": .44, "ask": .45, "bid_size": 0, "ask_size": 4,
                 "book_id": "kalshi", "venue_market_id": "K-A-yes", "side": "yes"},
                {"obs_ts": 31.0, "refreshed": 1, "bid": .50, "ask": .51, "bid_size": 1, "ask_size": 10,
                 "book_id": "kalshi", "venue_market_id": "K-A-yes", "side": "yes"}]
        tr = ioc_round_trip(rows, 0.0, .45, 10, ZeroFees(), latency_s=1.0, horizon_s=30.0)
        self.assertEqual((tr.filled, tr.cancelled), (4, 6))
        self.assertEqual(tr.unresolved, 3)
        self.assertIsNone(tr.pnl)
        self.assertEqual(exec_record(tr)["status"], "unresolved")


# ---------------------------------------------------------------------------------------
class LockFeeCausalityTests(unittest.TestCase):
    """The H3-lock entry is priced by the fee model of a row observed at or before its own
    decision, never by the first row that happens to come after it."""

    FOLLOWER = "rothera"

    def rows(self):
        out = []
        for t in range(0, 61):
            step = min(max(t - 10, 0), 30) / 30.0
            out.append(obs(t, self.FOLLOWER, "A", .50, venue="robinhood", exchange="rothera"))
            out.append(obs(t, self.FOLLOWER, "B", .50, venue="robinhood", exchange="rothera"))
            out.append(obs(t, "kalshi", "A", .47 + .06 * step, venue="kalshi", fee_params=dict(KALSHI_FEES)))
        return out

    def test_the_entry_fee_model_comes_from_the_decisions_own_row(self):
        from arb_engine.quant.microdata import build
        from scripts.microstructure_eval import _h3_direction, h3_lock_trades, select_trades

        rows = self.rows()
        with identical_settlement():
            samples = build(rows, sample="unconditional", horizons=(), fee_for_row=lambda r: None)
        decisions, _ = select_trades(samples, _h3_direction, 60.0)
        bought = [(s["t"], tuple(bc["key"])) for s, _, bc in decisions]
        self.assertTrue(bought, "the fixture must produce at least one H3 decision")
        asked: list[dict] = []

        def spy(row):
            if row is not None:
                asked.append(row)
            return ZeroFees()

        h3_lock_trades(samples, rows, spy, latency_s=1.0, watch_s=30.0, cooldown_s=60.0)
        for t, key in bought:
            entry_rows = [r for r in asked if (r.get("event_key"), r.get("book_id"), r.get("outcome"),
                                               r.get("side")) == key]
            self.assertTrue(entry_rows, f"no fee model was built for the contract bought at {t}")
            self.assertLessEqual(max(r["obs_ts"] for r in entry_rows), t,
                                 "the entry was priced by a row recorded after its own decision")


# ---------------------------------------------------------------------------------------
class EvaluationDeterminismTests(unittest.TestCase):
    """A whole synthetic fold is byte-identical across runs and across input permutations."""

    SPEC = {"version": 99, "primary": "H3@5s", "horizons": [5], "bootstrap": 20, "seed": 7,
            "signal_cooldown_s": 60, "latencies_s": [1], "haircuts": [1.0], "robust": {"latency_s": 1, "haircut": 1.0},
            "manual_leg_latency_s": 2, "secondary": [], "lock_watch_s": 20}

    def data(self):
        rows = []
        for gi, (h, a) in enumerate((("A", "B"), ("C", "D"))):
            ev = f"nfl:{h}|{a}:2026-09-27"
            for t in range(0, 60):
                wave = .04 * math.sin(t / 7.0 + gi)
                rows.append(obs(t, "kalshi", h, .50 + wave, event=ev, fee_params=dict(KALSHI_FEES)))
                rows.append(obs(t, "kalshi", a, .50 - wave, event=ev, fee_params=dict(KALSHI_FEES)))
                rows.append(obs(t, "rothera", h, .49 + wave, event=ev, venue="robinhood", exchange="rothera", tie=0.0))
                rows.append(obs(t, "rothera", a, .49 - wave, event=ev, venue="robinhood", exchange="rothera", tie=0.0))
        finals = {f"nfl:{h}|{a}:2026-09-27": (h, a, h) for h, a in (("A", "B"), ("C", "D"))}
        return {"rows": rows, "espn": [], "prints": [], "finals": finals, "event_keys": sorted(finals)}

    def report(self, rows, data):
        from scripts.microstructure_eval import evaluate

        return json.dumps(evaluate({**data, "rows": rows}, dict(self.SPEC), 1.0, False), sort_keys=True, default=str)

    def test_identical_across_runs_and_input_permutations(self):
        import random

        data = self.data()
        base = self.report(list(data["rows"]), data)
        self.assertEqual(base, self.report(list(data["rows"]), data))
        shuffled = list(data["rows"])
        random.Random(4).shuffle(shuffled)
        self.assertEqual(base, self.report(shuffled, data))
        self.assertEqual(base, self.report(list(reversed(data["rows"])), data))


if __name__ == "__main__":
    unittest.main()

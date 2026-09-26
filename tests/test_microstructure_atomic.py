"""Synthetic correctness tests for the evaluator's decision loops (spec v5).

These are constructed inputs with known answers: they prove the evaluator decides the same
thing whatever order rows arrive in, whatever the books are called, however often a row is
repeated and whatever is appended after the decision. They say nothing about whether any
candidate makes money - that is the discovery replay's job (tests/fixtures/results/
micro_discovery.json), and discovery is descriptive, not evidence.
"""
import json
import random
import unittest
from unittest import mock

from arb_engine.fees.base import ZeroFees

EV = "nfl:A|B:2026-09-27"
ZF = lambda r: ZeroFees()   # noqa: E731
BOOKS = {"kalshi": ("kalshi", None), "rothera": ("robinhood", "rothera"), "cdna": ("robinhood", "cdna")}


def obs(t, book, outcome, ask, bid=None, size=100, tie=.5, venue=None, exchange=None, **kw):
    v, ex = BOOKS.get(book, (venue or "robinhood", exchange))
    r = {"event_key": kw.pop("event_key", EV), "obs_ts": float(t), "req_ts": float(t), "refreshed": 1, "in_play": True, "source": "fast",
         "venue": venue or v, "book_id": book, "venue_market_id": f"{book}-{outcome}", "outcome": outcome, "side": "yes",
         "bid": round(ask - .01, 2) if bid is None else bid, "ask": ask, "bid_size": size, "ask_size": size, "tie_payout": tie}
    if exchange or ex:
        r["exchange"] = exchange or ex
    r.update(kw)
    return r


def scan(rows, **kw):
    from scripts.microstructure_eval import arb_scan

    return arb_scan(rows, ZF, 1.0, kw.pop("latency_rh", 1.0), **kw)


def decisions(recs):
    return [(r["t"], round(r["margin"], 9), r["legs"]) for r in recs]


def canon(recs):
    return json.dumps(recs, sort_keys=True, default=str)


def market(seed=3, n=240, books=("kalshi", "rothera", "cdna")):
    """Three books quoting both outcomes, every book updated at the same instants (as a 5 s
    recorder or a batched fast lane records them), asks wandering so pairs cross now and then."""
    rng = random.Random(seed)
    rows = []
    for t in range(n):
        for i, b in enumerate(books):
            for oc in ("A", "B"):
                ask = round(.5 + rng.uniform(-.06, .06), 2)
                rows.append(obs(t, b, oc, ask, size=100 + 37 * i + (5 if oc == "B" else 0)))
    return rows


class ArbScanAtomicTests(unittest.TestCase):
    def test_the_reported_reproduction_emits_nothing_in_either_order(self):
        # t=0: A .60 + B .50; t=1: both update at once to A .40 + B .70. No instant ever offers
        # a set under $1; row-at-a-time processing emitted a .10 "arb" when A's row came first.
        t0 = [obs(0, "kalshi", "A", .60), obs(0, "rothera", "B", .50)]
        a1, b1 = obs(1, "kalshi", "A", .40), obs(1, "rothera", "B", .70)
        tail = [obs(t, "kalshi", "A", .40) for t in range(2, 30)] + [obs(t, "rothera", "B", .70) for t in range(2, 30)]
        self.assertEqual(scan(t0 + [a1, b1] + tail), [])
        self.assertEqual(scan(t0 + [b1, a1] + tail), [])
        # Control: when only A moves, the .10 set is real and is found whatever the order.
        b1_still = obs(1, "rothera", "B", .50)
        tail2 = [obs(t, "kalshi", "A", .40) for t in range(2, 30)] + [obs(t, "rothera", "B", .50) for t in range(2, 30)]
        for order in (t0 + [a1, b1_still] + tail2, t0 + [b1_still, a1] + tail2):
            self.assertEqual(decisions(scan(order)), [(1.0, .10, [[EV, "kalshi", "A", "yes"], [EV, "rothera", "B", "yes"]])])

    def test_permutation_of_rows_changes_nothing(self):
        rows = market()
        base = scan(rows)
        self.assertGreater(len(base), 2)                    # the market does cross
        for seed in range(5):
            shuffled = list(rows)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(canon(scan(shuffled)), canon(base), seed)

    def test_book_names_that_sort_the_other_way_change_nothing(self):
        rows = market()
        base = scan(rows)
        rename = {"rothera": "zz_rothera", "cdna": "aa_cdna"}      # cdna now sorts before kalshi, rothera after
        back = {v: k for k, v in rename.items()}
        renamed = [dict(r, book_id=rename.get(r["book_id"], r["book_id"]), venue_market_id=r["venue_market_id"].replace(r["book_id"], rename.get(r["book_id"], r["book_id"])))
                   for r in rows]
        with mock.patch.dict(BOOKS, {"zz_rothera": ("robinhood", "rothera"), "aa_cdna": ("robinhood", "cdna")}):
            got = scan(renamed)
        for r in got:
            r["legs"] = [[k[0], back.get(k[1], k[1]), k[2], k[3]] for k in r["legs"]]
        self.assertEqual(canon(got), canon(base))

    def test_duplicate_rows_change_nothing_and_conflicting_ones_resolve_the_same_way(self):
        rows = market()
        base = scan(rows)
        self.assertEqual(canon(scan(rows + [dict(r) for r in rows])), canon(base))           # every row twice
        # Two different rows for one contract at one instant: one is chosen by content, so
        # every arrival order gives the same answer.
        clash = [dict(r) for r in rows]
        clash.append(dict(rows[10], ask=round(rows[10]["ask"] - .03, 2), bid=round(rows[10]["bid"] - .03, 2), source="full"))
        want = canon(scan(clash))
        for seed in range(4):
            c = list(clash)
            random.Random(seed).shuffle(c)
            self.assertEqual(canon(scan(c)), want, seed)

    def test_appending_the_future_changes_no_earlier_decision(self):
        rows = market(n=240)
        cut = 150
        past = scan([r for r in rows if r["obs_ts"] <= cut])
        full = scan(rows)
        self.assertEqual(decisions([r for r in full if r["t"] <= cut]), decisions(past))
        # Decisions whose fills and unwinds are settled well before the cut are identical records.
        settled = cut - 1.0 - 2.0 - 1.0 - 60.0
        self.assertEqual(canon([r for r in full if r["t"] <= settled]), canon([r for r in past if r["t"] <= settled]))

    def test_equal_sets_break_on_freshness_then_depth_never_on_names(self):
        # Rothera's B is fresh; CDNA's B is one second old: equal .95 sets, the fresher pair wins.
        rows = [obs(10, "kalshi", "A", .45), obs(10, "rothera", "B", .50), obs(9, "cdna", "B", .50)]
        rows += [obs(t, b, oc, a) for t in range(11, 40) for b, oc, a in (("kalshi", "A", .45), ("rothera", "B", .50), ("cdna", "B", .50))]
        self.assertEqual(scan(rows)[0]["legs"][1][1], "rothera")
        # Same instant, same price: the deeper book wins, whichever name sorts first.
        rows = [obs(t, "kalshi", "A", .45) for t in range(0, 40)]
        rows += [obs(t, "rothera", "B", .50, size=50) for t in range(0, 40)] + [obs(t, "cdna", "B", .50, size=200) for t in range(0, 40)]
        self.assertEqual(scan(rows)[0]["legs"][1][1], "cdna")
        random.Random(1).shuffle(rows)
        self.assertEqual(scan(rows)[0]["legs"][1][1], "cdna")


class ArbAccountingTests(unittest.TestCase):
    def test_an_attempt_that_fills_nothing_is_a_zero_trade_not_a_missing_one(self):
        from scripts.microstructure_eval import arb_metrics

        # The set is offered at t=0 and gone by the time either order arrives.
        rows = [obs(0, "kalshi", "A", .45), obs(0, "rothera", "B", .50)]
        rows += [obs(t, "kalshi", "A", .60) for t in range(1, 40)] + [obs(t, "rothera", "B", .60) for t in range(1, 40)]
        recs = scan(rows)
        self.assertEqual((recs[0]["legs_filled"], recs[0]["pnl_win"], recs[0]["pnl_win_per_filled_set"]), (0, 0.0, None))
        m = arb_metrics(recs, 1, 50, "pnl_win")
        self.assertEqual((m["attempts"], m["trades"], m["no_leg_filled"]), (1, 1, 1))
        self.assertEqual(m["mean_ret"]["point"], 0.0)

    def test_an_excluded_pair_is_not_an_attempt(self):
        from scripts.microstructure_eval import arb_metrics

        rows = [obs(t, "kalshi", "A", .45) for t in range(0, 40)] + [obs(t, "rothera", "B", .50) for t in range(0, 40)]
        with mock.patch("arb_engine.quant.microdata.settlement_relation", return_value="mismatch"):
            recs = scan(rows)
        self.assertEqual({r["excluded"] for r in recs}, {"settlement-mismatch"})
        m = arb_metrics(recs, 1, 50, "pnl_win")
        self.assertEqual((m["attempts"], m["trades"], m["excluded"]), (0, 0, {"settlement-mismatch": len(recs)}))

    def test_unwind_leftovers_are_held_to_a_known_settlement_else_unresolved(self):
        from scripts.microstructure_eval import arb_metrics

        # Kalshi A fills 10 at .45; Rothera B never comes back under its limit; A's bid shows no
        # size for the whole unwind window, so all 10 are left over.
        rows = [obs(0, "kalshi", "A", .45), obs(0, "rothera", "B", .50)]
        rows += [obs(1, "kalshi", "A", .45, bid=.44, size=10)]
        rows += [dict(obs(t, "kalshi", "A", .46), bid_size=0) for t in range(2, 80)]
        rows += [obs(t, "rothera", "B", .70) for t in range(1, 80)]
        open_ = scan(rows)[0]
        self.assertEqual((open_["legs_filled"], open_["unresolved"], open_["pnl_win"]), (1, 10, None))
        self.assertEqual(arb_metrics([open_], 1, 50, "pnl_win")["unresolved_attempts"], 1)
        won = scan(rows, settle={(EV, "kalshi", "A", "yes"): 1.0})[0]
        self.assertEqual((won["unresolved"], won["settled_excess"]), (0, 10))
        self.assertAlmostEqual(won["pnl_win"], (10 * 1.0 - 10 * .45) / 10)
        self.assertAlmostEqual(won["pnl_tie"], (10 * .5 - 10 * .45) / 10)        # A's own tie payout on the leftovers
        lost = scan(rows, settle={(EV, "kalshi", "A", "yes"): 0.0})[0]
        self.assertAlmostEqual(lost["pnl_win"], -.45)


class SelectTradesBatchTests(unittest.TestCase):
    def _two(self, kc_ask, comp_ask, names=("DEN", "KC")):
        den, kc = names
        up = {"kind": "trigger", "t": 5.0, "event_key": EV, "book_id": "kalshi", "outcome": kc, "side": "yes", "venue": "kalshi",
              "ask": kc_ask, "mid": kc_ask - .01, "dmid_30": .06, "tie_payout": .5}
        down = {"kind": "trigger", "t": 5.0, "event_key": EV, "book_id": "kalshi", "outcome": den, "side": "yes", "venue": "kalshi",
                "ask": .40, "mid": .39, "dmid_30": -.06, "tie_payout": .5,
                "complement": {"key": [EV, "kalshi", kc, "no"], "venue": "kalshi", "bid": comp_ask - .01, "ask": comp_ask, "tie_payout": .5}}
        return [up, down]

    def test_a_mirror_signal_at_the_same_instant_buys_the_cheaper_contract(self):
        from scripts.microstructure_eval import _dir_h1, select_trades

        for names in (("DEN", "KC"), ("ZZZ", "AAA")):          # the long outcome sorts last, then first
            for kc_ask, comp_ask, want in ((.62, .60, "no"), (.58, .60, "yes")):
                samples = self._two(kc_ask, comp_ask, names)
                for order in (samples, samples[::-1]):
                    out, st = select_trades(order, _dir_h1, 60.0)
                    self.assertEqual(len(out), 1)
                    self.assertEqual(out[0][2]["key"][3], want, (names, kc_ask))
                    self.assertEqual(st["same_exposure_dropped"], 1)


class H3LockBatchTests(unittest.TestCase):
    def _rows(self, rh_book="rothera", rh_ask=.25, k_ask=.30):
        rows = []
        for t in range(0, 700):
            a = .60 if t < 100 else .80
            rows.append(obs(t, "kalshi", "KC", round(a + .01, 2), bid=round(a - .01, 2)))
            lock = 150 <= t <= 155
            rows.append(obs(t, "kalshi", "DEN", k_ask if lock else .60, bid=.19))
            rows.append(obs(t, rh_book, "DEN", rh_ask if lock else .60, bid=.19, venue="robinhood", exchange="rothera"))
        return rows

    def _run(self, rows):
        from scripts.microstructure_eval import h3_lock_trades

        s = [{"kind": "unconditional", "t": 10.0, "event_key": EV, "book_id": "kalshi", "outcome": "KC", "side": "yes", "venue": "kalshi",
              "ask": .61, "mid": .60, "dmid_30": 0.0, "leader_dmid_30": .08, "gap_leader": .06}]
        return h3_lock_trades(s, rows, ZF, latency_s=1, watch_s=600, n=10)

    def test_the_cheapest_lock_at_an_instant_wins_whatever_the_names(self):
        for rh_book in ("rothera", "aaa"):                      # Robinhood's book sorts after Kalshi, then before
            with mock.patch("arb_engine.quant.microdata.settlement_relation", return_value="identical"):
                out = self._run(self._rows(rh_book))
            self.assertEqual(out["inventory"]["locked_contracts"], 10, rh_book)
            self.assertAlmostEqual(out["rets"][EV][0], (10 * 1.0 - 10 * .61 - 10 * .25) / 10, msg=rh_book)

    def test_a_hedge_on_unverified_settlement_is_not_a_lock(self):
        # With the real registry Kalshi vs Rothera is unverified: the cheaper Rothera DEN is not
        # taken, Kalshi's own DEN (same book: identical) is.
        out = self._run(self._rows())
        self.assertAlmostEqual(out["rets"][EV][0], (10 * 1.0 - 10 * .61 - 10 * .30) / 10)
        # Only the unverified book ever offers a lock: no hedge, and the entry is counted.
        out = self._run(self._rows(k_ask=.60))
        self.assertEqual(out["inventory"].get("locked_contracts", 0), 0)
        self.assertEqual(out["inventory"]["entries_lockable_only_on_unverified_settlement"], 1)


class DedupeTests(unittest.TestCase):
    def test_one_row_per_contract_per_instant_chosen_by_content(self):
        from arb_engine.quant.microdata import _dedupe

        direct = obs(5, "kalshi", "A", .45)
        routed = dict(direct, venue="robinhood", venue_market_id="rh-kx-A", ask=.44)       # Robinhood's KX route of the same book
        other = dict(direct, ask=.46, source="full")
        for order in ([direct, routed, other], [other, routed, direct], [routed, other, direct]):
            got = _dedupe(order)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0][2]["venue"], "kalshi")       # the direct venue first
        a, b = _dedupe([direct, other]), _dedupe([other, direct])
        self.assertEqual(a[0][2], b[0][2])                       # then content, not arrival


if __name__ == "__main__":
    unittest.main()

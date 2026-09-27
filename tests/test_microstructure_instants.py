"""Observations that share a timestamp are one atomic information batch in every evaluator
path: H4 (``arb_scan``), the feature builder (``microdata.build``), the decision selection
(``select_trades``) and the H3-lock hedge watch (``h3_lock_trades``).

The audit case (reproduced at bf7d2d5): independent books, compatible settlement, a $0.50 tie
payout on each leg, zero fees. t=0: A asks .60, B asks .50; t=1: A asks .40, B asks .70. Fed
row by row, t=1 as [A, B] read A's new .40 against B's stale .50 - a .10 "guaranteed" arb -
while [B, A] read nothing. Every test here is synthetic; none reads recorded data.
"""
import itertools
import json
import random
import unittest
from unittest import mock

from arb_engine.fees.base import ZeroFees

EV = "nfl:A|B:2026-09-27"


def obs(t, book, outcome, ask, venue=None, side="yes", size=100, tie=0.5, bid=None, event=EV, **kw):
    r = {"event_key": event, "obs_ts": float(t), "req_ts": float(t), "quote_time": float(t), "refreshed": 1, "in_play": True,
         "source": "fast", "venue": venue or book, "book_id": book, "venue_market_id": f"{book}-{outcome}-{side}", "outcome": outcome,
         "side": side, "bid": round(ask - .01, 2) if bid is None else bid, "ask": ask, "bid_size": size, "ask_size": size, "tie_payout": tie}
    r.update(kw)
    return r


def K(t, outcome, ask, **kw):          # Kalshi direct
    return obs(t, "kalshi", outcome, ask, **kw)


def R(t, outcome, ask, **kw):          # an independent Robinhood book (Rothera)
    return obs(t, "rothera", outcome, ask, venue="robinhood", **kw)


def scan(rows):
    from scripts.microstructure_eval import arb_scan

    with mock.patch("arb_engine.quant.microdata.settlement_relation", return_value="identical"):
        return arb_scan(rows, lambda r: ZeroFees(), 1, 5)


def signals(records):
    return [(r["t"], round(r["margin"], 6)) for r in records]


def canon(records, drop=()):
    return json.dumps([{k: v for k, v in r.items() if k not in drop} for r in records], sort_keys=True, default=str)


def hold(rows_at_t, until, t0=2):
    """The instant's quotes held for later seconds, so the paper legs have books to meet."""
    return [dict(r, obs_ts=float(t), req_ts=float(t), quote_time=float(t)) for t in range(t0, until) for r in rows_at_t]


class ArbScanInstantTests(unittest.TestCase):
    def test_the_audit_case_fires_in_neither_order_and_a_real_move_in_both(self):
        t0 = [K(0, "A", .60), R(0, "B", .50)]
        moved = [K(1, "A", .40), R(1, "B", .70)]
        for order in (moved, moved[::-1]):
            self.assertEqual(scan(t0 + order + hold(moved, 12)), [])
        # Control: only A moves (B is re-quoted unchanged at t=1): a real .10 set, in both orders.
        only_a = [K(1, "A", .40), R(1, "B", .50)]
        for order in (only_a, only_a[::-1]):
            self.assertEqual(signals(scan(t0 + order + hold(only_a, 12))), [(1.0, .10)])

    def _four_books(self):
        t0 = [K(0, "A", .60), K(0, "B", .45), R(0, "A", .58), R(0, "B", .47)]           # no pair under $1
        t1 = [K(1, "A", .52), K(1, "B", .52), R(1, "A", .44), R(1, "B", .40)]           # K-A + R-B = .92; R-A + K-B = .96
        return t0, t1

    def test_every_permutation_of_an_instant_gives_the_same_records(self):
        t0, t1 = self._four_books()
        tail = hold(t1, 12)
        want = None
        for perm in itertools.permutations(t1):
            for first in (t0, t0[::-1]):
                got = canon(scan(first + list(perm) + tail))
                want = want or got
                self.assertEqual(got, want, perm)
        self.assertEqual(signals(json.loads(want)), [(1.0, .08)])   # the cheapest real pair; never a stale-leg phantom

    def test_book_venue_and_outcome_names_do_not_decide(self):
        t0, t1 = self._four_books()
        base = scan(t0 + t1 + hold(t1, 12))
        # The same markets under names that sort the other way: the books swap order (kalshi ->
        # zulu, rothera -> alpha) and so do the outcomes (A -> Y, B -> X).
        ev2 = "nfl:X|Y:2026-09-27"
        books = {"kalshi": "zulu", "rothera": "alpha"}
        outs = {"A": "Y", "B": "X"}

        def rename(r):
            return dict(r, event_key=ev2, book_id=books[r["book_id"]], outcome=outs[r["outcome"]],
                        venue_market_id=f"{books[r['book_id']]}-{outs[r['outcome']]}")
        renamed = scan([rename(r) for r in t0 + t1 + hold(t1, 12)])
        self.assertEqual(canon(renamed, drop=("game",)), canon(base, drop=("game",)))
        self.assertEqual(signals(renamed), [(1.0, .08)])

    def test_a_decision_never_reads_a_later_observation(self):
        t0, t1 = self._four_books()
        rows = t0 + t1 + hold(t1, 20) + [K(40, "A", .50), R(40, "B", .45)] + hold([K(40, "A", .50), R(40, "B", .45)], 60, t0=41)
        full = scan(rows)
        self.assertEqual(signals(full), [(1.0, .08), (40.0, .05)])
        for r in full:
            # Every decision replays identically from the rows up to its own instant ...
            upto = [x for x in rows if x["obs_ts"] <= r["t"]]
            self.assertEqual(signals(scan(upto))[-1], (r["t"], round(r["margin"], 6)))
        # ... and appending the future changes no earlier record once its legs have resolved.
        past = [x for x in rows if x["obs_ts"] <= 20]
        self.assertEqual(canon([r for r in full if r["t"] <= 20]), canon(scan(past)))

    def test_a_resale_route_never_replaces_the_book_it_resells(self):
        # One contract (Kalshi's A) seen at one instant directly (.60) and through Robinhood's KX
        # route (.40, same book): the direct row is the observation, whatever the arrival order.
        direct, resale, b = K(1, "A", .60), obs(1, "kalshi", "A", .40, venue="robinhood"), R(1, "B", .55)
        for order in itertools.permutations([direct, resale, b]):
            self.assertEqual(scan([K(0, "A", .60), R(0, "B", .55)] + list(order) + hold([direct, b], 12)), [])
        # Without a direct row at that instant the resale route is the contract's quote.
        self.assertEqual(signals(scan([resale, b] + hold([resale, b], 12))), [(1.0, .05)])
        # Two routes of one book are never a pair, however cheap.
        same_book = [K(1, "A", .40), obs(1, "kalshi", "B", .50, venue="robinhood")]
        self.assertEqual(scan(same_book + hold(same_book, 12)), [])

    def test_identical_duplicate_rows_are_one_observation(self):
        rows = [K(0, "A", .60), R(0, "B", .50), K(1, "A", .40), R(1, "B", .50)]
        dup = rows + [K(1, "A", .40), R(1, "B", .50), dict(K(1, "A", .40), source="full", req_ts=0.4)]
        tail = hold([K(1, "A", .40), R(1, "B", .50)], 12)
        self.assertEqual(canon(scan(dup + tail)), canon(scan(rows + tail)))

    def test_conflicting_rows_void_the_contract_at_that_instant(self):
        # t=1: Kalshi's A is reported at .40 and at .45 by two direct rows. Neither is trusted,
        # and A's t=0 quote (.30) is not carried past t=1: no pair with B at t=1. The row-by-row
        # scan paired the stale .30 (a .20 phantom) or whichever duplicate arrived first.
        t0 = [K(0, "A", .30), R(0, "B", .80)]
        t1 = [K(1, "A", .40), K(1, "A", .45), R(1, "B", .50)]
        t2 = [K(2, "A", .40), R(2, "B", .50)]
        for perm in itertools.permutations(t1):
            self.assertEqual(signals(scan(t0 + list(perm) + t2 + hold(t2, 14, t0=3))), [(2.0, .10)])

    def test_a_conflicted_instant_is_never_a_fill(self):
        from arb_engine.quant.microdata import observation_instants, resolve_instant, series_by_contract

        a1, a2 = K(1, "A", .40), K(1, "A", .45)
        got, why = resolve_instant([(0, a1), (0, a2)])
        self.assertIsNone(got)
        self.assertIn("disagreeing", why)
        rows = [K(0, "A", .40), a2, a1, K(2, "A", .40)]
        self.assertEqual([r["obs_ts"] for r in series_by_contract(rows)[(EV, "kalshi", "A", "yes")]], [0.0, 2.0])
        self.assertEqual([(t, [r["ask"] for _, r in rs], voids) for t, rs, voids in observation_instants(rows)],
                         [(0.0, [.40], []), (1.0, [], [(EV, "kalshi", "A", "yes")]), (2.0, [.40], [])])


class ResolveInstantTests(unittest.TestCase):
    """The per-instant policy, alone: route first, content second, never arrival order."""

    def test_the_resolution_is_the_same_in_every_order(self):
        from arb_engine.quant.microdata import resolve_instant

        direct = K(1, "A", .60)
        resale = obs(1, "kalshi", "A", .40, venue="robinhood")
        approx = dict(direct, approx_time=1, source="full")
        for rows in ([direct, resale], [direct, approx], [direct, resale, approx], [direct, dict(direct)]):
            picks = {json.dumps(resolve_instant([(int(bool(r.get("approx_time"))), r) for r in p])[0], sort_keys=True)
                     for p in itertools.permutations(rows)}
            self.assertEqual(len(picks), 1, rows)
        got, _ = resolve_instant([(0, resale), (0, direct)])
        self.assertEqual(got[1]["venue"], "kalshi")                            # the direct route wins
        got, _ = resolve_instant([(1, approx), (0, direct)])
        self.assertEqual(got[0], 0)                                            # a measured time outranks an approximate one
        self.assertEqual(resolve_instant([(0, direct), (0, dict(direct, tie_payout=1.0))])[0], None)   # it pays differently: conflict
        # A disagreement between routes is not a conflict: the direct route is the observation.
        self.assertEqual(resolve_instant([(0, direct), (0, resale), (0, dict(resale, ask=.41))])[0][1]["ask"], .60)


class BuildConflictTests(unittest.TestCase):
    def test_a_conflicted_book_is_no_leader_complement_or_decision_until_it_is_clean_again(self):
        from arb_engine.quant.microdata import build

        rows = []
        for t in range(0, 16):
            rows += [K(t, "A", .50), R(t, "A", .50, tie=.5), K(t, "B", .48)]
        rows += [K(10, "A", .56)]                                              # t=10: Kalshi's A disagrees with itself
        random.Random(3).shuffle(rows)
        with mock.patch("arb_engine.quant.microdata.settlement_relation", return_value="identical"):
            s = build(rows, sample="unconditional", horizons=(), fee_for_row=lambda r: None)
        at = {(x["book_id"], x["outcome"], x["t"]): x for x in s}           # unconditional samples every 5 s
        self.assertNotIn(("kalshi", "A", 10.0), at)                            # no decision on an ambiguous quote
        self.assertIn(("kalshi", "A", 11.0), at)                               # ... the next clean one is sampled
        self.assertEqual((at[("rothera", "A", 10.0)]["cross"], at[("rothera", "A", 10.0)]["cross_excluded"]), ({}, {"ambiguous": 1}))
        self.assertEqual((at[("kalshi", "B", 10.0)]["complement"], at[("kalshi", "B", 10.0)]["complement_missing"]), (None, "ambiguous"))
        self.assertIn("kalshi", at[("rothera", "A", 15.0)]["cross"])           # clean again later
        self.assertEqual(at[("kalshi", "B", 15.0)]["complement"]["key"], [EV, "kalshi", "A", "yes"])


class SelectTradesInstantTests(unittest.TestCase):
    def _sample(self, outcome, ask, dmid, comp=None, t=10.0):
        return {"t": t, "kind": "trigger", "event_key": EV, "book_id": "kalshi", "outcome": outcome, "side": "yes", "venue": "kalshi",
                "ask": ask, "mid": ask - .01, "dmid_30": dmid, "complement": comp, "complement_missing": None}

    def _pick(self, samples, cooldown=60.0):
        from scripts.microstructure_eval import _dir_h1, select_trades

        return [(s["outcome"], d, bc["ask"]) for s, d, bc in select_trades(samples, _dir_h1, cooldown)[0]]

    def test_one_exposure_at_one_instant_is_decided_by_price_then_directness(self):
        # A rose (buy A directly at .51) and, at the same instant, B fell (buy B's complement:
        # that same A contract, quoted .52 by the complement). One exposure: the cheaper wins,
        # whatever the names or the order; on an equal price the direct buy wins.
        up_a = self._sample("A", .51, .06)
        down_z = self._sample("Z", .49, -.06, comp={"key": [EV, "kalshi", "A", "yes"], "venue": "kalshi", "bid": .50, "ask": .52})
        for order in ([up_a, down_z], [down_z, up_a]):
            self.assertEqual(self._pick(order), [("A", 1, .51)])
        cheap = dict(down_z, complement=dict(down_z["complement"], ask=.505))
        for order in ([up_a, cheap], [cheap, up_a]):
            self.assertEqual(self._pick(order), [("Z", -1, .505)])
        tie = dict(down_z, complement=dict(down_z["complement"], ask=.51))
        for order in ([up_a, tie], [tie, up_a]):
            self.assertEqual(self._pick(order), [("A", 1, .51)])
        # Without a cooldown every decision still trades, as before.
        self.assertEqual(sorted(self._pick([down_z, up_a], cooldown=0.0)), [("A", 1, .51), ("Z", -1, .52)])


class HedgeInstantTests(unittest.TestCase):
    def _rows(self, other):
        rows = []
        for t in range(0, 700):
            rows.append(K(t, "KC", .61, tie=.5))
            cheap = 150 <= t <= 151
            rows.append(K(t, "DEN", .25 if cheap else .60, tie=.5))
            rows.append(obs(t, other, "DEN", .30 if cheap else .60, venue="robinhood", tie=.5))
        return rows

    def test_the_cheapest_hedge_of_an_instant_is_the_order_whatever_the_book_is_called(self):
        from scripts.microstructure_eval import h3_lock_trades

        h3 = [{"kind": "unconditional", "t": 10.0, "event_key": EV, "book_id": "kalshi", "outcome": "KC", "side": "yes", "venue": "kalshi",
               "ask": .61, "mid": .60, "dmid_30": 0.0, "leader_dmid_30": .08, "gap_leader": .06}]
        rets = set()
        for name in ("alpha", "zulu"):                  # sorts before and after "kalshi"
            rows = self._rows(name)
            random.Random(11).shuffle(rows)
            out = h3_lock_trades(h3, rows, lambda r: ZeroFees(), latency_s=1, watch_s=600, n=10)
            self.assertEqual(out["inventory"]["locked_contracts"], 10)
            rets.add(round(out["rets"][EV][0], 9))
        self.assertEqual(rets, {round(1.0 - .61 - .25, 9)})    # locked on Kalshi's .25, not the other book's .30


if __name__ == "__main__":
    unittest.main()

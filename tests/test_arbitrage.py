import unittest

from arb_engine.fees import KalshiFees, PolymarketFees, RobinhoodFees, ZeroFees
from arb_engine.fees.registry import fee_model_for
from arb_engine.models import Book, Level, OutcomeQuote
from arb_engine.quant import Leg, best_leg_per_outcome, evaluate, kelly_fraction, kelly_stake, max_price_for_leg, size_from_books, walk_book
from arb_engine.quant.arbitrage import min_size_for_legs, tick_for_quote, tie_payout_for_quote

from .helpers import load

K = KalshiFees.from_series({"fee_type": "quadratic_with_maker_fees"})
P = PolymarketFees()
R = RobinhoodFees()


class EvaluateTests(unittest.TestCase):
    def test_gross_arb_killed_by_fees(self):
        # Sum of asks 0.99 (1% gross) but Kalshi+Polymarket taker fees ~2.3% at this size.
        legs = [Leg("A", "polymarket", 0.26, P), Leg("B", "kalshi", 0.73, K)]
        r = evaluate(legs, 100)
        self.assertAlmostEqual(r.gross_sum, 0.99)
        self.assertAlmostEqual(r.total_cost, 26 + 0.962 + 73 + 1.38, places=6)
        self.assertFalse(r.is_arb)
        self.assertAlmostEqual(r.margin, (100 - r.total_cost) / 100)

    def test_real_arb(self):
        legs = [Leg("A", "polymarket", 0.40, P), Leg("B", "kalshi", 0.55, K)]
        r = evaluate(legs, 100)
        self.assertTrue(r.is_arb)
        self.assertAlmostEqual(r.profit, 100 - (40 + 1.2 + 55 + 1.74), places=6)
        self.assertEqual([l.venue for l in r.legs], ["polymarket", "kalshi"])

    def test_fee_free_legs_sum_rule(self):
        legs = [Leg("A", "book", 0.48, ZeroFees()), Leg("B", "book", 0.50, ZeroFees())]
        r = evaluate(legs, 10)
        self.assertAlmostEqual(r.margin, 0.02)
        self.assertAlmostEqual(r.roi, 0.2 / 9.8)


class TiePayoutTests(unittest.TestCase):
    """Rothera pays $0 (YES) / $1 (NO) on a tie; Kalshi $0.50 per side. Same win-case margin,
    very different tie case."""

    def test_kalshi_yes_a_with_rothera_yes_b_loses_on_a_tie(self):
        legs = [Leg("A", "kalshi", 0.52, K, tie_payout=0.5), Leg("B", "robinhood", 0.47, R, tie_payout=0.0)]
        r = evaluate(legs, 100)
        self.assertAlmostEqual(r.tie_payout_total, 0.5)
        self.assertAlmostEqual(r.tie_margin, r.margin - 0.5)
        self.assertLess(r.tie_margin, 0)
        # Kalshi YES-A + Rothera NO-A: same price, same win-case margin, pays $1.50 on a tie.
        legs_no = [Leg("A", "kalshi", 0.52, K, tie_payout=0.5), Leg("B", "robinhood", 0.47, R, tie_payout=1.0)]
        r2 = evaluate(legs_no, 100)
        self.assertAlmostEqual(r2.margin, r.margin)
        self.assertAlmostEqual(r2.tie_payout_total, 1.5)
        self.assertGreaterEqual(r2.tie_margin, r2.margin)
        self.assertAlmostEqual(r2.tie_margin, r2.margin + 0.5)
        self.assertEqual([l.tie_payout for l in r2.legs], [0.5, 1.0])

    def test_default_meta_reproduces_todays_results(self):
        # No tie_payout in meta: tie margin == margin and is_arb is untouched.
        qa = OutcomeQuote("polymarket", "A", "e", "A", ask=0.40)
        qb = OutcomeQuote("kalshi", "B", "e", "B", ask=0.55, fee_params={"fee_type": "quadratic_with_maker_fees"})
        legs = best_leg_per_outcome({"A": [qa], "B": [qb]}, lambda q: P if q.venue == "polymarket" else K)
        r = evaluate(legs, 100)
        self.assertTrue(r.is_arb)
        self.assertAlmostEqual(r.profit, 100 - (40 + 1.2 + 55 + 1.74), places=6)
        self.assertAlmostEqual(r.tie_payout_total, 1.0)
        self.assertAlmostEqual(r.tie_margin, r.margin)
        self.assertEqual(tie_payout_for_quote(qa), 0.5)
        self.assertEqual(tie_payout_for_quote(OutcomeQuote("robinhood", "R", "e", "A", ask=0.5, meta={"tie_payout": 0.0})), 0.0)
        self.assertEqual(tick_for_quote(qa), 0.01)
        self.assertEqual(tick_for_quote(OutcomeQuote("polymarket", "T", "e", "A", ask=0.004, meta={"tick": 0.001})), 0.001)

    def test_best_leg_prefers_better_tie_payout_on_equal_all_in(self):
        yes_b = OutcomeQuote("robinhood", "B", "e", "B", ask=0.47, fee_params={"exchange": "rothera"}, meta={"side": "yes", "tie_payout": 0.0})
        no_a = OutcomeQuote("robinhood", "A#no", "e", "B", ask=0.47, fee_params={"exchange": "rothera"}, meta={"side": "no", "tie_payout": 1.0})
        legs = best_leg_per_outcome({"B": [yes_b, no_a]}, lambda q: R)
        self.assertEqual(legs[0].quote.venue_market_id, "A#no")
        self.assertEqual(legs[0].tie_payout, 1.0)
        # ...but never over a strictly cheaper leg.
        no_a.ask = 0.48
        legs = best_leg_per_outcome({"B": [yes_b, no_a]}, lambda q: R)
        self.assertEqual(legs[0].quote.venue_market_id, "B")


class MaxPriceTests(unittest.TestCase):
    def test_tick_0_001_gives_three_decimals(self):
        # Polymarket tail vs a Kalshi favourite at 0.97: the answer is on the 0.001 grid.
        others = [Leg("A", "kalshi", 0.97, K)]
        p = max_price_for_leg(others, P, 100, 0.0, tick=0.001)
        self.assertEqual(p, 0.026)
        self.assertEqual(round(p * 1000) / 1000, p)
        # On the cent grid the same hedge rounds down to 0.02.
        self.assertEqual(max_price_for_leg(others, P, 100, 0.0), 0.02)
        # The floor is one tick unless given: 0.997 elsewhere leaves 0.003 on the 0.001 grid.
        self.assertEqual(max_price_for_leg([Leg("A", "book", 0.997, ZeroFees())], ZeroFees(), 100, 0.0, tick=0.001), 0.003)
        self.assertIsNone(max_price_for_leg([Leg("A", "book", 0.997, ZeroFees())], ZeroFees(), 100, 0.0, tick=0.01))

    def test_max_price_exact_grid(self):
        # Hedge leg: Polymarket at 0.25 (fee 0.9375 per 100). Budget for the RH leg = 100 - 25.9375.
        others = [Leg("TEN", "polymarket", 0.25, P)]
        self.assertEqual(max_price_for_leg(others, R, 100, 0.0), 0.72)   # 72 + $2.00 fees = 74.00 <= 74.0625
        self.assertEqual(max_price_for_leg(others, K, 100, 0.01), 0.71)
        self.assertEqual(max_price_for_leg(others, K, 100, 0.0, role="maker"), 0.73)

    def test_max_price_none_when_impossible(self):
        others = [Leg("A", "polymarket", 0.99, P)]
        self.assertIsNone(max_price_for_leg(others, K, 100, 0.05))

    def test_max_price_respects_cap(self):
        others = [Leg("A", "book", 0.01, ZeroFees())]
        self.assertEqual(max_price_for_leg(others, ZeroFees(), 100, 0.0), 0.99)


class BestLegTests(unittest.TestCase):
    def test_lowest_all_in_wins_not_lowest_price(self):
        qk = OutcomeQuote("kalshi", "K", "e", "A", ask=0.50, fee_params={"fee_type": "quadratic"})
        qr = OutcomeQuote("robinhood", "R", "e", "A", ask=0.49, fee_params={"symbol": "NFLGAME-X-A"})
        legs = best_leg_per_outcome({"A": [qk, qr]}, lambda q: K if q.venue == "kalshi" else R, contracts=100)
        # kalshi: 50 + 1.75 = 51.75 ; robinhood: 49 + 1.00 + 1.00 = 51.00 -> robinhood wins
        self.assertEqual(legs[0].venue, "robinhood")
        qr.ask = 0.505
        legs = best_leg_per_outcome({"A": [qk, qr]}, lambda q: K if q.venue == "kalshi" else R, contracts=100)
        self.assertEqual(legs[0].venue, "kalshi")

    def test_missing_ask_skipped(self):
        q = OutcomeQuote("kalshi", "K", "e", "A", ask=None)
        self.assertEqual(best_leg_per_outcome({"A": [q]}, lambda q: K), [])


class DepthTests(unittest.TestCase):
    def test_walk_book(self):
        vwap, fills = walk_book([Level(0.40, 50), Level(0.41, 100)], 100)
        self.assertAlmostEqual(vwap, (50 * 0.40 + 50 * 0.41) / 100)
        self.assertEqual(fills, [(0.40, 50.0), (0.41, 50.0)])
        self.assertIsNone(walk_book([Level(0.40, 10)], 20))

    def test_size_from_books_stops_where_margin_dies(self):
        qa = OutcomeQuote("polymarket", "A", "e", "A", ask=0.40, book=Book(asks=[Level(0.40, 100), Level(0.60, 1000)]))
        qb = OutcomeQuote("kalshi", "B", "e", "B", ask=0.55, book=Book(asks=[Level(0.55, 1000)]))
        legs = [Leg("A", "polymarket", 0.40, P, quote=qa), Leg("B", "kalshi", 0.55, K, quote=qb)]
        r = size_from_books(legs)
        self.assertIsNotNone(r)
        self.assertLessEqual(r.contracts, 100 + 40)  # a few contracts at 0.60 can still clear margin
        self.assertGreater(r.profit, 0)
        self.assertTrue(all(l.vwap is not None for l in r.legs))

    def test_size_from_books_uses_top_of_book_size_without_depth(self):
        qa = OutcomeQuote("polymarket", "A", "e", "A", ask=0.40, ask_size=30)
        qb = OutcomeQuote("kalshi", "B", "e", "B", ask=0.55, ask_size=500)
        legs = [Leg("A", "polymarket", 0.40, P, quote=qa), Leg("B", "kalshi", 0.55, K, quote=qb)]
        r = size_from_books(legs)
        self.assertEqual(r.contracts, 30)


class MinSizeTests(unittest.TestCase):
    def test_step_5_sizing(self):
        qa = OutcomeQuote("polymarket", "A", "e", "A", ask=0.40, ask_size=33)
        qb = OutcomeQuote("kalshi", "B", "e", "B", ask=0.55, ask_size=500)
        legs = [Leg("A", "polymarket", 0.40, P, quote=qa), Leg("B", "kalshi", 0.55, K, quote=qb)]
        r = size_from_books(legs, step=5)
        self.assertEqual(r.contracts, 30)
        self.assertAlmostEqual(r.tie_margin, r.margin)

    def test_leg_min_size_is_a_floor(self):
        # 3 shares on offer, Polymarket wants 5: nothing can be placed.
        qa = OutcomeQuote("polymarket", "A", "e", "A", ask=0.40, ask_size=3, meta={"min_size": 5})
        qb = OutcomeQuote("kalshi", "B", "e", "B", ask=0.55, ask_size=500)
        legs = [Leg.from_quote("A", qa, P), Leg.from_quote("B", qb, K)]
        self.assertEqual(legs[0].min_size, 5.0)
        self.assertEqual(min_size_for_legs(legs), 5.0)
        self.assertIsNone(size_from_books(legs))
        qa.ask_size = 12
        self.assertEqual(size_from_books(legs).contracts, 12)


class ArbVectorTests(unittest.TestCase):
    """tests/fixtures/arb_vectors.json is what the JS twin (arb-core.js) is checked against;
    replaying it here keeps the fixture honest."""

    @staticmethod
    def _leg(d):
        meta = {k: d[k] for k in ("tie_payout", "min_size", "side") if k in d}
        q = None
        if meta or "book" in d or "ask_size" in d:
            q = OutcomeQuote(d["venue"], f"{d['venue']}-{d['outcome']}", "e", d["outcome"], ask=d["price"], ask_size=d.get("ask_size"), book=Book(asks=[Level(p, s) for p, s in d["book"]]) if d.get("book") else None, meta=meta)
        fee = fee_model_for(d["fee"]["venue"], d["fee"].get("params"), settings=d["fee"].get("settings"))
        return Leg(outcome=d["outcome"], venue=d["venue"], price=d["price"], fee_model=fee, role=d.get("role", "taker"), quote=q, tie_payout=d.get("tie_payout", 0.5), min_size=d.get("min_size"))

    def _check(self, r, exp):
        if exp is None:
            self.assertIsNone(r)
            return
        self.assertIsNotNone(r)
        for k, v in exp.items():
            self.assertAlmostEqual(getattr(r, k), v, places=9, msg=k)

    def test_vectors(self):
        vec = load("arb_vectors.json")
        self.assertGreaterEqual(len(vec["evaluate"]), 8)
        for v in vec["evaluate"]:
            with self.subTest(v["name"]):
                self._check(evaluate([self._leg(l) for l in v["legs"]], v["contracts"]), v["expected"])
        for v in vec["max_price"]:
            with self.subTest(v["name"]):
                fee = fee_model_for(v["fee"]["venue"], v["fee"].get("params"), settings=v["fee"].get("settings"))
                got = max_price_for_leg([self._leg(l) for l in v["others"]], fee, v["contracts"], v["target_margin"], tick=v["tick"], role=v["role"])
                self.assertEqual(got, v["expected"])
        for v in vec["size"]:
            with self.subTest(v["name"]):
                self._check(size_from_books([self._leg(l) for l in v["legs"]], max_contracts=v["max_contracts"], min_margin=v["min_margin"], step=v["step"]), v["expected"])
        names = {v["name"] for v in vec["evaluate"]} | {v["name"] for v in vec["max_price"]} | {v["name"] for v in vec["size"]}
        self.assertTrue({"kalshi_yesA_rothera_yesB_tie_loses", "polymarket_tail_tick_0.001", "step_5_top_of_book", "min_size_5_only_3_on_offer"} <= names)


class KellyTests(unittest.TestCase):
    def test_closed_form(self):
        # b = (1-k)/k ; f = (q*b - (1-q))/b = (q-k)/(1-k)
        self.assertAlmostEqual(kelly_fraction(0.55, 0.50), 0.10)
        self.assertEqual(kelly_fraction(0.45, 0.50), 0.0)
        self.assertAlmostEqual(kelly_fraction(0.55, 0.50, fraction=0.25), 0.025)
        r = kelly_stake(1000, 0.55, 0.50, fraction=1.0)
        self.assertEqual(r["contracts"], 200)


if __name__ == "__main__":
    unittest.main()

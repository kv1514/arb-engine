import unittest

from arb_engine.fees import KalshiFees, PolymarketFees, RobinhoodFees, ZeroFees
from arb_engine.models import Book, Level, OutcomeQuote
from arb_engine.quant import Leg, best_leg_per_outcome, evaluate, kelly_fraction, kelly_stake, max_price_for_leg, size_from_books, walk_book

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


class MaxPriceTests(unittest.TestCase):
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

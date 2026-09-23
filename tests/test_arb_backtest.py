"""scripts/arb_backtest.py: acting on an arb alert against the prices that were really there."""

import unittest

from arb_engine.models import OutcomeQuote
from scripts.arb_backtest import Ledger, Series, act, duration_table, kelly_fraction, with_bankroll

EV = "nfl:DEN|KC:2026-09-20"
KFEE = {"fee_type": "quadratic", "fee_multiplier": 0}   # fee-free here: the arithmetic is the point


def q(venue, outcome, ask, bid, size=100, t=0.0):
    return OutcomeQuote(venue, f"{venue}-{outcome}", EV, outcome, ask=ask, bid=bid, ask_size=size, ts=t,
                        fee_params=KFEE if venue == "kalshi" else {"exchange": "rothera", "rothera_fee_model": "flat_001"},
                        meta={"exchange": "rothera"} if venue == "robinhood" else {})


class _Me:
    def __init__(self, qs):
        self.event_key = EV
        self.quotes_by_venue = {}
        for x in qs:
            self.quotes_by_venue.setdefault(x.venue, []).append(x)


def alert(t=0.0, n=10, kc=0.55, den=0.36):
    legs = [{"venue": "kalshi", "outcome": "KC", "price": kc, "size": 100}, {"venue": "robinhood", "outcome": "DEN", "price": den, "size": 100}]
    return {"t": t, "event_key": EV, "legs": legs, "order": [0, 1], "max_prices": {1: 0.43}, "contracts": n,
            "cost": (kc + den) * n, "profit": (1 - kc - den) * n}


def series(kc_later, den_later):
    s = Series()
    s.add(0.0, _Me([q("kalshi", "KC", 0.55, 0.54), q("robinhood", "DEN", 0.36, 0.35)]))
    s.add(5.0, _Me([q("kalshi", "KC", *kc_later, t=5.0), q("robinhood", "DEN", 0.36, 0.35, t=5.0)]))
    s.add(15.0, _Me([q("kalshi", "KC", *kc_later, t=15.0), q("robinhood", "DEN", *den_later, t=15.0)]))
    s.add(20.0, _Me([q("kalshi", "KC", *kc_later, t=20.0), q("robinhood", "DEN", *den_later, t=20.0)]))
    return s


class ActTests(unittest.TestCase):
    def test_guided_locks_when_the_second_leg_is_within_its_limit(self):
        s = series((0.55, 0.54), (0.40, 0.39))            # DEN rose to 0.40 but still under the 0.43 limit
        r = act(s, alert(), "guided")
        self.assertEqual((r["status"], r["matched"]), ("locked", 10))
        self.assertAlmostEqual(r["pnl"], 10 * (1 - 0.55 - 0.40) - float(__import__("arb_engine.fees.robinhood", fromlist=["x"]).RobinhoodFees(exchange="rothera").fee(0.40, 10)), places=6)

    def test_guided_undoes_the_first_leg_when_the_second_ran_away(self):
        s = series((0.55, 0.52), (0.47, 0.46))            # DEN at 0.47 > 0.43: sell KC back at 0.52
        r = act(s, alert(), "guided")
        self.assertEqual((r["status"], r["matched"]), ("unwound", 0))
        self.assertAlmostEqual(r["pnl"], 10 * (0.52 - 0.55), places=6)   # Kalshi fee-free in this test

    def test_guided_skips_a_first_leg_that_already_moved(self):
        r = act(series((0.58, 0.57), (0.36, 0.35)), alert(), "guided")
        self.assertEqual(r["status"], "missed")          # KC's ask left the 0.55 limit: nothing bought

    def test_naive_buys_the_second_leg_at_any_price(self):
        s = series((0.60, 0.59), (0.36, 0.35))            # Robinhood first, then Kalshi at 0.60 regardless
        r = act(s, alert(), "naive")
        self.assertEqual(r["status"], "locked")
        self.assertLess(r["pnl"], 0.4)                   # 1 - 0.36 - 0.60 = +0.04/ct before Robinhood's fee: the edge is mostly gone


class LedgerTests(unittest.TestCase):
    def test_liquidity_we_took_is_not_offered_again(self):
        led = Ledger()
        key = (EV, "kalshi", "KC")
        self.assertEqual(led.available(key, 0.0, 30), 30)
        led.take(key, 0.0, 25)
        self.assertEqual(led.available(key, 30.0, 30), 5)
        self.assertEqual(led.available(key, 200.0, 30), 30)   # after USED_WINDOW_S the book has refilled
        self.assertEqual(led.available((EV, "robinhood", "DEN"), 0.0, None), 10)   # size not recorded: a small order only


class BankrollTests(unittest.TestCase):
    def test_a_lock_holds_its_cost_until_the_game_ends(self):
        s = series((0.55, 0.54), (0.36, 0.35))
        a1, a2 = alert(t=0.0, n=100), alert(t=30.0, n=100)
        res = with_bankroll(s, [a1, a2], "instant", 5, 15, bankroll=100.0, ends={EV: 3600.0})
        self.assertEqual(res[0]["sized"], 100)            # $100 buys 109 sets at $0.91, capped by the alert's size of 100
        self.assertEqual(res[0]["matched"], 100)
        self.assertEqual(res[1]["sized"], 9)              # only the $9 left over until the game settles
        res = with_bankroll(s, [a1, a2], "instant", 5, 15, bankroll=100.0, ends={EV: 10.0})
        self.assertEqual(res[1]["sized"], 100)            # game over at t=10: the first lock paid $100 back
        capped = with_bankroll(s, [a1, a2], "instant", 5, 15, bankroll=100.0, ends={EV: 3600.0}, max_per_alert=40.0)
        self.assertEqual([r["sized"] for r in capped], [43, 43])   # $40 each: both alerts get taken

    def test_each_tier_stakes_its_own_fraction_of_equity(self):
        s = series((0.55, 0.54), (0.36, 0.35))
        a1, a2 = {**alert(t=0.0, n=100), "tier": "BIG ARB"}, {**alert(t=30.0, n=100), "tier": "ARB"}
        res = with_bankroll(s, [a1, a2], "instant", 5, 15, bankroll=100.0, ends={EV: 3600.0}, fractions={"BIG ARB": 0.2, "ARB": 0.05})
        self.assertEqual(res[0]["sized"], 21)             # $20 at $0.91 a set
        self.assertEqual(res[1]["sized"], 5)              # 5 % of equity ($80.89 cash + $21 locked) = $5.09
        res = with_bankroll(s, [a1, a2], "instant", 5, 15, bankroll=100.0, ends={EV: 3600.0}, fractions={"BIG ARB": 0.2})
        self.assertEqual(res[1]["sized"], 0)              # a tier with no fraction is not traded


class UnwindTests(unittest.TestCase):
    def test_an_unsold_leg_is_valued_at_its_last_bid_not_zero(self):
        s = Series()
        s.add(0.0, _Me([q("kalshi", "KC", 0.55, 0.53), q("robinhood", "DEN", 0.36, 0.35)]))
        s.add(10.0, _Me([q("kalshi", "KC", 0.58, None, t=10.0), q("robinhood", "DEN", 0.47, 0.46, t=10.0)]))
        key = (EV, "kalshi", "KC")
        self.assertEqual(s.bid_after(key, 5.0).bid, 0.53)   # no bid after t: the last one before it
        self.assertIsNone(s.bid_after((EV, "kalshi", "XX"), 5.0))


class WindowAndKellyTests(unittest.TestCase):
    def test_duration_table_by_tier(self):
        eps = [{"seconds": 0.0, "margin": 0.05}, {"seconds": 10.0, "margin": 0.04}, {"seconds": 35.0, "margin": 0.035},
               {"seconds": 5.0, "margin": 0.02}]
        t = duration_table(eps)
        self.assertEqual(t["BIG ARB"]["episodes"], 3)
        self.assertEqual(t["BIG ARB"]["median_s"], 10.0)
        self.assertAlmostEqual(t["BIG ARB"]["seen_once"], 0.333, places=3)
        self.assertAlmostEqual(t["BIG ARB"]["open_30s"], 0.333, places=3)
        self.assertEqual(t["all"]["episodes"], 4)
        self.assertNotIn("ARB SMALL", t)

    def test_kelly_fraction(self):
        f, g = kelly_fraction([1.0, -1.0, 1.0, -1.0, 1.0])   # p = 0.6 at even odds: f* = 2p - 1 = 0.2
        self.assertAlmostEqual(f, 0.2, places=2)
        self.assertGreater(g, 0)
        self.assertEqual(kelly_fraction([0.02, -0.03])[0], 0.0)   # negative mean: stake nothing
        self.assertEqual(kelly_fraction([0.05, 0.01])[0], 1.0)    # never loses: all of it (no leverage)


if __name__ == "__main__":
    unittest.main()

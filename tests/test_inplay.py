import unittest

from arb_engine.matching.matcher import MergedEvent
from arb_engine.models import EventInfo, OutcomeQuote
from arb_engine.strategy.alerts import Alerter
from arb_engine.strategy.inplay import InplayWatcher, Lot, evaluate_inplay

KEY = "nfl:DEN|KC:2026-09-21"
KFEE = {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}


def _me(k_den=(0.59, 0.61), k_kc=(0.39, 0.41), r_den=(0.60, 0.62), r_kc=(0.38, 0.40)):
    info = EventInfo(event_key=KEY, sport="nfl", market_type="moneyline", outcomes=["DEN", "KC"], labels={"DEN": "Denver", "KC": "Kansas City"}, in_play=True)
    q = lambda v, o, bid, ask, **kw: OutcomeQuote(v, f"{v}-{o}", KEY, o, ask=ask, bid=bid, **kw)  # noqa: E731
    return MergedEvent(KEY, info, {
        "kalshi": [q("kalshi", "DEN", *k_den, fee_params=KFEE, meta={"ticker": "T", "side": "yes"}), q("kalshi", "KC", *k_kc, fee_params=KFEE, meta={"ticker": "T", "side": "no"})],
        "robinhood": [q("robinhood", "DEN", *r_den, fee_params={"exchange": "rothera"}, book_id="rothera"), q("robinhood", "KC", *r_kc, fee_params={"exchange": "rothera"}, book_id="rothera")],
    })


class InplayTests(unittest.TestCase):
    def test_lot_parse_and_fees(self):
        lot = Lot.parse("robinhood:DEN:0.50:100")
        self.assertEqual((lot.venue, lot.outcome, lot.price, lot.count, lot.exchange), ("robinhood", "DEN", 0.5, 100.0, "rothera"))
        self.assertEqual(lot.fee, 2.0)   # $1 commission cap + $1 exchange
        self.assertEqual(lot.cost, 52.0)
        self.assertAlmostEqual(Lot.parse("kalshi:DEN:0.50:100").fee, 1.75)

    def test_lock_now_scenario(self):
        # Hold 100 Denver @ 0.50 (cost $52). KC dips to 0.40 on Robinhood -> lock at <= 0.46.
        v = evaluate_inplay(_me(), [Lot("robinhood", "DEN", 0.50, 100)])
        kc = next(s for s in v.sides if s.outcome == "KC")
        self.assertEqual((kc.need, kc.lock_price, kc.lock_available), (100.0, 0.46, True))
        self.assertAlmostEqual(kc.lock_profit_if_now, 6.0)
        self.assertTrue(any(a.startswith("LOCK NOW") for a in v.actions))
        self.assertFalse(v.balanced)

    def test_wait_when_other_side_too_expensive(self):
        v = evaluate_inplay(_me(r_kc=(0.47, 0.49), k_kc=(0.48, 0.50)), [Lot("robinhood", "DEN", 0.50, 100)])
        kc = next(s for s in v.sides if s.outcome == "KC")
        self.assertFalse(kc.lock_available)
        self.assertEqual(kc.lock_price, 0.46)
        self.assertTrue(any(a.startswith("wait:") for a in v.actions))

    def test_averaging_down_lowers_lock_bar_then_flat(self):
        lots = [Lot("robinhood", "DEN", 0.50, 100), Lot("robinhood", "DEN", 0.40, 100)]  # 200 DEN, cost 90 + 4 fees
        v = evaluate_inplay(_me(), lots)
        kc = next(s for s in v.sides if s.outcome == "KC")
        self.assertEqual(kc.need, 200.0)
        # total cost 94 for 200 payout -> 106 budget for 200 KC -> 0.51 per contract incl fees -> lock at 0.50 (fees) 
        self.assertGreaterEqual(kc.lock_price, 0.49)
        v2 = evaluate_inplay(_me(), lots + [Lot("robinhood", "KC", 0.40, 200)])
        self.assertTrue(v2.balanced)
        self.assertAlmostEqual(v2.locked_pnl, 200 - (94.0 + 80 + 4.0))
        self.assertTrue(v2.actions[0].startswith("FLAT"))

    def test_steal_signal(self):
        # Robinhood KC ask 0.30 while everyone else says ~0.40.
        v = evaluate_inplay(_me(r_kc=(0.28, 0.30)), [], steal_edge=0.03)
        kc = next(s for s in v.sides if s.outcome == "KC")
        self.assertTrue(kc.steal)
        self.assertEqual(kc.best_venue, "robinhood")
        self.assertTrue(any(a.startswith("STEAL") for a in v.actions))

    def test_unknown_outcome(self):
        with self.assertRaises(ValueError):
            evaluate_inplay(_me(), [Lot("robinhood", "CHI", 0.5, 1)])

    def test_watcher_alerts_once_per_action(self):
        import os
        me = _me()
        w = InplayWatcher(lambda: me, [Lot("robinhood", "DEN", 0.50, 100)], Alerter(journal_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), "inplay_test.jsonl"), quiet=True, desktop=False, webhook=""))
        w.step(); w.step()
        alerts = [e for e in w.alerts.events if e["kind"] == "alert"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["title"], "LOCK NOW")


if __name__ == "__main__":
    unittest.main()

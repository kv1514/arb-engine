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


class GameStateTests(unittest.TestCase):
    """Blend + agreement filter + game line with a synthetic ESPN state."""

    def _gs(self, **kw):
        from arb_engine.venues.espn import GameState
        base = dict(event_id="1", home="KC", away="DEN", home_score=14, away_score=17, status="live", period=3, clock_seconds_remaining_in_period=252, game_seconds_remaining=900 + 252, possession="away", down=2, distance=7, yardline_100=35, home_timeouts=3, away_timeouts=2, espn_home_wp=0.43, vegas_spread_home=-3.0)
        base.update(kw)
        return GameState(**base)

    def test_model_and_espn_enter_the_blend(self):
        v = evaluate_inplay(_me(), [], game_state=self._gs())
        kc = next(s for s in v.sides if s.outcome == "KC")
        self.assertTrue(v.live)
        self.assertIsNotNone(kc.model_p)
        self.assertAlmostEqual(kc.espn_p, 0.43)
        self.assertTrue(0 < kc.model_p < 1)
        self.assertIsNotNone(kc.fair)
        self.assertIn("Q3 04:12", v.game_line)
        self.assertIn("DEN 17-14 KC", v.game_line)
        self.assertIn("DEN ball 2nd & 7", v.game_line)
        self.assertIn("TO 2/3", v.game_line)
        self.assertIn("market", v.fair_line)
        self.assertIn("model", v.fair_line)
        self.assertEqual(v.blend["live"], True)
        self.assertEqual(set(v.blend["weights"]), {"market", "model", "espn"})

    def test_steal_requires_model_agreement_in_play(self):
        # Robinhood KC ask 0.30 while the market consensus says ~0.40 -> market-only STEAL...
        me = _me(r_kc=(0.28, 0.30))
        pre = evaluate_inplay(me, [], steal_edge=0.03)
        self.assertTrue(next(s for s in pre.sides if s.outcome == "KC").steal)
        # ...but live, with a model that also says KC is only ~0.30, it is not a steal.
        class StubModel:
            pass
        import arb_engine.strategy.inplay as ip
        orig = ip.model_home_wp
        try:
            ip.model_home_wp = lambda gs, model=None: 0.31  # P(KC=home)
            live = evaluate_inplay(me, [], steal_edge=0.03, game_state=self._gs(espn_home_wp=None))
        finally:
            ip.model_home_wp = orig
        kc = next(s for s in live.sides if s.outcome == "KC")
        self.assertFalse(kc.steal)
        self.assertTrue(any("likely a stale quote" in a for a in live.actions))
        try:
            ip.model_home_wp = lambda gs, model=None: 0.45
            live2 = evaluate_inplay(me, [], steal_edge=0.03, game_state=self._gs(espn_home_wp=None))
        finally:
            ip.model_home_wp = orig
        self.assertTrue(next(s for s in live2.sides if s.outcome == "KC").steal)

    def test_disagreement_reported(self):
        import arb_engine.strategy.inplay as ip
        orig = ip.model_home_wp
        try:
            ip.model_home_wp = lambda gs, model=None: 0.70
            v = evaluate_inplay(_me(), [], game_state=self._gs(espn_home_wp=0.40))
        finally:
            ip.model_home_wp = orig
        self.assertGreater(v.disagreement, 0.05)
        self.assertTrue(any(a.startswith("sources disagree") for a in v.actions))

    def test_pregame_state_keeps_market_fair(self):
        v = evaluate_inplay(_me(), [], game_state=self._gs(status="pre", possession=None, home_score=0, away_score=0, game_seconds_remaining=3600, espn_home_wp=None))
        self.assertFalse(v.live)
        self.assertEqual(v.blend["weights"], {"market": 1.0})
        self.assertTrue(v.game_line.startswith("PRE"))

    def test_first_half_kickoff_flag_reaches_the_model(self):
        # Q2 7-7, KC (home) ball: with the 2H-kickoff recipient known the model must not
        # return the average of the two assignments (they differ by ~8 points here).
        from arb_engine.models.wp import home_win_probability
        base = dict(home_score=7, away_score=7, period=2, clock_seconds_remaining_in_period=300, game_seconds_remaining=2100, possession="home", down=1, distance=10, yardline_100=70, vegas_spread_home=0.0, espn_home_wp=None)
        gs = self._gs(**base, receive_2h_ko_home=True)
        kc = next(s for s in evaluate_inplay(_me(), [], game_state=gs).sides if s.outcome == "KC")
        args = dict(home_score=7, away_score=7, game_seconds_remaining=2100, possession="home", down=1, distance=10, yardline_100=70, home_timeouts=3, away_timeouts=2, vegas_spread_home=0.0)
        self.assertAlmostEqual(kc.model_p, home_win_probability(receive_2h_ko_home=True, **args), places=9)
        self.assertNotAlmostEqual(kc.model_p, home_win_probability(**args), places=3)
        unknown = next(s for s in evaluate_inplay(_me(), [], game_state=self._gs(**base)).sides if s.outcome == "KC")
        self.assertAlmostEqual(unknown.model_p, home_win_probability(**args), places=9)

    def test_watcher_passes_state(self):
        import os
        me = _me()
        w = InplayWatcher(lambda: me, [Lot("robinhood", "DEN", 0.50, 100)], Alerter(journal_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), "inplay_test2.jsonl"), quiet=True, desktop=False, webhook=""), fetch_state=lambda: self._gs())
        view = w.step()
        self.assertIsNotNone(view.game_state)
        self.assertEqual(view.game_state["home"], "KC")
        self.assertTrue(any(e["kind"] == "info" and "Q3 04:12" in e["msg"] for e in w.alerts.events))


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

    def test_partial_hedge_lock_price_uses_full_payout(self):
        # 100 DEN @ 0.50 ($52.00) + 40 KC @ 0.40 ($16.80): total $68.80, need 60 more KC to
        # pay 100 either way. Budget 100 - 68.80 = 31.20 -> 60 @ 0.50 + $1.20 fees fits exactly.
        # (The old code budgeted only `need` of payout, so a partial hedge got no lock at all.)
        lots = [Lot("robinhood", "DEN", 0.50, 100), Lot("robinhood", "KC", 0.40, 40)]
        v = evaluate_inplay(_me(), lots)
        kc = next(s for s in v.sides if s.outcome == "KC")
        self.assertAlmostEqual(v.total_cost, 68.80)
        self.assertEqual((kc.need, kc.lock_price, kc.lock_available), (60.0, 0.50, True))
        self.assertAlmostEqual(kc.lock_profit_if_now, 100 - 68.80 - (0.40 * 60 + 1.20))
        self.assertTrue(any(a.startswith("LOCK NOW: buy 60 x Kansas City") for a in v.actions))
        # Same book, no KC held: the classic case still locks at 0.46 (100 needed, budget 48).
        base = next(s for s in evaluate_inplay(_me(), lots[:1]).sides if s.outcome == "KC")
        self.assertEqual((base.need, base.lock_price), (100.0, 0.46))
        # A target margin comes off the full payout too: 100 * 0.98 - 68.80 = 29.20 -> 0.46.
        tm = next(s for s in evaluate_inplay(_me(), lots, target_margin=0.02).sides if s.outcome == "KC")
        self.assertEqual(tm.lock_price, 0.46)

    def test_three_way_market_is_refused(self):
        info = EventInfo(event_key="epl:ARS|CHE:2026-09-21", sport="epl", market_type="moneyline", outcomes=["ARS", "DRAW", "CHE"], labels={}, in_play=True)
        q = lambda o, ask: OutcomeQuote("kalshi", f"k-{o}", info.event_key, o, ask=ask, bid=ask - 0.02, fee_params=KFEE)  # noqa: E731
        me = MergedEvent(info.event_key, info, {"kalshi": [q("ARS", 0.42), q("DRAW", 0.27), q("CHE", 0.32)]})
        with self.assertRaises(ValueError):
            evaluate_inplay(me, [Lot("kalshi", "ARS", 0.40, 100)])

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

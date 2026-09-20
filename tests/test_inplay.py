import os
import unittest
from dataclasses import dataclass
from typing import Optional

from arb_engine.matching.matcher import MergedEvent
from arb_engine.models import EventInfo, OutcomeQuote
from arb_engine.strategy.alerts import Alerter
from arb_engine.strategy.inplay import FeedFreshness, InplayWatcher, Lot, evaluate_inplay, model_home_wp, resolve_spread_home, sportsbook_probs
from arb_engine.venues.espn import GameState

KEY = "nfl:DEN|KC:2026-09-21"
KFEE = {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}


def _me(k_den=(0.59, 0.61), k_kc=(0.39, 0.41), r_den=(0.60, 0.62), r_kc=(0.38, 0.40), r_exchange="rothera", r_meta=None, k_meta=None):
    info = EventInfo(event_key=KEY, sport="nfl", market_type="moneyline", outcomes=["DEN", "KC"], labels={"DEN": "Denver", "KC": "Kansas City"}, in_play=True)
    q = lambda v, o, bid, ask, **kw: OutcomeQuote(v, f"{v}-{o}", KEY, o, ask=ask, bid=bid, **kw)  # noqa: E731
    km = {"ticker": "T", **(k_meta or {})}
    rm = {"exchange": r_exchange, **(r_meta or {})}
    return MergedEvent(KEY, info, {
        "kalshi": [q("kalshi", "DEN", *k_den, fee_params=KFEE, meta={**km, "side": "yes"}), q("kalshi", "KC", *k_kc, fee_params=KFEE, meta={**km, "side": "no"})],
        "robinhood": [q("robinhood", "DEN", *r_den, fee_params={"exchange": r_exchange}, book_id=r_exchange, meta=dict(rm)), q("robinhood", "KC", *r_kc, fee_params={"exchange": r_exchange}, book_id=r_exchange, meta=dict(rm))],
    })


@dataclass
class GS2(GameState):
    """GameState plus the fields P02's ESPN hardening adds (stubbed here: this item is
    tested against the field names, not the parser)."""
    last_play_id: Optional[str] = None
    suspect: bool = False
    review_pending: bool = False
    espn_tie: Optional[float] = None
    sportsbook_ml_home: Optional[float] = None
    sportsbook_ml_away: Optional[float] = None
    pickcenter_spread: Optional[float] = None
    overtime_sentinel: bool = False


def _gs(cls=GameState, **kw):
    base = dict(event_id="1", home="KC", away="DEN", home_score=14, away_score=17, status="live", period=3, clock_seconds_remaining_in_period=252, game_seconds_remaining=900 + 252, possession="away", down=2, distance=7, yardline_100=35, home_timeouts=3, away_timeouts=2, espn_home_wp=0.43, vegas_spread_home=-3.0)
    base.update(kw)
    return cls(**base)


def _stub_model(p):
    """Replace the WP model with a constant P(KC=home) for the duration of a `with`."""
    import arb_engine.strategy.inplay as ip

    class _Ctx:
        def __enter__(self):
            self.orig = ip.model_home_wp
            ip.model_home_wp = lambda gs, model=None, **kw: p
        def __exit__(self, *a):
            ip.model_home_wp = self.orig
    return _Ctx()


def _side(view, o):
    return next(s for s in view.sides if s.outcome == o)


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


class SizingTests(unittest.TestCase):
    def test_steal_sizes_by_fractional_kelly_capped_by_depth(self):
        # Robinhood asks 0.50 for DEN with 180 on offer; consensus fair ~0.58 -> STEAL pre-game.
        me = _me(k_den=(0.59, 0.61), k_kc=(0.39, 0.41), r_den=(0.49, 0.50), r_kc=(0.48, 0.52))
        me.info.in_play = False
        for q in me.quotes_by_venue["robinhood"]:
            if q.outcome == "DEN":
                q.ask_size = 180
        view = evaluate_inplay(me, [], {}, steal_edge=0.03, bankroll=1000.0, kelly_fraction=0.25)
        den = next(s for s in view.sides if s.outcome == "DEN")
        self.assertTrue(den.steal)
        self.assertEqual(den.depth_contracts, 180)
        all_in = den.best_all_in
        f = 0.25 * (den.fair - all_in) / (1 - all_in)
        self.assertAlmostEqual(den.kelly_stake, round(1000 * f, 2), places=2)
        self.assertEqual(den.kelly_contracts, int((1000 * f) // all_in))
        self.assertEqual(den.suggested_contracts, min(den.kelly_contracts, 180))
        self.assertTrue(any("→ buy " in a and "Kelly" in a for a in view.actions), view.actions)
        # No bankroll: no sizing, same STEAL.
        view2 = evaluate_inplay(me, [], {}, steal_edge=0.03)
        den2 = next(s for s in view2.sides if s.outcome == "DEN")
        self.assertTrue(den2.steal)
        self.assertIsNone(den2.suggested_contracts)


class GateTests(unittest.TestCase):
    """Execution gates: the STEAL scenario (Robinhood KC ask 0.30, fair ~0.40, model 0.45
    agreeing) is a STEAL on a fresh feed and `wait: <reason>` under every gate."""

    def _steal_me(self, **kw):
        return _me(r_kc=(0.28, 0.30), **kw)

    def test_fresh_feed_is_a_steal_and_bit_identical_to_no_freshness(self):
        with _stub_model(0.45):
            plain = evaluate_inplay(self._steal_me(), [], steal_edge=0.03, game_state=_gs())
            fresh = evaluate_inplay(self._steal_me(), [], steal_edge=0.03, game_state=_gs(), freshness=FeedFreshness(), now=1000.0)
        self.assertTrue(_side(plain, "KC").steal and _side(fresh, "KC").steal)
        self.assertEqual([s.fair for s in plain.sides], [s.fair for s in fresh.sides])
        self.assertEqual(plain.blend, fresh.blend)
        self.assertEqual(fresh.gated_reasons, [])
        self.assertEqual(set(fresh.freshness) >= {"last_state_change_ts", "last_score_change_ts", "mids"}, True)

    def test_feed_stale_when_espn_unchanged_and_a_venue_moves(self):
        f = FeedFreshness(stale_after_s=15.0)
        with _stub_model(0.45):
            first = evaluate_inplay(self._steal_me(), [], steal_edge=0.03, game_state=_gs(), freshness=f, now=1000.0)
            # 20 s later: identical ESPN state, Kalshi mids moved 0.03 -> the market knows something.
            second = evaluate_inplay(self._steal_me(k_den=(0.62, 0.64), k_kc=(0.36, 0.38)), [], steal_edge=0.03, game_state=_gs(), freshness=f, now=1020.0)
            # ...and a fresh state change clears it (a mid move alone is not staleness).
            third = evaluate_inplay(self._steal_me(k_den=(0.65, 0.67), k_kc=(0.33, 0.35)), [], steal_edge=0.03, game_state=_gs(clock_seconds_remaining_in_period=240), freshness=f, now=1030.0)
        self.assertTrue(_side(first, "KC").steal)
        kc = _side(second, "KC")
        self.assertFalse(kc.steal)
        self.assertTrue(kc.steal_gated)
        self.assertEqual(kc.gated_reasons, ["feed-stale"])
        self.assertEqual(second.gated_reasons, ["feed-stale"])
        gated = [a for a in second.actions if a.startswith("GATED STEAL")]
        self.assertEqual(len(gated), 1)
        self.assertIn("wait: feed-stale", gated[0])
        self.assertFalse(any(a.startswith("STEAL") for a in second.actions))
        self.assertAlmostEqual(f.mid_moves["kalshi"], 0.03)
        self.assertTrue(_side(third, "KC").steal, third.actions)

    def test_clock_frozen_is_time_based_and_needs_a_market_move(self):
        # 5 s cadence, identical ESPN state for a minute, venues quiet: never frozen (a real
        # clock stoppage or ESPN's 10-20 s update lag must not gate everything).
        f = FeedFreshness(stale_after_s=100.0, interval_s=5.0)
        self.assertEqual(f.frozen_after_s, 30.0)                       # max(3 x 5 s, 30 s)
        with _stub_model(0.45):
            quiet = [evaluate_inplay(self._steal_me(), [], steal_edge=0.03, game_state=_gs(), freshness=f, now=1000.0 + 5 * i) for i in range(13)]
        self.assertTrue(all(_side(v, "KC").steal for v in quiet), [v.gated_reasons for v in quiet])
        # Same cadence, but Kalshi mids drifted 0.03 since the state last changed: frozen from
        # 30 s on (not at 25 s), and cleared the moment the state moves.
        f2 = FeedFreshness(stale_after_s=100.0, interval_s=5.0)
        moved = self._steal_me(k_den=(0.62, 0.64), k_kc=(0.36, 0.38))
        with _stub_model(0.45):
            first = evaluate_inplay(self._steal_me(), [], steal_edge=0.03, game_state=_gs(), freshness=f2, now=1000.0)
            at25 = evaluate_inplay(moved, [], steal_edge=0.03, game_state=_gs(), freshness=f2, now=1025.0)
            at30 = evaluate_inplay(moved, [], steal_edge=0.03, game_state=_gs(), freshness=f2, now=1030.0)
            after = evaluate_inplay(moved, [], steal_edge=0.03, game_state=_gs(clock_seconds_remaining_in_period=240), freshness=f2, now=1035.0)
        self.assertTrue(_side(first, "KC").steal)
        self.assertTrue(_side(at25, "KC").steal, at25.gated_reasons)
        self.assertEqual(_side(at30, "KC").gated_reasons, ["clock-frozen"])
        self.assertEqual(at30.freshness["frozen_s"], 30.0)
        self.assertAlmostEqual(at30.freshness["moved_since_state_change"], 0.03)
        self.assertTrue(_side(after, "KC").steal, after.actions)
        # Halftime / end of period: the clock is legitimately 0 -> no frozen gate even with a move.
        f3 = FeedFreshness(stale_after_s=100.0, interval_s=5.0)
        with _stub_model(0.45):
            evaluate_inplay(self._steal_me(), [], steal_edge=0.03, game_state=_gs(period=2, clock_seconds_remaining_in_period=0, game_seconds_remaining=1800), freshness=f3, now=1000.0)
            ht = evaluate_inplay(moved, [], steal_edge=0.03, game_state=_gs(period=2, clock_seconds_remaining_in_period=0, game_seconds_remaining=1800), freshness=f3, now=1040.0)
        self.assertTrue(_side(ht, "KC").steal, ht.actions)
        # The threshold is a setting (seconds) and scales with the cadence: 3 x 20 s at --every 20.
        self.assertEqual(FeedFreshness.from_settings({"inplay_frozen_s": 12.0}, interval_s=1.0).frozen_after_s, 12.0)
        self.assertEqual(FeedFreshness.from_settings({}, interval_s=20.0).frozen_after_s, 60.0)
        self.assertEqual(FeedFreshness.from_settings({}, interval_s=1.0).frozen_after_s, 30.0)

    def test_feed_stale_and_quote_old_at_a_one_second_cadence(self):
        # The overlay polls the bridge every second. feed-stale measures the mid move over a
        # trailing max(interval, 10 s) window, so a 0.003/s drift (0.03 over 10 s) trips it
        # after stale_after_s exactly as one 0.03 jump between two 10 s polls does.
        f = FeedFreshness(stale_after_s=15.0, interval_s=1.0)
        self.assertEqual(f.window_s, 10.0)
        views = []
        with _stub_model(0.45):
            for i in range(21):
                d = 0.003 * i
                views.append(evaluate_inplay(self._steal_me(k_den=(0.59 + d, 0.61 + d), k_kc=(0.39 - d, 0.41 - d)), [], steal_edge=0.03, game_state=_gs(), freshness=f, now=1000.0 + i))
        self.assertTrue(all(_side(v, "KC").steal for v in views[:16]), [(i, v.gated_reasons) for i, v in enumerate(views[:16])])   # <= 15 s: not stale yet
        self.assertEqual(_side(views[16], "KC").gated_reasons, ["feed-stale"])
        self.assertAlmostEqual(views[20].freshness["mid_moves"]["kalshi"], 0.03, places=6)   # vs the poll 10 s ago, not 1 s ago
        # quote-old at 1 s: a quote 5 s old is normal REST lag, 11 s is old (floor 10 s).
        q = OutcomeQuote("robinhood", "x", KEY, "KC", ask=0.3, bid=0.28, ts=1000.0, quote_time=995.0, meta={"exchange": "rothera"})
        self.assertEqual(f.quote_reasons(q), [])
        q.quote_time = 989.0
        self.assertEqual(f.quote_reasons(q), ["quote-old:robinhood"])
        # At a 30 s cadence the window is the cadence itself.
        self.assertEqual(FeedFreshness(interval_s=30.0).window_s, 30.0)

    def test_quote_old_gates_only_venues_that_report_a_time(self):
        me = self._steal_me()
        for q in me.quotes_by_venue["robinhood"]:
            q.ts, q.quote_time = 1000.0, 985.0     # 15 s old at a 10 s poll interval
        for q in me.quotes_by_venue["kalshi"]:
            q.ts, q.quote_time = 1000.0, None
        with _stub_model(0.45):
            v = evaluate_inplay(me, [], steal_edge=0.03, game_state=_gs(), freshness=FeedFreshness(interval_s=10.0), now=1000.0)
        kc = _side(v, "KC")
        self.assertEqual(kc.best_venue, "robinhood")
        self.assertEqual(kc.gated_reasons, ["quote-old:robinhood"])
        self.assertTrue(any("wait: quote-old:robinhood" in a for a in v.actions))
        # Same staleness on Robinhood but Kalshi (no quote_time) is the cheap venue: not gated.
        me2 = _me(k_kc=(0.28, 0.30))
        for q in me2.quotes_by_venue["robinhood"]:
            q.ts, q.quote_time = 1000.0, 985.0
        with _stub_model(0.45):
            v2 = evaluate_inplay(me2, [], steal_edge=0.03, game_state=_gs(), freshness=FeedFreshness(interval_s=10.0), now=1000.0)
        self.assertEqual(_side(v2, "KC").best_venue, "kalshi")
        self.assertTrue(_side(v2, "KC").steal)
        self.assertEqual(_side(v2, "KC").gated_reasons, [])

    def test_score_pending_until_last_play_id_advances(self):
        f = FeedFreshness()
        with _stub_model(0.45):
            evaluate_inplay(self._steal_me(), [], steal_edge=0.03, game_state=_gs(GS2, last_play_id="p1"), freshness=f, now=1000.0)
            # KC scores (14 -> 21) but the play id has not moved: ESPN has the score, not the play.
            scored = evaluate_inplay(self._steal_me(), [], steal_edge=0.03, game_state=_gs(GS2, home_score=21, last_play_id="p1"), freshness=f, now=1010.0)
            still = evaluate_inplay(self._steal_me(), [], steal_edge=0.03, game_state=_gs(GS2, home_score=21, last_play_id="p1"), freshness=f, now=1020.0)
            posted = evaluate_inplay(self._steal_me(), [], steal_edge=0.03, game_state=_gs(GS2, home_score=21, last_play_id="p2", clock_seconds_remaining_in_period=200), freshness=f, now=1030.0)
        self.assertEqual(_side(scored, "KC").gated_reasons, ["score-pending"])
        self.assertIn("score-pending", _side(still, "KC").gated_reasons)
        self.assertTrue(_side(posted, "KC").steal, posted.actions)
        self.assertFalse(f.score_pending)

    def test_score_pending_timer_fallback_without_play_ids(self):
        f = FeedFreshness(score_hold_s=20.0)
        with _stub_model(0.45):
            evaluate_inplay(self._steal_me(), [], steal_edge=0.03, game_state=_gs(), freshness=f, now=1000.0)
            scored = evaluate_inplay(self._steal_me(), [], steal_edge=0.03, game_state=_gs(home_score=21), freshness=f, now=1005.0)
            held = evaluate_inplay(self._steal_me(), [], steal_edge=0.03, game_state=_gs(home_score=21, clock_seconds_remaining_in_period=240), freshness=f, now=1015.0)
            over = evaluate_inplay(self._steal_me(), [], steal_edge=0.03, game_state=_gs(home_score=21, clock_seconds_remaining_in_period=230), freshness=f, now=1030.0)
        self.assertEqual(_side(scored, "KC").gated_reasons, ["score-pending"])
        self.assertEqual(_side(held, "KC").gated_reasons, ["score-pending"])
        self.assertTrue(_side(over, "KC").steal)

    def test_suspect_and_review_flags_gate(self):
        with _stub_model(0.45):
            v = evaluate_inplay(self._steal_me(), [], steal_edge=0.03, game_state=_gs(GS2, suspect=True, review_pending=True), freshness=FeedFreshness(), now=1000.0)
        self.assertEqual(v.gated_reasons, ["suspect", "review-pending"])
        self.assertTrue(any(a.startswith("GATED STEAL: wait: suspect, review-pending") for a in v.actions))

    def test_gated_lock_now(self):
        # Holding 100 DEN @ 0.50, KC at 0.40 on Robinhood locks $6 — but not on a suspect feed.
        f = FeedFreshness()
        v = evaluate_inplay(_me(), [Lot("robinhood", "DEN", 0.50, 100)], game_state=_gs(GS2, suspect=True), freshness=f, now=1000.0)
        kc = _side(v, "KC")
        self.assertTrue(kc.lock_available)
        self.assertTrue(kc.lock_gated)
        self.assertEqual(kc.gated_reasons, ["suspect"])
        self.assertTrue(any(a.startswith("GATED LOCK NOW: wait: suspect") for a in v.actions))
        self.assertFalse(any(a.startswith("LOCK NOW") for a in v.actions))
        clean = evaluate_inplay(_me(), [Lot("robinhood", "DEN", 0.50, 100)], game_state=_gs(GS2), freshness=f, now=1010.0)
        self.assertTrue(any(a.startswith("LOCK NOW") for a in clean.actions))

    def test_watcher_journals_gated_actions_as_info_not_alerts(self):
        tapes = iter([self._steal_me(), self._steal_me(k_den=(0.62, 0.64), k_kc=(0.36, 0.38))])   # Kalshi moves 0.03 on the second poll
        f = FeedFreshness(frozen_s=5.0, stale_after_s=100.0, interval_s=5.0)
        alerter = Alerter(journal_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), "inplay_gate_test.jsonl"), quiet=True, desktop=False, webhook="")
        w = InplayWatcher(lambda: next(tapes), [], alerter, fetch_state=lambda: _gs(), steal_edge=0.03, freshness=f)
        with _stub_model(0.45):
            w.step(now=1000.0)
            w.step(now=1005.0)
        alerts = [e for e in w.alerts.events if e["kind"] == "alert"]
        self.assertEqual([a["title"] for a in alerts], ["STEAL"])
        self.assertEqual(alerts[0]["steal"]["outcome"], "KC")
        self.assertEqual(alerts[0]["steal"]["gated"], False)
        self.assertTrue(any(e["kind"] == "info" and e["msg"].startswith("GATED STEAL: wait: clock-frozen") for e in w.alerts.events))


class CdnaTests(unittest.TestCase):
    def test_cdna_steal_needs_the_delay_haircut(self):
        # Robinhood/CDNA KC ask 0.37 (all-in 0.39) vs fair ~0.43: +3.8%, above 0.03 but below 0.05.
        me = _me(r_kc=(0.35, 0.37), r_exchange="cdna")
        with _stub_model(0.45):
            v = evaluate_inplay(me, [], steal_edge=0.03, game_state=_gs())
        kc = _side(v, "KC")
        self.assertEqual(kc.best_exchange, "cdna")
        self.assertAlmostEqual(kc.steal_threshold, 0.05)
        self.assertGreater(kc.steal_edge, 0.03)
        self.assertLess(kc.steal_edge, 0.05)
        self.assertFalse(kc.steal)
        # A bigger gap clears the haircut and the action says so.
        with _stub_model(0.45):
            v2 = evaluate_inplay(_me(r_kc=(0.22, 0.24), r_exchange="cdna"), [], steal_edge=0.03, game_state=_gs())
        self.assertTrue(_side(v2, "KC").steal)
        self.assertTrue(any("[cdna +2% haircut]" in a for a in v2.actions), v2.actions)
        # The haircut is a setting.
        with _stub_model(0.45):
            v3 = evaluate_inplay(me, [], {"inplay_delay_haircut_cdna": 0.0}, steal_edge=0.03, game_state=_gs())
        self.assertTrue(_side(v3, "KC").steal)

    def test_cdna_legs_never_lock_now_in_play(self):
        lots = [Lot("robinhood", "DEN", 0.50, 100)]
        # KC at 0.40 only on CDNA; Kalshi asks 0.48 (above the 0.46 lock): no LOCK NOW in play.
        me = _me(k_kc=(0.46, 0.48), r_exchange="cdna")
        v = evaluate_inplay(me, lots, game_state=_gs())
        kc = _side(v, "KC")
        self.assertFalse(kc.lock_available)
        self.assertEqual(kc.lock_price, 0.46)
        self.assertFalse(any(a.startswith("LOCK NOW") for a in v.actions))
        self.assertTrue(any("delayed 3 s - not lockable" in a for a in v.actions), v.actions)
        # Pre-game the same leg locks normally (the delay only matters against a moving game).
        pre = evaluate_inplay(me, lots, game_state=_gs(status="pre", possession=None, home_score=0, away_score=0, game_seconds_remaining=3600))
        self.assertTrue(any(a.startswith("LOCK NOW") and "robinhood" in a for a in pre.actions), pre.actions)
        # In play with Kalshi lockable, the lock goes to Kalshi and CDNA is a note.
        v2 = evaluate_inplay(_me(r_exchange="cdna"), lots, game_state=_gs())
        self.assertTrue(any(a.startswith("LOCK NOW") and "on kalshi" in a for a in v2.actions), v2.actions)

    def test_cdna_quote_age_carries_the_order_delay(self):
        f = FeedFreshness(interval_s=10.0)
        q = OutcomeQuote("robinhood", "x", KEY, "KC", ask=0.3, bid=0.28, ts=1000.0, quote_time=992.0, meta={"exchange": "cdna"})
        self.assertAlmostEqual(f.quote_age(q), 11.0)
        self.assertEqual(f.quote_reasons(q), ["quote-old:robinhood"])
        q.meta = {"exchange": "rothera"}
        self.assertEqual(f.quote_reasons(q), [])


class TieAwareFairTests(unittest.TestCase):
    def test_leg_fair_follows_the_contracts_tie_rule(self):
        # Robinhood (Rothera, tie pays 0) is the cheap KC venue; Kalshi (tie pays 0.5) for DEN.
        me = _me(r_kc=(0.36, 0.38), r_meta={"tie_payout": 0.0}, k_meta={"tie_payout": 0.5})
        v = evaluate_inplay(me, [], game_state=_gs(GS2, espn_tie=0.04))
        self.assertAlmostEqual(v.blend["p_tie"], 0.04)
        kc, den = _side(v, "KC"), _side(v, "DEN")
        self.assertEqual(kc.best_venue, "robinhood")
        self.assertEqual(den.best_venue, "kalshi")
        self.assertAlmostEqual(kc.fair, v.blend["win"]["KC"])                 # tie_payout 0: pure win probability
        self.assertAlmostEqual(kc.fair, v.blend["fair"]["KC"] - 0.02)
        self.assertAlmostEqual(den.fair, v.blend["fair"]["DEN"])              # tie_payout 0.5: win + 0.5 * tie
        # Without a tie estimate nothing changes (no p_tie key, fair = blend).
        v0 = evaluate_inplay(me, [], game_state=_gs(GS2))
        self.assertNotIn("p_tie", v0.blend)
        self.assertAlmostEqual(_side(v0, "KC").fair, v0.blend["fair"]["KC"])

    def test_tie_from_the_wp_series_when_espn_tie_is_absent(self):
        v = evaluate_inplay(_me(r_meta={"tie_payout": 0.0}), [], game_state=_gs(espn_wp_series=[{"play_id": "1", "home_wp": 0.43, "tie": 0.03}]))
        self.assertAlmostEqual(v.blend["p_tie"], 0.03)


class SpreadFallbackTests(unittest.TestCase):
    def test_chain_order(self):
        from arb_engine.models.wp import home_win_probability
        # 1. summary odds
        self.assertEqual(resolve_spread_home(_gs(GS2, vegas_spread_home=-3.0, pickcenter_spread=-2.5)), (-3.0, "espn-odds"))
        # 2. pickcenter
        self.assertEqual(resolve_spread_home(_gs(GS2, vegas_spread_home=None, pickcenter_spread=-2.5)), (-2.5, "pickcenter"))
        # 3. sportsbook moneylines inverted through the model, cached on the freshness object
        f = FeedFreshness()
        s, src = resolve_spread_home(_gs(GS2, vegas_spread_home=None, sportsbook_ml_home=-150, sportsbook_ml_away=130), f, "KC", "DEN")
        self.assertEqual(src, "sportsbook-ml")
        p = sportsbook_probs(_gs(GS2, sportsbook_ml_home=-150, sportsbook_ml_away=130), "KC", "DEN")["KC"]
        self.assertAlmostEqual(home_win_probability(home_score=0, away_score=0, game_seconds_remaining=3600, vegas_spread_home=s), p, delta=0.01)
        self.assertLess(s, 0)                     # home favourite -> negative home line
        self.assertEqual((f.spread_home, f.spread_source), (s, "sportsbook-ml"))
        # 4. the last pre-game blended fair, remembered per event
        f2 = FeedFreshness()
        evaluate_inplay(_me(), [], game_state=_gs(GS2, status="pre", vegas_spread_home=None, possession=None, home_score=0, away_score=0, game_seconds_remaining=3600), freshness=f2, now=1000.0)
        self.assertIsNotNone(f2.pregame_home_p)
        s2, src2 = resolve_spread_home(_gs(GS2, vegas_spread_home=None), f2, "KC", "DEN")
        self.assertEqual(src2, "pregame-fair")
        self.assertAlmostEqual(home_win_probability(home_score=0, away_score=0, game_seconds_remaining=3600, vegas_spread_home=s2), f2.pregame_home_p, delta=0.01)
        # 5. nothing: None, logged once
        f3 = FeedFreshness()
        with self.assertLogs("arb_engine.strategy.inplay", level="INFO") as cm:
            self.assertEqual(resolve_spread_home(_gs(GS2, vegas_spread_home=None), f3, "KC", "DEN"), (None, None))
        self.assertEqual(len(cm.output), 1)
        self.assertEqual(resolve_spread_home(_gs(GS2, vegas_spread_home=None), f3, "KC", "DEN"), (None, None))  # no second log line
        self.assertTrue(f3.spread_logged)

    def test_live_model_uses_the_resolved_spread(self):
        from arb_engine.models.wp import home_win_probability
        v = evaluate_inplay(_me(), [], game_state=_gs(GS2, vegas_spread_home=None, pickcenter_spread=-2.5, espn_home_wp=None), freshness=FeedFreshness(), now=1000.0)
        self.assertEqual(v.spread_source, "pickcenter")
        args = dict(home_score=14, away_score=17, game_seconds_remaining=1152, possession="away", down=2, distance=7, yardline_100=35, home_timeouts=3, away_timeouts=2)
        self.assertAlmostEqual(_side(v, "KC").model_p, home_win_probability(vegas_spread_home=-2.5, **args), places=9)

    def test_sportsbook_probs_anchor_pregame_only(self):
        pre = dict(status="pre", possession=None, home_score=0, away_score=0, game_seconds_remaining=3600, espn_home_wp=None)
        without = evaluate_inplay(_me(), [], game_state=_gs(GS2, **pre))
        with_sb = evaluate_inplay(_me(), [], game_state=_gs(GS2, sportsbook_ml_home=-300, sportsbook_ml_away=250, **pre))
        self.assertNotAlmostEqual(_side(without, "KC").market_p, _side(with_sb, "KC").market_p, places=3)
        self.assertGreater(_side(with_sb, "KC").market_p, _side(without, "KC").market_p)   # -300 pulls KC up
        # Live: the same moneylines change nothing (the fair is bit-identical).
        live0 = evaluate_inplay(_me(), [], game_state=_gs(GS2))
        live1 = evaluate_inplay(_me(), [], game_state=_gs(GS2, sportsbook_ml_home=-300, sportsbook_ml_away=250))
        self.assertEqual([s.fair for s in live0.sides], [s.fair for s in live1.sides])
        self.assertEqual([s.market_p for s in live0.sides], [s.market_p for s in live1.sides])


class ModelForwardingTests(unittest.TestCase):
    def test_overtime_sentinel_returns_none(self):
        self.assertIsNone(model_home_wp(_gs(GS2, sport="ncaaf", period=5, overtime_sentinel=True)))
        self.assertIsNotNone(model_home_wp(_gs(GS2, sport="ncaaf")))

    def test_extra_state_fields_forwarded_only_when_the_model_accepts_them(self):
        import arb_engine.models.wp as wp
        seen = {}
        orig = wp.home_win_probability

        def newer(*, play_class=None, overtime=False, season=None, **kw):
            seen.update({"play_class": play_class, "overtime": overtime, "season": season})
            return 0.5

        def older(**kw):
            seen.update(kw)
            return 0.5

        @dataclass
        class GS3(GS2):
            play_class: Optional[str] = None
            overtime: bool = False
            season: Optional[int] = None

        try:
            wp.home_win_probability = newer
            self.assertEqual(model_home_wp(_gs(GS3, play_class="try", overtime=True, season=2026)), 0.5)
            self.assertEqual(seen, {"play_class": "try", "overtime": True, "season": 2026})
            seen.clear()
            wp.home_win_probability = older
            self.assertEqual(model_home_wp(_gs(GS3, play_class="try")), 0.5)
            self.assertEqual(seen.get("play_class"), "try")   # **kwargs accepts everything
        finally:
            wp.home_win_probability = orig
        # Today's signature: the call must not pass fields it does not know.
        self.assertIsNotNone(model_home_wp(_gs(GS3, play_class="try", season=2026)))


class LineEdgeTests(unittest.TestCase):
    def _line_me(self, fair_fav, ask_fav):
        key = "nfl:DEN|KC:2026-09-21:spread:KC-2.5"
        info = EventInfo(event_key=key, sport="nfl", market_type="spread", outcomes=["KC-2.5", "DEN+2.5"], labels={"KC-2.5": "KC -2.5", "DEN+2.5": "DEN +2.5"}, line=2.5, in_play=False, venues={"_line_fair": {"KC-2.5": fair_fav, "DEN+2.5": 1 - fair_fav}})
        q = lambda v, o, bid, ask, **kw: OutcomeQuote(v, f"{v}-{o}", key, o, ask=ask, bid=bid, **kw)  # noqa: E731
        return MergedEvent(key, info, {"kalshi": [q("kalshi", "KC-2.5", ask_fav - 0.02, ask_fav, fee_params=KFEE), q("kalshi", "DEN+2.5", 0.48, 0.50, fee_params=KFEE)]})

    def test_line_fair_needs_twice_the_edge(self):
        # fair 0.56 vs all-in ~0.518 (0.50 + fee): +4.2% clears 3% but not the 6% line rule.
        v = evaluate_inplay(self._line_me(0.56, 0.50), [], settings={"line_fair": True}, steal_edge=0.03)
        fav = _side(v, "KC-2.5")
        self.assertAlmostEqual(fav.fair, 0.56)
        self.assertAlmostEqual(fav.steal_threshold, 0.06)
        self.assertGreater(fav.steal_edge, 0.03)
        self.assertFalse(fav.steal)
        self.assertEqual(v.blend["weights"], {"line": 1.0})
        v2 = evaluate_inplay(self._line_me(0.60, 0.50), [], settings={"line_fair": True}, steal_edge=0.03)
        self.assertTrue(_side(v2, "KC-2.5").steal)
        self.assertTrue(any("[line 2x edge]" in a for a in v2.actions))
        # The knob is off by default (shared with scanner.py's line_fair): the attached fair is ignored.
        v3 = evaluate_inplay(self._line_me(0.56, 0.50), [], steal_edge=0.03)
        self.assertNotEqual(v3.blend["weights"], {"line": 1.0})

    def test_moneyline_ignores_line_fair(self):
        me = _me(r_kc=(0.28, 0.30))
        me.info.venues["_line_fair"] = {"DEN": 0.9, "KC": 0.1}
        v = evaluate_inplay(me, [], steal_edge=0.03)
        self.assertEqual(_side(v, "KC").steal_threshold, 0.03)
        self.assertTrue(_side(v, "KC").steal)


def _me3(p_den=(0.59, 0.61), p_kc=(0.39, 0.41), **kw):
    """Kalshi + Robinhood (``_me``) plus a Polymarket leg: the signal-only venue by default."""
    me = _me(**kw)
    q = lambda o, bid, ask: OutcomeQuote("polymarket", f"pm-{o}", KEY, o, ask=ask, bid=bid, ask_size=500, meta={"tie_payout": 0.5})  # noqa: E731
    me.quotes_by_venue["polymarket"] = [q("DEN", *p_den), q("KC", *p_kc)]
    return me


class ExecutableVenueTests(unittest.TestCase):
    """Tonight's bug: 'STEAL: UCLA all-in 0.770 on polymarket ... buy 80 contracts' for an
    account whose compliance table says Polymarket is signal-only."""

    def test_default_table_resolves_from_settings(self):
        v = evaluate_inplay(_me3(), [], {})
        self.assertEqual(v.executable_venues, ["kalshi", "robinhood"])
        self.assertIsNone(evaluate_inplay(_me3(), [], {"executable_venues": "all"}).executable_venues)
        self.assertEqual(evaluate_inplay(_me3(), [], {}, executable_venues=["Kalshi"]).executable_venues, ["kalshi"])
        # load_settings() carries executable_venues=None (unset): the table applies, not "unrestricted".
        self.assertEqual(evaluate_inplay(_me3(), [], {"executable_venues": None}).executable_venues, ["kalshi", "robinhood"])

    def test_cheap_polymarket_ask_is_signal_only_never_a_steal(self):
        # Polymarket KC 0.30 while Kalshi / Robinhood ask ~0.41: the market's consensus says
        # ~0.38, the model 0.45 -> a STEAL on Polymarket, which this account cannot take.
        me = _me3(p_kc=(0.28, 0.30))
        with _stub_model(0.45):
            v = evaluate_inplay(me, [], {}, steal_edge=0.03, game_state=_gs(), bankroll=1000.0)
        kc = _side(v, "KC")
        self.assertFalse(kc.steal)
        self.assertFalse(kc.steal_gated)
        self.assertIsNone(kc.suggested_contracts)
        self.assertIsNone(kc.kelly_stake)
        self.assertEqual((kc.best_venue, kc.best_ask, kc.best_ineligible), ("polymarket", 0.30, "not executable"))
        self.assertGreater(kc.steal_edge, 0.03)                       # the signal's edge, for the display
        self.assertEqual((kc.exec_venue, kc.exec_ask), ("robinhood", 0.40))   # the executable best beside it (0.40 + $0.01 < Kalshi 0.41 + quadratic fee)
        self.assertEqual((kc.signal_venue, kc.signal_ask), ("polymarket", 0.30))
        self.assertAlmostEqual(kc.signal_all_in, kc.best_all_in)
        self.assertFalse(any(a.startswith("STEAL") or a.startswith("GATED STEAL") for a in v.actions), v.actions)
        sig = [a for a in v.actions if a.startswith("signal only:")]
        self.assertEqual(len(sig), 1, v.actions)
        self.assertIn("on polymarket", sig[0])
        self.assertIn("not executable", sig[0])
        self.assertIn("best executable robinhood", sig[0])
        # Every venue's mid still prices the fair: dropping Polymarket changes the market consensus.
        with _stub_model(0.45):
            without = evaluate_inplay(_me(), [], {}, steal_edge=0.03, game_state=_gs())
        self.assertNotAlmostEqual(kc.market_p, _side(without, "KC").market_p, places=3)
        # Opt in (EXECUTABLE_VENUES=all, or the explicit kwarg) and the same quote is a STEAL with a size.
        with _stub_model(0.45):
            opted = evaluate_inplay(me, [], {"executable_venues": "kalshi,robinhood,polymarket"}, steal_edge=0.03, game_state=_gs(), bankroll=1000.0)
            explicit = evaluate_inplay(me, [], {}, steal_edge=0.03, game_state=_gs(), bankroll=1000.0, executable_venues={"polymarket"})
        for view in (opted, explicit):
            side = _side(view, "KC")
            self.assertTrue(side.steal)
            self.assertEqual((side.best_venue, side.best_ineligible, side.signal_venue), ("polymarket", None, None))
            self.assertTrue(side.suggested_contracts)
            self.assertTrue(any(a.startswith("STEAL: Kansas City all-in") and "on polymarket" in a for a in view.actions), view.actions)

    def test_executable_steal_wins_the_headline_over_a_cheaper_signal(self):
        # Robinhood KC 0.30 (executable STEAL) and Polymarket KC 0.27 (cheaper, not executable).
        me = _me3(r_kc=(0.28, 0.30), p_kc=(0.25, 0.27))
        for q in me.quotes_by_venue["robinhood"]:
            q.ask_size = 150
        with _stub_model(0.45):
            v = evaluate_inplay(me, [], {}, steal_edge=0.03, game_state=_gs(), bankroll=1000.0)
        kc = _side(v, "KC")
        self.assertTrue(kc.steal)
        self.assertEqual((kc.best_venue, kc.best_ask, kc.best_ineligible), ("robinhood", 0.30, None))
        self.assertEqual((kc.exec_venue, kc.signal_venue, kc.signal_ask), ("robinhood", "polymarket", 0.27))
        self.assertEqual(kc.depth_contracts, 150)                     # sized on the executable book, not Polymarket's 500
        self.assertLessEqual(kc.suggested_contracts, 150)
        steal = next(a for a in v.actions if a.startswith("STEAL"))
        self.assertIn("on robinhood", steal)
        self.assertIn("[polymarket all-in 0.280 signal only]", steal)     # the signal's all-in (0.27 + Polymarket fee), like every other price in the line
        self.assertNotIn("on polymarket", steal.split("[")[0])
        # The bridge recomputes best_ineligible from best_venue and lands on the same answer.
        from dataclasses import asdict

        from arb_engine.bridge import with_gate_fields
        out = with_gate_fields(asdict(v), {"kalshi", "robinhood"})
        self.assertEqual([sv["best_ineligible"] for sv in out["sides"]], [sv.best_ineligible for sv in v.sides])
        self.assertEqual(out["sides"][1]["exec_venue"], "robinhood")

    def test_gated_executable_steal_keeps_the_executable_headline(self):
        me = _me3(r_kc=(0.28, 0.30), p_kc=(0.25, 0.27))
        with _stub_model(0.45):
            v = evaluate_inplay(me, [], {}, steal_edge=0.03, game_state=_gs(GS2, suspect=True), freshness=FeedFreshness(), now=1000.0)
        kc = _side(v, "KC")
        self.assertTrue(kc.steal_gated)
        self.assertEqual((kc.best_venue, kc.best_ineligible), ("robinhood", None))
        self.assertTrue(any(a.startswith("GATED STEAL: wait: suspect") and "on robinhood" in a for a in v.actions), v.actions)

    def test_lock_never_lands_on_a_non_executable_venue(self):
        # Holding 100 DEN @ 0.50; KC at 0.40 only on Polymarket, Kalshi / Robinhood ask 0.48.
        me = _me3(k_kc=(0.46, 0.48), r_kc=(0.46, 0.48), p_kc=(0.38, 0.40))
        v = evaluate_inplay(me, [Lot("robinhood", "DEN", 0.50, 100)], {})
        kc = _side(v, "KC")
        self.assertFalse(kc.lock_available)
        self.assertEqual(kc.lock_price, 0.46)
        self.assertFalse(any(a.startswith("LOCK NOW") for a in v.actions), v.actions)
        # With no executable KC ask at all the watcher says why it cannot lock.
        me2 = _me3(p_kc=(0.38, 0.40))
        me2.quotes_by_venue["kalshi"] = [q for q in me2.quotes_by_venue["kalshi"] if q.outcome != "KC"]
        me2.quotes_by_venue["robinhood"] = [q for q in me2.quotes_by_venue["robinhood"] if q.outcome != "KC"]
        v2 = evaluate_inplay(me2, [Lot("robinhood", "DEN", 0.50, 100)], {})
        kc2 = _side(v2, "KC")
        self.assertEqual((kc2.best_venue, kc2.best_ineligible, kc2.exec_venue, kc2.need), ("polymarket", "not executable", None, 100.0))
        self.assertTrue(any(a.startswith("wait: Kansas City only offered on polymarket") and "not executable" in a for a in v2.actions), v2.actions)
        # Opted in, the same Polymarket ask locks.
        v3 = evaluate_inplay(me, [Lot("robinhood", "DEN", 0.50, 100)], {"executable_venues": "all"})
        self.assertTrue(any(a.startswith("LOCK NOW") and "on polymarket" in a for a in v3.actions), v3.actions)

    def test_watcher_and_slate_resolve_the_set_once(self):
        me = _me3(p_kc=(0.28, 0.30))
        w = InplayWatcher(lambda: me, [], Alerter(journal_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), "inplay_exec_test.jsonl"), quiet=True, desktop=False, webhook=""), fetch_state=lambda: _gs(), steal_edge=0.03)
        self.assertEqual(w.executable_venues, {"kalshi", "robinhood"})
        with _stub_model(0.45):
            view = w.step(now=1000.0)
        self.assertFalse(_side(view, "KC").steal)
        self.assertEqual([e for e in w.alerts.events if e["kind"] == "alert"], [])
        self.assertTrue(any(e["kind"] == "info" and e["msg"].startswith("signal only:") for e in w.alerts.events))


class AgreementGateTests(unittest.TestCase):
    """PUR 28-31 UCLA, Q3 10:23, spread -13.5: model 0.93 vs Kalshi / Polymarket 0.77 and ESPN
    0.77 - the blend still said STEAL +11 %. Two sources against the model is not an edge."""

    def _ucla(self, **kw):
        # KC plays UCLA here: home ask 0.78 on both venues (all-in ~0.79); model 0.93 -> blend ~0.85.
        return _me(k_den=(0.21, 0.23), k_kc=(0.77, 0.78), r_den=(0.21, 0.23), r_kc=(0.77, 0.78), **kw)

    def test_gate_rule(self):
        from arb_engine.strategy.inplay import agreement_gate
        self.assertTrue(agreement_gate(0.93, 0.77, 0.77))
        self.assertTrue(agreement_gate(0.93, 0.77, 0.80))            # ESPN nearer the market
        self.assertFalse(agreement_gate(0.93, 0.77, 0.90))           # ESPN sides with the model
        self.assertFalse(agreement_gate(0.88, 0.77, 0.77))           # gap 0.11 <= 0.12
        self.assertTrue(agreement_gate(0.88, 0.77, 0.77, gap=0.10))
        self.assertFalse(agreement_gate(None, 0.77, 0.77))
        self.assertFalse(agreement_gate(0.93, 0.77, None))
        self.assertTrue(agreement_gate(0.07, 0.23, 0.23))            # symmetric in the side

    def test_steal_gated_when_espn_sides_with_the_market(self):
        with _stub_model(0.93):
            v = evaluate_inplay(self._ucla(), [], {}, steal_edge=0.03, game_state=_gs(espn_home_wp=0.77), freshness=FeedFreshness(), now=1000.0, bankroll=500.0)
        kc = _side(v, "KC")
        self.assertGreater(kc.steal_edge, 0.03)
        self.assertGreater(v.disagreement, 0.12)
        self.assertTrue(v.disagreement_gated)
        self.assertFalse(kc.steal)
        self.assertTrue(kc.steal_gated)
        self.assertEqual(kc.gated_reasons, ["disagreement"])
        self.assertIsNone(kc.suggested_contracts)
        self.assertEqual(v.gated_reasons, [])                          # a STEAL gate, not an event / LOCK gate
        self.assertTrue(any(a.startswith("GATED STEAL: wait: disagreement") for a in v.actions), v.actions)
        self.assertTrue(any(a.startswith("sources disagree") and "STEAL gated" in a for a in v.actions), v.actions)
        # No freshness object needed: the gate is a probability rule, not poll memory.
        with _stub_model(0.93):
            plain = evaluate_inplay(self._ucla(), [], {}, steal_edge=0.03, game_state=_gs(espn_home_wp=0.77))
        self.assertEqual(_side(plain, "KC").gated_reasons, ["disagreement"])

    def test_not_gated_when_espn_sides_with_the_model_or_gap_is_small(self):
        with _stub_model(0.93):
            with_model = evaluate_inplay(self._ucla(), [], {}, steal_edge=0.03, game_state=_gs(espn_home_wp=0.91))
            wide_gap = evaluate_inplay(self._ucla(), [], {"inplay_agreement_gap": 0.30}, steal_edge=0.03, game_state=_gs(espn_home_wp=0.77))
            no_espn = evaluate_inplay(self._ucla(), [], {}, steal_edge=0.03, game_state=_gs(espn_home_wp=None))
        for view in (with_model, wide_gap, no_espn):
            self.assertTrue(_side(view, "KC").steal, view.actions)
            self.assertFalse(view.disagreement_gated)
        # Pre-game the blend is market-only and the gate never applies.
        with _stub_model(0.93):
            pre = evaluate_inplay(self._ucla(), [], {}, steal_edge=0.03, game_state=_gs(status="pre", possession=None, home_score=0, away_score=0, game_seconds_remaining=3600, espn_home_wp=0.77))
        self.assertFalse(pre.disagreement_gated)

    def test_gate_stacks_with_the_feed_gates_and_the_lock_ignores_it(self):
        with _stub_model(0.93):
            v = evaluate_inplay(self._ucla(), [], {}, steal_edge=0.03, game_state=_gs(GS2, espn_home_wp=0.77, suspect=True), freshness=FeedFreshness(), now=1000.0)
        self.assertEqual(_side(v, "KC").gated_reasons, ["suspect", "disagreement"])
        # Holding 100 DEN @ 0.18: KC at 0.78 locks (0.19 + 0.792 all-in < 1) regardless of the model gap.
        with _stub_model(0.93):
            lock = evaluate_inplay(self._ucla(), [Lot("robinhood", "DEN", 0.18, 100)], {}, game_state=_gs(espn_home_wp=0.77))
        self.assertTrue(any(a.startswith("LOCK NOW") for a in lock.actions), lock.actions)


class FeeMultiplierTests(unittest.TestCase):
    def test_lot_fee_uses_the_series_multiplier(self):
        full = Lot("kalshi", "DEN", 0.50, 100)
        half = Lot("kalshi", "DEN", 0.50, 100, fee_params={"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 0.5})
        self.assertAlmostEqual(full.fee, 1.75)
        self.assertAlmostEqual(half.fee, 0.88)            # ceil(0.875) at the cent
        self.assertAlmostEqual(Lot.parse("kalshi:DEN:0.50:100:0.5").fee, 0.88)
        self.assertEqual(Lot.parse("robinhood:DEN:0.50:100:kalshi").exchange, "kalshi")
        self.assertAlmostEqual(Lot("kalshi", "DEN", 0.50, 100, fee_params={"fee_type": "quadratic", "fee_multiplier": 0}).fee, 0.0)

    def test_evaluate_prices_lots_from_the_quotes_fee_params(self):
        me = _me()
        for q in me.quotes_by_venue["kalshi"]:
            q.fee_params = {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 0.5}
        v = evaluate_inplay(me, [Lot("kalshi", "DEN", 0.50, 100)])
        self.assertAlmostEqual(v.total_cost, 50.88)
        # An explicit lot fee schedule wins over the quote's.
        v2 = evaluate_inplay(me, [Lot("kalshi", "DEN", 0.50, 100, fee_params={"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1})])
        self.assertAlmostEqual(v2.total_cost, 51.75)
        # Robinhood lots keep the user's exchange (the $52.00 constant elsewhere in this file).
        self.assertAlmostEqual(evaluate_inplay(me, [Lot("robinhood", "DEN", 0.50, 100)]).total_cost, 52.0)


class HedgeKellyInLockTests(unittest.TestCase):
    def test_lock_alert_shows_the_kelly_hedge(self):
        v = evaluate_inplay(_me(), [Lot("robinhood", "DEN", 0.50, 100)], bankroll=1000.0)
        kc = _side(v, "KC")
        self.assertTrue(kc.lock_available)
        self.assertIsNotNone(kc.kelly_hedge)
        self.assertTrue(0 <= kc.kelly_hedge <= 100)
        lock = next(a for a in v.actions if a.startswith("LOCK NOW"))
        self.assertIn(f"buy 100 (kelly hedge {kc.kelly_hedge}) x Kansas City", lock)
        # No bankroll: no hedge sizing, same lock.
        v0 = evaluate_inplay(_me(), [Lot("robinhood", "DEN", 0.50, 100)])
        self.assertIsNone(_side(v0, "KC").kelly_hedge)
        self.assertTrue(any(a.startswith("LOCK NOW: buy 100 x Kansas City") for a in v0.actions))


if __name__ == "__main__":
    unittest.main()

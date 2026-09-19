"""Multi-game replay helpers, strata, intervals, simulations, pooling and the CLI plugin — offline."""

import argparse
import json
import os
import tempfile
import unittest
from dataclasses import asdict

from arb_engine.backtest import (
    HEADLINE_CLASSES, GameReplayer, ReplayResult, ReplayRow, class_gaps, dump_results_json, fit_blend_weights, in_play_gate, interval_report, lead_bucket, load_results,
    pool_weeks, pooled_metrics, resolve_polymarket_tokens, resolve_spread, season_of, secs_bucket, shuffled_labels, simulate_pairings, simulate_steal, slice_label, strata_tables,
    summarize_many, summarize_sim, walk_forward, week_games, week_report,
)
from arb_engine.cli_plugins.backtest_flags import build_parser, handle_backtest, register
from arb_engine.fees.kalshi import KalshiFees
from arb_engine.quant.inplay_fair import market_confidence_from_spread
from arb_engine.venues.espn import ESPNClient
from arb_engine.venues.history import HistoryClient

from .helpers import FIXTURES, FakeHttp, load

REPLAY_TRIM = str(FIXTURES / "history" / "replay_trim")


class ResolveTests(unittest.TestCase):
    def test_direct_slug(self):
        http = FakeHttp({"gamma-api.polymarket.com/events": load("history/gamma_event_det_buf.json")})
        toks = resolve_polymarket_tokens(http, "DET", "BUF", "2026-09-18T00:15:00Z")
        self.assertEqual(set(toks), {"home", "away"})
        self.assertNotEqual(toks["home"], toks["away"])
        # outcomes are ["Lions", "Bills"]: home (BUF) is index 1
        ev = load("history/gamma_event_det_buf.json")[0]
        ids = json.loads(ev["markets"][0]["clobTokenIds"])
        self.assertEqual(toks, {"home": ids[1], "away": ids[0]})

    def test_search_fallback_when_codes_differ(self):
        ev = load("history/gamma_event_det_buf.json")[0]
        calls = []

        class Http:
            def get(self, url, params=None, headers=None):
                calls.append((url, dict(params or {})))
                if url.endswith("/events"):
                    return [ev] if params.get("slug") == "nfl-sf-la-2026-09-11" else []
                if url.endswith("/public-search"):
                    return {"events": [{"slug": "nfl-49ers-vs-rams"}, {"slug": "nfl-sf-la-2025-10-02"}, {"slug": "nfl-sf-la-2026-09-11", "markets": []}]}
                raise AssertionError(url)

        toks = resolve_polymarket_tokens(Http(), "SF", "LAR", "2026-09-11T00:35:00Z", away_name="San Francisco 49ers", home_name="Los Angeles Rams")
        self.assertEqual([c[1].get("slug") for c in calls if c[0].endswith("/events")], ["nfl-sf-la-2026-09-11"])  # LAR -> la directly
        self.assertIsNone(toks)  # fixture event is DET/BUF, so the codes do not match SF/LAR -> None, not a wrong pair

    def test_kalshi_ticker_codes(self):
        from datetime import datetime, timezone

        home, away = GameReplayer.kalshi_tickers(None, "JAX", "CLE", datetime(2026, 9, 13, 17, 0, tzinfo=timezone.utc))
        self.assertEqual((home, away), ("KXNFLGAME-26SEP13CLEJAC-JAC", "KXNFLGAME-26SEP13CLEJAC-CLE"))


class WeekTests(unittest.TestCase):
    def test_week_games_filters_finals(self):
        espn = ESPNClient(http=FakeHttp({"/scoreboard": load("history/espn_week1_scoreboard.json")}))
        games = week_games(espn, 2026, 1)
        self.assertEqual([g["id"] for g in games], ["401872656", "401872657"])
        self.assertEqual(games[0]["name"], "New England Patriots at Seattle Seahawks")


def _row(**kw):
    base = dict(ts=0.0, period=1, clock=600, home_score=0, away_score=0, possession="home", model_p=None, espn_p=None, kalshi_p=None, robinhood_p=None, polymarket_p=None, market_p=None, blend_p=None, disagreement=None, kalshi_arb_margin=None, cross_arb_margin=None, play_class="scrimmage")
    base.update(kw)
    if "in_play" not in kw:
        base["in_play"] = bool(base["period"] and (base["period"] < 4 or (base["clock"] or 0) > 0))
    if "kalshi_before_p" not in kw and base.get("kalshi_p") is not None:
        base["kalshi_before_p"] = base["kalshi_p"]
    return ReplayRow(**base)


def _result(home_won, rows, final="", **kw):
    return ReplayResult(espn_event_id="x", home="H", away="A", home_won=home_won, final=final, kickoff=None, n_plays=len(rows), n_inplay=len(rows), metrics={}, arb_minutes={}, rows=rows, **kw)


class LabelTests(unittest.TestCase):
    def test_slice_lead_secs_labels(self):
        self.assertEqual(slice_label(1, 3500), "q1")
        self.assertEqual(slice_label(3, 1000), "q3")
        self.assertEqual(slice_label(4, 600), "q4_early")
        self.assertEqual(slice_label(4, 299), "q4_late")
        self.assertEqual(slice_label(5, 400), "ot")
        self.assertEqual(slice_label(5, None, overtime=True), "ot")
        self.assertIsNone(slice_label(0, None))
        self.assertEqual([lead_bucket(h, 0) for h in (0, 3, 4, 8, 9, 16, 17, 40)], ["tie", "1-3", "4-8", "4-8", "9-16", "9-16", "17+", "17+"])
        self.assertEqual(lead_bucket(0, 21), "17+")
        self.assertEqual([secs_bucket(g) for g in (0, 299, 300, 899, 900, 1799, 1800, 2699, 2700, 3600, None)], ["0-5m", "0-5m", "5-15m", "5-15m", "15-30m", "15-30m", "30-45m", "30-45m", "45-60m", "45-60m", "ot"])

    def test_in_play_gate_is_sport_aware(self):
        self.assertTrue(in_play_gate(1, 3500, 0, 0))
        self.assertFalse(in_play_gate(0, None, 0, 0))                 # no period
        self.assertFalse(in_play_gate(4, 0, 21, 17, "end_period"))   # 0:00 with a margin: decided
        self.assertTrue(in_play_gate(4, 0, 21, 21))                   # 0:00 tied: overtime coming
        self.assertTrue(in_play_gate(5, None, 21, 21, "ot"))          # college OT: no clock, still in play
        self.assertFalse(in_play_gate(5, None, 28, 21, "end_period", last=True))  # final row of a college OT game


class PooledTests(unittest.TestCase):
    def test_pooled_and_fit(self):
        # Game 1: home wins; model confident, market lukewarm, espn wrong-ish.
        g1 = _result(True, [_row(model_p=0.8, market_p=0.6, espn_p=0.4, kalshi_p=0.6, kalshi_spread=0.02), _row(model_p=0.9, market_p=0.7, espn_p=0.5, kalshi_p=0.7, kalshi_spread=0.10)])
        # Game 2: away wins; everyone leans home but the model least.
        g2 = _result(False, [_row(model_p=0.4, market_p=0.6, espn_p=0.7, kalshi_p=0.6, kalshi_spread=0.02), _row(model_p=0.3, market_p=0.5, espn_p=0.6, kalshi_p=0.5, kalshi_spread=0.03)])
        pm = pooled_metrics([g1, g2])
        self.assertEqual(pm["model"]["n"], 4)
        self.assertLess(pm["model"]["log_loss"], pm["market"]["log_loss"])
        self.assertLess(pm["market"]["log_loss"], pm["espn"]["log_loss"])
        self.assertEqual(pm["robinhood"]["n"], 0)
        self.assertEqual((pm["kalshi_tight"]["n"], pm["kalshi_wide"]["n"], pm["model_when_tight"]["n"]), (3, 1, 3))
        fit = fit_blend_weights([g1, g2], step=0.25)
        self.assertEqual(fit["n"], 4)
        self.assertEqual(fit["best"]["model"], 1.0)
        self.assertLessEqual(fit["best"]["log_loss"], fit["current"]["log_loss"])
        self.assertEqual(set(fit["corners"]), {"market", "model", "espn"})
        self.assertIn("logit_pool_available_live", fit)
        logit = fit_blend_weights([g1, g2], step=0.25, pool="logit")
        self.assertEqual(logit["pool"], "logit")
        self.assertEqual(logit["corners"], fit["corners"])  # a single source pools to itself either way

    def test_synthetic_rows_and_classes_are_filtered(self):
        g = _result(True, [_row(model_p=0.8), _row(model_p=0.2, synthetic=True, play_class="try_synth"), _row(model_p=0.6, play_class="timeout"), _row(model_p=0.5, in_play=False)])
        self.assertEqual(pooled_metrics([g])["model"]["n"], 3)                                     # synthetic out
        self.assertEqual(pooled_metrics([g], inplay_only=True)["model"]["n"], 2)                   # + decided row out
        self.assertEqual(pooled_metrics([g], inplay_only=True, play_classes=HEADLINE_CLASSES)["model"]["n"], 1)
        self.assertEqual(pooled_metrics([g], synthetic=True)["model"]["n"], 4)

    def test_market_confidence_from_spread(self):
        self.assertEqual(market_confidence_from_spread(None), 1.0)
        self.assertEqual(market_confidence_from_spread(0.02), 1.0)
        self.assertEqual(market_confidence_from_spread(0.04), 1.0)
        self.assertAlmostEqual(market_confidence_from_spread(0.08), 0.65)
        self.assertEqual(market_confidence_from_spread(0.30), 0.3)

    def test_extra_models_scored_on_the_same_rows(self):
        g = _result(True, [_row(model_p=0.8, extra_model_p={"alt": 0.6}), _row(model_p=0.7, extra_model_p={"alt": None})])
        pm = pooled_metrics([g])
        self.assertEqual((pm["model"]["n"], pm["model:alt"]["n"]), (2, 1))


class StrataTests(unittest.TestCase):
    def _games(self, n_rows):
        rows = [_row(ts=i, slice="q1", model_p=0.7, espn_p=0.6, kalshi_before_p=0.65, kalshi_after_p=0.66, blend_p=0.68, model_after_p=0.71, blend_after_p=0.69, kalshi_before_home_ask=0.55, kalshi_before_away_ask=0.47, lead_bucket="tie") for i in range(n_rows)]
        return [_result(True, rows)]

    def test_cells_under_min_n_show_n_only(self):
        st = strata_tables(self._games(49))
        self.assertEqual(st["by_slice"]["q1"]["n"], 49)
        self.assertIsNone(st["by_slice"]["q1"]["model"]["log_loss"])
        self.assertEqual(st["by_slice"]["q1"]["model"]["n"], 49)
        st = strata_tables(self._games(50))
        self.assertAlmostEqual(st["by_slice"]["q1"]["model"]["log_loss"], 0.3567, places=4)
        self.assertEqual(st["by_slice"]["q2"]["n"], 0)
        self.assertEqual(st["by_play_class"]["scrimmage"]["n"], 50)
        self.assertEqual(st["by_lead"]["tie"]["n"], 50)
        st10 = strata_tables(self._games(12), min_n=10)
        self.assertIsNotNone(st10["by_slice"]["q1"]["kalshi_before"]["log_loss"])

    def test_class_gaps_count_steal_qualifiers(self):
        rows = [
            _row(play_class="kneel", model_p=0.99, blend_p=0.98, kalshi_before_p=0.80, kalshi_before_home_ask=0.82, kalshi_before_away_ask=0.20),   # model 0.99 vs all-in ~0.83: qualifies at every edge
            _row(play_class="scrimmage", model_p=0.60, blend_p=0.58, kalshi_before_p=0.59, kalshi_before_home_ask=0.60, kalshi_before_away_ask=0.42),  # no edge
            _row(play_class="scrimmage", model_p=0.70, blend_p=0.55, kalshi_before_p=0.59, kalshi_before_home_ask=0.60, kalshi_before_away_ask=0.42),  # model says yes, blend says no: agreement rule blocks it
        ]
        cg = class_gaps([_result(True, rows)])
        self.assertEqual(cg["kneel"]["steal"], {"0.03": 1, "0.05": 1, "0.08": 1})
        self.assertAlmostEqual(cg["kneel"]["mean_abs_gap"], 0.19)
        self.assertEqual(cg["scrimmage"]["steal"], {"0.03": 0, "0.05": 0, "0.08": 0})
        self.assertEqual(cg["scrimmage"]["n"], 2)


class IntervalTests(unittest.TestCase):
    def test_bootstrap_intervals_and_games_needed(self):
        games = []
        for i in range(8):
            won = i % 2 == 0
            rows = [_row(model_p=0.8 if won else 0.3, kalshi_before_p=0.7 if won else 0.4, kalshi_after_p=0.75 if won else 0.35, blend_p=0.75 if won else 0.35, espn_p=0.6 if won else 0.45, model_after_p=0.8 if won else 0.3, market_p=0.7 if won else 0.4) for _ in range(5)]
            games.append(_result(won, rows))
        iv = interval_report(games, B=200, seed=1)
        d = iv["model_minus_kalshi_before"]
        self.assertEqual(d["n_games"], 8)
        self.assertLess(d["mean"], 0)                     # the model is better on these rows
        self.assertLessEqual(d["lo90"], d["mean"])
        self.assertLessEqual(d["mean"], d["hi90"])
        self.assertIsInstance(d["games_needed_at_observed"], int)
        self.assertIn("blend_minus_model", iv)
        fit = fit_blend_weights(games, step=0.5)
        iv2 = interval_report(games, B=100, seed=1, fit=fit)
        self.assertIn("best_grid_minus_current", iv2)


class SimTests(unittest.TestCase):
    def test_steal_then_lock_and_hold(self):
        # Game A (home wins): fair 0.70 vs home ask 0.55 -> steal home; later away ask 0.30 locks the pair.
        a = _result(True, [
            _row(blend_p=0.70, kalshi_home_ask=0.55, kalshi_away_ask=0.48),
            _row(blend_p=0.85, kalshi_home_ask=0.84, kalshi_away_ask=0.30),
            _row(period=4, clock=0, blend_p=0.99, kalshi_home_ask=0.99, kalshi_away_ask=0.02),  # decided: ignored
        ])
        # Game B (away wins): steal home at 0.60 on fair 0.75, never a lock -> loses the stake.
        b = _result(False, [_row(blend_p=0.75, kalshi_home_ask=0.60, kalshi_away_ask=0.42), _row(blend_p=0.50, kalshi_home_ask=0.50, kalshi_away_ask=0.52)])
        sim = simulate_steal([a, b], edges=(0.05, 0.30), contracts=10, lock=True)
        e = sim["by_edge"]["0.05"]
        self.assertEqual((e["games_traded"], e["steals"], e["locks"]), (2, 2, 1))
        kinds = [(t["game"], t["side"], t["kind"]) for t in e["trades"]]
        self.assertEqual(kinds[0][1:], ("home", "steal"))
        self.assertIn(("", "away", "lock"), kinds)
        ga = next(g for g in e["games"] if g["locked"])
        self.assertGreater(ga["pnl"], 0)          # 0.55 + 0.30 + fees < 1.00
        gb = next(g for g in e["games"] if not g["locked"])
        self.assertLess(gb["pnl"], 0)
        self.assertAlmostEqual(gb["pnl"], -gb["cost"], places=6)
        self.assertEqual(sim["by_edge"]["0.3"]["games_traded"], 0)  # edge too big: nothing trades
        text = summarize_sim(sim)
        self.assertIn("STEAL/LOCK", text)
        self.assertIn("0.05", text)

    def test_no_lock_holds_to_settlement(self):
        a = _result(True, [_row(blend_p=0.70, kalshi_home_ask=0.55, kalshi_away_ask=0.48), _row(blend_p=0.85, kalshi_home_ask=0.84, kalshi_away_ask=0.30)])
        sim = simulate_steal([a], edges=(0.05,), contracts=10, lock=False)
        e = sim["by_edge"]["0.05"]
        self.assertEqual((e["steals"], e["locks"]), (1, 0))
        self.assertAlmostEqual(e["games"][0]["payout"], 10.0)
        self.assertGreater(e["pnl"], 4.0)  # bought at ~0.55 + fee, paid 1.00

    def test_pairings_use_their_own_columns(self):
        # Pre-play fair says steal home at the before ask; the after bar has already repriced (no edge); post-play fair is neutral.
        r = _row(blend_p=0.70, model_p=0.70, kalshi_before_home_ask=0.55, kalshi_before_away_ask=0.47, kalshi_after_home_ask=0.72, kalshi_after_away_ask=0.30, blend_after_p=0.71, model_after_p=0.71, kalshi_shift_home_ask=0.74, kalshi_shift_away_ask=0.28)
        res = [_result(True, [r])]
        self.assertEqual(simulate_steal(res, edges=(0.05,), pairing="pre_before")["by_edge"]["0.05"]["steals"], 1)
        self.assertEqual(simulate_steal(res, edges=(0.05,), pairing="post_after")["by_edge"]["0.05"]["steals"], 0)
        self.assertEqual(simulate_steal(res, edges=(0.05,), pairing="pre_after")["by_edge"]["0.05"]["steals"], 0)
        self.assertEqual(simulate_steal(res, edges=(0.05,), pairing="shift")["by_edge"]["0.05"]["steals"], 0)
        self.assertEqual(simulate_steal(res, edges=(0.05,), pairing="primary")["by_edge"]["0.05"]["steals"], 0)  # the _row helper leaves kalshi_home_ask unset

    def test_simulate_pairings_reports_both_pairings_and_both_placebos(self):
        r = _row(blend_p=0.70, kalshi_before_home_ask=0.55, kalshi_before_away_ask=0.47, kalshi_after_home_ask=0.60, kalshi_after_away_ask=0.42, blend_after_p=0.71, kalshi_shift_home_ask=0.62, kalshi_shift_away_ask=0.40)
        sims = simulate_pairings([_result(True, [r]), _result(False, [r])], edges=(0.03,))
        tags = [(s["pairing"], s["placebo"]) for s in sims]
        self.assertEqual(tags, [("pre_before", None), ("post_after", None), ("pre_after", None), ("shift", None), ("post_after", "shuffle")])
        self.assertEqual(len(simulate_pairings([_result(True, [r])], placebos=False)), 2)
        text = summarize_many([_result(True, [r], final="A 1-2 H")], [], None, sims)
        self.assertIn("pairing=shift", text)
        self.assertIn("placebo=shuffle", text)

    def test_shuffle_placebo_permutes_outcomes_deterministically(self):
        games = [_result(i < 3, [_row(blend_p=0.9, kalshi_home_ask=0.5, kalshi_away_ask=0.5)], final=f"g{i}") for i in range(6)]
        sh = shuffled_labels(games, seed=3)
        self.assertEqual(sorted(g.home_won for g in sh), sorted(g.home_won for g in games))
        self.assertNotEqual([g.home_won for g in sh], [g.home_won for g in games])
        self.assertEqual([g.home_won for g in shuffled_labels(games, seed=3)], [g.home_won for g in sh])
        self.assertEqual([g.final for g in sh], [g.final for g in games])  # rows stay with their game; only labels move
        real = simulate_steal(games, edges=(0.05,), lock=False)["by_edge"]["0.05"]
        plac = simulate_steal(games, edges=(0.05,), lock=False, placebo="shuffle", seed=3)["by_edge"]["0.05"]
        self.assertEqual(real["wins"], 3)
        self.assertEqual(plac["wins"], 3)  # same label multiset -> same win count on identical rows, different games win
        self.assertNotEqual([g["pnl"] for g in real["games"]], [g["pnl"] for g in plac["games"]])

    def test_fee_multiplier_halves_the_kalshi_fee(self):
        r = _row(blend_p=0.70, kalshi_home_ask=0.55, kalshi_away_ask=0.48)
        full = simulate_steal([_result(True, [r])], edges=(0.05,), lock=False)["by_edge"]["0.05"]
        half = simulate_steal([_result(True, [r], kalshi_fee_multiplier=0.5)], edges=(0.05,), lock=False)["by_edge"]["0.05"]
        forced = simulate_steal([_result(True, [r])], edges=(0.05,), lock=False, fee_multiplier=0.5)["by_edge"]["0.05"]
        fee_full = float(KalshiFees.from_series({"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}).fee(0.55, 10, "taker"))
        fee_half = float(KalshiFees.from_series({"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 0.5}).fee(0.55, 10, "taker"))
        self.assertAlmostEqual(full["cost"], 5.5 + fee_full, places=2)
        self.assertAlmostEqual(half["cost"], 5.5 + fee_half, places=2)
        self.assertAlmostEqual(fee_half, fee_full / 2, places=6)
        self.assertEqual(half["cost"], forced["cost"])
        self.assertLess(half["cost"], full["cost"])

    def test_interval_on_pnl_per_game(self):
        r = _row(blend_p=0.70, kalshi_home_ask=0.55, kalshi_away_ask=0.48)
        e = simulate_steal([_result(True, [r]), _result(False, [r]), _result(True, [r])], edges=(0.05,), lock=False)["by_edge"]["0.05"]
        self.assertIsNotNone(e["pnl_per_game_90"])
        self.assertLessEqual(e["pnl_per_game_90"]["lo"], e["pnl_per_game_90"]["hi"])


class LockFractionTests(unittest.TestCase):
    def test_lock_fraction_skips_low_value_locks(self):
        # Steal home at 0.55 (fair 0.70). Later the away ask is 0.40: pair costs ~0.985 incl. fees -> tiny
        # guarantee, while fair for home is still 0.60 -> holding is worth far more.
        a = _result(True, [_row(blend_p=0.70, kalshi_home_ask=0.55, kalshi_away_ask=0.48), _row(blend_p=0.60, kalshi_home_ask=0.58, kalshi_away_ask=0.40)])
        greedy = simulate_steal([a], edges=(0.05,), contracts=10, lock=True, lock_fraction=0.0)
        patient = simulate_steal([a], edges=(0.05,), contracts=10, lock=True, lock_fraction=1.0)
        self.assertEqual(greedy["by_edge"]["0.05"]["locks"], 1)
        self.assertEqual(patient["by_edge"]["0.05"]["locks"], 0)
        self.assertGreater(patient["by_edge"]["0.05"]["pnl"], greedy["by_edge"]["0.05"]["pnl"])  # home won


class SpreadFallbackTests(unittest.TestCase):
    def test_chain_order(self):
        s = load("espn/summary_401872932.json")
        self.assertEqual(resolve_spread(s, "BUF", 0.5), (-5.5, "pickcenter"))
        ml_only = {"pickcenter": [{"moneyline": {"home": {"close": {"odds": "-245"}}, "away": {"close": {"odds": "+200"}}}, "provider": {"name": "x"}}]}
        sp, src = resolve_spread(ml_only, "BUF", None)
        self.assertEqual(src, "sportsbook_moneyline")
        self.assertLess(sp, -3.0)  # -245 / +200 de-vigs to ~0.70 for the home team: a clear favourite
        self.assertGreater(sp, -9.0)
        sp2, src2 = resolve_spread({}, "BUF", 0.695)
        self.assertEqual(src2, "pregame_kalshi")
        self.assertLess(sp2, 0)
        sp3, _ = resolve_spread({}, "BUF", 0.40)
        self.assertGreater(sp3, 0)  # home underdog -> positive home line
        with self.assertLogs("arb_engine.backtest", level="WARNING") as cm:
            self.assertEqual(resolve_spread({}, "BUF", None), (None, "none"))
        self.assertIn("no pre-game spread", cm.output[0])

    def test_lineless_report_compares_zero_spread_with_fallback(self):
        # The fallback spread only changes the pre-game-ish rows' model; the report must show both numbers.
        rows = [_row(slice="q1", model_p=0.75, model_p_zero_spread=0.5), _row(slice="q2", model_p=0.8, model_p_zero_spread=0.55), _row(slice="q4_late", model_p=0.9, model_p_zero_spread=0.6)]
        rep = week_report([_result(True, rows, spread_source="pregame_kalshi"), _result(True, [_row(slice="q1", model_p=0.6, model_p_zero_spread=0.6)], spread_source="pickcenter")], None, None, B=50)
        ll = rep["lineless_q1q2"]
        self.assertEqual((ll["games"], ll["n"]), (1, 2))
        self.assertLess(ll["log_loss_with_fallback"], ll["log_loss_spread_zero"])


class SpreadScaleTests(unittest.TestCase):
    def test_spread_scale_and_clamp(self):
        rep = GameReplayer(espn=ESPNClient(http=FakeHttp({})), history=HistoryClient(http=FakeHttp({})), spread_scale=0.5, spread_clamp=2.0)
        self.assertEqual(rep._spread_for_model(-6.0), -2.0)
        self.assertEqual(rep._spread_for_model(3.0), 1.5)
        self.assertEqual(rep._spread_for_model(None), 0.0)


class PostPlayStateTests(unittest.TestCase):
    def test_touchdown_row_after_state_is_scored_at_the_kickoff_pending_state(self):
        # Real ESPN TD end block (down -1, yardLine 0): the after candle has repriced on the score, so
        # model_after_p must be the kickoff-pending state (receiver at 35), not "scorer on the goal line".
        from datetime import datetime, timezone

        from arb_engine.models.wp import home_win_probability

        s = load("espn/summary_401872932.json")
        hist = HistoryClient(http=FakeHttp({"/candlesticks": {"candlesticks": []}, "/series/": {"series": {"fee_multiplier": 1}}}))
        rep = GameReplayer(espn=ESPNClient(http=FakeHttp({"/summary": s})), history=hist)
        res = rep.replay("401872932")
        td = next(r for r in res.rows if "TOUCHDOWN" in r.text and not r.synthetic)
        ko = next(r for r in res.rows if r.play_class == "kickoff_pending_synth")
        self.assertEqual(td.model_after_p, ko.model_p)
        gsr_after = ko.gsr  # the synthetic kickoff row sits at the TD's post-play clock (102 s)
        self.assertEqual(gsr_after, 102)
        # Scored with the model's kickoff class (possession = the receiver), the same hint the
        # synthetic kickoff_pending row gets, so the WP model's onside/receiver mixture applies.
        want = home_win_probability(home_score=41, away_score=31, game_seconds_remaining=gsr_after, possession="home", down=None, distance=None, yardline_100=35, home_timeouts=2, away_timeouts=3, vegas_spread_home=rep._spread_for_model(res.spread_home), receive_2h_ko_home=None, play_class="kickoff", season=2026)
        self.assertAlmostEqual(td.model_after_p, want, places=9)
        plain = home_win_probability(home_score=41, away_score=31, game_seconds_remaining=gsr_after, possession="home", down=None, distance=None, yardline_100=35, home_timeouts=2, away_timeouts=3, vegas_spread_home=rep._spread_for_model(res.spread_home), receive_2h_ko_home=None)
        self.assertGreater(plain, 0.9)  # and never "scorer on the goal line" (the old reading was 0.93 for DET)
        wrong = home_win_probability(home_score=41, away_score=31, game_seconds_remaining=gsr_after, possession="away", down=None, distance=10, yardline_100=0, home_timeouts=2, away_timeouts=3, vegas_spread_home=rep._spread_for_model(res.spread_home))
        self.assertNotAlmostEqual(td.model_after_p, wrong, places=3)  # the old "scorer on the goal line" reading
        self.assertGreater(td.model_after_p, wrong)  # BUF up 10 with the ball beats "DET at the goal line"

    def test_season_is_the_football_season_not_the_calendar_year(self):
        from datetime import datetime, timezone

        s = load("espn/summary_401872932.json")
        self.assertEqual(season_of(s, datetime(2027, 1, 4, tzinfo=timezone.utc)), 2026)  # ESPN's header (2026) wins over the calendar year
        self.assertEqual(season_of({}, datetime(2027, 1, 4, tzinfo=timezone.utc)), 2026)  # week 18 / playoffs
        self.assertEqual(season_of({}, datetime(2026, 9, 18, tzinfo=timezone.utc)), 2026)
        self.assertEqual(season_of({"header": {"season": {"year": "bad"}}}, datetime(2026, 12, 1, tzinfo=timezone.utc)), 2026)
        self.assertIsNone(season_of({}, None))


class CollegeOvertimeReplayTests(unittest.TestCase):
    def test_college_ot_rows_scored_for_espn_and_market_with_model_n0(self):
        s = json.loads(json.dumps(load("espn/summary_401872932.json")))
        comp = s["header"]["competitions"][0]
        plays = s["drives"]["previous"][-1]["plays"]
        base = plays[1]
        for i, (h, a) in enumerate(((41, 41), (41, 41), (47, 41))):
            ot = json.loads(json.dumps(base))
            ot["id"], ot["period"], ot["clock"], ot["wallclock"], ot["homeScore"], ot["awayScore"] = f"ot{i}", {"number": 5}, {"displayValue": "0:00"}, f"2026-09-18T03:4{i}:00Z", h, a
            plays.append(ot)
        plays[-1]["text"] = "END GAME"
        plays[-1]["type"] = {"id": "66", "text": "End of Game"}
        for c in comp["competitors"]:
            c["score"] = "47" if c["homeAway"] == "home" else "41"
        s["winprobability"] += [{"playId": "ot0", "homeWinPercentage": 0.55}, {"playId": "ot1", "homeWinPercentage": 0.58}, {"playId": "ot2", "homeWinPercentage": 1.0}]
        cands = {"candlesticks": [{"end_period_ts": 1789702800 + 60 * i, "yes_bid": {"close_dollars": "0.5500"}, "yes_ask": {"close_dollars": "0.5700"}, "price": {"close_dollars": "0.5600"}} for i in range(12)]}
        hist = HistoryClient(http=FakeHttp({"/candlesticks": cands, "/series/": {"series": {"fee_multiplier": 1}}}))
        rep = GameReplayer(espn=ESPNClient(http=FakeHttp({"/summary": s}), sport="ncaaf"), history=hist, sport="ncaaf")
        res = rep.replay("401872932")
        ot_rows = [r for r in res.rows if r.slice == "ot"]
        self.assertEqual(len(ot_rows), 3)
        self.assertTrue(all(r.model_p is None and r.gsr is None for r in ot_rows))
        self.assertEqual([r.play_class for r in ot_rows], ["ot", "ot", "end_period"])
        self.assertEqual([r.in_play for r in ot_rows], [True, True, False])  # the final row is decided
        self.assertTrue(all(r.espn_p is not None for r in ot_rows))
        self.assertTrue(all(r.kalshi_before_p is not None for r in ot_rows[1:]))
        st = strata_tables([res], min_n=1)
        self.assertEqual(st["by_slice"]["ot"]["n"], 2)
        self.assertEqual(st["by_slice"]["ot"]["model"]["n"], 0)
        self.assertEqual(st["by_slice"]["ot"]["espn"]["n"], 2)
        self.assertEqual(st["by_slice"]["ot"]["kalshi_before"]["n"], 2)  # candles end at 03:40:00 and 03:41:00: both in-play OT rows have a closed candle
        self.assertEqual(res.n_inplay, 19)  # 17 regulation rows + 2 OT rows


class PoolTests(unittest.TestCase):
    def _week_json(self, path, week, seed):
        import random

        rng = random.Random(seed)
        games = []
        for i in range(4):
            won = rng.random() < 0.5
            rows = [_row(ts=t, model_p=min(0.95, 0.6 + 0.1 * t) if won else 0.4 - 0.05 * t, kalshi_before_p=0.6 if won else 0.45, kalshi_after_p=0.62 if won else 0.43, blend_p=0.62 if won else 0.42, espn_p=0.58 if won else 0.44, model_after_p=0.65 if won else 0.4, market_p=0.6 if won else 0.45, slice="q2") for t in range(3)]
            games.append(_result(won, rows, final=f"w{week}g{i}"))
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"season": 2026, "week": week, "games": [asdict(g) for g in games]}, f)
        return games

    def test_pool_over_two_weeks_runs_strata_bootstrap_and_walk_forward(self):
        with tempfile.TemporaryDirectory() as d:
            p1, p2 = os.path.join(d, "w1.json"), os.path.join(d, "w2.json")
            g1 = self._week_json(p1, 1, 1)
            self._week_json(p2, 2, 2)
            loaded, hdr = load_results(p1)
            self.assertEqual(hdr["week"], 1)
            self.assertEqual([g.final for g in loaded], [g.final for g in g1])
            self.assertEqual(loaded[0].rows[0].model_p, g1[0].rows[0].model_p)
            rep = pool_weeks([p1, p2], B=100)
            self.assertEqual((rep["n_weeks"], rep["n_games"]), (2, 8))
            self.assertEqual(rep["pooled_inplay"]["model"]["n"], 24)
            self.assertIn("model_minus_kalshi_before", rep["intervals"])
            self.assertEqual(rep["intervals"]["model_minus_kalshi_before"]["n_games"], 8)
            self.assertIn("games_needed_at_observed", rep["intervals"]["model_minus_kalshi_before"])
            wf = rep["walk_forward"]
            self.assertEqual(len(wf["weeks"]), 1)
            self.assertEqual(wf["weeks"][0]["n_train"], 12)  # week 1 only (4 games x 3 rows)
            self.assertEqual(wf["weeks"][0]["n_test"], 12)
            # --pool through the plugin handler writes the results file.
            out = []
            rc = handle_backtest(build_parser().parse_args(["--pool", f"{d}/w*.json", "--results-json", os.path.join(d, "pool.json")]), out=out.append)
            self.assertEqual(rc, 0)
            with open(os.path.join(d, "pool.json")) as f:
                self.assertEqual(json.load(f)["n_weeks"], 2)

    def test_walk_forward_uses_only_prior_weeks(self):
        w1 = [_result(True, [_row(model_p=0.9, market_p=0.5, espn_p=0.5)] * 3)]      # week 1: model is right
        w2 = [_result(True, [_row(model_p=0.5, market_p=0.9, espn_p=0.5)] * 3)]      # week 2: market is right
        w3 = [_result(True, [_row(model_p=0.5, market_p=0.5, espn_p=0.9)] * 3)]      # week 3: espn is right
        wf = walk_forward([w1, w2, w3], step=0.5)
        self.assertEqual([w["week_index"] for w in wf["weeks"]], [1, 2])
        self.assertEqual(wf["weeks"][0]["weights"], {"market": 0.0, "model": 1.0, "espn": 0.0})  # fitted on week 1 alone
        self.assertEqual(wf["weeks"][0]["n_train"], 3)
        self.assertEqual(wf["weeks"][1]["n_train"], 6)                                       # weeks 1-2, never week 3
        self.assertNotEqual(wf["weeks"][1]["weights"]["espn"], 1.0)


class PluginTests(unittest.TestCase):
    def test_register_extends_an_existing_backtest_parser(self):
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="cmd")
        bt = sub.add_parser("backtest")
        bt.add_argument("--week", type=int)
        bt.add_argument("--json")
        handlers = register(sub, {"backtest": bt})
        self.assertEqual(handlers, {"backtest": handle_backtest})
        args = p.parse_args(["backtest", "--week", "1", "--bar-mode", "both", "--slices", "--placebo", "--offline", "--models", "a.json,b.json", "--pool", "x.json", "--spread-scale", "0.8"])
        self.assertEqual((args.bar_mode, args.slices, args.placebo, args.offline, args.models, args.pool, args.spread_scale), ("both", True, True, True, "a.json,b.json", "x.json", 0.8))
        self.assertIs(args.func, handle_backtest)
        register(sub, {"backtest": bt})  # idempotent: no argparse conflict on a second load
        # Without an existing parser the plugin creates the subcommand itself.
        p2 = argparse.ArgumentParser()
        sub2 = p2.add_subparsers(dest="cmd")
        register(sub2, {})
        self.assertEqual(p2.parse_args(["backtest", "--espn", "1"]).espn, "1")

    def test_handler_needs_a_target(self):
        out = []
        self.assertEqual(handle_backtest(build_parser().parse_args([]), out=out.append), 2)


class ReplayTrimFixtureTests(unittest.TestCase):
    """The committed cache replays offline and reproduces the committed metrics file byte for byte."""

    ARGS = ["--week", "1", "--season", "2026", "--offline", "--cache-dir", REPLAY_TRIM, "--no-polymarket", "--slices", "--placebo", "--min-cell", "10"]

    def test_reproduces_week1_p03_results(self):
        with tempfile.TemporaryDirectory() as d:
            out = []
            path = os.path.join(d, "week1_p03.json")
            rc = handle_backtest(build_parser().parse_args(self.ARGS + ["--results-json", path]), out=out.append)
            self.assertEqual(rc, 0)
            with open(path, encoding="utf-8") as f:
                got = f.read()
            with open(FIXTURES / "results" / "week1_p03.json", encoding="utf-8") as f:
                want = f.read()
            self.assertEqual(got, want)
            rep = json.loads(got)
            self.assertEqual(rep["n_games"], 2)
            self.assertEqual(rep["spread_sources"], {"pickcenter": 1, "pregame_kalshi": 1})
            self.assertGreater(rep["strata"]["by_play_class"]["try_synth"]["n"], 0)
            self.assertGreater(rep["strata"]["by_play_class"]["kickoff_pending_synth"]["n"], 0)
            self.assertGreater(rep["strata"]["by_play_class"]["timeout"]["n"], 0)
            self.assertIsNotNone(rep["strata"]["by_slice"]["q1"]["model"]["log_loss"])
            self.assertIsNotNone(rep["pooled_scrimmage"]["kalshi_before"]["log_loss"])
            self.assertNotEqual(rep["pooled_scrimmage"]["kalshi_before"]["log_loss"], rep["pooled_scrimmage"]["kalshi_after"]["log_loss"])
            self.assertEqual([(s["pairing"], s["placebo"]) for s in rep["simulations"][:5]], [("pre_before", None), ("post_after", None), ("pre_after", None), ("shift", None), ("post_after", "shuffle")])
            self.assertIn("model_minus_kalshi_before", rep["intervals"])
            self.assertGreater(rep["lineless_q1q2"]["n"], 0)
            text = "\n".join(out)
            self.assertIn("offline", text)
            self.assertIn("0 misses", text)
            self.assertIn("the truth is inside that range", text)

    def test_after_mode_moves_the_headline_market_column(self):
        out = []
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "after.json")
            handle_backtest(build_parser().parse_args(self.ARGS + ["--bar-mode", "after", "--json", path]), out=out.append)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            res, hdr = load_results(path)
        self.assertEqual(data["bar_mode"], "after")
        rows = data["games"][0]["rows"]
        with_both = [r for r in rows if r["kalshi_before_p"] is not None and r["kalshi_after_p"] is not None]
        self.assertTrue(with_both)
        self.assertTrue(all(r["kalshi_p"] == r["kalshi_after_p"] for r in with_both))
        for key in ("ts", "model_p", "model_after_p", "espn_p", "kalshi_before_p", "kalshi_after_p", "play_class", "slice", "in_play", "synthetic"):
            self.assertIn(key, rows[0])
        self.assertEqual(hdr["week"], 1)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0].rows[0].kalshi_p, rows[0]["kalshi_p"])


if __name__ == "__main__":
    unittest.main()

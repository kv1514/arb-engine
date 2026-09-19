"""Multi-game replay helpers — offline."""

import unittest

from arb_engine.backtest import GameReplayer, ReplayResult, ReplayRow, fit_blend_weights, pooled_metrics, resolve_polymarket_tokens, week_games
from arb_engine.quant.inplay_fair import market_confidence_from_spread
from arb_engine.venues.espn import ESPNClient

from .helpers import FakeHttp, load


class ResolveTests(unittest.TestCase):
    def test_direct_slug(self):
        http = FakeHttp({"gamma-api.polymarket.com/events": load("history/gamma_event_det_buf.json")})
        toks = resolve_polymarket_tokens(http, "DET", "BUF", "2026-09-18T00:15:00Z")
        self.assertEqual(set(toks), {"home", "away"})
        self.assertNotEqual(toks["home"], toks["away"])
        # outcomes are ["Lions", "Bills"]: home (BUF) is index 1
        ev = load("history/gamma_event_det_buf.json")[0]
        import json

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
    base = dict(ts=0.0, period=1, clock=600, home_score=0, away_score=0, possession="home", model_p=None, espn_p=None, kalshi_p=None, robinhood_p=None, polymarket_p=None, market_p=None, blend_p=None, disagreement=None, kalshi_arb_margin=None, cross_arb_margin=None)
    base.update(kw)
    return ReplayRow(**base)


def _result(home_won, rows):
    return ReplayResult(espn_event_id="x", home="H", away="A", home_won=home_won, final="", kickoff=None, n_plays=len(rows), n_inplay=len(rows), metrics={}, arb_minutes={}, rows=rows)


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

    def test_market_confidence_from_spread(self):
        self.assertEqual(market_confidence_from_spread(None), 1.0)
        self.assertEqual(market_confidence_from_spread(0.02), 1.0)
        self.assertEqual(market_confidence_from_spread(0.04), 1.0)
        self.assertAlmostEqual(market_confidence_from_spread(0.08), 0.65)
        self.assertEqual(market_confidence_from_spread(0.30), 0.3)


if __name__ == "__main__":
    unittest.main()

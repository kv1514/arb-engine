"""History clients, ESPN play timeline and the game replay — offline."""

import unittest

from arb_engine.backtest import GameReplayer, summarize
from arb_engine.venues.espn import ESPNClient
from arb_engine.venues.history import Bar, HistoryClient, bar_at, espn_timeline

from .helpers import FakeHttp, load


def _history():
    return HistoryClient(http=FakeHttp({
        "KXNFLGAME-26SEP17DETBUF-BUF/candlesticks": load("history/kalshi_candles_BUF.json"),
        "KXNFLGAME-26SEP17DETBUF-DET/candlesticks": load("history/kalshi_candles_DET.json"),
        "/marketdata/event/contract/historicals/v1/": load("history/robinhood_bars.json"),
        "clob.polymarket.com/prices-history": load("history/polymarket_history_DET.json"),
    }))


class HistoryClientTests(unittest.TestCase):
    def test_kalshi_candles(self):
        bars = _history().kalshi_candles("KXNFLGAME-26SEP17DETBUF-BUF", 1789690500, 1789691400)
        self.assertEqual(len(bars), 12)
        self.assertEqual(bars, sorted(bars, key=lambda b: b.ts))
        b = bars[0]
        self.assertIsNotNone(b.ask)
        self.assertIsNotNone(b.bid)
        self.assertGreater(b.ask, b.bid)
        self.assertAlmostEqual(b.mid, (b.bid + b.ask) / 2)

    def test_robinhood_bars(self):
        bars = _history().robinhood_bars(["68a948d7-0087-436b-86ce-78a24d2677a6", "90efc89f-0754-444d-8416-f5caedee185d"], "2026-09-18T00:00:00.000Z")
        self.assertEqual(set(bars), {"68a948d7-0087-436b-86ce-78a24d2677a6", "90efc89f-0754-444d-8416-f5caedee185d"})
        buf = bars["68a948d7-0087-436b-86ce-78a24d2677a6"]
        self.assertEqual(len(buf), 8)
        self.assertIsNone(buf[0].ask)  # trade prices only
        self.assertIsNotNone(buf[0].close)
        self.assertEqual(buf[0].mid, buf[0].close)
        self.assertEqual(buf[1].ts - buf[0].ts, 300)

    def test_polymarket_history(self):
        pts = _history().polymarket_history("3594", 1789690500, 1789691400)
        self.assertEqual(len(pts), 10)
        self.assertTrue(all(0 <= p.close <= 1 for p in pts))

    def test_bar_at(self):
        bars = [Bar(ts=100, bid=None, ask=None, close=0.5), Bar(ts=160, bid=None, ask=None, close=0.6), Bar(ts=220, bid=None, ask=None, close=0.7)]
        self.assertEqual(bar_at(bars, 130, "kalshi").ts, 160)     # first bar ending at/after t
        self.assertEqual(bar_at(bars, 130, "start").ts, 100)      # last bar starting at/before t
        self.assertEqual(bar_at(bars, 300, "kalshi").ts, 220)
        self.assertIsNone(bar_at(bars, 5000, "start", max_gap=60))
        self.assertIsNone(bar_at([], 100))


class TimelineTests(unittest.TestCase):
    def test_espn_timeline_from_summary(self):
        rows, meta = espn_timeline(load("espn/summary_401872932.json"))
        self.assertEqual((meta["home"], meta["away"], meta["home_score"], meta["away_score"]), ("BUF", "DET", 41, 31))
        self.assertEqual(len(rows), 18)
        self.assertEqual([r.ts for r in rows], sorted(r.ts for r in rows))
        last = rows[-1]
        self.assertEqual((last.period, last.clock_seconds, last.game_seconds_remaining), (4, 0, 0))
        self.assertEqual((last.home_score_after, last.away_score_after), (41, 31))
        # Score *before* the play is the previous play's score after.
        self.assertEqual((rows[1].home_score, rows[1].away_score), (rows[0].home_score_after, rows[0].away_score_after))
        self.assertIn(rows[0].possession, ("home", "away", None))
        self.assertTrue(any(r.espn_home_wp is not None for r in rows))


class ReplayTests(unittest.TestCase):
    def test_replay_scores_every_source(self):
        espn = ESPNClient(http=FakeHttp({"/summary": load("espn/summary_401872932.json")}))
        rep = GameReplayer(espn=espn, history=_history())
        res = rep.replay("401872932", rh_contracts={"home": "68a948d7-0087-436b-86ce-78a24d2677a6", "away": "90efc89f-0754-444d-8416-f5caedee185d"}, pm_tokens={"away": "3594"})
        self.assertEqual((res.home, res.away, res.home_won, res.final), ("BUF", "DET", True, "DET 31-41 BUF"))
        self.assertEqual(res.n_plays, 18)
        self.assertEqual(res.arb_minutes["kalshi_tickers"], {"home": "KXNFLGAME-26SEP17DETBUF-BUF", "away": "KXNFLGAME-26SEP17DETBUF-DET"})
        for k in ("model", "espn", "blend"):
            self.assertGreater(res.metrics[k]["n"], 0)
            self.assertGreaterEqual(res.metrics[k]["log_loss"], 0.0)
        # The fixture candles cover only the first 12 minutes; late plays have no Kalshi bar within max_gap.
        self.assertTrue(all(r.model_p is None or 0 < r.model_p < 1 for r in res.rows))
        text = summarize(res)
        self.assertIn("DET 31-41 BUF", text)
        self.assertIn("arb minutes", text)


if __name__ == "__main__":
    unittest.main()

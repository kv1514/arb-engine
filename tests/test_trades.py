import os
import shutil
import unittest

from arb_engine.venues.trades import TradesClient, as_home_prices, parse_kalshi_trade

from .helpers import FakeHttp, load


class NoNetwork(FakeHttp):
    """Any request is a test failure: the cache must answer."""

    def __init__(self):
        super().__init__({})


def _cache_dir(name):
    d = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"arb_trades_cache_{name}_{os.getpid()}")
    shutil.rmtree(d, ignore_errors=True)
    return d


class KalshiTradesTests(unittest.TestCase):
    def test_cursor_pagination_and_microsecond_timestamps(self):
        http = FakeHttp({"cursor=abc123": load("trades/kalshi_trades_page2.json"), "/markets/trades": load("trades/kalshi_trades_page1.json")})
        cache = _cache_dir("k")
        trades = TradesClient(http=http, cache_dir=cache).kalshi_trades("KXNFLGAME-26SEP14DETBUF-BUF")
        self.assertEqual(len(http.calls), 2)
        self.assertIn("limit=1000", http.calls[0])
        self.assertNotIn("cursor", http.calls[0])
        self.assertIn("cursor=abc123", http.calls[1])
        self.assertEqual(len(trades), 8)
        self.assertEqual([t.trade_id for t in trades], [f"t{i:03d}" for i in range(8)])   # oldest first
        t1 = trades[1]
        self.assertEqual((t1.venue, t1.market, t1.side), ("kalshi", "KXNFLGAME-26SEP14DETBUF-BUF", "yes"))
        self.assertEqual(t1.price, 0.56)                # from yes_price_dollars
        self.assertEqual(trades[0].price, 0.55)         # from the cents field
        self.assertEqual((t1.size, trades[0].size), (6.0, 5.0))
        # created_time 17:03:12.345678Z + 37 s + 123 µs -> sub-second precision survives.
        self.assertAlmostEqual(t1.ts % 1, 0.345801, places=5)
        self.assertEqual(t1.ts_ms % 1000, 346)
        # Cache hit: a client with no transport answers from disk, identically.
        again = TradesClient(http=NoNetwork(), cache_dir=cache).kalshi_trades("KXNFLGAME-26SEP14DETBUF-BUF")
        self.assertEqual(again, trades)
        # A different window is a different key -> offline mode refuses to fetch.
        with self.assertRaises(FileNotFoundError):
            TradesClient(http=NoNetwork(), cache_dir=cache, offline=True).kalshi_trades("KXNFLGAME-26SEP14DETBUF-BUF", min_ts=1)
        shutil.rmtree(cache, ignore_errors=True)

    def test_window_filter_and_bad_rows(self):
        http = FakeHttp({"cursor=abc123": load("trades/kalshi_trades_page2.json"), "/markets/trades": load("trades/kalshi_trades_page1.json")})
        all_trades = TradesClient(http=http, cache_dir=_cache_dir("w")).kalshi_trades("T")
        mid = all_trades[3].ts
        cache = _cache_dir("w2")
        some = TradesClient(http=http, cache_dir=cache).kalshi_trades("T", min_ts=mid, max_ts=all_trades[5].ts)
        self.assertEqual([t.trade_id for t in some], ["t003", "t004", "t005"])
        self.assertIn(f"min_ts={int(mid)}", http.calls[-2])
        self.assertIsNone(parse_kalshi_trade({"ticker": "T", "created_time": "garbage", "yes_price": 50}))
        self.assertIsNone(parse_kalshi_trade({"ticker": "T", "created_time": "2026-09-14T17:03:12Z"}))
        shutil.rmtree(cache, ignore_errors=True)


class PolymarketTradesTests(unittest.TestCase):
    def test_data_api_parsing_filters_token_and_caches(self):
        http = FakeHttp({"data-api.polymarket.com/trades": load("trades/polymarket_trades.json")})
        cache = _cache_dir("p")
        trades = TradesClient(http=http, cache_dir=cache).polymarket_trades("1122334455", condition_id="0xcond")
        self.assertEqual(len(http.calls), 1)                       # fewer than a page -> no second call
        self.assertIn("market=0xcond", http.calls[0])
        self.assertEqual(len(trades), 5)                           # the other asset's print is dropped
        self.assertEqual([t.ts for t in trades], sorted(t.ts for t in trades))
        self.assertEqual((trades[0].venue, trades[0].market, trades[0].side, trades[0].size), ("polymarket", "1122334455", "BUY", 15.0))
        self.assertEqual(trades[0].price, 0.45)
        self.assertEqual(trades[-1].ts_ms, 1789405392 * 1000 + 3000000)
        again = TradesClient(http=NoNetwork(), cache_dir=cache).polymarket_trades("1122334455", condition_id="0xcond")
        self.assertEqual(again, trades)
        home = as_home_prices(trades, is_home=False)
        self.assertAlmostEqual(home[0][1], 0.55)
        self.assertEqual(home[0][0], trades[0].ts)
        shutil.rmtree(cache, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

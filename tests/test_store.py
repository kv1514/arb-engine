import os
import unittest

from arb_engine.scanner import scan
from arb_engine.store import Store
from arb_engine.strategy.inplay import evaluate_inplay

from .test_inplay import _me
from .test_scanner import _adapters


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"arb_store_test_{os.getpid()}.db")
        if os.path.exists(self.path):
            os.unlink(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_record_scan_and_stats(self):
        res = scan("nfl", _adapters(), settings={})
        st = Store(self.path)
        n = st.record_scan(res)
        self.assertGreater(n, len(res.events))
        stats = st.arb_stats("nfl")
        self.assertIn("moneyline", stats)
        self.assertEqual(stats["moneyline"]["events"], 2)
        rows = st.conn.execute("SELECT COUNT(*) FROM quotes WHERE venue='robinhood'").fetchone()[0]
        self.assertGreater(rows, 0)
        st.close()

    def test_record_tick(self):
        st = Store(self.path)
        view = evaluate_inplay(_me(), [])
        st.record_tick(view)
        row = st.conn.execute("SELECT event_key, live, market_p, actions FROM inplay_ticks").fetchone()
        self.assertEqual(row[0], view.event_key)
        self.assertEqual(row[1], 1)
        self.assertIsNotNone(row[2])
        st.close()


if __name__ == "__main__":
    unittest.main()

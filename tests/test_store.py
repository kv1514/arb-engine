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


class FrequencyTests(unittest.TestCase):
    def test_arb_frequency_buckets_and_episodes(self):
        path = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"arb_freq_test_{os.getpid()}.db")
        if os.path.exists(path):
            os.unlink(path)
        st = Store(path)
        # Three scans of two events: event A has an arb in scans 1-2 (one episode, 2 scans),
        # event B has an arb only in scan 3 (one episode). Kickoff 5h after the first scan.
        import datetime as dt

        t0 = 1_800_000_000.0
        ko = dt.datetime.fromtimestamp(t0 + 5 * 3600, tz=dt.timezone.utc).isoformat()
        rows = []
        for i, (a_arb, b_arb) in enumerate(((1, 0), (1, 0), (0, 1))):
            ts = t0 + i * 300
            rows.append((ts, "nfl", "A", "total", "A", "kalshi,robinhood", 0.98, 0.012 if a_arb else -0.01, a_arb, 0, 10, 1.2 if a_arb else None, "", ko))
            rows.append((ts, "nfl", "B", "spread", "B", "kalshi,robinhood", 0.99, 0.005 if b_arb else -0.02, b_arb, 0, 5, 0.3 if b_arb else None, "", ko))
        with st.conn:
            st.conn.executemany("INSERT INTO scans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        fq = st.arb_frequency("nfl")
        self.assertEqual(fq["snapshots"], 6)
        buckets = {(b["market_type"], b["bucket"]): b for b in fq["buckets"]}
        self.assertEqual(buckets[("total", "1-6h")]["arb_snapshots"], 2)
        self.assertAlmostEqual(buckets[("total", "1-6h")]["arb_share"], 0.6667, places=4)
        self.assertEqual(buckets[("spread", "1-6h")]["arb_snapshots"], 1)
        self.assertEqual(len(fq["episodes"]), 2)
        ep_a = next(e for e in fq["episodes"] if e["event_key"] == "A")
        self.assertEqual((ep_a["scans"], ep_a["end"] - ep_a["start"]), (2, 300.0))
        self.assertEqual(fq["episode_scans_mean"], 1.5)
        # min_margin filters the small one out
        self.assertEqual(len(st.arb_frequency("nfl", min_margin=0.01)["episodes"]), 1)
        st.close()
        os.unlink(path)


if __name__ == "__main__":
    unittest.main()

import json
import os
import sqlite3
import unittest

from arb_engine.scanner import scan
from arb_engine.store import L1_VENUES, Store, state_hash
from arb_engine.strategy.alerts import Alerter
from arb_engine.strategy.inplay import evaluate_inplay
from arb_engine.venues.espn import GameState

from .helpers import FIXTURE_NOW
from .test_inplay import _me
from .test_scanner import _adapters

# The pre-P07 schema: what an out/history.db written before this item looks like.
OLD_SCHEMA = """
CREATE TABLE scans (ts REAL, sport TEXT, event_key TEXT, market_type TEXT, title TEXT, venues TEXT, gross_sum REAL, margin REAL, fillable INTEGER, live INTEGER, sized_contracts REAL, sized_profit REAL, flags TEXT);
CREATE TABLE quotes (ts REAL, event_key TEXT, outcome TEXT, venue TEXT, exchange TEXT, ask REAL, bid REAL, ask_size REAL, all_in REAL, max_buy REAL, max_buy_maker REAL);
CREATE TABLE inplay_ticks (ts REAL, event_key TEXT, live INTEGER, game_line TEXT, home_score INTEGER, away_score INTEGER, period INTEGER, model_p REAL, market_p REAL, espn_p REAL, blend_p REAL, disagreement REAL, actions TEXT, view TEXT);
"""


def _gs(**kw):
    base = dict(event_id="401", home="KC", away="DEN", home_score=14, away_score=17, status="live", period=3, clock_seconds_remaining_in_period=252, game_seconds_remaining=1152, possession="away", down=2, distance=7, yardline_100=35, home_timeouts=3, away_timeouts=2, espn_home_wp=0.43, vegas_spread_home=-3.0, event_key="nfl:DEN|KC:2026-09-21")
    base.update(kw)
    return GameState(**base)


def _tmp(name):
    path = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"arb_{name}_{os.getpid()}.db")
    if os.path.exists(path):
        os.unlink(path)
    return path


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"arb_store_test_{os.getpid()}.db")
        if os.path.exists(self.path):
            os.unlink(self.path)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)

    def test_two_recorders_adding_the_same_column_do_not_crash(self):
        a = Store(self.path)
        b = Store(self.path)
        a.conn.execute("ALTER TABLE inplay_ticks ADD COLUMN extra_probe TEXT")
        a.conn.commit()
        b.columns = lambda table: []   # b's view is stale: it thinks every column is missing
        b._ensure_columns("inplay_ticks", {"extra_probe": "TEXT"})   # must not raise
        self.assertIn("extra_probe", a.columns("inplay_ticks"))

    def test_file_store_uses_wal_so_a_reader_never_blocks_the_recorder(self):
        # Two recorders and the study scripts share one file all week: a second connection
        # holding a read transaction must not stop the store's insert (WAL), and the store's
        # busy timeout must be long enough to ride out another process's write burst.
        st = Store(self.path)
        self.assertEqual(st.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        reader = sqlite3.connect(self.path)
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM scans").fetchone()   # an open read transaction
        st.record_scan(scan("nfl", _adapters(), now=FIXTURE_NOW, settings={}))   # must not raise
        reader.rollback()
        self.assertEqual(Store(":memory:").conn.execute("PRAGMA journal_mode").fetchone()[0], "memory")

    def test_record_scan_and_stats(self):
        res = scan("nfl", _adapters(), now=FIXTURE_NOW, settings={})
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


class MigrationTests(unittest.TestCase):
    def test_old_schema_file_gains_the_new_columns_in_place(self):
        path = _tmp("migrate")
        conn = sqlite3.connect(path)
        conn.executescript(OLD_SCHEMA)
        conn.execute("INSERT INTO inplay_ticks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (1.0, "nfl:A|B:2026-09-13", 1, "Q1", 0, 0, 1, 0.5, 0.5, 0.5, 0.5, 0.0, "", "{}"))
        conn.commit()
        conn.close()
        st = Store(path)
        cols = set(st.columns("inplay_ticks"))
        for v in L1_VENUES:
            self.assertIn(f"{v}_home_bid", cols)
            self.assertIn(f"{v}_away_ask_size", cols)
            self.assertIn(f"{v}_book_id", cols)
        self.assertTrue({"gated_reasons", "state_hash", "freshness_json", "l1_json", "home", "away"} <= cols)
        self.assertTrue({"bid_size", "venue_ts", "is_mm"} <= set(st.columns("quotes")))
        self.assertIn("start_time", st.columns("scans"))
        self.assertTrue({"bid_10", "mid_60", "ts_300", "bid_900", "last_bid", "pnl_settle"} <= set(st.columns("steal_observations")))
        self.assertIn("espn_ticks", {r[0] for r in st.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")})
        self.assertTrue({"req_ts", "obs_ts"} <= set(st.columns("trade_prints")))
        old = st.tick_rows()[0]
        self.assertEqual((old["event_key"], old["kalshi_home_bid"], old["l1_json"]), ("nfl:A|B:2026-09-13", None, None))
        # Reopening is idempotent (no duplicate-column error).
        st.close()
        Store(path).close()
        os.unlink(path)


class TickRecordingTests(unittest.TestCase):
    def setUp(self):
        self.path = _tmp("ticks")
        self.st = Store(self.path)

    def tearDown(self):
        self.st.close()
        os.unlink(self.path)

    def test_record_tick_with_quotes_and_freshness_fills_l1_columns(self):
        me = _me()
        view = evaluate_inplay(me, [], game_state=_gs())
        fresh = {"last_state_change_ts": 100.0, "last_score_change_ts": None, "mids": {"kalshi": {"KC": 0.40}}}
        self.st.record_tick(view, quotes_by_venue=me.quotes_by_venue, freshness=fresh, ts=123.0)
        row = self.st.tick_rows()[0]
        self.assertEqual((row["home"], row["away"], row["ts"]), ("KC", "DEN", 123.0))
        self.assertEqual((row["kalshi_home_bid"], row["kalshi_home_ask"]), (0.39, 0.41))   # KC is home
        self.assertEqual((row["kalshi_away_bid"], row["kalshi_away_ask"]), (0.59, 0.61))
        self.assertEqual((row["robinhood_home_ask"], row["robinhood_book_id"]), (0.40, "rothera"))
        self.assertEqual(json.loads(row["freshness_json"])["last_state_change_ts"], 100.0)
        l1 = json.loads(row["l1_json"])
        self.assertEqual(l1["kalshi"]["DEN"]["fee_params"]["fee_type"], "quadratic_with_maker_fees")
        self.assertEqual(l1["robinhood"]["KC"]["exchange"], "rothera")
        self.assertEqual(row["state_hash"], state_hash(_gs()))
        self.assertEqual(len(row["state_hash"]), 12)
        self.assertIsNone(row["gated_reasons"])
        # Without quotes / freshness the L1 columns stay NULL and the row still lands.
        self.st.record_tick(view)
        bare = self.st.tick_rows()[1]
        self.assertIsNone(bare["kalshi_home_bid"])
        self.assertIsNone(bare["freshness_json"])
        self.assertIsNone(bare["l1_json"])
        self.assertIsNotNone(bare["market_p"])

    def test_tick_timestamp_cannot_precede_exact_observation(self):
        me = _me()
        quote = me.quotes_by_venue["kalshi"][0]
        quote.meta.update({"req_ts": 129.5, "obs_ts": 130.0, "approx_time": False})
        view = evaluate_inplay(me, [], game_state=_gs())
        self.st.record_tick(view, quotes_by_venue=me.quotes_by_venue, ts=123.0)
        row = self.st.tick_rows()[0]
        recorded = json.loads(row["l1_json"])
        exact = next(r for r in recorded["rows"] if r["venue_market_id"] == quote.venue_market_id)
        self.assertEqual((exact["obs_ts"], row["ts"]), (130.0, 130.0))

    def test_lossless_rows_keep_yes_and_normalized_no_and_provenance(self):
        from arb_engine.models import OutcomeQuote
        yes = OutcomeQuote("robinhood", "YES-A", "nfl:A|B:2026-09-21", "A", bid=.4, ask=.42, ts=99,
                           book_id="rothera", meta={"side": "yes", "tie_payout": 0})
        no = OutcomeQuote("robinhood", "NO-B#no", "nfl:A|B:2026-09-21", "A", bid=.39, ask=.41, ts=99,
                          book_id="rothera", meta={"side": "no", "no_of": "B", "tie_payout": 1})
        l1 = self.st.l1_from_quotes({"robinhood": [yes, no]}, req_ts=98, obs_ts=99, refreshed=False)
        self.assertEqual([(r["venue_market_id"], r["side"]) for r in l1["rows"]], [("NO-B#no", "no"), ("YES-A", "yes")])
        self.assertEqual([r["tie_payout"] for r in l1["rows"]], [1.0, 0.0])
        self.assertTrue(all(r["obs_ts"] <= 100 and r["refreshed"] == 0 for r in l1["rows"]))

    def test_exact_fast_times_and_approximate_full_times_are_explicit(self):
        from arb_engine.models import OutcomeQuote
        exact = OutcomeQuote("kalshi", "K", "nfl:A|B:2026-09-21", "A", bid=.4, ask=.42, ts=12,
                             meta={"side": "yes", "req_ts": 10, "obs_ts": 12, "refreshed": True})
        full = OutcomeQuote("robinhood", "R", "nfl:A|B:2026-09-21", "B", bid=.5, ask=.52, ts=8,
                            meta={"side": "yes"})
        rows = self.st.l1_from_quotes({"kalshi": [exact], "robinhood": [full]}, req_ts=13, obs_ts=13, source="fast")["rows"]
        by_market = {r["venue_market_id"]: r for r in rows}
        self.assertEqual((by_market["K"]["req_ts"], by_market["K"]["obs_ts"], by_market["K"]["approx_time"]), (10.0, 12.0, 0))
        self.assertEqual((by_market["R"]["req_ts"], by_market["R"]["obs_ts"], by_market["R"]["approx_time"]), (13, 13, 1))
        self.assertEqual({r["source"] for r in rows}, {"fast"})

    def test_trade_print_pages_are_idempotent(self):
        root = os.path.join(os.path.dirname(__file__), "fixtures", "trades")
        pages = []
        for i in (1, 2):
            with open(os.path.join(root, f"kalshi_trades_page{i}.json"), encoding="utf-8") as fh:
                pages.append(json.load(fh)["trades"])
        self.assertGreater(self.st.record_trade_prints(pages[0]), 0)
        total = self.st.record_trade_prints(pages[1])
        self.assertEqual(self.st.record_trade_prints(pages[0] + pages[1]), 0)
        count = self.st.conn.execute("select count(*) from trade_prints").fetchone()[0]
        self.assertEqual(count, len({r["trade_id"] for page in pages for r in page}))
        self.assertGreaterEqual(total, 0)
        self.st.conn.execute("delete from trade_prints")
        self.st.record_trade_prints(pages[0], req_ts=100, obs_ts=101)
        receipt = self.st.conn.execute("select req_ts, obs_ts from trade_prints limit 1").fetchone()
        self.assertEqual(tuple(receipt), (100.0, 101.0))
        ticker = pages[0][0]["ticker"]
        expected = int(max(__import__("datetime").datetime.fromisoformat(t["created_time"].replace("Z", "+00:00")).timestamp() for t in pages[0]))
        self.assertEqual(self.st.latest_trade_ts(ticker), expected)

    def test_live_slate_with_fixture_adapters_records_l1_per_priced_game(self):
        """The slate's record call site (``record_tick``) plus the per-venue L1 / ESPN plumbing
        this item adds, driven by the fixture adapters: one inplay tick and one espn tick per
        priced game per poll, with the merged event's quotes in the flat columns."""
        from arb_engine.strategy.live import LiveSlate

        from .test_live import FakeFeed, _moneyline_key

        adapters = _adapters()
        key, me = _moneyline_key(adapters)
        away, home = me.info.outcomes[0], me.info.outcomes[1]
        now = 1_800_000_000.0
        live = GameState(event_id="1", home=home, away=away, home_score=14, away_score=10, status="live", period=3, clock_seconds_remaining_in_period=600, game_seconds_remaining=1500, possession="home", down=2, distance=7, yardline_100=45, home_timeouts=3, away_timeouts=2, event_key=key)
        journal = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"arb_slate_journal_{os.getpid()}.jsonl")
        slate = LiveSlate(adapters, feed=FakeFeed([live]), settings={}, store=self.st, alerter=Alerter(journal_path=journal, quiet=True, desktop=False, webhook=""))
        for i in range(2):
            tick = slate.tick(now + 10 * i)
            self.assertEqual(len(tick.views), 1)
        # The slate's own call sites (record_view: record_espn_tick + record_tick with the
        # per-venue L1 and freshness) write one full row and one ESPN state per poll.
        rows = self.st.tick_rows(key)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(self.st.espn_tick_rows(key)), 2)
        full = [r for r in rows if r["l1_json"]]
        self.assertEqual(len(full), 2)
        venues = {v for v in L1_VENUES if full[0][f"{v}_home_ask"] is not None}
        self.assertGreaterEqual(len(venues), 2)
        for v in venues:
            q = next(q for q in me.quotes_by_venue[v] if q.outcome == home)
            self.assertEqual(full[0][f"{v}_home_ask"], q.ask)
            self.assertEqual(full[0][f"{v}_home_bid"], q.bid)
        self.assertEqual(full[0]["home"], home)
        self.assertEqual(full[0]["state_hash"], state_hash(live))
        self.assertIsNotNone(full[0]["blend_p"])
        if os.path.exists(journal):
            os.unlink(journal)

    def test_espn_tick_row_per_game_per_poll(self):
        for t in (10.0, 20.0):
            self.st.record_espn_tick(_gs(), ts=t)
            self.st.record_espn_tick(_gs(event_id="402", home="BUF", away="DET", event_key="nfl:BUF|DET:2026-09-21").as_dict() | {"suspect": True, "last_play_id": "p7", "state_source": "summary"}, ts=t)
        rows = self.st.espn_tick_rows()
        self.assertEqual(len(rows), 4)
        det = self.st.espn_tick_rows("nfl:BUF|DET:2026-09-21")
        self.assertEqual([r["ts"] for r in det], [10.0, 20.0])
        self.assertEqual((det[0]["last_play_id"], det[0]["suspect"], det[0]["state_source"], det[0]["clock"]), ("p7", 1, "summary", 252))
        self.assertEqual(json.loads(det[0]["situation_json"])["home_score"], 14)
        an = self.st.anomaly_counts()
        self.assertEqual((an["ticks"], an["games"], an["suspect"], an["by_source"]["summary"]), (4, 2, 2, 2))

    def test_ladder_settlement_and_convergence(self):
        key = "nfl:DEN|KC:2026-09-21"
        quotes = lambda kc_bid, kc_ask: {"kalshi": [{"outcome": "KC", "bid": kc_bid, "ask": kc_ask}, {"outcome": "DEN", "bid": round(1 - kc_ask, 2), "ask": round(1 - kc_bid, 2)}]}  # noqa: E731
        t0 = 1000.0
        self.st.record_l1(t0, key, quotes(0.30, 0.32), game_state=_gs())
        oid = self.st.record_steal(t0, key, "KC", "kalshi", ask=0.32, all_in=0.335, fair=0.40, model_p=0.41, market_p=0.33, period=3)
        self.assertEqual(oid, 1)
        # Price converges: +10 s / +60 s ticks exist, +300 s does not yet.
        self.st.record_l1(t0 + 10, key, quotes(0.34, 0.36), game_state=_gs())
        self.st.record_l1(t0 + 60, key, quotes(0.37, 0.39), game_state=_gs())
        n = self.st.update_ladder(now=t0 + 65)
        self.assertGreater(n, 0)
        o = self.st.steal_rows()[0]
        self.assertEqual((o["bid_10"], o["mid_10"], o["ts_10"]), (0.34, 0.35, t0 + 10))
        self.assertEqual((o["bid_60"], o["last_bid"], o["last_ts"]), (0.37, 0.37, t0 + 60))
        self.assertIsNone(o["bid_300"])
        self.assertEqual(o["settled"], 0)
        # Game ends: KC wins -> settled at 1 - all_in.
        self.st.record_l1(t0 + 300, key, quotes(0.60, 0.62), game_state=_gs())
        self.st.record_espn_tick(_gs(status="final", home_score=27, away_score=24), ts=t0 + 4000)
        self.st.update_ladder(now=t0 + 4001)
        o = self.st.steal_rows()[0]
        self.assertEqual((o["bid_300"], o["settled"], o["settle_value"]), (0.60, 1, 1.0))
        self.assertAlmostEqual(o["pnl_settle"], 1.0 - 0.335)
        self.assertIsNone(o["bid_900"])
        conv = self.st.convergence(min_games=30)
        self.assertEqual((conv["n"], conv["n_games"], conv["complete_ladders"]), (1, 1, 0))
        cell = conv["cells"][0]
        self.assertEqual((cell["bucket"], cell["gated"], cell["n"], cell["n_games"], cell["ok"]), ("5-8%", False, 1, 1, False))
        self.assertNotIn("toward_away_ratio_60", cell)     # withheld under 30 games
        loose = self.st.convergence(min_games=1)["cells"][0]
        self.assertEqual((loose["toward_60"], loose["away_60"]), (1, 0))
        self.assertAlmostEqual(loose["clv_bid_60"], 0.37 - 0.335)
        self.assertAlmostEqual(loose["clv_mid_10"], 0.35 - 0.335)
        self.assertAlmostEqual(loose["pnl_settle_mean"], 0.665)
        # Non-default offsets add their columns on the fly.
        self.st.update_ladder(now=t0 + 4001, offsets=(30,))
        self.assertEqual((self.st.steal_rows()[0]["bid_30"], self.st.steal_rows()[0]["ask_30"]), (0.37, 0.39))

    def test_ladder_skips_ticks_that_do_not_quote_the_venue(self):
        # One poll lost the Kalshi L1 (only Robinhood quoted): the +60 s rung must come from
        # the next tick that quotes Kalshi, not stay NULL forever because the first tick at or
        # after ts+60 had nothing for that venue.
        key = "nfl:DEN|KC:2026-09-21"
        kq = lambda b, a: {"kalshi": [{"outcome": "KC", "bid": b, "ask": a}, {"outcome": "DEN", "bid": round(1 - a, 2), "ask": round(1 - b, 2)}]}  # noqa: E731
        rq = lambda b, a: {"robinhood": [{"outcome": "KC", "bid": b, "ask": a}, {"outcome": "DEN", "bid": round(1 - a, 2), "ask": round(1 - b, 2)}]}  # noqa: E731
        t0 = 1000.0
        self.st.record_l1(t0, key, kq(0.30, 0.32), game_state=_gs())
        self.st.record_steal(t0, key, "KC", "kalshi", ask=0.32, all_in=0.335, fair=0.40, bid=0.30)
        self.st.record_l1(t0 + 60, key, rq(0.31, 0.33), game_state=_gs())        # kalshi missing on this poll
        self.st.record_l1(t0 + 70, key, kq(0.34, 0.36), game_state=_gs())
        self.st.update_ladder(now=t0 + 75)
        o = self.st.steal_rows()[0]
        self.assertEqual((o["bid_60"], o["ask_60"], o["mid_60"], o["ts_60"]), (0.34, 0.36, 0.35, t0 + 70))
        self.assertEqual((o["bid_10"], o["ts_10"]), (0.34, t0 + 70))                 # +10 s rung: same forward scan
        self.assertEqual((o["last_bid"], o["last_ts"]), (0.34, t0 + 70))
        self.st.update_ladder(now=t0 + 400)
        self.assertEqual(self.st.steal_rows()[0]["bid_60"], 0.34)
        self.assertEqual(self.st.quote_at_or_after(key, "kalshi", "KC", t0 + 60), (t0 + 70, 0.34, 0.36))
        self.assertEqual(self.st.quote_at_or_after(key, "robinhood", "KC", t0 + 60), (t0 + 60, 0.31, 0.33))
        self.assertIsNone(self.st.quote_at_or_after(key, "polymarket", "KC", t0))
        self.assertIsNone(self.st.quote_at_or_after(key, "kalshi", "KC", t0 + 60, max_ticks=1))   # bounded scan

    def test_convergence_compares_like_with_like(self):
        # A market that does not move is neither toward nor away; one that ticks half a spread
        # toward fair is toward (the old ask-vs-mid rule called both "away").
        key = "nfl:DEN|KC:2026-09-21"
        kq = lambda b, a: {"kalshi": [{"outcome": "KC", "bid": b, "ask": a}, {"outcome": "DEN", "bid": round(1 - a, 2), "ask": round(1 - b, 2)}]}  # noqa: E731
        t0 = 1000.0
        self.st.record_l1(t0, key, kq(0.30, 0.32), game_state=_gs())
        self.st.record_steal(t0, key, "KC", "kalshi", ask=0.32, all_in=0.335, fair=0.40, bid=0.30)      # entry mid 0.31
        self.st.record_steal(t0, key, "KC", "kalshi", ask=0.32, all_in=0.335, fair=0.40)                # no bid: ask vs ask
        self.st.record_l1(t0 + 10, key, kq(0.30, 0.32), game_state=_gs())        # flat
        self.st.record_l1(t0 + 60, key, kq(0.31, 0.33), game_state=_gs())        # up one cent
        self.st.record_l1(t0 + 300, key, kq(0.29, 0.31), game_state=_gs())       # down one cent
        self.st.update_ladder(now=t0 + 301)
        cell = self.st.convergence(min_games=1)["cells"][0]
        self.assertEqual((cell["n"], cell["toward_10"], cell["away_10"]), (2, 0, 0))
        self.assertEqual((cell["toward_60"], cell["away_60"]), (2, 0))
        self.assertEqual((cell["toward_300"], cell["away_300"]), (0, 2))
        self.assertAlmostEqual(cell["clv_mid_60"], 0.32 - 0.335)
        self.assertAlmostEqual(cell["clv_bid_300"], 0.29 - 0.335)

    def test_manual_settlement_and_tie(self):
        key = "nfl:A|B:2026-09-21"
        self.st.record_steal(5.0, key, "A", "kalshi", ask=0.5, all_in=0.51, fair=0.6)
        self.st.record_steal(6.0, key, "B", "kalshi", ask=0.5, all_in=0.51, fair=0.6, gated=True, gated_reasons=["feed-stale"])
        self.assertEqual(self.st.settle_event(key, None), 2)
        rows = self.st.steal_rows(key)
        self.assertEqual([r["settle_value"] for r in rows], [0.5, 0.5])
        self.assertEqual((rows[1]["gated"], rows[1]["gated_reasons"]), (1, "feed-stale"))
        cells = {(c["bucket"], c["gated"]) for c in self.st.convergence(min_games=1)["cells"]}
        self.assertEqual(cells, {("8%+", False), ("8%+", True)})

    def test_record_pregame_line_idempotent(self):
        self.assertTrue(self.st.record_pregame_line("nfl:A|B:2026-09-21", -150, 130, 0.58, ts=1.0))
        self.assertFalse(self.st.record_pregame_line("nfl:A|B:2026-09-21", -160, 140, 0.60, ts=2.0))
        line = self.st.pregame_line("nfl:A|B:2026-09-21")
        self.assertEqual((line["sportsbook_ml_home"], line["kalshi_mid"], line["ts"]), (-150, 0.58, 1.0))
        rep = self.st.clv_report(min_games=1)
        self.assertEqual(rep["pregame_anchors"][0]["kalshi_mid"], 0.58)

    def test_alerter_journals_structured_steal_and_records_it(self):
        journal = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"arb_alert_{os.getpid()}.jsonl")
        al = Alerter(journal_path=journal, quiet=True, desktop=False, webhook="", store=self.st)
        al.alert("STEAL", "Denver all-in 0.61 vs fair 0.66", event="nfl:DEN|KC:2026-09-21", outcome="DEN", venue="robinhood", ask=0.60, all_in=0.61, fair=0.66, model_p=0.67, market_p=0.64, gated=False, ts=77.0)
        al.alert("GATED STEAL", "Denver ...", event="nfl:DEN|KC:2026-09-21", outcome="DEN", venue="kalshi", ask=0.60, all_in=0.61, fair=0.66, gated_reasons=["feed-stale"], ts=78.0)
        al.alert("HEDGE NOW", "plain alert keeps working", event="x")
        self.assertEqual(len(al.steals), 2)
        rec = al.events[0]
        self.assertEqual(rec["steal"]["outcome"], "DEN")
        self.assertEqual(rec["steal"]["event_key"], "nfl:DEN|KC:2026-09-21")
        self.assertAlmostEqual(rec["steal"]["edge"], 0.05)
        rows = self.st.steal_rows()
        self.assertEqual([(r["venue"], r["gated"], r["ts"]) for r in rows], [("robinhood", 0, 77.0), ("kalshi", 1, 78.0)])
        self.assertEqual(rows[1]["gated_reasons"], "feed-stale")
        with open(journal, encoding="utf-8") as f:
            lines = [json.loads(x) for x in f]
        self.assertEqual(lines[0]["steal"]["venue"], "robinhood")
        os.unlink(journal)


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

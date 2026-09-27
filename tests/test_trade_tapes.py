"""Historical Kalshi trade tapes: completeness, resumption, deduplication and cache keys.

Adversarial cases for ``venues/trades.py`` (the research tape, not the live print poller in
``strategy/fastlane.py``), all offline: a scripted cursor-paginated fake of
``GET /markets/trades`` and temporary cache directories.
"""
from __future__ import annotations

import json
import math
import os
import random
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

from arb_engine.venues.http import HttpError
from arb_engine.venues.trades import (ConflictingTrades, IncompleteTape, TapeError, TradesClient, _cache_key, _legacy_cache_key)

TK = "KXNFLGAME-26SEP27DENKC-KC"
BASE = 1_790_000_000.0


def row(i, sec, price=0.50, count=1.0, ticker=TK, tid=None, frac=0.25, **kw):
    t = datetime.fromtimestamp(BASE + sec + frac, tz=timezone.utc)
    r = {"trade_id": f"t{i:04d}" if tid is None else tid, "ticker": ticker, "created_time": t.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
         "yes_price_dollars": f"{price:.4f}", "count_fp": f"{count:.2f}", "taker_side": "yes"}
    r.update(kw)
    return r


class Pages:
    """``GET /markets/trades`` by cursor: ``pages[cursor]`` is a page, an exception to raise,
    or a list of those consumed one per call (the last repeats). Records every call's params."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, params=None, headers=None, raw=False):
        assert url.endswith("/markets/trades"), url
        self.calls.append(dict(params or {}))
        v = self.pages[(params or {}).get("cursor")]
        if isinstance(v, list):
            v = v.pop(0) if len(v) > 1 else v[0]
        if isinstance(v, BaseException):
            raise v
        return v


class NoNetwork:
    def get(self, *a, **k):
        raise AssertionError("must be answered from the cache")


def three_pages():
    """30 prints, newest first: None -> t0020..t0029 (cursor c1) -> t0010..t0019 (c2) -> t0000..t0009 (end)."""
    return {None: {"trades": [row(i, i) for i in range(29, 19, -1)], "cursor": "c1"},
            "c1": {"trades": [row(i, i) for i in range(19, 9, -1)], "cursor": "c2"},
            "c2": {"trades": [row(i, i) for i in range(9, -1, -1)], "cursor": ""}}


class TapeCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="arb_tapes_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def client(self, http, offline=False):
        return TradesClient(http=http, cache_dir=self.dir, offline=offline)

    def doc(self, market=TK, lo=None, hi=None, venue="kalshi"):
        with open(os.path.join(self.dir, _cache_key(venue, market, lo, hi) + ".json"), encoding="utf-8") as f:
            return json.load(f)

    def ids(self, trades):
        return [t.trade_id for t in trades]


class BudgetAndResumeTests(TapeCase):
    def test_a_used_up_budget_is_never_served_as_the_tape(self):
        http = Pages(three_pages())
        with self.assertRaises(IncompleteTape) as cm:
            self.client(http).kalshi_trades(TK, max_pages=2)
        self.assertEqual(len(http.calls), 2)
        self.assertIn("page budget 2", str(cm.exception))
        self.assertEqual(len(cm.exception.tape.trades), 20)             # what is known so far, flagged incomplete
        d = self.doc()
        self.assertEqual((d["complete"], d["next_cursor"], d["pages"]), (False, "c2", 2))
        with self.assertRaises(IncompleteTape):                           # offline: the partial file is not an answer
            self.client(NoNetwork(), offline=True).kalshi_trades(TK)

    def test_the_same_query_with_a_larger_budget_fetches_only_the_backlog(self):
        http = Pages(three_pages())
        with self.assertRaises(IncompleteTape):
            self.client(http).kalshi_trades(TK, max_pages=1)
        trades = self.client(http).kalshi_trades(TK, max_pages=50)
        self.assertEqual([c.get("cursor") for c in http.calls], [None, "c1", "c2"])   # resumed at c1, nothing refetched
        self.assertEqual(self.ids(trades), [f"t{i:04d}" for i in range(30)])
        self.assertEqual(self.doc()["complete"], True)
        again = self.client(NoNetwork(), offline=True).kalshi_trades(TK)             # complete: offline replay works
        self.assertEqual(again, trades)

    def test_the_same_budget_again_keeps_progressing(self):
        http = Pages(three_pages())
        for _ in range(2):
            with self.assertRaises(IncompleteTape):
                self.client(http).kalshi_trades(TK, max_pages=1)
        self.assertEqual(len(self.client(http).kalshi_trades(TK, max_pages=1)), 30)
        self.assertEqual([c.get("cursor") for c in http.calls], [None, "c1", "c2"])

    def test_a_failed_page_keeps_progress_and_the_next_call_resumes(self):
        pages = three_pages()
        pages["c1"] = [HttpError(503, "https://x/markets/trades", "unavailable"), pages["c1"]]
        http = Pages(pages)
        with self.assertRaises(IncompleteTape) as cm:
            self.client(http).kalshi_trades(TK)
        self.assertIn("page request failed", str(cm.exception))
        self.assertEqual(self.doc()["next_cursor"], "c1")
        self.assertEqual(len(self.client(http).kalshi_trades(TK)), 30)
        self.assertEqual([c.get("cursor") for c in http.calls], [None, "c1", "c1", "c2"])
        # A non-HTTP failure (a broken connection, a bad body) is kept the same way.
        shutil.rmtree(self.dir)
        pages = three_pages()
        pages["c2"] = [ValueError("truncated JSON"), pages["c2"]]
        http = Pages(pages)
        with self.assertRaises(IncompleteTape):
            self.client(http).kalshi_trades(TK)
        self.assertEqual(len(self.client(http).kalshi_trades(TK)), 30)

    def test_a_restarted_process_resumes_from_its_last_checkpoint(self):
        class Killed(BaseException):
            """The process dies mid-fetch: no handler runs."""
        pages = three_pages()
        pages["c2"] = [Killed(), pages["c2"]]
        http = Pages(pages)
        with self.assertRaises(Killed):
            self.client(http).kalshi_tape(TK, checkpoint_every=1)
        d = self.doc()
        self.assertEqual((d["complete"], d["next_cursor"], len(d["trades"])), (False, "c2", 20))
        trades = TradesClient(http=http, cache_dir=self.dir).kalshi_trades(TK)     # a new process, same cache
        self.assertEqual(len(trades), 30)
        self.assertEqual([c.get("cursor") for c in http.calls], [None, "c1", "c2", "c2"])

    def test_a_saved_cursor_the_venue_no_longer_accepts_restarts_from_the_top(self):
        pages = three_pages()
        http = Pages(pages)
        with self.assertRaises(IncompleteTape):
            self.client(http).kalshi_trades(TK, max_pages=1)
        pages["c1"] = HttpError(400, "https://x/markets/trades", "invalid cursor")
        pages[None] = {"trades": [row(i, i) for i in range(29, -1, -1)], "cursor": ""}   # one page now holds everything
        tape = self.client(http).kalshi_tape(TK)
        self.assertEqual(([c.get("cursor") for c in http.calls], tape.restarts), ([None, "c1", None], 1))
        self.assertEqual(self.ids(tape.trades), [f"t{i:04d}" for i in range(30)])
        self.assertEqual(tape.duplicates, 10)                            # the first page's prints came back once more

    def test_a_repeated_cursor_is_never_followed_and_never_complete(self):
        pages = three_pages()
        pages["c2"] = {"trades": [row(i, i) for i in range(9, -1, -1)], "cursor": "c1"}   # loops back
        http = Pages(pages)
        with self.assertRaises(IncompleteTape) as cm:
            self.client(http).kalshi_trades(TK)
        self.assertIn("repeated cursor", str(cm.exception))
        self.assertEqual(len(http.calls), 3)
        d = self.doc()
        self.assertEqual((d["complete"], d["next_cursor"]), (False, None))  # next call starts again from the top
        pages["c2"] = {"trades": [row(i, i) for i in range(9, -1, -1)], "cursor": ""}
        self.assertEqual(len(self.client(http).kalshi_trades(TK)), 30)
        self.assertEqual(http.calls[3].get("cursor"), None)


class DedupeTests(TapeCase):
    def test_overlapping_pages_and_same_second_prints_keep_one_copy_each(self):
        # Page boundaries inside one second: the last print of a page comes back first on the next
        # page, beside another print of the same second.
        same = [row(100, 10, frac=0.1), row(101, 10, frac=0.1, count=3.0), row(102, 10, frac=0.9)]
        pages = {None: {"trades": [row(i, i) for i in range(20, 10, -1)] + [same[2], same[1]], "cursor": "c1"},
                 "c1": {"trades": [same[1], same[0]] + [row(i, i) for i in range(9, -1, -1)], "cursor": ""}}
        tape = self.client(Pages(pages)).kalshi_tape(TK)
        ids = self.ids(tape.trades)
        self.assertEqual(len(ids), len(set(ids)))
        sent = {r["trade_id"] for p in pages.values() for r in p["trades"]}
        self.assertEqual(sorted(ids), sorted(sent))                      # every id once: 10 + 10 + 3
        self.assertEqual(tape.duplicates, 1)
        self.assertEqual([t.ts for t in tape.trades], sorted(t.ts for t in tape.trades))

    def test_one_trade_id_with_two_payloads_fails_and_is_not_cached_complete(self):
        pages = three_pages()
        pages["c1"] = {"trades": [row(25, 25, price=0.61)] + [row(i, i) for i in range(19, 9, -1)], "cursor": "c2"}   # t0025 again, repriced
        with self.assertRaises(ConflictingTrades) as cm:
            self.client(Pages(pages)).kalshi_trades(TK)
        self.assertIn("t0025", str(cm.exception))
        self.assertIsInstance(cm.exception, TapeError)
        self.assertEqual(self.doc()["complete"], False)
        with self.assertRaises(IncompleteTape):
            self.client(NoNetwork(), offline=True).kalshi_trades(TK)
        # Identical copies are not a conflict.
        shutil.rmtree(self.dir)
        pages = three_pages()
        pages["c1"]["trades"].insert(0, row(25, 25))
        self.assertEqual(len(self.client(Pages(pages)).kalshi_trades(TK)), 30)

    def test_unreadable_or_foreign_rows_keep_the_tape_incomplete(self):
        for bad in ({"trade_id": ""}, {"created_time": "garbage"}, {"yes_price_dollars": "NaN", "yes_price": None},
                    {"count_fp": "inf", "count": None}, {"ticker": "KXNFLGAME-26SEP27DENKC-DEN"}):
            shutil.rmtree(self.dir, ignore_errors=True)
            pages = three_pages()
            broken = row(16, 16)
            broken.update(bad)
            pages["c1"]["trades"][3] = broken
            with self.assertRaises(IncompleteTape, msg=bad) as cm:
                self.client(Pages(pages)).kalshi_trades(TK)
            self.assertEqual(cm.exception.tape.rejected, 1, bad)
            self.assertEqual(self.doc()["complete"], False, bad)


class WindowAndKeyTests(TapeCase):
    def test_same_second_window_edges_are_requested_whole_and_trimmed_exactly(self):
        edge = [row(1, 100, frac=0.2), row(2, 100, frac=0.5), row(3, 150, frac=0.0), row(4, 200, frac=0.5), row(5, 200, frac=0.9)]
        http = Pages({None: {"trades": edge[::-1], "cursor": ""}})
        lo, hi = BASE + 100.5, BASE + 200.5
        trades = self.client(http).kalshi_trades(TK, lo, hi)
        self.assertEqual(self.ids(trades), ["t0002", "t0003", "t0004"])
        self.assertEqual((http.calls[0]["min_ts"], http.calls[0]["max_ts"]), (math.floor(lo) - 1, math.ceil(hi) + 1))

    def test_windows_that_truncate_to_the_same_seconds_never_share_a_file(self):
        a, b = (BASE + 100.2, BASE + 200.7), (BASE + 100.9, BASE + 200.1)
        self.assertEqual(_legacy_cache_key("kalshi", TK, *a), _legacy_cache_key("kalshi", TK, *b))   # the old collision
        self.assertNotEqual(_cache_key("kalshi", TK, *a), _cache_key("kalshi", TK, *b))
        tape = [row(i, 100 + i, frac=0.5) for i in range(101)]
        http = Pages({None: {"trades": tape[::-1], "cursor": ""}})
        first = self.client(http).kalshi_trades(TK, *a)
        self.assertEqual((first[0].ts, first[-1].ts), (BASE + 100.5, BASE + 200.5))
        with self.assertRaises(FileNotFoundError):                            # never the other window's tape
            self.client(NoNetwork(), offline=True).kalshi_trades(TK, *b)
        second = self.client(http).kalshi_trades(TK, *b)
        self.assertEqual((second[0].ts, second[-1].ts), (BASE + 101.5, BASE + 199.5))
        self.assertNotEqual(_cache_key("kalshi", "A/B", None, None), _cache_key("kalshi", "A_B", None, None))

    def test_a_file_whose_stored_query_differs_is_not_used(self):
        http = Pages(three_pages())
        self.client(http).kalshi_trades(TK, BASE, BASE + 50)
        path = os.path.join(self.dir, _cache_key("kalshi", TK, BASE, BASE + 50) + ".json")
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        d["query"]["max_ts"] = BASE + 60
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f)
        with self.assertRaises(FileNotFoundError):
            self.client(NoNetwork(), offline=True).kalshi_trades(TK, BASE, BASE + 50)

    def test_non_finite_or_reversed_windows_are_refused(self):
        for lo, hi in ((float("nan"), None), (None, float("inf")), (float("-inf"), 5.0), ("nan", None), (10.0, 5.0)):
            with self.assertRaises(ValueError, msg=(lo, hi)):
                self.client(NoNetwork()).kalshi_trades(TK, lo, hi)
        with self.assertRaises(ValueError):
            self.client(NoNetwork()).kalshi_trades(TK, max_pages=0)


class LegacyCacheTests(TapeCase):
    def _legacy(self, lo=None, hi=None):
        os.makedirs(self.dir, exist_ok=True)
        p = os.path.join(self.dir, _legacy_cache_key("kalshi", TK, lo, hi) + ".json")
        with open(p, "w", encoding="utf-8") as f:     # the old format: no schema, no completeness, a truncated tape
            json.dump({"venue": "kalshi", "market": TK, "min_ts": lo, "max_ts": hi, "fetched_at": 0,
                       "trades": [{"venue": "kalshi", "market": TK, "ts": BASE + 1, "price": .5, "size": 1, "side": "yes", "trade_id": "t0001"}]}, f)
        return p

    def test_an_old_file_is_never_served_offline_and_is_named(self):
        p = self._legacy()
        with self.assertRaises(FileNotFoundError) as cm:
            self.client(NoNetwork(), offline=True).kalshi_trades(TK)
        self.assertIn(p, str(cm.exception))
        self.assertIn("never used", str(cm.exception))

    def test_online_an_old_file_is_ignored_and_the_tape_fetched_whole(self):
        p = self._legacy()
        http = Pages(three_pages())
        self.assertEqual(len(self.client(http).kalshi_trades(TK)), 30)
        self.assertEqual(len(http.calls), 3)
        self.assertTrue(os.path.exists(p))                                    # left for the user to delete
        self.assertEqual(len(self.client(NoNetwork(), offline=True).kalshi_trades(TK)), 30)

    def test_a_file_of_an_older_schema_under_the_new_name_is_ignored(self):
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, _cache_key("kalshi", TK, None, None) + ".json"), "w", encoding="utf-8") as f:
            json.dump({"venue": "kalshi", "market": TK, "trades": []}, f)
        with self.assertRaises(FileNotFoundError):
            self.client(NoNetwork(), offline=True).kalshi_trades(TK)


class CacheHygieneTests(TapeCase):
    def test_writes_are_atomic_and_leave_no_temporary_files(self):
        http = Pages(three_pages())
        with self.assertRaises(IncompleteTape):
            self.client(http).kalshi_tape(TK, max_pages=2, checkpoint_every=1)
        self.client(http).kalshi_trades(TK)
        self.assertEqual([n for n in os.listdir(self.dir) if not n.endswith(".json")], [])
        self.assertEqual(len(os.listdir(self.dir)), 1)

    def test_shuffled_pages_give_the_same_tape(self):
        # The tape does not depend on the order prints arrive in inside the pages.
        base = self.client(Pages(three_pages())).kalshi_trades(TK)
        for seed in range(3):
            shutil.rmtree(self.dir)
            pages = three_pages()
            for p in pages.values():
                random.Random(seed).shuffle(p["trades"])
            self.assertEqual(self.client(Pages(pages)).kalshi_trades(TK), base)


class PolymarketBudgetTests(TapeCase):
    def test_a_used_up_polymarket_budget_raises_and_is_not_cached_complete(self):
        class Full:
            calls = 0

            def get(self, url, params=None, headers=None, raw=False):
                Full.calls += 1
                return [{"asset": "123", "price": 0.5, "size": 1, "side": "BUY", "timestamp": BASE + i, "transactionHash": f"h{Full.calls}-{i}"} for i in range(500)]
        with self.assertRaises(IncompleteTape):
            self.client(Full()).polymarket_trades("123", max_pages=2)
        self.assertEqual(Full.calls, 2)
        with self.assertRaises(IncompleteTape):
            self.client(NoNetwork(), offline=True).polymarket_trades("123")


if __name__ == "__main__":
    unittest.main()

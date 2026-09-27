"""Historical Kalshi trade tapes: completeness, resumption, deduplication and cache keys.

Adversarial cases for ``venues/trades.py`` (the research tape, not the live print poller in
``strategy/fastlane.py``), all offline: a scripted cursor-paginated fake of
``GET /markets/trades``, an injected clock and temporary cache directories.
"""
from __future__ import annotations

import io
import json
import math
import os
import random
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from arb_engine.venues.http import HttpError
from arb_engine.venues.trades import (SETTLE_S, ConflictingTrades, IncompleteTape, TapeError, Trade, TradesClient, _cache_key,
                                      _legacy_cache_key)

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

TK = "KXNFLGAME-26SEP27DENKC-KC"
BASE = 1_790_000_000.0
HI = BASE + 5_000.0             # a window end after every print below
NOW = BASE + 100_000.0          # the clock: the window closed long ago


def row(i, sec, price=0.50, count=1.0, ticker=TK, tid=None, frac=0.25):
    t = datetime.fromtimestamp(BASE + sec + frac, tz=timezone.utc)
    return {"trade_id": f"t{i:04d}" if tid is None else tid, "ticker": ticker, "created_time": t.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "yes_price_dollars": f"{price:.4f}", "count_fp": f"{count:.2f}", "taker_side": "yes"}


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
        self.now = NOW

    def client(self, http, offline=False, cache_dir=None):
        return TradesClient(http=http, cache_dir=cache_dir or self.dir, offline=offline, clock=lambda: self.now)

    def kt(self, http, lo=None, hi=HI, offline=False, **kw):
        return self.client(http, offline).kalshi_trades(TK, lo, hi, **kw)

    def doc(self, lo=None, hi=HI, market=TK, venue="kalshi"):
        with open(os.path.join(self.dir, _cache_key(venue, market, lo, hi) + ".json"), encoding="utf-8") as f:
            return json.load(f)

    def ids(self, trades):
        return [t.trade_id for t in trades]


class BudgetAndResumeTests(TapeCase):
    def test_a_used_up_budget_is_never_served_as_the_tape(self):
        http = Pages(three_pages())
        with self.assertRaises(IncompleteTape) as cm:
            self.kt(http, max_pages=2)
        self.assertEqual(len(http.calls), 2)
        self.assertIn("page budget 2", str(cm.exception))
        self.assertEqual(len(cm.exception.tape.trades), 20)             # what is known so far, flagged incomplete
        d = self.doc()
        self.assertEqual((d["complete"], d["next_cursor"], d["pages"]), (False, "c2", 2))
        with self.assertRaises(IncompleteTape):                           # offline: the partial file is not an answer
            self.kt(NoNetwork(), offline=True)

    def test_the_same_query_with_a_larger_budget_fetches_only_the_backlog(self):
        http = Pages(three_pages())
        with self.assertRaises(IncompleteTape):
            self.kt(http, max_pages=1)
        trades = self.kt(http, max_pages=50)
        self.assertEqual([c.get("cursor") for c in http.calls], [None, "c1", "c2"])   # resumed at c1, nothing refetched
        self.assertEqual(self.ids(trades), [f"t{i:04d}" for i in range(30)])
        self.assertEqual(self.doc()["complete"], True)
        self.assertEqual(self.kt(NoNetwork(), offline=True), trades)                    # complete: offline replay works

    def test_the_same_budget_again_keeps_progressing(self):
        http = Pages(three_pages())
        for _ in range(2):
            with self.assertRaises(IncompleteTape):
                self.kt(http, max_pages=1)
        self.assertEqual(len(self.kt(http, max_pages=1)), 30)
        self.assertEqual([c.get("cursor") for c in http.calls], [None, "c1", "c2"])

    def test_a_failed_page_keeps_progress_and_the_next_call_resumes(self):
        pages = three_pages()
        pages["c1"] = [HttpError(503, "https://x/markets/trades", "unavailable"), pages["c1"]]
        http = Pages(pages)
        with self.assertRaises(IncompleteTape) as cm:
            self.kt(http)
        self.assertIn("page request failed", str(cm.exception))
        self.assertEqual(self.doc()["next_cursor"], "c1")
        self.assertEqual(len(self.kt(http)), 30)
        self.assertEqual([c.get("cursor") for c in http.calls], [None, "c1", "c1", "c2"])
        shutil.rmtree(self.dir)                                           # a non-HTTP failure is kept the same way
        pages = three_pages()
        pages["c2"] = [ValueError("truncated JSON"), pages["c2"]]
        http = Pages(pages)
        with self.assertRaises(IncompleteTape):
            self.kt(http)
        self.assertEqual(len(self.kt(http)), 30)

    def test_a_restarted_process_resumes_from_its_last_checkpoint(self):
        class Killed(BaseException):
            """The process dies mid-fetch: no handler runs."""
        pages = three_pages()
        pages["c2"] = [Killed(), pages["c2"]]
        http = Pages(pages)
        with self.assertRaises(Killed):
            self.client(http).kalshi_tape(TK, None, HI, checkpoint_every=1)
        d = self.doc()
        self.assertEqual((d["complete"], d["next_cursor"], len(d["trades"])), (False, "c2", 20))
        trades = self.kt(http)                                             # a new process, same cache
        self.assertEqual(len(trades), 30)
        self.assertEqual([c.get("cursor") for c in http.calls], [None, "c1", "c2", "c2"])

    def test_a_crash_after_the_first_page_is_resumable(self):
        class Killed(BaseException):
            pass
        pages = three_pages()
        pages["c1"] = [Killed(), pages["c1"]]
        http = Pages(pages)
        with self.assertRaises(Killed):
            self.kt(http)                                                  # default checkpointing: page 1 is kept
        self.assertEqual(self.doc()["next_cursor"], "c1")

    def test_a_saved_cursor_the_venue_no_longer_knows_starts_a_fresh_pass(self):
        pages = three_pages()
        http = Pages(pages)
        with self.assertRaises(IncompleteTape):
            self.kt(http, max_pages=1)
        pages["c1"] = HttpError(410, "https://x/markets/trades", "cursor expired")
        pages[None] = {"trades": [row(i, i) for i in range(29, -1, -1)], "cursor": ""}   # one page now holds everything
        tape = self.client(http).kalshi_tape(TK, None, HI)
        self.assertEqual(([c.get("cursor") for c in http.calls], tape.restarts), ([None, "c1", None], 1))
        self.assertEqual(self.ids(tape.trades), [f"t{i:04d}" for i in range(30)])

    def test_throttling_or_auth_errors_on_a_resume_keep_the_saved_cursor(self):
        for status in (429, 401, 403, 408):
            shutil.rmtree(self.dir, ignore_errors=True)
            pages = three_pages()
            http = Pages(pages)
            with self.assertRaises(IncompleteTape):
                self.kt(http, max_pages=1)
            pages["c1"] = [HttpError(status, "https://x/markets/trades", "no"), pages["c1"]]
            with self.assertRaises(IncompleteTape, msg=status) as cm:
                self.kt(http)
            self.assertEqual((self.doc()["next_cursor"], cm.exception.tape.restarts), ("c1", 0), status)   # not a fresh pass
            self.assertEqual(len(self.kt(http)), 30, status)

    def test_a_repeated_cursor_is_never_followed_and_never_complete(self):
        pages = three_pages()
        pages["c2"] = {"trades": [row(i, i) for i in range(9, -1, -1)], "cursor": "c1"}   # loops back
        http = Pages(pages)
        with self.assertRaises(IncompleteTape) as cm:
            self.kt(http)
        self.assertIn("repeated cursor", str(cm.exception))
        self.assertEqual(len(http.calls), 3)
        d = self.doc()
        self.assertEqual((d["complete"], d["next_cursor"]), (False, None))  # next call starts again from the top
        pages["c2"] = {"trades": [row(i, i) for i in range(9, -1, -1)], "cursor": ""}
        self.assertEqual(len(self.kt(http)), 30)
        self.assertEqual(http.calls[3].get("cursor"), None)


class MalformedPageTests(TapeCase):
    """An answer that is not a well-formed page is a failed page - never the last page."""

    def test_empty_error_or_cursorless_answers_are_failed_pages(self):
        for bad in ({}, None, [], {"error": {"code": "internal"}}, {"trades": [row(1, 1)]}, {"trades": 5, "cursor": ""},
                    {"trades": [row(1, 1)], "cursor": None}, {"trades": [row(1, 1)], "cursor": 0}, "oops"):
            shutil.rmtree(self.dir, ignore_errors=True)
            pages = three_pages()
            pages["c1"] = [bad, pages["c1"]]
            http = Pages(pages)
            with self.assertRaises(IncompleteTape, msg=repr(bad)) as cm:
                self.kt(http)
            self.assertIn("malformed page", str(cm.exception), repr(bad))
            self.assertEqual(self.doc()["next_cursor"], "c1", repr(bad))     # resumes at the failed page
            with self.assertRaises(IncompleteTape):
                self.kt(NoNetwork(), offline=True)
            self.assertEqual(len(self.kt(http)), 30, repr(bad))

    def test_the_real_http_client_empty_200_is_not_the_end_of_the_tape(self):
        from arb_engine.venues.http import HttpClient

        class Empty(HttpClient):
            def _request_retrying(self, method, url, hdrs, data, raw):   # an empty 200 body, as the client parses it
                return {}
        with self.assertRaises(IncompleteTape):
            self.kt(Empty())


class OpenWindowTests(TapeCase):
    def test_an_open_ended_or_still_open_window_is_refused_before_any_request(self):
        for hi, now, words in ((None, NOW, "no end"), (HI, HI - 100, "still open"), (HI, HI + SETTLE_S - 1, "still open")):
            shutil.rmtree(self.dir, ignore_errors=True)
            self.now = now
            http = Pages(three_pages())
            with self.assertRaises(IncompleteTape, msg=(hi, now)) as cm:
                self.client(http).kalshi_trades(TK, None, hi)
            self.assertIn(words, str(cm.exception))
            self.assertIn("nothing was fetched", str(cm.exception))
            with self.assertRaises(IncompleteTape, msg=(hi, now)):
                self.client(NoNetwork()).polymarket_trades("123", None, hi)
            self.assertEqual(http.calls, [])
            self.assertFalse(os.path.isdir(self.dir) and os.listdir(self.dir))     # nothing cached either
        self.now = HI + SETTLE_S                                            # closed from exactly the margin on
        self.assertEqual(len(self.kt(Pages(three_pages()))), 30)

    def test_a_saved_pass_that_cannot_end_complete_is_not_resumed(self):
        # (a) A pass begun 10 s after the window's end by a client with no margin: by the
        # default margin it began while the window was open, so it starts over.
        pages = three_pages()
        http = Pages(pages)
        self.now = HI + 10
        eager = TradesClient(http=http, cache_dir=self.dir, clock=lambda: self.now, settle_s=0)
        with self.assertRaises(IncompleteTape):
            eager.kalshi_trades(TK, None, HI, max_pages=1)
        self.assertEqual(self.doc()["next_cursor"], "c1")
        pages[None] = {"trades": [row(i, i) for i in range(31, 19, -1)], "cursor": "c1"}   # two prints published late
        self.now = NOW
        trades = self.kt(http)
        self.assertEqual([c.get("cursor") for c in http.calls], [None, None, "c1", "c2"])   # a fresh pass, not a resume at c1
        self.assertEqual(len(trades), 32)
        self.assertIn("t0031", self.ids(trades))
        # (b) A saved pass stamped later than the clock (a clock that ran ahead, an edited file).
        shutil.rmtree(self.dir)
        http = Pages(three_pages())
        with self.assertRaises(IncompleteTape):
            self.kt(http, max_pages=1)
        d = self.doc()
        d["pass_started_at"] = NOW + 3600
        Path(self.dir, _cache_key("kalshi", TK, None, HI) + ".json").write_text(json.dumps(d))
        self.assertEqual(len(self.kt(http)), 30)
        self.assertEqual([c.get("cursor") for c in http.calls], [None, None, "c1", "c2"])

    def test_a_complete_file_is_checked_again_with_the_readers_margin(self):
        self.now = HI + 1
        TradesClient(http=Pages(three_pages()), cache_dir=self.dir, clock=lambda: self.now, settle_s=0).kalshi_trades(TK, None, HI)
        self.assertEqual(self.doc()["complete"], True)
        self.now = NOW
        with self.assertRaises(FileNotFoundError) as cm:
            self.kt(NoNetwork(), offline=True)
        self.assertIn("fails its checks: marked complete, but the window was still open", str(cm.exception))
        http = Pages(three_pages())
        self.assertEqual(len(self.kt(http)), 30)                           # online: fetched again under the margin
        self.assertEqual(len(http.calls), 3)
        self.assertEqual(len(self.kt(NoNetwork(), offline=True)), 30)

    def test_a_bad_margin_timeout_or_clock_is_refused(self):
        for bad in (float("nan"), float("inf"), -1.0, True, "60", None):
            with self.assertRaises(ValueError, msg=bad):
                TradesClient(http=NoNetwork(), cache_dir=self.dir, settle_s=bad)
            with self.assertRaises(ValueError, msg=bad):
                TradesClient(http=NoNetwork(), cache_dir=self.dir, lock_timeout_s=bad)
        for t in (float("nan"), float("inf"), None, str(NOW)):
            with self.assertRaises(ValueError, msg=t):
                TradesClient(http=NoNetwork(), cache_dir=self.dir, clock=lambda t=t: t).kalshi_trades(TK, None, HI)


class DedupeTests(TapeCase):
    def test_overlapping_pages_and_same_second_prints_keep_one_copy_each(self):
        same = [row(100, 10, frac=0.1), row(101, 10, frac=0.1, count=3.0), row(102, 10, frac=0.9)]
        pages = {None: {"trades": [row(i, i) for i in range(20, 10, -1)] + [same[2], same[1]], "cursor": "c1"},
                 "c1": {"trades": [same[1], same[0]] + [row(i, i) for i in range(9, -1, -1)], "cursor": ""}}
        tape = self.client(Pages(pages)).kalshi_tape(TK, None, HI)
        ids = self.ids(tape.trades)
        self.assertEqual(len(ids), len(set(ids)))
        sent = {r["trade_id"] for p in pages.values() for r in p["trades"]}
        self.assertEqual(sorted(ids), sorted(sent))
        self.assertEqual(tape.duplicates, 1)
        self.assertEqual([t.ts for t in tape.trades], sorted(t.ts for t in tape.trades))

    def test_one_trade_id_with_two_payloads_fails_then_the_next_consistent_pass_heals(self):
        pages = three_pages()
        pages["c1"] = {"trades": [row(25, 25, price=0.61)] + [row(i, i) for i in range(19, 9, -1)], "cursor": "c2"}
        with self.assertRaises(ConflictingTrades) as cm:
            self.kt(Pages(pages))
        self.assertIn("t0025", str(cm.exception))
        self.assertIsInstance(cm.exception, TapeError)
        self.assertEqual(self.doc()["complete"], False)
        with self.assertRaises(IncompleteTape):
            self.kt(NoNetwork(), offline=True)
        self.assertEqual(len(self.kt(Pages(three_pages()))), 30)          # a consistent venue: not wedged forever

    def test_a_fresh_pass_is_judged_alone(self):
        # The pass ends early (a repeated cursor) holding t0025 at 0.50; the venue now reports
        # 0.61 on every page: the new pass is the tape, not a conflict with the old one.
        pages = three_pages()
        pages["c2"] = {"trades": [row(i, i) for i in range(9, -1, -1)], "cursor": "c1"}
        with self.assertRaises(IncompleteTape):
            self.kt(Pages(pages))
        revised = three_pages()
        revised[None]["trades"] = [row(i, i, price=0.61 if i == 25 else 0.50) for i in range(29, 19, -1)]
        self.assertEqual({t.trade_id: t.price for t in self.kt(Pages(revised))}["t0025"], 0.61)
        # After a conflict inside one pass, a failed first page, then a retry: one clean pass.
        shutil.rmtree(self.dir)
        bad = three_pages()
        bad["c1"]["trades"].insert(0, row(25, 25, price=0.61))
        with self.assertRaises(ConflictingTrades):
            self.kt(Pages(bad))
        flaky = three_pages()
        flaky[None] = [HttpError(503, "https://x/markets/trades", "busy"), flaky[None]]
        http = Pages(flaky)
        with self.assertRaises(IncompleteTape):
            self.kt(http)
        self.assertEqual(len(self.kt(http)), 30)

    def test_a_print_the_venue_no_longer_reports_does_not_survive_a_fresh_pass(self):
        pages = three_pages()
        pages["c2"] = {"trades": [row(i, i) for i in range(9, -1, -1)], "cursor": "c1"}   # repeated cursor: the pass ends
        with self.assertRaises(IncompleteTape):
            self.kt(Pages(pages))
        clean = three_pages()
        clean[None]["trades"] = [r for r in clean[None]["trades"] if r["trade_id"] != "t0025"]
        trades = self.kt(Pages(clean))
        self.assertNotIn("t0025", self.ids(trades))
        self.assertEqual(len(trades), 29)

    def test_a_conflict_just_outside_the_window_is_still_caught(self):
        lo, hi = BASE + 100.0, BASE + 200.5
        pages = {None: {"trades": [row(1, 200, frac=0.4), row(2, 150)], "cursor": "c1"},
                 "c1": {"trades": [row(1, 200, frac=0.6, price=0.61), row(3, 120)], "cursor": ""}}   # t0001 moved past the edge
        with self.assertRaises(ConflictingTrades):
            self.client(Pages(pages)).kalshi_trades(TK, lo, hi)

    def test_unreadable_or_foreign_rows_keep_the_tape_incomplete(self):
        for bad in ({"trade_id": ""}, {"created_time": "garbage"}, {"yes_price_dollars": "NaN", "yes_price": None},
                    {"count_fp": "inf", "count": None}, {"ticker": "KXNFLGAME-26SEP27DENKC-DEN"}):
            shutil.rmtree(self.dir, ignore_errors=True)
            pages = three_pages()
            broken = row(16, 16)
            broken.update(bad)
            pages["c1"]["trades"][3] = broken
            with self.assertRaises(IncompleteTape, msg=bad) as cm:
                self.kt(Pages(pages))
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
        self.kt(Pages(three_pages()), BASE, BASE + 50)
        path = os.path.join(self.dir, _cache_key("kalshi", TK, BASE, BASE + 50) + ".json")
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        d["query"]["max_ts"] = BASE + 60
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f)
        with self.assertRaises(FileNotFoundError):
            self.kt(NoNetwork(), BASE, BASE + 50, offline=True)

    def test_bad_windows_and_budgets_are_refused(self):
        for lo, hi in ((float("nan"), HI), (None, float("inf")), (float("-inf"), 5.0), ("nan", HI), (10.0, 5.0), (True, HI), (None, False)):
            with self.assertRaises(ValueError, msg=(lo, hi)):
                self.client(NoNetwork()).kalshi_trades(TK, lo, hi)
        for mp in (0, -1, 1.5, "2", True, None):
            with self.assertRaises(ValueError, msg=mp):
                self.client(NoNetwork()).kalshi_trades(TK, None, HI, max_pages=mp)
            with self.assertRaises(ValueError, msg=mp):
                self.client(NoNetwork()).polymarket_trades("123", None, HI, max_pages=mp)
        # -0.0 and 0.0 are one window (one cache file).
        self.kt(Pages(three_pages()), 0.0, HI)
        self.assertEqual(len(self.kt(NoNetwork(), -0.0, HI, offline=True)), 30)


class CacheFileTests(TapeCase):
    def _complete_then_edit(self, edit):
        self.kt(Pages(three_pages()))
        path = os.path.join(self.dir, _cache_key("kalshi", TK, None, HI) + ".json")
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        edit(d)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(d))                     # json.dumps writes NaN / Infinity literals when present
        return path

    def test_a_hand_edited_or_damaged_file_is_never_used(self):
        edits = {
            "complete as a string": lambda d: d.update(complete="false"),
            "complete as 1": lambda d: d.update(complete=1),
            "complete with a cursor left": lambda d: d.update(next_cursor="c9"),
            "complete with rejected rows": lambda d: d.update(rejected=3),
            "a trade outside the window": lambda d: d["trades"].append(dict(d["trades"][0], ts=HI + 1, trade_id="x1")),
            "a trade of another ticker": lambda d: d["trades"].append(dict(d["trades"][0], market="OTHER", trade_id="x2")),
            "a duplicated trade id": lambda d: d["trades"].append(dict(d["trades"][0])),
            "a NaN price": lambda d: d["trades"][0].update(price=float("nan")),
            "an Infinity time": lambda d: d["trades"][0].update(ts=float("inf")),
            "pages as NaN": lambda d: d.update(pages=float("nan")),
            "pages as text": lambda d: d.update(pages="3 pages"),
            "seen_cursors not a list": lambda d: d.update(seen_cursors=5),
            "a price as text": lambda d: d["trades"][0].update(price="0.5"),
            "a price above 1": lambda d: d["trades"][0].update(price=1.5),
            "a negative size": lambda d: d["trades"][0].update(size=-1),
            "a zero Kalshi size": lambda d: d["trades"][0].update(size=0),
            "a side that is not text": lambda d: d["trades"][0].update(side=5),
            "a trade id that is not text": lambda d: d["trades"][0].update(trade_id=7),
            "an unknown field in a print": lambda d: d["trades"][0].update(fee=0.01),
            "pass_started_at as text": lambda d: d.update(pass_started_at=str(NOW)),
            "complete, but no pass start": lambda d: d.update(pass_started_at=0.0),
            "complete, begun inside the margin": lambda d: d.update(pass_started_at=HI + SETTLE_S - 1),
        }
        for name, edit in edits.items():
            shutil.rmtree(self.dir, ignore_errors=True)
            self._complete_then_edit(edit)
            with self.assertRaises(FileNotFoundError, msg=name) as cm:
                self.kt(NoNetwork(), offline=True)
            self.assertIn("fails its checks", str(cm.exception), name)
            self.assertEqual(len(self.kt(Pages(three_pages()))), 30, name)   # online: refetched and replaced

    def test_store_complete_checks_what_it_is_given(self):
        c = self.client(NoNetwork())
        good = [Trade("kalshi", TK, BASE + 1, .5, 1, "yes", "a"), Trade("kalshi", TK, BASE + 2, .5, 1, "yes", "b")]
        bad = {"duplicate id": good + [Trade("kalshi", TK, BASE + 3, .5, 1, "yes", "a")],
               "outside the window": good + [Trade("kalshi", TK, HI + 50, .5, 1, "yes", "c")],
               "another ticker": good + [Trade("kalshi", "OTHER", BASE + 3, .5, 1, "yes", "d")],
               "another venue": good + [Trade("polymarket", TK, BASE + 3, .5, 1, "BUY", "e")],
               "a NaN price": good + [Trade("kalshi", TK, BASE + 3, float("nan"), 1, "yes", "f")],
               "no trade id": good + [Trade("kalshi", TK, BASE + 3, .5, 1, "yes", "")]}
        for name, trades in bad.items():
            with self.assertRaises(ValueError, msg=name):
                c.store_complete("kalshi", TK, BASE, HI, trades)
        for lo, hi in ((None, None), (BASE, NOW - 10)):                     # open-ended, or still open now
            with self.assertRaises(ValueError, msg=(lo, hi)):
                c.store_complete("kalshi", TK, lo, hi, good)
        with self.assertRaises(ValueError):
            c.store_complete("kalshi-demo", TK, BASE, HI, good)
        tape = c.store_complete("kalshi", TK, BASE, HI, list(reversed(good)))
        self.assertEqual(self.ids(tape.trades), ["a", "b"])                  # stored sorted
        self.assertEqual((tape.pass_started_at, type(tape.trades[0].size)), (NOW, float))
        self.assertEqual(self.kt(NoNetwork(), BASE, HI, offline=True), tape.trades)

    def test_an_incomplete_state_never_replaces_a_complete_file(self):
        c = self.client(Pages(three_pages()))
        done = c.kalshi_tape(TK, None, HI)
        stale = done.__class__(**{**done.__dict__, "complete": False, "reason": "late writer", "trades": done.trades[:3], "next_cursor": "c1"})
        c._store(stale)
        self.assertEqual(len(self.kt(NoNetwork(), offline=True)), 30)

    def test_an_unwritable_cache_says_progress_was_not_saved(self):
        blocker = os.path.join(self.dir, "not-a-dir")
        Path(blocker).write_text("x")
        c = self.client(Pages(three_pages()), cache_dir=os.path.join(blocker, "cache"))
        with self.assertRaises(IncompleteTape) as cm:
            c.kalshi_trades(TK, None, HI, max_pages=1)
        self.assertIn("could NOT be saved", str(cm.exception))
        with self.assertLogs("arb_engine.venues.trades", "WARNING") as logs:
            self.assertEqual(len(c.kalshi_trades(TK, None, HI)), 30)         # the data itself is still whole ...
        self.assertIn("was not cached", logs.output[0])                      # ... and not silently uncached

    def test_a_read_only_lock_file_does_not_block_saves(self):
        http = Pages(three_pages())
        with self.assertRaises(IncompleteTape):
            self.kt(http, max_pages=1)
        lock = os.path.join(self.dir, "." + _cache_key("kalshi", TK, None, HI) + ".json.lock")
        os.chmod(lock, 0o444)
        self.addCleanup(os.chmod, lock, 0o644)
        self.assertEqual(len(self.kt(http)), 30)
        self.assertEqual(self.doc()["complete"], True)

    @unittest.skipIf(fcntl is None, "no fcntl")
    def test_a_writer_stuck_holding_the_lock_does_not_hang_a_fetch(self):
        lock = os.path.join(self.dir, "." + _cache_key("kalshi", TK, None, HI) + ".json.lock")
        fd = os.open(lock, os.O_RDONLY | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)                                      # another writer, stopped mid-save
        c = TradesClient(http=Pages(three_pages()), cache_dir=self.dir, clock=lambda: self.now, lock_timeout_s=0.05)
        with self.assertLogs("arb_engine.venues.trades", "WARNING") as logs:
            self.assertEqual(len(c.kalshi_trades(TK, None, HI)), 30)         # the tape, uncached, and a warning
        self.assertIn("TimeoutError", c.last_store_error)
        self.assertIn("was not cached", logs.output[0])
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertEqual(len(self.kt(Pages(three_pages()))), 30)
        self.assertEqual(len(self.kt(NoNetwork(), offline=True)), 30)

    def test_writes_are_atomic_and_leave_no_temporary_files(self):
        http = Pages(three_pages())
        with self.assertRaises(IncompleteTape):
            self.client(http).kalshi_tape(TK, None, HI, max_pages=2, checkpoint_every=1)
        self.kt(http)
        names = os.listdir(self.dir)
        self.assertEqual([x for x in names if x.startswith(".tape-")], [])                 # no temporary file left
        self.assertEqual([x for x in names if not x.startswith(".")], [_cache_key("kalshi", TK, None, HI) + ".json"])

    def test_shuffled_pages_give_the_same_tape(self):
        base = self.kt(Pages(three_pages()))
        for seed in range(3):
            shutil.rmtree(self.dir)
            pages = three_pages()
            for p in pages.values():
                random.Random(seed).shuffle(p["trades"])
            self.assertEqual(self.kt(Pages(pages)), base)


class LegacyCacheTests(TapeCase):
    def _legacy(self, lo=None, hi=HI):
        os.makedirs(self.dir, exist_ok=True)
        p = os.path.join(self.dir, _legacy_cache_key("kalshi", TK, lo, hi) + ".json")
        with open(p, "w", encoding="utf-8") as f:     # the old format: no schema, no completeness, a truncated tape
            json.dump({"venue": "kalshi", "market": TK, "min_ts": lo, "max_ts": hi, "fetched_at": 0,
                       "trades": [{"venue": "kalshi", "market": TK, "ts": BASE + 1, "price": .5, "size": 1, "side": "yes", "trade_id": "t0001"}]}, f)
        return p

    def test_an_old_file_is_never_served_offline_and_is_named(self):
        p = self._legacy()
        with self.assertRaises(FileNotFoundError) as cm:
            self.kt(NoNetwork(), offline=True)
        self.assertIn(p, str(cm.exception))
        self.assertIn("never used", str(cm.exception))

    def test_online_an_old_file_is_ignored_and_the_tape_fetched_whole(self):
        p = self._legacy()
        http = Pages(three_pages())
        self.assertEqual(len(self.kt(http)), 30)
        self.assertEqual(len(http.calls), 3)
        self.assertTrue(os.path.exists(p))                                    # left for the migration script
        self.assertEqual(len(self.kt(NoNetwork(), offline=True)), 30)

    def test_a_schema_2_file_is_never_used_and_is_named(self):
        self.kt(Pages(three_pages()))
        current = os.path.join(self.dir, _cache_key("kalshi", TK, None, HI) + ".json")
        d = self.doc()
        d["schema"] = d["query"]["schema"] = 2                            # as the first two rounds wrote it
        old = os.path.join(self.dir, _cache_key("kalshi", TK, None, HI, schema=2) + ".json")
        Path(old).write_text(json.dumps(d))
        os.unlink(current)
        with self.assertRaises(FileNotFoundError) as cm:
            self.kt(NoNetwork(), offline=True)
        self.assertIn(old, str(cm.exception))
        self.assertIn("never used", str(cm.exception))
        http = Pages(three_pages())
        self.assertEqual(len(self.kt(http)), 30)
        self.assertEqual(len(http.calls), 3)

    def test_the_migration_script_moves_only_unusable_files_one_at_a_time(self):
        from scripts import migrate_trade_cache as mig

        self.kt(Pages(three_pages()))                                         # one current tape ...
        with self.assertRaises(IncompleteTape):
            self.kt(Pages(three_pages()), BASE, BASE + 50, max_pages=1)       # ... and one resumable partial one
        keep = sorted([_cache_key("kalshi", TK, None, HI) + ".json", _cache_key("kalshi", TK, BASE, BASE + 50) + ".json"])
        doc = self.doc()
        old = self._legacy()
        Path(self.dir, _cache_key("kalshi", TK, None, HI, schema=2) + ".json").write_text(
            json.dumps(dict(doc, schema=2, query=dict(doc["query"], schema=2))))
        dup = dict(doc, query=dict(doc["query"], max_ts=HI - 1), trades=doc["trades"] + doc["trades"][:1])
        Path(self.dir, _cache_key("kalshi", TK, None, HI - 1) + ".json").write_text(json.dumps(dup))    # current schema, a check fails
        Path(self.dir, "kalshi-renamed.json").write_text(json.dumps(doc))  # a good tape under another name
        Path(self.dir, "kalshi-X-1-2.json").write_text('{"venue": "kalshi", "tra')   # truncated by the old non-atomic writer
        Path(self.dir, "number.json").write_text("5")
        Path(self.dir, "notes.txt").write_text("keep me")
        dest = os.path.join(self.dir, "legacy")
        os.makedirs(dest)
        Path(dest, os.path.basename(old)).write_text("{}")                   # a name already taken there
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(mig.main(["--cache-dir", self.dir, "--to", dest]), 0)   # dry run
        for why in ("old format", "schema 2", "fails checks: kalshi trade id", "fails checks: the file name", "unreadable", "not an object"):
            self.assertIn(f"would move ({why}", out.getvalue())
        self.assertIn("2 current", out.getvalue())
        self.assertTrue(os.path.exists(old))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(mig.main(["--cache-dir", self.dir, "--to", self.dir + "/.", "--apply"]), 2)   # never into itself
        self.assertTrue(os.path.exists(old))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(mig.main(["--cache-dir", self.dir, "--to", dest, "--apply"]), 0)
        self.assertEqual(sorted(n for n in os.listdir(self.dir) if n.endswith(".json")), keep)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "notes.txt")))
        self.assertEqual(len(os.listdir(dest)), 7)                          # six moved, one renamed beside the old one
        self.assertEqual(len(self.kt(NoNetwork(), offline=True)), 30)


class PolymarketTests(TapeCase):
    class Full:
        def __init__(self, rows=500):
            self.calls, self.rows = 0, rows

        def get(self, url, params=None, headers=None, raw=False):
            self.calls += 1
            return [{"asset": "123", "price": 0.5, "size": 1, "side": "BUY", "timestamp": BASE + i, "transactionHash": f"h{self.calls}-{i}"}
                    for i in range(self.rows)]

    def test_a_used_up_budget_raises_and_is_not_cached_complete(self):
        full = self.Full()
        with self.assertRaises(IncompleteTape):
            self.client(full).polymarket_trades("123", None, HI, max_pages=2)
        self.assertEqual(full.calls, 2)
        with self.assertRaises(IncompleteTape):
            self.client(NoNetwork(), offline=True).polymarket_trades("123", None, HI)

    def test_the_condition_id_is_part_of_the_cache_key(self):
        self.assertEqual(self.client(self.Full(rows=0)).polymarket_trades("123", None, HI, condition_id="0xwrong"), [])
        seven = self.Full(rows=7)
        self.assertEqual(len(self.client(seven).polymarket_trades("123", None, HI, condition_id="0xright")), 7)
        self.assertEqual(seven.calls, 1)                                   # not answered by the wrong id's empty tape
        with self.assertRaises(FileNotFoundError):
            self.client(NoNetwork(), offline=True).polymarket_trades("123", None, HI)

    def test_a_malformed_page_is_not_the_end(self):
        class Err:
            def get(self, url, params=None, headers=None, raw=False):
                return {"error": "rate limited"}
        with self.assertRaises(IncompleteTape) as cm:
            self.client(Err()).polymarket_trades("123", None, HI)
        self.assertIn("malformed page", str(cm.exception))


class EventStudyCliTests(TapeCase):
    def test_missing_or_incomplete_tapes_exit_with_the_reason(self):
        import argparse

        from arb_engine.cli_plugins import record_flags

        fixture = Path(__file__).resolve().parent / "fixtures" / "trades" / "replay_rows_trim.json"
        p = argparse.ArgumentParser()
        handlers = record_flags.register(p.add_subparsers(dest="cmd"), {})
        base = ["event-study", "--rows", str(fixture), "--limit", "1", "--cache-dir", self.dir, "--trades", "kalshi=KXNFLGAME-DETBUF-DET"]
        with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()):
            handlers["event-study"](p.parse_args(base + ["--offline"]), {})
        self.assertIn("no usable cached trades", str(cm.exception))
        a = p.parse_args(base + ["--max-pages", "3"])
        self.assertEqual(a.max_pages, 3)
        for bad in ("0", "-1", "two"):
            err = io.StringIO()
            with self.assertRaises(SystemExit) as cm, redirect_stderr(err):
                p.parse_args(base + ["--max-pages", bad])
            self.assertEqual(cm.exception.code, 2, bad)                     # a usage error, not a traceback
            self.assertIn("--max-pages", err.getvalue())


if __name__ == "__main__":
    unittest.main()

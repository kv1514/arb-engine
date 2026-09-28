"""Reproduction 3: the Kalshi public-print poller (``FastLane._poll_one_ticker``).

Run: ``python3 -B audit/repro_trade_poller.py`` from the worktree root.

Checks, all offline against a scripted fake client and a temp-dir ``Store``:

  A. a cursor that never advances               -> must terminate and bound its buffer
  B. a malformed page (no ``trades`` list)      -> must not be read as "no trades"
  C. a page whose trades are not a list         -> must not crash the whole lane silently
  D. overlapping pages / same-second prints     -> exactly one row per trade_id, none skipped
  E. restart from the durable watermark         -> no loss, no duplication
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arb_engine.store import Store  # noqa: E402
from arb_engine.strategy.fastlane import FastLane  # noqa: E402
from audit.harness import KEY, kalshi_quote, tmp_path  # noqa: E402


def lane(client, clock=None):
    ln = FastLane(kalshi_client=client, clock=clock or (lambda: 1_780_000_000.0))
    ln.seed({KEY: {"kalshi": [kalshi_quote()]}})
    return ln


def trade(tid, ts, price=0.55, count=10):
    return {"trade_id": tid, "ticker": "KXT-KC", "created_time": ts,
            "yes_price": int(price * 100), "count": count, "taker_side": "yes"}


# ---------------------------------------------------------------- A. cursor that repeats
def case_cursor_loop() -> list[str]:
    calls = {"n": 0}

    class LoopClient:
        def get(self, path, params=None):
            calls["n"] += 1
            # The exchange keeps handing back the same cursor and the same page.
            return {"trades": [trade("loop-1", 1_780_000_000)], "cursor": "SAME"}

    ln = lane(LoopClient())
    store = Store(tmp_path("loop.db"))
    sizes = []
    for _ in range(4):
        ln.poll_trades(store, max_pages=5, cadence_s=0.0)
        sizes.append(len(ln._trade_pending.get("KXT-KC", [])))
    rows = store.conn.execute("SELECT COUNT(*) FROM trade_prints").fetchone()[0]
    store.close()

    print("A. a cursor that never advances")
    print(f"   HTTP pages fetched over 4 polls   : {calls['n']}")
    print(f"   buffered trades after each poll   : {sizes}")
    print(f"   rows recorded                     : {rows}")
    print(f"   backlog flag                      : {ln.trade_poll_status()['KXT-KC']['backlog']}")
    print(f"   errors reported                   : {ln.trade_errors}")
    fails = []
    if sizes == sorted(sizes) and sizes[-1] > sizes[0]:
        fails.append("A) the pending buffer grows without bound on a repeating cursor")
    if not ln.trade_errors:
        fails.append("A) a non-advancing cursor is never reported as a failed walk")
    return fails


# ---------------------------------------------------------------- B/C. malformed pages
def case_malformed() -> list[str]:
    class NoTradesKey:
        def get(self, path, params=None):
            return {"cursor": ""}          # a page without its 'trades' list

    ln = lane(NoTradesKey())
    store = Store(tmp_path("malformed.db"))
    before = store.latest_trade_ts("KXT-KC")
    ln.trade_cursor["KXT-KC"] = 1_779_000_000
    ln.poll_trades(store, max_pages=3, cadence_s=0.0)
    after_cursor = ln.trade_cursor.get("KXT-KC")
    store.close()

    print()
    print("B. a page that carries no 'trades' list")
    print(f"   errors reported                   : {ln.trade_errors}")
    print(f"   cursor before / after             : 1779000000 / {after_cursor}")
    print(f"   watermark in the db               : {before}")
    fails = []
    if not ln.trade_errors:
        fails.append("B) a page without its 'trades' list is read as an empty, complete answer")

    class TradesNotAList:
        def get(self, path, params=None):
            return {"trades": {"oops": 1}, "cursor": ""}

    ln2 = lane(TradesNotAList())
    store2 = Store(tmp_path("malformed2.db"))
    ln2.poll_trades(store2, max_pages=3, cadence_s=0.0)
    rows = store2.conn.execute("SELECT COUNT(*) FROM trade_prints").fetchone()[0]
    store2.close()
    print()
    print("C. a page whose 'trades' is not a list")
    print(f"   errors reported                   : {ln2.trade_errors}")
    print(f"   rows recorded                     : {rows}")
    if rows:
        fails.append("C) a malformed page produced rows")

    # B2 - the loss case. Kalshi pages newest-first. If page 2 comes back malformed the walk
    # is declared complete, the *newest* buffered prints are recorded and the watermark jumps
    # to them, so every older print the walk had not reached yet is skipped for good.
    class TruncatedByMalformed:
        def __init__(self):
            self.i = 0

        def get(self, path, params=None):
            self.i += 1
            if self.i == 1:
                return {"trades": [trade("new-2", 1_780_000_020), trade("new-1", 1_780_000_019)],
                        "cursor": "page2"}
            return {"cursor": None}        # the answer carries no 'trades' list at all

    ln3 = lane(TruncatedByMalformed())
    store3 = Store(tmp_path("malformed3.db"))
    ln3.poll_trades(store3, max_pages=5, cadence_s=0.0)
    watermark = store3.latest_trade_ts("KXT-KC")
    store3.close()
    print()
    print("B2. a malformed page in the middle of a newest-first walk")
    print(f"   errors reported                   : {ln3.trade_errors}")
    print(f"   backlog kept for the rest of walk : {ln3.trade_poll_status()['KXT-KC']['backlog']}")
    print(f"   durable watermark now             : {watermark}  (the walk never reached the "
          f"older prints behind cursor 'page2')")
    if not ln3.trade_errors:
        fails.append("B2) a truncated walk was declared complete and the watermark advanced "
                     "past prints that were never fetched")
    return fails


# ---------------------------------------------------------------- D. overlap + same second
def case_overlap_same_second() -> list[str]:
    pages = [
        {"trades": [trade("t3", 1_780_000_010), trade("t2", 1_780_000_010)], "cursor": "p2"},
        # page 2 overlaps page 1 (a print arrived between the two requests) and reaches back
        {"trades": [trade("t2", 1_780_000_010), trade("t1", 1_780_000_009)], "cursor": ""},
        # the next poll: another print stamped in the *same* second as the cursor
        {"trades": [trade("t4", 1_780_000_010), trade("t3", 1_780_000_010),
                    trade("t2", 1_780_000_010)], "cursor": ""},
    ]
    seen_params = []

    class PagedClient:
        def __init__(self):
            self.i = 0

        def get(self, path, params=None):
            seen_params.append(dict(params or {}))
            page = pages[min(self.i, len(pages) - 1)]
            self.i += 1
            return page

    ln = lane(PagedClient())
    store = Store(tmp_path("overlap.db"))
    n1 = ln.poll_trades(store, max_pages=5, cadence_s=0.0)
    cursor_after = ln.trade_cursor["KXT-KC"]
    n2 = ln.poll_trades(store, max_pages=5, cadence_s=0.0)
    ids = sorted(r[0] for r in store.conn.execute("SELECT trade_id FROM trade_prints"))
    store.close()

    print()
    print("D. overlapping pages and same-second arrivals")
    print(f"   min_ts sent per request           : {[p.get('min_ts') for p in seen_params]}")
    print(f"   inserted on poll 1 / poll 2       : {n1} / {n2}")
    print(f"   cursor after poll 1               : {cursor_after}")
    print(f"   trade ids stored                  : {ids}")
    fails = []
    if ids != ["t1", "t2", "t3", "t4"]:
        fails.append(f"D) expected t1..t4 exactly once, got {ids}")
    if cursor_after != 1_780_000_010:
        fails.append(f"D) cursor is not the inclusive newest second ({cursor_after})")
    return fails


# ---------------------------------------------------------------- E. restart
def case_restart() -> list[str]:
    path = tmp_path("restart.db")
    store = Store(path)

    class Client:
        def __init__(self, page):
            self.page = page
            self.params = []

        def get(self, p, params=None):
            self.params.append(dict(params or {}))
            return self.page

    c1 = Client({"trades": [trade("t2", 1_780_000_010), trade("t1", 1_780_000_009)], "cursor": ""})
    ln1 = lane(c1)
    ln1.poll_trades(store, max_pages=5, cadence_s=0.0)
    store.close()

    # a fresh process: a new FastLane, a new Store on the same file
    store2 = Store(path)
    c2 = Client({"trades": [trade("t3", 1_780_000_011), trade("t2", 1_780_000_010)], "cursor": ""})
    ln2 = lane(c2)
    ln2.poll_trades(store2, max_pages=5, cadence_s=0.0)
    ids = sorted(r[0] for r in store2.conn.execute("SELECT trade_id FROM trade_prints"))
    store2.close()

    print()
    print("E. restart resumes from the durable watermark")
    print(f"   min_ts on the restarted request   : {[p.get('min_ts') for p in c2.params]}")
    print(f"   trade ids stored                  : {ids}")
    fails = []
    if [p.get("min_ts") for p in c2.params] != [1_780_000_010]:
        fails.append("E) the restart did not resume inclusively from the stored watermark")
    if ids != ["t1", "t2", "t3"]:
        fails.append(f"E) restart lost or duplicated prints: {ids}")
    return fails


def main() -> int:
    fails = case_cursor_loop() + case_malformed() + case_overlap_same_second() + case_restart()
    print()
    for f in fails:
        print("FAIL " + f)
    if not fails:
        print("HOLDS: the print poller survives every scripted page shape")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

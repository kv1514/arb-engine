"""Reproduction 6: venue quote timestamps.

Run: ``python3 -B audit/repro_quote_time.py`` from the worktree root.

  A. ``fastlane._epoch`` passes Robinhood's "never quoted" sentinel straight through, so a
     contract with no venue timestamp is recorded as last quoted in **1754**
     (-6,795,364,578 = Go's zero time as the quotes API encodes it; the 2026-09-26 audit
     found it in 30.5 % / 17.6 % of home / away rows). ``venues/robinhood._epoch`` already
     rejects the ISO form (``dt.year < 2000``); the fast lane's numeric branch does not.
     Downstream: ``quant.microdata._usable`` requires ``0 <= obs_ts - quote_time <=
     VENUE_LAG_MAX_S``, so every such row is silently dropped from the research dataset, and
     ``strategy.leadlag._fresh`` silently drops it from every LAG comparison.

  B. Non-finite venue timestamps (NaN / inf) also pass, and a NaN exchange timestamp reaches
     the ``trade_prints`` table and then breaks the restart cursor.

  C. Kalshi (and Polymarket) quotes carry no ``quote_time`` at all, so their venue lag cannot
     be measured. Reported, not fixed: which Kalshi field is the quote time is a venue fact
     that needs a source in docs/VENUES.md, and the market-quote construction
     (venues/kalshi.py:687) is outside this audit's file ownership.
"""

from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arb_engine.store import Store  # noqa: E402
from arb_engine.strategy.fastlane import _epoch, refresh_robinhood  # noqa: E402
from audit.harness import kalshi_quote, robinhood_quote, tmp_path  # noqa: E402

SENTINEL = -6795364578


def main() -> int:
    fails = []

    print("A. the Robinhood 'never quoted' sentinel")
    print(f"   _epoch({SENTINEL})           -> {_epoch(SENTINEL)}  (1754-08-30 UTC)")
    print(f"   _epoch('0001-01-01T00:00:00Z')      -> {_epoch('0001-01-01T00:00:00Z')}")

    class Adapter:
        def quotes(self, ids):
            return {"rh-kc": {"yes_ask_price": "0.57", "yes_bid_price": "0.54",
                              "ask_size": "90", "bid_size": "70",
                              "ask_venue_timestamp": SENTINEL, "state": "open"}}

    ticks = iter([1_780_000_000.0, 1_780_000_000.4])
    rows = refresh_robinhood(Adapter(), [robinhood_quote()], clock=lambda: next(ticks))
    qt = rows[0].quote_time
    print(f"   recorded quote_time                 -> {qt}")
    if qt is not None and qt < 0:
        fails.append(f"A) a contract with no venue timestamp was recorded as quoted at {qt}")

    print()
    print("B. non-finite venue and exchange timestamps")
    print(f"   _epoch(nan) -> {_epoch(float('nan'))}   _epoch(inf) -> {_epoch(float('inf'))}")
    if not all(_epoch(v) is None for v in (float("nan"), float("inf"))):
        fails.append("B) a non-finite venue timestamp is accepted as a quote time")

    store = Store(tmp_path("prints.db"))
    counts, rejects = {}, {}
    for tid, ts in (("nan-1", float("nan")), ("inf-1", float("inf")),
                    ("old-1", -6795364578.0), ("ok-1", 1_780_000_000.0)):
        counts[tid] = store.record_trade_prints([{"trade_id": tid, "ticker": "KXT-KC",
                                                  "created_time": ts, "yes_price": 55,
                                                  "count": 10}])
        rejects[tid] = store.last_trade_print_rejects
    got = [tuple(r) for r in store.conn.execute("SELECT trade_id, ts FROM trade_prints")]
    print(f"   record_trade_prints returned        : {counts}")
    print(f"   prints reported unreadable          : {rejects}")
    print(f"   trade_prints rows                   : {got}")
    # A NaN REAL violates the column's NOT NULL in SQLite, so 'INSERT OR IGNORE' used to drop
    # the print with no row, no error and no count: unreadable looked exactly like absent.
    if counts["nan-1"] == 0 and not rejects["nan-1"]:
        fails.append("B) a print with a NaN exchange timestamp is dropped silently "
                     "(no row, no error, no reject count)")
    if any(r[1] is not None and not math.isfinite(r[1]) for r in got):
        fails.append("B) a non-finite exchange timestamp was stored in trade_prints")
    try:
        wm = store.latest_trade_ts("KXT-KC")
        print(f"   latest_trade_ts (restart cursor)    : {wm}")
    except Exception as exc:                                   # noqa: BLE001
        print(f"   latest_trade_ts (restart cursor)    : raised {exc!r}")
        fails.append("B) one non-finite exchange ts breaks the restart cursor for that ticker "
                     "for good: every later poll of it raises before recording anything")
    store.close()

    print()
    print("C. Kalshi quotes carry no venue timestamp")
    print(f"   kalshi quote.quote_time             : {kalshi_quote().quote_time}")
    print("   (reported, not fixed - see the module docstring)")

    print()
    for f in fails:
        print("FAIL " + f)
    if not fails:
        print("HOLDS: implausible and non-finite venue / exchange timestamps are read as missing")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

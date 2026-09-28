"""A5 - where the 15-minute settle margin actually stops working, measured.

Two independent errors eat into it:

* **venue publication lag** L: a print made at t is only returned from t+L on;
* **local clock skew** S: the machine believes it is S seconds later than it is.

A tape is fetched at the earliest moment the client allows, and compared with the venue's true
closed-window set. Two readers are measured, because they fail differently:

* ``same client``   - the machine that fetched is the machine that reads. Its own skew cancels
  out of every check, so the margin it really applies is ``settle_s - S``. Nothing in the file
  can reveal this, and the fix does not change it.
* ``correct reader`` - a second machine, clock right, reads the first one's cache file. Before
  the fix it accepted the file whatever stamp it carried; after, it refuses a stamp from its own
  future, so the tape is fetched again properly.

Also here: a partially written file, and a file written by a permissive client read by a strict
one.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from common import TK, BASE, NoNetwork, banner  # noqa: E402
from arb_engine.venues.trades import SETTLE_S, IncompleteTape, TradesClient, _cache_key  # noqa: E402

CLOSE = BASE + 5_000.0           # the window ends here
N = 20


class LaggyVenue:
    """Every print exists at its own timestamp, but is only returned ``lag`` seconds later."""

    def __init__(self, lag, now):
        self.lag, self.now, self.rows = lag, now, []
        for i in range(N):
            ts = CLOSE - 120 + i * 6          # the last print is 6 s before the close
            self.rows.append({"trade_id": f"t{i:03d}", "ticker": TK, "_ts": ts,
                              "created_time": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                              "yes_price_dollars": "0.5000", "count_fp": "1.00", "taker_side": "yes"})

    def get(self, url, params=None, headers=None, raw=False):
        visible = [{k: v for k, v in r.items() if k != "_ts"} for r in self.rows if r["_ts"] + self.lag <= self.now()]
        return {"trades": visible[::-1], "cursor": ""}


def one(lag, skew, delay=None, reader_at=None):
    """Fetch with a clock ``skew`` s fast, ``delay`` s (true time) after the window closed; then
    read the cache with a correct clock at ``reader_at``.
    -> (what the fetching client got, what the correct reader got) as "short" / "whole" / "refetched"."""
    delay = SETTLE_S - skew if delay is None else delay
    d = tempfile.mkdtemp(prefix="marg_")
    try:
        real = {"t": CLOSE + delay}
        venue = LaggyVenue(lag, lambda: real["t"])
        try:
            got = TradesClient(http=venue, cache_dir=d, clock=lambda: real["t"] + skew).kalshi_trades(TK, None, CLOSE)
        except IncompleteTape:
            return "refused", "-"
        mine = "whole" if len(got) == N else "SHORT"
        real["t"] = CLOSE + SETTLE_S + 10_000 if reader_at is None else reader_at
        nocache = LaggyVenue(lag, lambda: real["t"])
        try:
            again = TradesClient(http=nocache, cache_dir=d, clock=lambda: real["t"]).kalshi_trades(TK, None, CLOSE)
        except IncompleteTape:
            return mine, "refused"
        if nocache.calls if hasattr(nocache, "calls") else False:
            pass
        theirs = "whole" if len(again) == N else "SHORT"
        return mine, theirs
    finally:
        shutil.rmtree(d, ignore_errors=True)


def grid():
    banner("A5a  venue publication lag x clock skew, fetching the moment the skewed clock allows")
    print(f"settle margin {SETTLE_S:g} s; {N} prints, the last 6 s before the close.")
    print("'fetcher' = what the machine that fetched ended up with; 'later reader' = a correct clock,")
    print("long afterwards, reading that cache file.")
    print(f"{'lag s':>7} {'skew s':>7} | {'fetcher':>9} | {'later reader':>13}")
    for lag in (0, 300, 899, 900, 1500):
        for skew in (0, 60, 300, 1200):
            a, b = one(lag, skew)
            print(f"{lag:>7} {skew:>7} | {a:>9} | {b:>13}")
    banner("A5b  where the future-stamp check can fire at all")
    print("A fetch d seconds (true time) after the close, by a clock skew seconds fast, is stamped")
    print(f"d+skew. A reader may look from d={SETTLE_S:g} on, and refuses a stamp more than 60 s ahead of its")
    print("own clock - so the check can only fire while the reader has not caught up to the stamp.")
    print(f"{'d s':>7} {'skew s':>7} {'stamp':>7} | {'fetcher':>9} | {'reader at close+900':>20} | {'reader long after':>18}")
    print("(venue lag 500 s, so a fetch under 500 s after the close is necessarily short)")
    for d, skew in ((100, 2000), (100, 1000), (100, 960), (100, 900), (100, 300), (600, 900), (0, 960), (900, 0)):
        a, soon = one(500, skew, delay=d, reader_at=CLOSE + SETTLE_S)
        _, late = one(500, skew, delay=d)
        print(f"{d:>7} {skew:>7} {d + skew:>7} | {a:>9} | {soon:>20} | {late:>18}")


def partial_write():
    banner("A5c  a file half written when the process died")
    d = tempfile.mkdtemp(prefix="part_")
    try:
        venue = LaggyVenue(0, lambda: CLOSE + SETTLE_S + 10)
        TradesClient(http=venue, cache_dir=d, clock=lambda: CLOSE + SETTLE_S + 10).kalshi_trades(TK, None, CLOSE)
        path = Path(d, _cache_key("kalshi", TK, None, CLOSE) + ".json")
        whole = path.read_text()
        for cut in (len(whole) // 2, len(whole) - 1, len(whole) - 20):
            path.write_text(whole[:cut])
            try:
                TradesClient(http=NoNetwork(), cache_dir=d, offline=True,
                             clock=lambda: CLOSE + SETTLE_S + 10).kalshi_trades(TK, None, CLOSE)
                print(f"  cut at {cut}: SERVED")
            except FileNotFoundError as e:
                print(f"  cut at {cut}: refused ({str(e).split('(')[-1][:40]}...)")
        # A stray temporary file from a killed writer must not be mistaken for the tape.
        Path(d, ".tape-dead.tmp").write_text(whole)
        path.write_text(whole)
        n = len(TradesClient(http=NoNetwork(), cache_dir=d, offline=True, clock=lambda: CLOSE + SETTLE_S + 10).kalshi_trades(TK, None, CLOSE))
        print(f"  with a stray .tape-*.tmp beside it: the real file still serves {n} prints")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def permissive_then_strict():
    banner("A5d  a file written by a permissive client, read by a strict one")
    d = tempfile.mkdtemp(prefix="perm_")
    try:
        now = CLOSE + 10                                  # only 10 s after the close
        TradesClient(http=LaggyVenue(0, lambda: now), cache_dir=d, clock=lambda: now, settle_s=0).kalshi_trades(TK, None, CLOSE)
        print("  a client with settle_s=0 cached it as complete 10 s after the close")
        for settle in (0.0, 900.0):
            try:
                n = len(TradesClient(http=NoNetwork(), cache_dir=d, offline=True, clock=lambda: CLOSE + 100_000,
                                     settle_s=settle).kalshi_trades(TK, None, CLOSE))
                print(f"  reader with settle_s={settle:g}: served {n} prints")
            except FileNotFoundError as e:
                print(f"  reader with settle_s={settle:g}: refused - {str(e).split('checks:')[-1][:80].strip()}")
        # And the other way: does the strict reader's own write destroy the permissive file?
        strict = TradesClient(http=LaggyVenue(0, lambda: CLOSE + 100_000), cache_dir=d, clock=lambda: CLOSE + 100_000)
        strict.kalshi_trades(TK, None, CLOSE)
        doc = json.loads(Path(d, _cache_key("kalshi", TK, None, CLOSE) + ".json").read_text())
        print(f"  after the strict client fetched: complete={doc['complete']}, source={doc['source']}, "
              f"pass_started_at now {doc['pass_started_at'] - CLOSE:.0f} s after the close")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    grid()
    partial_write()
    permissive_then_strict()

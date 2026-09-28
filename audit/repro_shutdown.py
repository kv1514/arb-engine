"""Reproduction 5: ``LiveSlate.run()`` only stops its workers on the happy path.

Run: ``python3 -B audit/repro_shutdown.py`` from the worktree root.

``run()`` sets ``self._stop``, joins the fast thread and flushes the background print walk
*after* the while loop, with no ``try/finally``. A ``KeyboardInterrupt`` (how every one of
these recorders is actually stopped - see the deploy runbook's ``sunday.sh stop``) leaves:

  * the fast-lane thread running and still writing to the store;
  * the background trade-print page walk unflushed;

and ``cli.cmd_live`` never closes the ``Store`` at all, so the writer the fast thread is
still using is torn down by interpreter exit. ``wait_for_trade_polls``'s bounded-wait return
value is also dropped, so an expired wait is indistinguishable from a clean flush.
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arb_engine.store import Store  # noqa: E402
from audit.harness import Clock, live_game, make_slate, tmp_path  # noqa: E402


def main() -> int:
    clock = Clock()
    store = Store(tmp_path("shutdown.db"))
    slate = make_slate(store=store, games=[live_game()], clock=clock, fast=0.05)

    class Client:
        def get(self, path, params=None):
            if path == "/markets":
                return {"markets": [{"ticker": "KXT-KC", "yes_bid_dollars": ".52",
                                     "yes_ask_dollars": ".54"}]}
            time.sleep(0.6)                       # a slow tape still being paged
            return {"trades": [], "cursor": ""}

    slate.fastlane.kalshi = Client()
    slate.fastlane.robinhood = None

    interrupted = {"n": 0}
    real_tick = slate.tick

    def tick(now=None):
        out = real_tick(now)
        interrupted["n"] += 1
        if interrupted["n"] >= 1:
            raise KeyboardInterrupt("operator stopped the recorder")
        return out

    slate.tick = tick
    before = threading.active_count()
    try:
        slate.run(interval=0.05, duration=5.0, max_iterations=5, printer=lambda *_: None)
    except KeyboardInterrupt:
        pass

    time.sleep(0.2)
    names = sorted(t.name for t in threading.enumerate() if t.name in ("fastlane", "kalshi-trade-prints"))
    still_running = not slate._stop.is_set()

    print("=== run() interrupted by the operator ===")
    print(f"   _stop set by run()                : {slate._stop.is_set()}")
    print(f"   worker threads still alive        : {names}")
    print(f"   threads before the run            : {before}")

    fails = []
    if still_running:
        fails.append("run() left _stop clear: the fast-lane thread keeps recording after the "
                     "operator stopped the slate")
    if names:
        fails.append(f"worker threads still alive after run() returned: {names}")

    # Whatever happens, clean up this reproduction's own threads before closing the store.
    slate._stop.set()
    slate.fastlane.wait_for_trade_polls(timeout=3)
    time.sleep(0.2)
    store.close()

    for f in fails:
        print("FAIL " + f)
    if not fails:
        print("HOLDS: run() stops its workers on every exit path")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

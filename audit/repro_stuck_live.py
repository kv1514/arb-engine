"""Reproduction 2: ``tick()`` returns at ``if not games`` (live.py:274) *before*
``_live_priced`` is rebuilt (live.py:341), so a finished game stays in the fast lane and
keeps being recorded ``live=1`` for as long as the process runs.

Run: ``python3 -B audit/repro_stuck_live.py`` from the worktree root.

Two consequences, both seen in the 2026-09-26 deployment audit:
  * `inplay_ticks.live = 1` rows continue for hours after the game ended (ATL|GB 6.1 h);
  * ``run()``'s idle test ``idle = not self._live_priced and not tick.views`` is never
    true, so the loop keeps the 5 s cadence instead of ``inplay_idle_every_s`` (60 s).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arb_engine.store import Store  # noqa: E402
from audit.harness import (Clock, live_game, make_slate, tmp_path)  # noqa: E402


class FakeKalshiClient:
    def __init__(self):
        self.calls = 0

    def get(self, path, params=None):
        self.calls += 1
        if path == "/markets":
            return {"markets": [{"ticker": "KXT-KC", "yes_bid_dollars": ".52",
                                 "yes_ask_dollars": ".54", "yes_bid_size_fp": "100",
                                 "yes_ask_size_fp": "120"}]}
        return {"trades": [], "cursor": ""}


def main() -> int:
    clock = Clock()
    store = Store(tmp_path("history.db"))
    feed_games = [live_game()]
    slate = make_slate(store=store, games=feed_games, clock=clock, fast=1.0)
    slate.fastlane.kalshi = FakeKalshiClient()
    slate.fastlane.robinhood = None
    slate.fastlane.clock = clock.now

    # Tick 1: the game is live, so it is priced and seeded into the fast lane.
    slate.tick(clock.now())
    print(f"after the live tick   : _live_priced = {sorted(slate._live_priced)}")

    # The game ends. ESPN drops it from the scoreboard (status 'post' / off the board),
    # so wanted_games() returns nothing.
    slate.feed.games_list.clear()
    clock.advance(600.0)
    t = slate.tick(clock.now())
    print(f"after the empty tick  : games={t.games}, _live_priced = {sorted(slate._live_priced)}")

    # The fast lane keeps running against the stale view.
    clock.advance(1.0)
    slate.fast_step(clock.now())

    rows = store.tick_rows()
    fast_rows = [r for r in rows if r["source"] == "fast"]
    store.close()

    print()
    print(f"inplay_ticks rows written  : {len(rows)} ({len(fast_rows)} from the fast lane)")
    for r in fast_rows:
        print(f"  fast tick ts={r['ts']:.1f} live={r['live']}  (the game left the scoreboard "
              f"{r['ts'] - 1_780_000_000.0:.0f}s into the run)")

    failures = []
    if slate._live_priced:
        failures.append("_live_priced still holds a game the scoreboard no longer wants")
    if any(r["live"] == 1 for r in fast_rows):
        failures.append("a finished game is still recorded live=1 by the fast lane")
    for f in failures:
        print("FAIL " + f)
    if not failures:
        print("HOLDS: an empty scoreboard clears the fast lane")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

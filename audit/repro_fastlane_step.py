"""Reproduction 4: two problems in the one-second fast lane.

Run: ``python3 -B audit/repro_fastlane_step.py`` from the worktree root.

  A. ``FastLane.step`` bounds ``fut.result(timeout=self.timeout)``, but the surrounding
     ``with ThreadPoolExecutor(...)`` calls ``shutdown(wait=True)`` on exit, so the step
     still blocks for the whole of the slow venue's request. The 2.5 s timeout does not
     bound the lane's cadence at all.

  B. ``LiveSlate._fast_loop`` calls ``fast_step(s0)`` (live.py:522), which pins the lane's
     clock: every fast-lane row is stamped req_ts == obs_ts == the *start* of the step,
     although the response arrived later. A recorded obs_ts earlier than the real receipt
     is a look-ahead in the dataset, not a rounding error.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arb_engine.store import Store  # noqa: E402
from arb_engine.strategy.fastlane import FastLane  # noqa: E402
from audit.harness import KEY, Clock, kalshi_quote, live_game, make_slate, tmp_path  # noqa: E402


def case_timeout() -> list[str]:
    class SlowClient:
        def get(self, path, params=None):
            time.sleep(1.5)
            return {"markets": [{"ticker": "KXT-KC", "yes_bid_dollars": ".52",
                                 "yes_ask_dollars": ".54"}]}

    lane = FastLane(kalshi_client=SlowClient(), timeout=0.2)
    lane.seed({KEY: {"kalshi": [kalshi_quote()]}})
    t0 = time.monotonic()
    _, errors = lane.step()
    elapsed = time.monotonic() - t0
    print("A. a slow venue against a 0.2 s lane timeout")
    print(f"   step wall time                    : {elapsed:.2f}s")
    print(f"   errors                            : {errors}")
    fails = []
    if elapsed > 0.6:
        fails.append(f"A) step() blocked {elapsed:.2f}s despite a 0.2s timeout "
                     "(ThreadPoolExecutor.__exit__ waits for the abandoned request)")
    return fails


def case_pinned_fast_clock() -> list[str]:
    real = {"t": 1_780_000_000.0}
    completions: list[float] = []

    class Client:
        def get(self, path, params=None):
            real["t"] += 0.4          # the request really takes 0.4 s
            completions.append(real["t"])
            return {"markets": [{"ticker": "KXT-KC", "yes_bid_dollars": ".52",
                                 "yes_ask_dollars": ".54", "yes_ask_size_fp": "120",
                                 "yes_bid_size_fp": "100"}]}

    clock = Clock()
    store = Store(tmp_path("fast.db"))
    slate = make_slate(store=store, games=[live_game()], clock=clock, fast=1.0)
    slate.fastlane.kalshi = Client()
    slate.fastlane.robinhood = None
    slate.fastlane.clock = lambda: real["t"]
    slate.tick(clock.now())          # seed the fast lane
    completions.clear()

    # Drive the production fast-lane thread itself for one step, rather than calling
    # fast_step by hand: the defect was in how _fast_loop calls it (live.py:522).
    import threading
    s0 = real["t"]
    slate.fast = 0.01
    slate._stop.clear()
    t = threading.Thread(target=slate._fast_loop, args=(lambda *_: None,), daemon=True)
    t.start()
    while not completions:
        time.sleep(0.01)
    slate._stop.set()
    t.join(timeout=3)
    first_response = completions[0]

    import json
    rows = [r for r in store.tick_rows() if r["source"] == "fast"]
    l1 = json.loads(rows[0]["l1_json"])["rows"]
    kalshi_rows = [r for r in l1 if r["venue"] == "kalshi"]
    store.close()

    print()
    print("B. the fast lane through the production call path (_fast_loop)")
    print(f"   step start / first response in    : {s0} / {first_response}")
    for r in kalshi_rows:
        print(f"   recorded req_ts / obs_ts          : {r['req_ts']} / {r['obs_ts']} "
              f"(approx_time={r['approx_time']}, refreshed={r['refreshed']})")
    print(f"   recorded tick ts                  : {rows[0]['ts']}")
    fails = []
    for r in kalshi_rows:
        if r["obs_ts"] < first_response:
            fails.append(f"B) obs_ts {r['obs_ts']} is earlier than the real receipt "
                         f"{first_response}: the recorded observation predates its own response")
        if r["req_ts"] == r["obs_ts"]:
            fails.append("B) req_ts == obs_ts: the fast lane records no request/response boundary")
        if r["obs_ts"] > rows[0]["ts"]:
            fails.append("B) the tick timestamp is earlier than an observation inside it")
    return fails


def main() -> int:
    fails = case_timeout() + case_pinned_fast_clock()
    print()
    for f in fails:
        print("FAIL " + f)
    if not fails:
        print("HOLDS: the fast lane bounds its step and stamps real boundaries")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

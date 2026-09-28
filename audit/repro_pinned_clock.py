"""Reproduction 1: ``LiveSlate.run()`` pins the tick clock, so four recorder guarantees
that the tests exercise through ``tick()`` are inert on the production call path.

Run: ``python3 -B audit/repro_pinned_clock.py`` from the worktree root.

``run()`` calls ``self.tick(t0)``. Inside ``tick`` every timing branch is guarded by
``pinned is None``:

  live.py:253   polling-gap record        -> never emitted
  live.py:276   ``_fetch_pinned_now``     -> every full-tick quote's req_ts == obs_ts == t0
  live.py:196   carried-quote detection   -> a cached price is stamped refreshed=1
  live.py:281   post-fetch clock refresh  -> the tick decides at its start time
  live.py:298   per-game decision time    -> every game decides at the tick's start time

The same pin exists in the fast lane: ``_fast_loop`` calls ``fast_step(s0)`` (live.py:522).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audit.harness import (Adapter, Clock, RecordingAlerter, kalshi_quote,  # noqa: E402
                           live_game, make_slate, robinhood_quote)


def main() -> int:
    import arb_engine.strategy.live as live

    clock = Clock()
    alerts = RecordingAlerter()

    # A Robinhood contract the quotes refresh did not answer: the adapter hands back the
    # cached catalogue price, 1800 s old (venues/robinhood._quote_ts).
    stale = robinhood_quote(ts=clock.t - 1800.0)
    adapters = [
        Adapter("kalshi", [kalshi_quote(ts=clock.t)], delay=0.4, clock=clock),
        Adapter("robinhood", [stale], delay=0.3, clock=clock),
    ]
    slate = make_slate(adapters=adapters, games=[live_game()], clock=clock, alerter=alerts)
    # Every adapter re-reads the clock as the fetch progresses, so the fetch really does
    # take 0.7 s of this run's time.
    for ad in adapters:
        ad._quotes = [q for q in ad._quotes]

    captured = {}

    real_merged = slate.merged_events

    def spy(errors):
        merged = real_merged(errors)
        captured["merged"] = merged
        return merged

    slate.merged_events = spy

    original_time = live.time
    live.time = clock
    try:
        # A 300 s stall before this tick: the Mac slept, or the process restarted.
        slate._last_tick_at = clock.t - 300.0
        slate.run(interval=5.0, duration=1.0, max_iterations=1, printer=lambda *_: None)
    finally:
        live.time = original_time

    merged = captured["merged"]
    kq = merged["nfl:DEN|KC:2026-09-21"].quotes_by_venue["kalshi"][0]
    rq = merged["nfl:DEN|KC:2026-09-21"].quotes_by_venue["robinhood"][0]

    gap_records = [t for t, _ in alerts.infos if "polling gap" in t]

    print("=== run() -> tick(t0): what the recorder actually stored ===")
    print(f"a) polling-gap records after a 300 s stall : {gap_records!r}")
    print(f"   expected                                : ['polling gap: 300s since the last tick']")
    print()
    print(f"b) kalshi req_ts / obs_ts                  : {kq.meta['req_ts']} / {kq.meta['obs_ts']}")
    print(f"   the fetch really spanned                : 0.7 s of clock time")
    print(f"   expected                                : req_ts < obs_ts (request start, response completion)")
    print()
    print(f"c) 1800 s-old cached Robinhood quote        : refreshed={rq.meta['refreshed']}, "
          f"approx_time={rq.meta['approx_time']}, obs_ts={rq.meta['obs_ts']}")
    print(f"   expected                                : refreshed=False, approx_time=True, obs_ts={stale.ts}")
    print()

    failures = []
    if not gap_records:
        failures.append("a) no polling-gap record was emitted through run()")
    if kq.meta["req_ts"] == kq.meta["obs_ts"]:
        failures.append("b) req_ts == obs_ts: the full tick records no request/response boundary")
    if rq.meta["refreshed"] is not False:
        failures.append("c) a 1800 s-old cached price was recorded as freshly observed")

    for f in failures:
        print("FAIL " + f)
    if not failures:
        print("HOLDS: run() no longer pins the tick clock")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

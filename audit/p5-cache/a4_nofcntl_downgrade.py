"""A4 - without ``fcntl`` the "an incomplete state never replaces a complete file" check is
not atomic, so a late writer throws a finished tape away.

``_store`` reads the file, decides, then writes. With ``fcntl`` both steps happen under the
tape's lock. Without it (the module says "on Windows that check is best effort") another
writer can finish in between.

The race window is widened here without touching the module: the late writer's ``_read`` is
wrapped so it pauses after reading - exactly the interval the lock is meant to cover. With
``fcntl`` present the lock still closes it; with ``trades.fcntl = None`` it does not.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time

from common import TK, BASE, Pages, banner, row  # noqa: E402
import arb_engine.venues.trades as trades  # noqa: E402
from arb_engine.venues.trades import IncompleteTape, TradesClient, _cache_key  # noqa: E402

NOW = BASE + 100_000.0
HI = BASE + 5_000.0


def pages(chunk=2, n=10):
    rows = [row(i, i) for i in range(n - 1, -1, -1)]
    out, cur = {}, None
    for i in range(0, len(rows), chunk):
        nxt = f"c{i // chunk + 1}" if i + chunk < len(rows) else ""
        out[cur] = {"trades": rows[i:i + chunk], "cursor": nxt}
        cur = nxt or None
    return out


def trial(no_fcntl):
    d = tempfile.mkdtemp(prefix="dg_")
    path = os.path.join(d, _cache_key("kalshi", TK, None, HI) + ".json")
    saved = trades.fcntl
    if no_fcntl:
        trades.fcntl = None
    try:
        # The late writer gets one page, so it will save an incomplete state.
        late = TradesClient(http=Pages(pages()), cache_dir=d, clock=lambda: NOW)
        gate = threading.Event()
        real_read = late._read

        def slow_read(*a, **kw):
            out = real_read(*a, **kw)
            gate.set()               # tell the other writer to finish now
            time.sleep(0.15)         # the interval between the check and the write
            return out
        late._read = slow_read

        def finisher():
            gate.wait(2)
            TradesClient(http=Pages(pages()), cache_dir=d, clock=lambda: NOW).kalshi_tape(TK, None, HI)

        t = threading.Thread(target=finisher)
        t.start()
        try:
            late.kalshi_tape(TK, None, HI, max_pages=1)
        except IncompleteTape:
            pass
        t.join()
        doc = json.loads(open(path).read())
        return doc["complete"], len(doc["trades"])
    finally:
        trades.fcntl = saved
        shutil.rmtree(d, ignore_errors=True)


def main():
    banner("A4  the check-then-write window in _store")
    for no_fcntl in (False, True):
        wins = [trial(no_fcntl) for _ in range(6)]
        downgrades = sum(1 for c, _ in wins if not c)
        print(f"fcntl {'DISABLED' if no_fcntl else 'present '}: {downgrades} of {len(wins)} runs left an "
              f"incomplete file where a complete one had been written  {wins}")
    print("EXPECTED: 0 downgrades in both rows - a finished tape is never thrown away by a late writer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""A1 - clock trust / provenance: a complete tape stamped in the READER's future is served.

The resume path refuses a saved pass whose ``pass_started_at`` is later than the clock
(``trades.py`` "prior.pass_started_at <= now"). A *complete* tape gets no such check:
``tape_from_doc`` only asks whether ``max_ts`` was ``settle_s`` behind ``pass_started_at``,
and ``pass_started_at`` is a number the writer chose.

So a writer whose clock runs fast (or any file whose stamp was edited / copied from a
machine with a fast clock) can turn an open-window fetch into a "complete" tape, and a
reader with a perfectly correct clock cannot tell: the larger the writer's skew, the more
closed the window looks.

Everything here is offline: a scripted fake of GET /markets/trades and a temp directory.
"""
from __future__ import annotations

import shutil
import tempfile

from common import TK, BASE, Pages, NoNetwork, banner, one_page  # noqa: E402
from arb_engine.venues.trades import SETTLE_S, TradesClient  # noqa: E402

T = BASE + 100_000.0          # "real" wall clock while the venue is still publishing
LO, HI = T - 3600.0, T - 100.0   # a window that ended 100 s ago: NOT closed under a 900 s margin
SKEW = 2000.0                 # the writer's clock is 2000 s fast


def main():
    banner("A1  a complete tape whose pass_started_at is in the reader's future")
    d = tempfile.mkdtemp(prefix="a1_")
    try:
        # The venue, at real time T, has published 2 of the 3 prints in the window; the third
        # (t0002, at HI - 50) is published late, as Kalshi's tape does.
        early = [row_ for row_ in ([_r(0, 0), _r(1, 1)])]
        late = early + [_r(2, 2)]

        writer_http = Pages(one_page(early))
        writer = TradesClient(http=writer_http, cache_dir=d, clock=lambda: T + SKEW, settle_s=SETTLE_S)
        tape = writer.kalshi_tape(TK, LO, HI)
        print(f"writer (clock {SKEW:g} s fast) fetched at real time T and got {len(tape.trades)} prints, "
              f"complete={tape.complete}, pass_started_at={tape.pass_started_at - T:+.0f} s from real T")
        print(f"  the window really ended {T - HI:.0f} s before the fetch - inside the {SETTLE_S:g} s settle margin")

        # Real time moves on; the late print lands. A reader with a CORRECT clock, well past
        # the margin, asks for the same window. It must not be answered from that file.
        reader = TradesClient(http=Pages(one_page(late)), cache_dir=d, clock=lambda: T + 1000.0, settle_s=SETTLE_S)
        got = reader.kalshi_tape(TK, LO, HI)
        print(f"reader (correct clock, {T + 1000.0 - HI:.0f} s after the window closed) got "
              f"{len(got.trades)} prints, complete={got.complete}")
        ids = [t.trade_id for t in got.trades]
        print(f"  ids: {ids}")
        if len(got.trades) == 2:
            print("OBSERVED : 2 prints served as the complete tape; the late print t0002 is missing for good")
            print("EXPECTED : the file is refused (its pass began after the reader's clock) and the tape refetched -> 3 prints")
            return 1
        print("no defect: the reader refused the future-stamped file and refetched")
        return 0
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _r(i, k):
    from common import row
    r = row(i, 0)
    # place the print inside [LO, HI]
    from datetime import datetime, timezone
    ts = HI - 60 + k
    r["created_time"] = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return r


if __name__ == "__main__":
    raise SystemExit(main())

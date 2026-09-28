"""Fuzz the Kalshi tape: random page shapes, budgets, cursor behaviour and kill points.

The invariant asserted after every seed: **a tape served as complete equals the venue's true
closed-window set** - same ids, same payloads, no extra, none missing - and carries no cursor
and no rejected rows. Offline replay of the same cache must give the identical tape.

Kill points model a process dying:

* ``mid-fetch``  - the transport raises a BaseException (not caught by the fetch loop) on the
  k-th request, and the client object is thrown away; the next call is a new "process";
* ``mid-write``  - ``_write`` raises after the temporary file is written but before
  ``os.replace``, i.e. the window in which a crash could leave half a file.

Usage: python3 audit/fuzz_tape.py [seeds] [--kills]
"""
from __future__ import annotations

import random
import shutil
import sys
import tempfile
from datetime import datetime, timezone

from common import TK, BASE, banner  # noqa: E402
import arb_engine.venues.trades as trades  # noqa: E402
from arb_engine.venues.http import HttpError  # noqa: E402
from arb_engine.venues.trades import ConflictingTrades, IncompleteTape, TradesClient  # noqa: E402

NOW = BASE + 100_000.0
LO, HI = BASE, BASE + 5_000.0


class Kill(BaseException):
    """A process death: never caught by the fetch loop's ``except Exception``."""


def make_rows(rnd, n):
    """The venue's true set: n prints in [LO, HI], newest first, some sharing a second."""
    out = []
    for i in range(n):
        sec = rnd.randrange(0, 4000)
        frac = rnd.choice([0.0, 0.25, 0.5, 0.999999])
        ts = BASE + sec + frac
        out.append({"trade_id": f"t{i:04d}", "ticker": TK,
                    "created_time": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    "yes_price_dollars": f"{rnd.randrange(1, 100) / 100:.4f}",
                    "count_fp": f"{rnd.randrange(1, 50)}.00", "taker_side": rnd.choice(["yes", "no"])})
    out.sort(key=lambda r: r["created_time"], reverse=True)
    return out


class Venue:
    """A cursor-paginated tape. Page size, overlap, transient failures, malformed answers,
    stale cursors and repeated cursors are all random; every failure mode is transient so a
    persistent client can always finish."""

    def __init__(self, rnd, rows):
        self.rnd, self.rows, self.calls = rnd, rows, 0
        self.page = rnd.choice([1, 2, 3, 5, 10])
        self.overlap = rnd.choice([0, 0, 1, 2])
        self.p_fail = rnd.choice([0.0, 0.1, 0.25])
        self.p_malformed = rnd.choice([0.0, 0.1])
        self.p_stale = rnd.choice([0.0, 0.15])
        self.p_repeat = rnd.choice([0.0, 0.1])
        self.p_badrow = rnd.choice([0.0, 0.1])
        self.p_conflict = rnd.choice([0.0, 0.08])
        self.p_edge = rnd.choice([0.0, 0.3])   # also serve prints just outside the exact window
        self.cursors = {}                      # cursor -> start index
        self.this_pass = []                    # cursors handed out during the current pass
        self.epoch = 0
        self.lying = False                     # re-issue a cursor of an EARLIER pass as "next"

    def get(self, url, params=None, headers=None, raw=False):
        assert url.endswith("/markets/trades"), url
        self.calls += 1
        cur = (params or {}).get("cursor")
        if cur is None:
            start, self.this_pass = 0, []
        elif cur in self.cursors:
            start = self.cursors[cur]
        else:
            raise HttpError(404, "u", "unknown cursor")
        if self.rnd.random() < self.p_fail:
            raise HttpError(self.rnd.choice([500, 503, 429]), "u", "transient")
        if self.rnd.random() < self.p_malformed:
            return self.rnd.choice([{}, {"error": "x"}, {"trades": []}, [], None])
        if cur is not None and self.rnd.random() < self.p_stale:
            self.cursors.pop(cur, None)        # the venue forgot it: a 404 next time
            raise HttpError(self.rnd.choice(trades.STALE_CURSOR_STATUS), "u", "stale cursor")
        lo = max(0, start - self.overlap)
        end = min(len(self.rows), start + self.page)
        body = [dict(r) for r in self.rows[lo:end]]
        if body and self.rnd.random() < self.p_badrow:
            body[self.rnd.randrange(len(body))] = self.rnd.choice([
                {"ticker": TK, "created_time": "garbage", "yes_price": 50},
                {"ticker": "OTHER-TICKER", "created_time": body[0]["created_time"], "yes_price_dollars": "0.5", "count_fp": "1", "trade_id": "z"},
                dict(body[0], trade_id=""), dict(body[0], count_fp="0"), dict(body[0], yes_price_dollars="NaN"), "not a dict"])
        if self.rnd.random() < self.p_conflict and start > lo:
            # Revise a row of the OVERLAP tail only: that id is served twice in one pass with two
            # payloads, which is the conflict the client must surface. (A revision the venue makes
            # once, on a row served once, is simply the venue's current answer.)
            i = self.rnd.randrange(start - lo)
            if isinstance(body[i], dict) and body[i].get("trade_id"):
                body[i] = dict(body[i], yes_price_dollars="0.9999")
        if self.p_edge and self.rnd.random() < self.p_edge:
            body = body + [_edge_row(self.rnd)]                          # a print inside the widened query, outside the window
        if end >= len(self.rows):
            return {"trades": body, "cursor": ""}
        # A realistic pagination loop: the venue hands back a cursor for a position it has
        # already served, so following it can only re-read, never skip. ``--lying-venue`` drops
        # that restriction (any cursor ever issued), which lets the venue skip rows silently -
        # something an opaque-cursor client cannot detect at all; see the report.
        pool = ([c for c, i in self.cursors.items() if i <= start] if not self.lying
                else list(self.cursors))
        if self.rnd.random() < self.p_repeat and pool:
            return {"trades": body, "cursor": self.rnd.choice(pool)}     # a cursor already handed out
        nxt = f"c{self.epoch}-{self.calls}"
        self.cursors[nxt] = end
        self.this_pass.append(nxt)
        return {"trades": body, "cursor": nxt}


class KillAfter:
    def __init__(self, inner, k):
        self.inner, self.k, self.n = inner, k, 0

    def get(self, *a, **kw):
        self.n += 1
        if self.n == self.k:
            raise Kill("process died mid-fetch")
        return self.inner.get(*a, **kw)


def _edge_row(rnd):
    """A print the widened query (±1 s) returns but the exact window excludes."""
    ts = rnd.choice([LO - 0.5, HI + 0.5])
    return {"trade_id": f"edge{rnd.randrange(10**6)}", "ticker": TK,
            "created_time": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "yes_price_dollars": "0.5000", "count_fp": "1.00", "taker_side": "yes"}


def truth(rows):
    out = {}
    for r in rows:
        t = trades.parse_kalshi_trade(r)
        if t is not None and LO <= t.ts <= HI:
            out[t.trade_id] = (round(t.ts, 6), t.price, t.size, t.side)
    return out


def run_seed(seed, kills=True, lying=False):
    """-> (violation message or None, stats dict)"""
    rnd = random.Random(seed)
    rows = make_rows(rnd, rnd.choice([1, 5, 12, 30]))
    want = truth(rows)
    venue = Venue(rnd, rows)
    d = tempfile.mkdtemp(prefix=f"fuzz{seed}_")
    stats = {"calls": 0, "kill_fetch": 0, "kill_write": 0, "conflicts": 0, "incomplete": 0}
    try:
        for attempt in range(400):
            if attempt == 200:                            # the venue heals: every fault becomes transient
                venue.p_fail = venue.p_malformed = venue.p_stale = venue.p_repeat = 0.0
                venue.p_badrow = venue.p_conflict = 0.0
            stats["calls"] += 1
            budget = rnd.choice([1, 1, 2, 3, 7, 50])
            chk = rnd.choice([1, 2, 10])
            http = venue
            kill_write_at = None
            if kills and rnd.random() < 0.25:
                http = KillAfter(venue, rnd.randrange(1, 4))
                stats["kill_fetch"] += 1
            if kills and rnd.random() < 0.20:
                kill_write_at = rnd.randrange(1, 4)
                stats["kill_write"] += 1
            c = TradesClient(http=http, cache_dir=d, clock=lambda: NOW)
            if kill_write_at is not None:
                real_write, box = c._write, {"n": 0}

                def killing_write(path, tape, _r=real_write, _b=box, _k=kill_write_at):
                    _b["n"] += 1
                    if _b["n"] == _k:
                        import os as _os
                        import tempfile as _tf
                        fd, tmp = _tf.mkstemp(dir=c.cache_dir, prefix=".tape-", suffix=".tmp")
                        _os.close(fd)           # a stray temporary file, as a real kill leaves
                        raise Kill("process died mid-write")
                    return _r(path, tape)
                c._write = killing_write
            try:
                tape = c.kalshi_tape(TK, LO, HI, max_pages=budget, checkpoint_every=chk)
            except Kill:
                continue                                  # the "process" died; a new one next round
            except ConflictingTrades:
                stats["conflicts"] += 1
                continue
            except IncompleteTape:
                stats["incomplete"] += 1
                continue
            got = {t.trade_id: (round(t.ts, 6), t.price, t.size, t.side) for t in tape.trades}
            if not tape.complete or tape.next_cursor is not None or tape.rejected:
                return f"seed {seed}: returned tape is not clean ({tape.complete=}, {tape.next_cursor=}, {tape.rejected=})", stats
            if got != want:
                miss = sorted(set(want) - set(got))
                extra = sorted(set(got) - set(want))
                diff = sorted(k for k in set(got) & set(want) if got[k] != want[k])
                return (f"seed {seed}: complete tape != the venue's closed-window set "
                        f"(missing {miss[:5]}, extra {extra[:5]}, changed {diff[:5]}; {len(got)} vs {len(want)})"), stats
            off = TradesClient(http=None, cache_dir=d, offline=True, clock=lambda: NOW)
            again = off.kalshi_tape(TK, LO, HI)
            if [(t.trade_id, t.ts, t.price) for t in again.trades] != [(t.trade_id, t.ts, t.price) for t in tape.trades]:
                return f"seed {seed}: the offline replay differs from the tape just returned", stats
            return None, stats
        return f"seed {seed}: never completed in 400 calls", stats
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main(argv):
    n = int(argv[0]) if argv and not argv[0].startswith("-") else 500
    kills = "--no-kills" not in argv
    lying = "--lying-venue" in argv
    banner(f"fuzz: {n} seeds, kills={kills}, lying_venue={lying}")
    tot = {"calls": 0, "kill_fetch": 0, "kill_write": 0, "conflicts": 0, "incomplete": 0}
    bad = []
    for seed in range(n):
        v, s = run_seed(seed, kills, lying)
        for k in tot:
            tot[k] += s[k]
        if v:
            bad.append(v)
            print("VIOLATION:", v)
    print(f"seeds {n}, client calls {tot['calls']}, kills mid-fetch {tot['kill_fetch']}, kills mid-write {tot['kill_write']}, "
          f"conflicts {tot['conflicts']}, incomplete {tot['incomplete']}, violations {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

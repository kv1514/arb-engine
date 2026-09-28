"""A2 - concurrency: threads and processes racing on one tape, a stuck lock, a foreign lock
file, and the no-``fcntl`` (Windows) path.

Counted per run:
  wrong      a tape returned or cached as complete that is not the venue's closed-window set
  downgrade  a complete cache file replaced by an incomplete one (progress thrown away)
  stuck      a save lost to the lock timeout

Usage: python3 audit/a2_concurrency.py [threads|procs|nofcntl|lock|all]
"""
from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import sys
import tempfile
import threading
import time

from common import TK, BASE, banner, row  # noqa: E402
import arb_engine.venues.trades as trades  # noqa: E402
from arb_engine.venues.trades import IncompleteTape, TradesClient  # noqa: E402

NOW = BASE + 100_000.0
HI = BASE + 5_000.0
N = 40


def pages(chunk=4):
    """N prints over ceil(N/chunk) cursor pages, newest first."""
    rows = [row(i, i) for i in range(N - 1, -1, -1)]
    out, cur = {}, None
    for i in range(0, len(rows), chunk):
        nxt = f"c{i // chunk + 1}" if i + chunk < len(rows) else ""
        out[cur] = {"trades": rows[i:i + chunk], "cursor": nxt}
        cur = nxt or None
    return out


class Slow:
    """The scripted venue, with a pause per page so the racers interleave."""

    def __init__(self, delay=0.002):
        self.pages, self.delay = pages(), delay

    def get(self, url, params=None, headers=None, raw=False):
        time.sleep(self.delay)
        return self.pages[(params or {}).get("cursor")]


def _worker(d, budget, results, idx):
    """results is a dict (threads) or a directory path (processes: no shared memory, and the
    sandbox has no local sockets for a Manager)."""
    c = TradesClient(http=Slow(), cache_dir=d, clock=lambda: NOW)
    try:
        t = c.kalshi_tape(TK, None, HI, max_pages=budget, checkpoint_every=1)
        out = ("complete", len(t.trades))
    except IncompleteTape:
        out = ("incomplete", 0)
    except BaseException as e:  # noqa: BLE001
        out = (f"{type(e).__name__}: {e}", 0)
    if isinstance(results, str):
        with open(os.path.join(results, f"r{idx}.json"), "w") as f:
            json.dump(out, f)
    else:
        results[idx] = out


class Watcher(threading.Thread):
    """Poll the cache file and record every complete -> incomplete transition."""

    def __init__(self, path):
        super().__init__(daemon=True)
        self.path, self.stop, self.downgrades, self.wrong = path, False, 0, 0
        self.was_complete = False

    def run(self):
        while not self.stop:
            try:
                with open(self.path, encoding="utf-8") as f:
                    doc = json.load(f)
            except (OSError, ValueError):
                time.sleep(0.001)
                continue
            if doc.get("complete"):
                if len(doc.get("trades", [])) != N:
                    self.wrong += 1
                self.was_complete = True
            elif self.was_complete:
                self.downgrades += 1
                self.was_complete = False
            time.sleep(0.001)


def race(kind, workers=6, rounds=12, no_fcntl=False):
    tot = {"wrong": 0, "downgrade": 0, "complete": 0, "incomplete": 0, "error": 0}
    saved = trades.fcntl
    if no_fcntl:
        trades.fcntl = None
    try:
        for r in range(rounds):
            d = tempfile.mkdtemp(prefix="race_")
            path = os.path.join(d, trades._cache_key("kalshi", TK, None, HI) + ".json")
            w = Watcher(path)
            w.start()
            try:
                results = {}
                budgets = [1, 2, 3, 5, 50, 50]
                if kind == "threads":
                    ts = [threading.Thread(target=_worker, args=(d, budgets[i % len(budgets)], results, i))
                          for i in range(workers)]
                    for t in ts:
                        t.start()
                    for t in ts:
                        t.join()
                else:
                    box = tempfile.mkdtemp(prefix="res_")
                    ps = [multiprocessing.Process(target=_worker, args=(d, budgets[i % len(budgets)], box, i))
                          for i in range(workers)]
                    for p in ps:
                        p.start()
                    for p in ps:
                        p.join()
                    results = {}
                    for n in os.listdir(box):
                        with open(os.path.join(box, n)) as f:
                            results[n] = tuple(json.load(f))
                    shutil.rmtree(box, ignore_errors=True)
                for v in results.values():
                    if v[0] == "complete":
                        tot["complete"] += 1
                        if v[1] != N:
                            tot["wrong"] += 1
                    elif v[0] == "incomplete":
                        tot["incomplete"] += 1
                    else:
                        tot["error"] += 1
                        print("   worker error:", v[0])
            finally:
                w.stop = True
                w.join(timeout=1)
                tot["wrong"] += w.wrong
                tot["downgrade"] += w.downgrades
                shutil.rmtree(d, ignore_errors=True)
    finally:
        trades.fcntl = saved
    return tot


def stuck_lock():
    """Another writer holds the lock and never lets go."""
    if trades.fcntl is None:
        return "no fcntl"
    import fcntl as F
    d = tempfile.mkdtemp(prefix="stuck_")
    try:
        lock = os.path.join(d, "." + trades._cache_key("kalshi", TK, None, HI) + ".json.lock")
        fd = os.open(lock, os.O_RDONLY | os.O_CREAT, 0o644)
        F.flock(fd, F.LOCK_EX)
        c = TradesClient(http=Slow(0), cache_dir=d, clock=lambda: NOW, lock_timeout_s=0.05)
        t = c.kalshi_tape(TK, None, HI)
        out = (f"tape returned with {len(t.trades)} prints, cached={os.path.exists(os.path.join(d, trades._cache_key('kalshi', TK, None, HI) + '.json'))}, "
               f"last_store_error={c.last_store_error}")
        F.flock(fd, F.LOCK_UN)
        os.close(fd)
        return out
    finally:
        shutil.rmtree(d, ignore_errors=True)


def foreign_lock():
    """A lock file this user may not even open (mode 000), as another account's would be."""
    d = tempfile.mkdtemp(prefix="foreign_")
    try:
        lock = os.path.join(d, "." + trades._cache_key("kalshi", TK, None, HI) + ".json.lock")
        open(lock, "w").close()
        os.chmod(lock, 0o000)
        c = TradesClient(http=Slow(0), cache_dir=d, clock=lambda: NOW)
        try:
            t = c.kalshi_tape(TK, None, HI)
            got = f"{len(t.trades)} prints returned, last_store_error={c.last_store_error}"
        except BaseException as e:  # noqa: BLE001
            got = f"raised {type(e).__name__}: {e}"
        os.chmod(lock, 0o644)
        return got
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main(which="all"):
    if which in ("all", "threads"):
        banner("A2a  6 threads on one tape (fcntl present)")
        print(race("threads"))
    if which in ("all", "procs"):
        banner("A2b  6 processes on one tape (fcntl present)")
        print(race("procs"))
    if which in ("all", "nofcntl"):
        banner("A2c  6 threads on one tape with trades.fcntl = None (the Windows path)")
        print(race("threads", no_fcntl=True))
    if which in ("all", "lock"):
        banner("A2d  a stuck lock, and a lock file this user cannot open")
        print("stuck lock  :", stuck_lock())
        print("foreign lock:", foreign_lock())
    return 0


if __name__ == "__main__":
    multiprocessing.set_start_method("fork", force=True)
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "all"))

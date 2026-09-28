"""Replay one fuzz seed with every request, page and cache write printed, to name the
mechanism behind a violation.  Usage: python3 audit/trace_seed.py <seed>
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sys
import tempfile

import fuzz_tape as F  # noqa: E402
from arb_engine.venues.trades import ConflictingTrades, IncompleteTape, TradesClient  # noqa: E402


class Loud(F.Venue):
    def get(self, url, params=None, headers=None, raw=False):
        cur = (params or {}).get("cursor")
        try:
            out = F.Venue.get(self, url, params, headers, raw)
        except BaseException as e:
            print(f"    GET cursor={cur!r} -> {type(e).__name__} {getattr(e, 'status', '')}")
            raise
        if isinstance(out, dict) and isinstance(out.get("trades"), list):
            ids = [r["trade_id"] for r in out["trades"]]
            nxt = out.get("cursor")
            known = " (A CURSOR ALREADY HANDED OUT)" if nxt in self.cursors and nxt else ""
            print(f"    GET cursor={cur!r} -> {ids} next={nxt!r}{known} [cursor->index {self.cursors}]")
        else:
            print(f"    GET cursor={cur!r} -> malformed {out!r}")
        return out


def main(seed):
    rnd = random.Random(seed)
    rows = F.make_rows(rnd, rnd.choice([1, 5, 12, 30]))
    want = F.truth(rows)
    print(f"venue truth: {len(want)} prints, ids {sorted(want)}")
    v = Loud(rnd, rows)
    print(f"venue: page={v.page} overlap={v.overlap} p_fail={v.p_fail} p_malformed={v.p_malformed} "
          f"p_stale={v.p_stale} p_repeat={v.p_repeat}")
    d = tempfile.mkdtemp(prefix="trace_")
    try:
        for n in range(400):
            budget = rnd.choice([1, 1, 2, 3, 7, 50])
            chk = rnd.choice([1, 2, 10])
            print(f"call {n}: max_pages={budget} checkpoint_every={chk}")
            c = TradesClient(http=v, cache_dir=d, clock=lambda: F.NOW)
            try:
                tape = c.kalshi_tape(F.TK, F.LO, F.HI, max_pages=budget, checkpoint_every=chk)
            except IncompleteTape as e:
                print(f"    -> IncompleteTape: {e}")
                _dump(d)
                continue
            except ConflictingTrades as e:
                print(f"    -> ConflictingTrades: {e}")
                _dump(d)
                continue
            got = {t.trade_id for t in tape.trades}
            print(f"    -> COMPLETE with {len(got)} prints; missing {sorted(set(want) - got)} extra {sorted(got - set(want))}")
            print(f"       pages={tape.pages} restarts={tape.restarts} duplicates={tape.duplicates} rejected={tape.rejected}")
            return 0 if got == set(want) else 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _dump(d):
    for n in sorted(os.listdir(d)):
        if n.endswith(".json"):
            with open(os.path.join(d, n)) as f:
                doc = json.load(f)
            print(f"       cache: complete={doc['complete']} pages={doc['pages']} next={doc['next_cursor']!r} "
                  f"seen={doc['seen_cursors']} restarts={doc['restarts']} n={len(doc['trades'])}")


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1])))

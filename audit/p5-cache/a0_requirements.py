"""A0 - one offline check per stated requirement. Prints HOLDS / FAILS per line."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from common import TK, BASE, NoNetwork, Pages, banner, row  # noqa: E402
from arb_engine.venues.trades import (SETTLE_S, ConflictingTrades, IncompleteTape, TradesClient,  # noqa: E402
                                      _cache_key, _legacy_cache_key, tape_from_doc)

NOW = BASE + 100_000.0
HI = BASE + 5_000.0
RESULTS = []


def check(name, where, fn):
    try:
        ok, detail = fn()
    except BaseException as e:  # noqa: BLE001
        ok, detail = False, f"{type(e).__name__}: {e}"
    RESULTS.append((name, ok, where, detail))
    print(f"{'HOLDS' if ok else 'FAILS':<5}  {name}\n         {where}\n         {detail}")


def tmp():
    d = tempfile.mkdtemp(prefix="req_")
    return d


def three_pages():
    return {None: {"trades": [row(i, i) for i in range(29, 19, -1)], "cursor": "c1"},
            "c1": {"trades": [row(i, i) for i in range(19, 9, -1)], "cursor": "c2"},
            "c2": {"trades": [row(i, i) for i in range(9, -1, -1)], "cursor": ""}}


def c(http, d, **kw):
    return TradesClient(http=http, cache_dir=d, clock=lambda: NOW, **kw)


def r_schema():
    d = tmp()
    try:
        c(Pages(three_pages()), d).kalshi_tape(TK, None, HI)
        doc = json.loads(Path(d, _cache_key("kalshi", TK, None, HI) + ".json").read_text())
        keys = {"schema", "complete", "reason", "pages", "next_cursor", "seen_cursors",
                "pass_started_at", "duplicates", "rejected", "restarts", "fetched_at"}
        return keys <= set(doc), f"stored keys include {sorted(keys & set(doc))}; schema={doc.get('schema')}"
    finally:
        shutil.rmtree(d, True)


def r_fractional_key():
    a = _cache_key("kalshi", TK, 100.2, 200.7)
    b = _cache_key("kalshi", TK, 100.9, 200.1)
    e = _cache_key("kalshi", TK, 100.0, 200.0)
    f = _cache_key("kalshi", TK, 100, 200)
    g = _cache_key("kalshi", TK, 1e-9, 200.0)
    h = _cache_key("kalshi", TK, 0.0, 200.0)
    i = _cache_key("kalshi", TK, -0.0, 200.0)
    ok = len({a, b}) == 2 and e == f and g != h and h == i
    return ok, f"[100.2,200.7] != [100.9,200.1]: {a[-6:]} vs {b[-6:]}; 100 == 100.0: {e == f}; 1e-9 != 0.0: {g != h}; -0.0 == 0.0: {h == i}"


def r_same_second_windows():
    d = tmp()
    try:
        http = Pages(three_pages())
        c(http, d).kalshi_tape(TK, BASE + 100.2, BASE + 200.7)
        c(http, d).kalshi_tape(TK, BASE + 100.9, BASE + 200.1)
        files = sorted(n for n in os.listdir(d) if n.endswith(".json"))
        asked = [(p.get("min_ts"), p.get("max_ts")) for p in http.calls]
        return len(files) == 2, f"{len(files)} cache files; API asked {asked[0]} and {asked[3]} (whole seconds, widened by 1)"
    finally:
        shutil.rmtree(d, True)


def r_old_files():
    d = tmp()
    try:
        os.makedirs(d, exist_ok=True)
        p = Path(d, _legacy_cache_key("kalshi", TK, None, HI) + ".json")
        p.write_text(json.dumps({"venue": "kalshi", "market": TK, "min_ts": None, "max_ts": HI, "fetched_at": 0,
                                 "trades": [{"venue": "kalshi", "market": TK, "ts": BASE + 1, "price": .5, "size": 1, "side": "yes", "trade_id": "t1"}]}))
        try:
            c(NoNetwork(), d, offline=True).kalshi_tape(TK, None, HI)
            return False, "the old file was served"
        except FileNotFoundError as e:
            return str(p) in str(e) and "never used" in str(e), f"refused and named: {str(e)[:110]}..."
    finally:
        shutil.rmtree(d, True)


def r_offline_incomplete():
    d = tmp()
    try:
        try:
            c(Pages(three_pages()), d).kalshi_tape(TK, None, HI, max_pages=1)
        except IncompleteTape:
            pass
        try:
            got = c(NoNetwork(), d, offline=True).kalshi_tape(TK, None, HI)
            return False, f"served {len(got.trades)} prints offline"
        except IncompleteTape as e:
            return True, f"IncompleteTape: {str(e)[:110]}..."
    finally:
        shutil.rmtree(d, True)


def r_budget_cursor():
    d = tmp()
    try:
        try:
            c(Pages(three_pages()), d).kalshi_tape(TK, None, HI, max_pages=1)
            return False, "no error on a used-up budget"
        except IncompleteTape as e:
            doc = json.loads(Path(d, _cache_key("kalshi", TK, None, HI) + ".json").read_text())
            return doc["next_cursor"] == "c1" and not doc["complete"], \
                f"raised, and left next_cursor={doc['next_cursor']!r} with {len(doc['trades'])} prints; {str(e)[:70]}..."
    finally:
        shutil.rmtree(d, True)


def r_resume_bigger_budget():
    d = tmp()
    try:
        http = Pages(three_pages())
        try:
            c(http, d).kalshi_tape(TK, None, HI, max_pages=1)
        except IncompleteTape:
            pass
        n0 = len(http.calls)
        t = c(http, d).kalshi_tape(TK, None, HI, max_pages=50)
        asked = [p.get("cursor") for p in http.calls[n0:]]
        return len(t.trades) == 30 and asked == ["c1", "c2"], f"{len(t.trades)} prints, resumed by asking only {asked}"
    finally:
        shutil.rmtree(d, True)


def r_dupes():
    d = tmp()
    try:
        over = {None: {"trades": [row(i, i) for i in range(29, 19, -1)], "cursor": "c1"},
                "c1": {"trades": [row(i, i) for i in range(21, 9, -1)], "cursor": "c2"},   # 2 rows overlap
                "c2": {"trades": [row(i, i) for i in range(11, -1, -1)], "cursor": ""}}
        t = c(Pages(over), d).kalshi_tape(TK, None, HI)
        ids = [x.trade_id for x in t.trades]
        return len(ids) == 30 and ids == sorted(set(ids)) and t.duplicates == 4, \
            f"{len(ids)} unique prints, duplicates={t.duplicates}, sorted oldest first"
    finally:
        shutil.rmtree(d, True)


def r_conflict():
    d = tmp()
    try:
        p = three_pages()
        p["c1"]["trades"] = [row(20, 20, price=0.99)] + p["c1"]["trades"]      # t0020 again, another price
        try:
            c(Pages(p), d).kalshi_tape(TK, None, HI)
            return False, "the conflict was not surfaced"
        except ConflictingTrades as e:
            doc = json.loads(Path(d, _cache_key("kalshi", TK, None, HI) + ".json").read_text())
            return not doc["complete"] and doc["next_cursor"] is None, f"ConflictingTrades, tape kept incomplete: {str(e)[:100]}..."
    finally:
        shutil.rmtree(d, True)


def r_repeat_cursor():
    d = tmp()
    try:
        p = three_pages()
        p["c1"]["cursor"] = "c1"
        try:
            c(Pages(p), d).kalshi_tape(TK, None, HI)
            return False, "the repeated cursor was followed"
        except IncompleteTape as e:
            return "repeated cursor" in str(e), f"{str(e)[:100]}..."
    finally:
        shutil.rmtree(d, True)


def r_same_second():
    d = tmp()
    try:
        # three prints in one second, split across a page boundary, with one repeated
        a = [row(2, 10, frac=0.75), row(1, 10, frac=0.50)]
        b = [row(1, 10, frac=0.50), row(0, 10, frac=0.25)]
        t = c(Pages({None: {"trades": a, "cursor": "c1"}, "c1": {"trades": b, "cursor": ""}}), d).kalshi_tape(TK, None, HI)
        return [x.trade_id for x in t.trades] == ["t0000", "t0001", "t0002"] and t.duplicates == 1, \
            f"{[x.trade_id for x in t.trades]}, duplicates={t.duplicates}"
    finally:
        shutil.rmtree(d, True)


def r_provenance():
    d = tmp()
    try:
        cl = c(NoNetwork(), d)
        from arb_engine.venues.trades import Trade
        cl.store_complete("kalshi", TK, BASE, HI, [Trade("kalshi", TK, BASE + 1, .5, 1, "yes", "a")])
        doc = json.loads(Path(d, _cache_key("kalshi", TK, BASE, HI) + ".json").read_text())
        labelled = "source" in doc
        return labelled, ("the file says how it was produced: source=" + repr(doc.get("source")) if labelled else
                          f"a tape asserted by store_complete() is stored as complete with no field saying so "
                          f"(keys {sorted(set(doc) - {'trades'})}); only pages=={doc['pages']} hints at it")
    finally:
        shutil.rmtree(d, True)


def main():
    banner("A0  requirement checks")
    check("the cache schema explicitly records completeness", "arb_engine/venues/trades.py:452 (_write)", r_schema)
    check("the cache key preserves fractional min/max timestamps", "arb_engine/venues/trades.py:267 (_cache_key)", r_fractional_key)
    check("windows that truncate to the same seconds never share a file", "arb_engine/venues/trades.py:566 (widen+trim)", r_same_second_windows)
    check("old cache files without metadata are treated conservatively", "arb_engine/venues/trades.py:503 (_older_files_note)", r_old_files)
    check("an incomplete cache can never silently satisfy an offline request", "arb_engine/venues/trades.py:514 (_cached_or_offline)", r_offline_incomplete)
    check("max_pages exhaustion leaves a resumable cursor", "arb_engine/venues/trades.py:583 (budget check)", r_budget_cursor)
    check("a larger page budget resumes from the incomplete state", "arb_engine/venues/trades.py:553 (resume guard)", r_resume_bigger_budget)
    check("duplicate trade ids collapse deterministically", "arb_engine/venues/trades.py:621 (duplicates)", r_dupes)
    check("conflicting duplicate payloads are surfaced", "arb_engine/venues/trades.py:616 (ConflictingTrades)", r_conflict)
    check("repeated cursors are detected", "arb_engine/venues/trades.py:631 (seen cursors)", r_repeat_cursor)
    check("same-second prints at a page boundary are handled", "arb_engine/venues/trades.py:608 (dedupe by id)", r_same_second)
    check("no cache entry can be mistaken for evidence without provenance", "arb_engine/venues/trades.py:486 (store_complete)", r_provenance)
    bad = [n for n, ok, *_ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)} hold, {len(bad)} fail: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

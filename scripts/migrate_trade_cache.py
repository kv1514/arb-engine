#!/usr/bin/env python3
"""Move trade-tape cache files that the tape client will never use out of the cache.

    python3 scripts/migrate_trade_cache.py [--cache-dir out/cache/trades] [--to out/cache/trades-legacy] [--apply]

``arb_engine/venues/trades.py`` (schema 3) serves only tapes it can prove complete, and uses a
cache file only after checking it (``trades.tape_from_doc``). This script applies the same
checks to every ``*.json`` file in the cache and lists (dry run) or, with ``--apply``, moves aside
the ones the client will never use:

* ``old format``: no ``schema`` (``<venue>-<market>-<int min>-<int max>.json``); it cannot say
  whether its fetch ran out of pages;
* ``schema N``: written by an earlier version of the client (schema 2 tapes were never
  re-checked against the closed-window rule, and the first round's can be cut short);
* ``fails checks``: the current schema, but a check the client makes fails (printed), or the
  file name is not the key of the query the file stores;
* ``unreadable`` / ``not an object``.

Current files - complete tapes and resumable partial ones - are left alone. Complete tapes are
checked with the client's default settle margin. Files move one at a time to ``--to``, which
must not be the cache directory itself: a file that cannot be read is moved too, a name already
taken there gets a numbered suffix, and one failure never stops the rest. Nothing is deleted.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from arb_engine.venues.trades import TAPE_SCHEMA, _cache_key, tape_from_doc  # noqa: E402


def classify(path: str) -> tuple[str, str]:
    """('current', '') or (why the client never uses the file, detail)."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        return "unreadable", type(e).__name__
    if not isinstance(doc, dict):
        return "not an object", ""
    if "schema" not in doc:
        return "old format", ""
    if doc["schema"] != TAPE_SCHEMA:
        return f"schema {doc['schema']!r}", ""
    try:
        tape = tape_from_doc(doc)
    except (TypeError, ValueError) as e:
        return "fails checks", str(e)
    key = _cache_key(tape.venue, tape.market, tape.min_ts, tape.max_ts, tape.condition_id)
    if os.path.basename(path) != key + ".json":
        return "fails checks", f"the file name is not the key of its query ({key}.json)"
    return "current", ""


def free_name(dest_dir: str, name: str) -> str:
    base, ext = os.path.splitext(name)
    cand, i = os.path.join(dest_dir, name), 1
    while os.path.exists(cand):
        cand = os.path.join(dest_dir, f"{base}.{i}{ext}")
        i += 1
    return cand


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Move unusable trade-tape cache files aside (dry run unless --apply).")
    ap.add_argument("--cache-dir", default="out/cache/trades")
    ap.add_argument("--to", default="out/cache/trades-legacy")
    ap.add_argument("--apply", action="store_true", help="move the files (default: only list them)")
    a = ap.parse_args(argv)
    if not os.path.isdir(a.cache_dir):
        print(f"no cache directory at {a.cache_dir}")
        return 0
    if os.path.realpath(a.to) == os.path.realpath(a.cache_dir):
        print(f"--to {a.to} is the cache directory itself: name another directory", file=sys.stderr)
        return 2
    counts: dict[str, int] = {}
    failures = 0
    for name in sorted(os.listdir(a.cache_dir)):
        path = os.path.join(a.cache_dir, name)
        if not name.endswith(".json") or name.startswith(".") or not os.path.isfile(path):
            continue
        kind, detail = classify(path)
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "current":
            continue
        why = f"{kind}: {detail}" if detail else kind
        if not a.apply:
            print(f"would move ({why}): {path}")
            continue
        try:
            os.makedirs(a.to, exist_ok=True)
            dest = free_name(a.to, name)
            shutil.move(path, dest)
            print(f"moved ({why}): {path} -> {dest}")
        except OSError as e:
            failures += 1
            print(f"could not move {path}: {e}", file=sys.stderr)
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "no files"
    print(f"{summary}{'' if a.apply else ' (dry run: add --apply to move)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

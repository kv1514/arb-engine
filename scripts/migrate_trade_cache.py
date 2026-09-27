#!/usr/bin/env python3
"""Move trade-tape cache files that the tape client will never use out of the cache.

    python3 scripts/migrate_trade_cache.py [--cache-dir out/cache/trades] [--to out/cache/trades-legacy] [--apply]

``arb_engine/venues/trades.py`` (schema 2) serves only tapes it can prove complete. Files of the
old format (no ``schema``: ``<venue>-<market>-<int min>-<int max>.json``) cannot say whether
their fetch ran out of pages, so they are never read; neither is a file that is not valid
JSON or not a JSON object. This script lists them (dry run) or, with ``--apply``, moves them
to ``--to``, one file at a time: a file that cannot be read is moved too, a name already taken
in ``--to`` gets a numbered suffix, and one failure never stops the rest. Current (schema 2)
files are left alone. Nothing is deleted.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

SCHEMA = 2


def classify(path: str) -> str:
    """'current', or why the file is not used ('old format', 'unreadable', 'not an object')."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return "unreadable"
    if not isinstance(doc, dict):
        return "not an object"
    return "current" if doc.get("schema") == SCHEMA else "old format"


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
    counts: dict[str, int] = {}
    failures = 0
    for name in sorted(os.listdir(a.cache_dir)):
        path = os.path.join(a.cache_dir, name)
        if not name.endswith(".json") or not os.path.isfile(path):
            continue
        kind = classify(path)
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "current":
            continue
        if not a.apply:
            print(f"would move ({kind}): {path}")
            continue
        try:
            os.makedirs(a.to, exist_ok=True)
            dest = free_name(a.to, name)
            shutil.move(path, dest)
            print(f"moved ({kind}): {path} -> {dest}")
        except OSError as e:
            failures += 1
            print(f"could not move {path}: {e}", file=sys.stderr)
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "no files"
    print(f"{summary}{'' if a.apply else ' (dry run: add --apply to move)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

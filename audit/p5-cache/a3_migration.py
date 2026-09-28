"""A3 - scripts/migrate_trade_cache.py against the files a real cache accumulates.

Probes: destination collisions, --to inside / equal to --cache-dir, unreadable files, symlinks
(good, bad and dangling), non-JSON, a directory named *.json, leftovers of a killed writer
(.tape-*.tmp) and stale lock files, and a complete file whose pass_started_at is in the future.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from common import TK, BASE, Pages, banner, row  # noqa: E402
from arb_engine.venues.trades import TradesClient, _cache_key  # noqa: E402
import scripts.migrate_trade_cache as mig  # noqa: E402

NOW = BASE + 100_000.0
HI = BASE + 5_000.0


def build(d):
    """One good tape, then the debris."""
    http = Pages({None: {"trades": [row(i, i) for i in range(9, -1, -1)], "cursor": ""}})
    TradesClient(http=http, cache_dir=d, clock=lambda: NOW).kalshi_tape(TK, None, HI)
    good = os.path.join(d, _cache_key("kalshi", TK, None, HI) + ".json")
    doc = json.loads(Path(good).read_text())
    return good, doc


def run(d, to, apply=False):
    out, err = io.StringIO(), io.StringIO()
    args = ["--cache-dir", d, "--to", to] + (["--apply"] if apply else [])
    with redirect_stdout(out), redirect_stderr(err):
        rc = mig.main(args)
    return rc, out.getvalue(), err.getvalue()


def case(name, fn):
    banner(name)
    d = tempfile.mkdtemp(prefix="mig_")
    try:
        fn(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def c_future_stamp(d):
    good, doc = build(d)
    doc["pass_started_at"] = NOW + 10_000_000.0          # a stamp no clock here can have produced
    Path(good).write_text(json.dumps(doc))
    rc, out, err = run(d, os.path.join(d, "legacy"))
    print(out.strip() or "(no output)")
    print("EXPECTED: the file is named as unusable (its pass began after any clock that reads it)")


def c_unreadable_and_odd(d):
    build(d)
    Path(d, "locked.json").write_text("{}")
    os.chmod(os.path.join(d, "locked.json"), 0o000)
    Path(d, "notes.txt").write_text("keep")
    os.makedirs(os.path.join(d, "adir.json"))
    Path(d, ".tape-abcd.tmp").write_text("half a document")     # a killed writer's leftover
    Path(d, "." + _cache_key("kalshi", TK, None, HI) + ".json.lock").write_text("")
    rc, out, err = run(d, os.path.join(d, "legacy"), apply=True)
    os.chmod(os.path.join(d, "legacy", "locked.json"), 0o644) if os.path.exists(os.path.join(d, "legacy", "locked.json")) else None
    print(out.strip())
    print("stderr:", err.strip() or "(none)")
    left = sorted(os.listdir(d))
    print("left in the cache:", left)
    print("EXPECTED: the .tape-*.tmp leftover is at least reported; a directory named *.json is skipped")


def c_symlinks(d):
    good, doc = build(d)
    outside = tempfile.mkdtemp(prefix="outside_")
    try:
        target = os.path.join(outside, "real.json")
        Path(target).write_text(json.dumps(dict(doc, schema=2)))          # an unusable tape, outside the cache
        os.symlink(target, os.path.join(d, "link-to-schema2.json"))
        os.symlink(os.path.join(outside, "gone.json"), os.path.join(d, "dangling.json"))
        os.symlink(outside, os.path.join(d, "dirlink.json"))
        rc, out, err = run(d, os.path.join(d, "legacy"), apply=True)
        print(out.strip())
        print("stderr:", err.strip() or "(none)")
        print("target still there:", os.path.exists(target))
        print("cache now:", sorted(os.listdir(d)))
        print("EXPECTED: the symlink moves (not its target); the dangling one is at least reported")
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def c_to_equals_cache(d):
    build(d)
    for to in (d, d + "/", d + "/.", os.path.join(d, "..", os.path.basename(d))):
        rc, out, err = run(d, to)
        print(f"--to {to!r} -> rc={rc} {err.strip()[:70]!r}")
    link = tempfile.mkdtemp(prefix="link_")
    shutil.rmtree(link)
    os.symlink(d, link)
    rc, out, err = run(d, link)
    print(f"--to <symlink to the cache dir> -> rc={rc} {err.strip()[:70]!r}")
    os.unlink(link)
    print("EXPECTED: every spelling of the cache directory itself is refused (rc=2)")


def c_collisions(d):
    good, doc = build(d)
    dest = os.path.join(d, "legacy")
    os.makedirs(dest)
    for n in ("a.json", "a.1.json", "a.2.json"):
        Path(dest, n).write_text("{}")
    Path(d, "a.json").write_text("not json")
    rc, out, err = run(d, dest, apply=True)
    print(out.strip())
    print("destination:", sorted(os.listdir(dest)))
    print("EXPECTED: the file lands under a free name, nothing in the destination is overwritten")


def main():
    case("A3a  a complete file stamped in the future", c_future_stamp)
    case("A3b  unreadable, non-JSON, a directory named *.json, a killed writer's leftovers", c_unreadable_and_odd)
    case("A3c  symlinks: to an unusable tape outside the cache, dangling, and to a directory", c_symlinks)
    case("A3d  --to spelled as the cache directory itself", c_to_equals_cache)
    case("A3e  destination name collisions", c_collisions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Build ``arb_engine/data/nfl_margin_dist.json`` and ``margin_sigma.json`` from nflverse games.csv.

    curl -L -o /tmp/games.csv https://github.com/nflverse/nfldata/raw/master/data/games.csv
    python scripts/build_margin_table.py /tmp/games.csv

Hand-download on purpose: the build is a pure function of the file (see
``arb_engine/quant/margintable.py``) and the written ``source`` block carries its sha256 and
row count, so anyone can re-run this and diff byte-for-byte. ``--expected`` writes the
trimmed-fixture expectation used by ``tests/test_margintable.py`` instead.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arb_engine.quant.margintable import DEFAULT_MIN_SEASON, build_tables, dump_json  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("games_csv", help="path to a hand-downloaded nflverse games.csv")
    ap.add_argument("--min-season", type=int, default=DEFAULT_MIN_SEASON, help="first season for the margin buckets (default 2016)")
    ap.add_argument("--max-season", type=int, help="last season to use (default: every complete season; the one in progress is excluded so evaluated games never enter the tables)")
    ap.add_argument("--sport", default="nfl")
    ap.add_argument("--out-dir", default=str(ROOT / "arb_engine" / "data"))
    ap.add_argument("--expected", help="write a single combined {dist, sigma} JSON here instead (test fixture)")
    args = ap.parse_args(argv)

    path = Path(args.games_csv)
    text = path.read_text(encoding="utf-8")
    dist, sig = build_tables(text, name=path.name, min_season=args.min_season, sport=args.sport, max_season=args.max_season)
    if args.expected:
        Path(args.expected).write_text(dump_json({"dist": dist, "sigma": sig}), encoding="utf-8")
        print(f"wrote {args.expected}")
        return 0
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{args.sport}_margin_dist.json").write_text(dump_json(dist), encoding="utf-8")
    (out / "margin_sigma.json").write_text(dump_json(sig), encoding="utf-8")
    src = dist["source"]
    print(f"{args.sport}: {src['rows_used']} games {src['seasons']} (excluded incomplete {src['excluded_incomplete_seasons']}) from {src['rows']} rows (sha256 {src['sha256'][:12]}), "
          f"{len(dist['buckets'])} spread buckets; margin sigma {sig['margin']['sigma']} (se {sig['margin']['se']}), "
          f"total sigma {sig['total']['sigma']}, tie rate {sig['tie_rate']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

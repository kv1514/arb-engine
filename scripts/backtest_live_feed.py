#!/usr/bin/env python3
"""Feed parity: the replay's ESPN-derived game state vs nflverse play-by-play, one NFL week.

    python scripts/backtest_live_feed.py --season 2026 --week 1
    python scripts/backtest_live_feed.py --season 2025 --week 1 --limit 4 --offline

For every final of the week the ESPN summary (read-through cache under
``out/cache/history/espn/``, the replay's layout) is reduced to per-play rows exactly as
``backtest.py`` sees them (``history.espn_timeline`` -> ``feedparity.espn_state_plays``),
aligned to the nflverse rows of the same game (``play_by_play_<season>.csv.gz``, downloaded
by ``scripts/train_wp_model.py`` into ``$TMPDIR/nflpbp`` or given with ``--pbp``), and the
per-field agreement rates (down, distance, yardline_100, possession, timeouts, clock) are
pooled with the first 20 disagreements per field. Writes ``out/feed_parity_w<N>.txt`` and
the same numbers as JSON beside it.

The acceptance bar (plan item P04): timeouts >= 95 % exact on a full week; anything below
that is a P02/P03 bug to file with the printed discrepancy list before P05 starts.

2026 week 1 (16 games, 2,633 aligned plays; ``tests/fixtures/results/feed_parity_w1_p04.json``):
down 0.9996, distance 0.9983, yardline_100 0.9984, possession 0.9996, home timeouts 0.9996,
away timeouts 1.0000, clock 0.9472. What the discrepancy lists say:

* ESPN stamps scoring plays (and the odd turnover) with the *post-play* clock, 2–10 s after
  the snap clock nflverse records — hence the clock rate and the 10 s alignment slack.
* Timeouts only reconcile once a lost coach's challenge ("… challenged …, and the play was
  Upheld") is charged as a timeout and ESPN's GSIS club codes (BLT, ARZ, HST, CLV) are
  mapped; a plain "Timeout #N by X" count is 92–95 % (P02's ``count_timeouts`` must do both).
* ESPN gives some timeout rows a wallclock exactly one day late (12 rows in the week, 11 of
  them in SF@LAR); the replay sorts plays by wallclock, so those rows land after the game
  with a Q4 score (a P03 bug: sort by drive order / ``sequenceNumber``, clamp the wallclock).
* The remaining down/distance/yardline disagreements are single-yard spot differences on
  4 plays and one first-down ruling (SF@LAR Q4 5:52: ESPN 4th & 1, nflverse 1st & 10).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from arb_engine.quant import feedparity as fp  # noqa: E402
from arb_engine.venues.espn import ESPNClient  # noqa: E402

DEFAULT_CACHE = REPO / "out" / "cache" / "history" / "espn"


def default_pbp(season: int) -> Path:
    base = Path(os.environ.get("TMPDIR") or "out/data") / "nflpbp"
    return base / f"play_by_play_{season}.csv.gz"


def finals_of_week(espn: ESPNClient, season: int, week: int, cache_dir: Path | None, offline: bool) -> list[dict]:
    sb = fp.cached_json(cache_dir, f"scoreboard_nfl_{season}_w{week}", lambda: espn.scoreboard_week(season, week), offline)
    out = []
    for ev in sb.get("events") or []:
        st = ((ev.get("status") or {}).get("type") or {}).get("name") or ""
        if st == "STATUS_FINAL":
            out.append({"id": str(ev.get("id")), "name": ev.get("name"), "date": ev.get("date")})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--week", type=int, default=1)
    ap.add_argument("--espn", nargs="*", help="ESPN event ids instead of the whole week")
    ap.add_argument("--limit", type=int, help="first N finals of the week")
    ap.add_argument("--pbp", help="nflverse play_by_play_<season>.csv.gz (default: $TMPDIR/nflpbp/…)")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE), help="ESPN read-through cache ('' to disable)")
    ap.add_argument("--offline", action="store_true", help="fail instead of fetching anything")
    ap.add_argument("--clock-tolerance", type=int, default=10, help="seconds of clock slack when aligning plays (ESPN stamps scoring plays with the post-play clock)")
    ap.add_argument("--timeouts", choices=("text", "rows"), default="text", help="ESPN timeouts: recount from play texts (independent) or take the PlayRow fields (P03)")
    ap.add_argument("--out", help="text report path (default out/feed_parity_w<N>.txt)")
    ap.add_argument("--json", help="JSON report path (default beside --out)")
    ap.add_argument("--record", help="also write a metrics-only JSON (pooled rates, per-game rates, no discrepancy lists) e.g. tests/fixtures/results/feed_parity_w1_p04.json")
    args = ap.parse_args(argv)

    pbp = Path(args.pbp) if args.pbp else default_pbp(args.season)
    if not pbp.exists():
        print(f"nflverse file missing: {pbp} (python scripts/train_wp_model.py --years {args.season} downloads it, or curl the nflverse-data release)", file=sys.stderr)
        return 2
    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    espn = ESPNClient(sport="nfl")
    if args.espn:
        games = [{"id": e, "name": e} for e in args.espn]
    else:
        games = finals_of_week(espn, args.season, args.week, cache_dir, args.offline)
    if args.limit:
        games = games[: args.limit]
    print(f"{len(games)} games; nflverse {pbp}")
    nfl = fp.load_nflverse_pbp(pbp, weeks=[args.week] if not args.espn else None)
    reports: list[dict] = []
    per_game_lines: list[str] = []
    for g in games:
        try:
            summary = fp.cached_json(cache_dir, f"summary_{g['id']}", lambda gid=g["id"]: espn.summary(gid), args.offline)
        except Exception as e:
            per_game_lines.append(f"  {g['name']}: no summary ({e!r})")
            continue
        esp, meta = fp.espn_state_plays(summary, timeouts=args.timeouts)
        ng = fp.find_nfl_game(nfl, meta["home"], meta["away"], meta.get("kickoff"))
        label = f"{meta['away']}@{meta['home']}"
        if ng is None:
            per_game_lines.append(f"  {label}: no nflverse game for {meta.get('kickoff')} (nflverse has {len(nfl)} games)")
            continue
        pairs = fp.align_plays(esp, ng.plays, clock_tolerance=args.clock_tolerance)
        rep = fp.agreement(pairs)
        rep["_game"] = label
        rep["_wallclock_anomalies"] = meta.get("wallclock_anomalies", 0)
        reports.append(rep)
        a = rep["_alignment"]
        per_game_lines.append(f"  {label:<8} espn {len(esp):>4} nfl {len(ng.plays):>4} aligned {a['matched']:>4} unmatched {a['espn_unmatched']:>3}/{a['nfl_unmatched']:<3} wallclock-anomalies {meta.get('wallclock_anomalies', 0):>2} " + "  ".join(f"{f[:8]} {rep[f]['rate'] if rep[f]['rate'] is not None else '-'}" for f in fp.FIELDS))
    pooled = fp.merge_agreement(reports)
    pooled["wallclock_anomalies"] = sum(r["_wallclock_anomalies"] for r in reports)
    per_game_lines.append(f"  ESPN rows whose wallclock is out of sequence by > {fp.WALLCLOCK_SLACK:.0f}s (the replay sorts by wallclock): {pooled['wallclock_anomalies']}")
    title = f"feed parity {args.season} week {args.week}: ESPN summary rows (as the replay builds them) vs nflverse, clock tolerance {args.clock_tolerance}s, timeouts from {args.timeouts}"
    text = "\n".join([title, *per_game_lines, "", fp.format_agreement(pooled, "pooled")])
    print(text)
    out = Path(args.out) if args.out else REPO / "out" / f"feed_parity_w{args.week}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    jpath = Path(args.json) if args.json else out.with_suffix(".json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump({"season": args.season, "week": args.week, "clock_tolerance": args.clock_tolerance, "timeouts": args.timeouts, "pooled": pooled, "games": reports}, f, indent=1)
    print(f"wrote {out} and {jpath}")
    if args.record:
        rates = lambda rep: {f: {"n": rep[f]["n"], "agree": rep[f]["agree"], "rate": rep[f]["rate"]} for f in fp.FIELDS}  # noqa: E731
        rec = {
            "season": args.season, "week": args.week, "clock_tolerance": args.clock_tolerance, "timeouts": args.timeouts, "nflverse_file": pbp.name,
            "pooled": {**rates(pooled), "_alignment": pooled["_alignment"], "wallclock_anomalies": pooled["wallclock_anomalies"]},
            "games": [{"game": r["_game"], "wallclock_anomalies": r["_wallclock_anomalies"], **rates(r), "_alignment": r["_alignment"]} for r in reports],
        }
        Path(args.record).parent.mkdir(parents=True, exist_ok=True)
        with open(args.record, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1)
            f.write("\n")
        print(f"wrote {args.record}")
    to = pooled.get("home_timeouts", {}).get("rate")
    return 0 if to is not None and to >= 0.95 and pooled.get("away_timeouts", {}).get("rate", 0) >= 0.95 else 1


if __name__ == "__main__":
    sys.exit(main())

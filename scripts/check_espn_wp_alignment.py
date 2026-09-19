#!/usr/bin/env python3
"""Is ESPN's ``winprobability[playId].homeWinPercentage`` the state before or after the play?

    python scripts/check_espn_wp_alignment.py --season 2026 --week 1
    python scripts/check_espn_wp_alignment.py --sport ncaaf --season 2026 --week 1 --limit 20
    python scripts/check_espn_wp_alignment.py --espn 401872932 --out tests/fixtures/results/espn_wp_alignment_p04.json

The replay pairs each play's ESPN number with the model's *pre-play* number and the market
bar around the play, so the answer decides what the ESPN column in every published table
means. The test is model-free: on a scoring play ESPN's own series must jump by the score
either between the previous entry and this one (the entry already describes the post-play
state) or between this one and the next (pre-play); ``feedparity.classify_wp_alignment``
counts which side the jump falls on. The model's WP on the state before the play and on the
next play's start state is reported beside it as secondary evidence only — ESPN's model
reacts less than ours, so its number often sits between our two states. Also reports how
often a play id has no WP entry at all (the timeline's index+1 fallback). Cached ESPN
summaries are shared with the replay and the feed-parity script (``out/cache/history/espn/``).

2026 week 1 (16 games, 148 scoring plays): POST — ESPN's number has already moved on
0.885 of scoring plays (mean |jump| 0.059 before the entry vs 0.014 after); no play lacked an
entry. Recorded in ``tests/fixtures/results/espn_wp_alignment_p04.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from arb_engine.quant import feedparity as fp  # noqa: E402
from arb_engine.venues.espn import ESPNClient  # noqa: E402

DEFAULT_CACHE = REPO / "out" / "cache" / "history" / "espn"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sport", default="nfl", choices=("nfl", "ncaaf"))
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--week", type=int, default=1)
    ap.add_argument("--espn", nargs="*", help="ESPN event ids instead of the whole week")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--out", help="write the metrics-only JSON here (e.g. tests/fixtures/results/espn_wp_alignment_p04.json)")
    ap.add_argument("--verbose", action="store_true", help="print every scoring-play sample")
    args = ap.parse_args(argv)

    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    espn = ESPNClient(sport=args.sport)
    if args.espn:
        games = [{"id": e, "name": e} for e in args.espn]
    else:
        sb = fp.cached_json(cache_dir, f"scoreboard_{args.sport}_{args.season}_w{args.week}", lambda: espn.scoreboard_week(args.season, args.week), args.offline)
        games = [{"id": str(ev.get("id")), "name": ev.get("name")} for ev in sb.get("events") or [] if (((ev.get("status") or {}).get("type") or {}).get("name") == "STATUS_FINAL")]
    if args.limit:
        games = games[: args.limit]
    all_samples: list[dict] = []
    per_game: list[dict] = []
    fallback_plays = n_plays = 0
    for g in games:
        try:
            summary = fp.cached_json(cache_dir, f"summary_{g['id']}", lambda gid=g["id"]: espn.summary(gid), args.offline)
        except Exception as e:
            print(f"  {g['name']}: no summary ({e!r})")
            continue
        res = fp.wp_alignment_samples(summary)
        cls = fp.classify_wp_alignment(res["samples"])
        per_game.append({"game": f"{res['away']}@{res['home']}", "id": g["id"], **cls, "fallback_share": res["fallback_share"], "wp_entries": res["wp_entries"], "n_plays": res["n_plays"]})
        all_samples.extend(res["samples"])
        fallback_plays += res["fallback_plays"]
        n_plays += res["n_plays"]
        print(f"  {res['away']}@{res['home']:<5} scoring plays {cls['n']:>3}  ESPN jump before the entry {cls['share_jump_before']} (mean |jump| before {cls['mean_jump_before']} / after {cls['mean_jump_after']})  -> {cls['alignment']:<7} model: closer-to-post {cls['model_share_closer_to_post']}  wp entries {res['wp_entries']} / plays {res['n_plays']} (no entry for {res['fallback_plays']})")
        if args.verbose:
            f = lambda x: "  -  " if x is None else f"{x:.3f}"  # noqa: E731
            for s in res["samples"]:
                print(f"      Q{s['period']} {(s['clock'] or 0) // 60}:{(s['clock'] or 0) % 60:02d} {s['score_before']}->{s['score_after']} espn prev {f(s['espn_prev'])} this {f(s['espn'])} next {f(s['espn_next'])} | model pre {f(s['pre'])} post {f(s['post'])}  {s['text']}")
    pooled = fp.classify_wp_alignment(all_samples)
    pooled["fallback_share"] = round(fallback_plays / n_plays, 4) if n_plays else None
    pooled["games"] = len(per_game)
    pooled["n_plays"] = n_plays
    print(f"pooled over {len(per_game)} games: ESPN winprobability[playId] is the *{pooled['alignment'].upper()}*-play state on {pooled['n']} scoring plays (ESPN's own number jumps before the entry on {pooled['share_jump_before']} of them, mean |jump| before {pooled['mean_jump_before']} vs after {pooled['mean_jump_after']}); model check: closer to our post-play WP on {pooled['model_share_closer_to_post']} (mean |delta| pre {pooled['mean_abs_delta_pre']} vs post {pooled['mean_abs_delta_post']}); plays without a WP entry (index+1 fallback): {pooled['fallback_share']}")
    if args.out:
        out = {"sport": args.sport, "season": args.season, "week": args.week if not args.espn else None, "espn_ids": args.espn, "method": "scoring plays only; primary: does ESPN's own number jump between the previous entry and this one (post) or between this one and the next (pre); secondary: closer of the model's WP on the pre-play state / the next play's start state", "pooled": pooled, "games": per_game}
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
            f.write("\n")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

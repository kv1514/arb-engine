#!/usr/bin/env python3
"""College spread-rescale experiment: is the cheapest college adaptation of the NFL model worth it?

    python scripts/college_experiment.py --season 2026 --week 1 --limit 40
    python scripts/college_experiment.py --season 2026 --week 1 2 --offline --out tests/fixtures/results/college_experiment_p04.json

The replay scores college games with the NFL model as is (``docs/MODEL.md``). Two things
are obviously wrong with that: college margins are wider (a 16-point sigma against the
NFL's ~13.5), so a college line of −24 is fed to a model that never saw a spread past
±19.5, and college overtime has no clock, so the timeline's ``gsr = 0`` makes every
non-tied OT state "decided" (P = 0 or 1). This script re-scores every cached college game
row by row under

* ``baseline``      the replay's current inputs (spread as ESPN's pickcenter gives it),
* ``clamp``         spread clipped to ±``--clamp`` (the NFL training range),
* ``rescale``       spread × ``--scale`` (13.5/16 by default) then clipped,
* ``rescale_ot``    the above plus OT rows scored as ``--ot-seconds`` left on the clock
                    with the possession state ESPN reports (a college OT possession from
                    the 25 as a late-game NFL drive),

and reports the paired same-row log-loss difference against the baseline with a
game-cluster bootstrap 90 % interval (negative = the variant is better), on regulation
rows, OT rows and all rows. Metrics only go to ``--out`` (the docs quote them); the
answer decides whether college model work (R27) is reopened. Cached ESPN summaries are
shared with the replay (``out/cache/history/espn/``).

2026 weeks 1–2 (185 games, 32,052 regulation rows; ``tests/fixtures/results/college_experiment_p04.json``):
rescale +0.0033 [−0.0051, +0.0110] and clamp +0.0014 [−0.0067, +0.0077] against a baseline
log-loss of 0.1852 — no improvement, the interval straddles zero (week 1 alone read −0.0017
[−0.0155, +0.0095]). The OT clock mapping is the one real change: on the 79 OT rows of the
three OT games the baseline's decided-state 0/1 outputs cost 3.32 log-loss and the mapped
rows 1.05 (−2.27 [−3.49, −1.25]). So R27 stays closed on the spread side; the OT mapping
(or P03's gsr=None gate for college OT) is a bug fix worth taking, not model work. The
``receive_2h_ko_home`` flag is the opening kicker (``feedparity.receive_2h_ko_home``);
every variant shares it, so the paired differences do not depend on it.
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing
import os
import random
import sys
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from arb_engine.quant import feedparity as fp  # noqa: E402
from arb_engine.venues.espn import ESPNClient  # noqa: E402

DEFAULT_CACHE = REPO / "out" / "cache" / "history" / "espn"
NFL_SIGMA, COLLEGE_SIGMA = 13.5, 16.0
DEFAULT_SCALE = NFL_SIGMA / COLLEGE_SIGMA
DEFAULT_CLAMP = 19.5  # |spread_line| never exceeds this in the 2016-2024 nflverse training rows
DEFAULT_OT_SECONDS = 120
VARIANTS = ("baseline", "clamp", "rescale", "rescale_ot")
EPS = 1e-6


def rescale_spread(spread: Optional[float], scale: float = 1.0, clamp: Optional[float] = None) -> Optional[float]:
    """Home line × scale, clipped to ±clamp. None stays None (the model then uses 0)."""
    if spread is None:
        return None
    v = float(spread) * scale
    if clamp is not None:
        v = max(-abs(clamp), min(abs(clamp), v))
    return v


def variant_gsr(period: int, clock: Optional[int], gsr: Optional[int], ot_seconds: Optional[int]) -> Optional[int]:
    """Game seconds remaining for a row: the timeline's number in regulation; in overtime the
    experiment's ``ot_seconds`` (None keeps the timeline's ``min(clock, 600)``, which is 0
    on a college OT row because college OT has no clock)."""
    if period >= 5 and ot_seconds is not None:
        return int(ot_seconds)
    return gsr


def log_loss(p: float, y: int) -> float:
    p = min(max(p, EPS), 1 - EPS)
    return -math.log(p) if y else -math.log(1 - p)


def pickcenter_spread(summary: dict) -> Optional[float]:
    for pc in summary.get("pickcenter") or []:
        sp = pc.get("spread")
        if sp is not None:
            fav_home = (pc.get("homeTeamOdds") or {}).get("favorite")
            return -abs(float(sp)) if fav_home else abs(float(sp))
    return None


def score_summary(summary: dict, scale: float = DEFAULT_SCALE, clamp: Optional[float] = DEFAULT_CLAMP, ot_seconds: Optional[int] = DEFAULT_OT_SECONDS, model: Any = None, wp_fn: Any = None, spread_home: Optional[float] = None) -> dict[str, Any]:
    """Per in-play row: the outcome and the model's P(home) under every variant.
    Rows: ``{"period", "ot", "y", "p": {variant: p}}``; ``spread`` is the home line used
    (ESPN's pickcenter unless ``spread_home`` overrides it — old summaries carry none)."""
    if wp_fn is None:
        from arb_engine.models.wp import home_win_probability as wp_fn
    rows, texts, tids, meta = fp.ordered_rows(summary)
    y = 1 if meta["home_score"] > meta["away_score"] else 0
    spread = pickcenter_spread(summary) if spread_home is None else spread_home
    spreads = {"baseline": spread, "clamp": rescale_spread(spread, 1.0, clamp), "rescale": rescale_spread(spread, scale, clamp), "rescale_ot": rescale_spread(spread, scale, clamp)}
    ko_home = fp.receive_2h_ko_home(rows, texts, tids)  # opening kicker (ESPN's start.team) receives the 2H kickoff
    out_rows: list[dict[str, Any]] = []
    for i, r in enumerate(rows):
        ot = r.period >= 5
        inplay = bool(r.period) and (r.period < 4 or (r.period == 4 and (r.clock_seconds or 0) > 0) or (ot and i + 1 < len(rows)))
        if not inplay or r.game_seconds_remaining is None:
            continue
        ps: dict[str, Optional[float]] = {}
        for v in VARIANTS:
            gsr = variant_gsr(r.period, r.clock_seconds, r.game_seconds_remaining, ot_seconds if v == "rescale_ot" else None)
            try:
                ps[v] = float(wp_fn(home_score=r.home_score, away_score=r.away_score, game_seconds_remaining=gsr, possession=r.possession, down=r.down, distance=r.distance, yardline_100=r.yardline_100, vegas_spread_home=spreads[v] or 0.0, receive_2h_ko_home=ko_home, model=model))
            except Exception:
                ps[v] = None
        if any(p is None for p in ps.values()):
            continue
        out_rows.append({"period": r.period, "ot": ot, "y": y, "p": ps})
    return {"id": meta.get("event_id"), "rows": out_rows, "spread": spread, "spreads": spreads, "home": meta.get("home"), "away": meta.get("away"), "home_won": bool(y), "n_ot_rows": sum(1 for r in out_rows if r["ot"])}


def _game_key(g: dict[str, Any]) -> str:
    """Bootstrap cluster id: the ESPN event id, never the ``away@home`` label — a rematch in
    a pooled multi-week run (or neutral-site games sharing abbreviations) must stay two
    clusters, and ``n_games`` must count the same thing."""
    return str(g.get("id") or id(g))


def paired_diff(diffs_by_game: dict[str, list[float]], B: int = 1000, seed: int = 0) -> dict[str, Any]:
    """Row-weighted mean of the paired differences with a game-cluster bootstrap: games are
    resampled with replacement and the pooled mean recomputed. Zero (and a zero interval) on
    identical inputs. Keys match ``quant.calibration.bootstrap_paired`` (P01) so the docs
    table can quote either."""
    games = [(g, d) for g, d in diffs_by_game.items() if d]
    n_rows = sum(len(d) for _, d in games)
    if not games or not n_rows:
        return {"mean": None, "lo90": None, "hi90": None, "frac_positive": None, "n_games": 0, "n_rows": 0}
    mean = sum(sum(d) for _, d in games) / n_rows
    sums = [(sum(d), len(d)) for _, d in games]
    rng = random.Random(seed)
    boots: list[float] = []
    for _ in range(B):
        s = n = 0.0
        for _k in range(len(sums)):
            a, b = sums[rng.randrange(len(sums))]
            s += a
            n += b
        boots.append(s / n if n else 0.0)
    boots.sort()
    lo, hi = boots[int(0.05 * (B - 1))], boots[int(0.95 * (B - 1))]
    per_game = [a / b for a, b in sums]
    sd = (sum((m - sum(per_game) / len(per_game)) ** 2 for m in per_game) / (len(per_game) - 1)) ** 0.5 if len(per_game) > 1 else 0.0
    return {"mean": round(mean, 5), "lo90": round(lo, 5), "hi90": round(hi, 5), "frac_positive": round(sum(1 for b in boots if b > 0) / B, 4), "n_games": len(games), "n_rows": n_rows, "sd_per_game": round(sd, 5)}


def experiment(scored: list[dict[str, Any]], B: int = 1000, seed: int = 0) -> dict[str, Any]:
    """Pooled log-loss per variant and paired (variant − baseline) differences with the
    bootstrap, on regulation rows, OT rows and all rows."""
    out: dict[str, Any] = {}
    for slice_name, keep in (("regulation", lambda r: not r["ot"]), ("overtime", lambda r: r["ot"]), ("all", lambda r: True)):
        rows = [(g, r) for g in scored for r in g["rows"] if keep(r)]
        res: dict[str, Any] = {"n_rows": len(rows), "n_games": len({_game_key(g) for g, _ in rows}), "log_loss": {}, "vs_baseline": {}}
        for v in VARIANTS:
            ll = [log_loss(r["p"][v], r["y"]) for _, r in rows]
            res["log_loss"][v] = round(sum(ll) / len(ll), 4) if ll else None
        for v in VARIANTS[1:]:
            by_game: dict[str, list[float]] = {}
            for g, r in rows:
                by_game.setdefault(_game_key(g), []).append(log_loss(r["p"][v], r["y"]) - log_loss(r["p"]["baseline"], r["y"]))
            res["vs_baseline"][v] = paired_diff(by_game, B=B, seed=seed)
        out[slice_name] = res
    return out


def format_experiment(exp: dict[str, Any], params: dict[str, Any]) -> str:
    lines = [f"college spread-rescale experiment: scale {params['scale']:.4f} (13.5/16), clamp ±{params['clamp']}, OT rows as {params['ot_seconds']} s left; paired log-loss vs baseline (negative = better), 90 % game-cluster bootstrap"]
    for slice_name in ("regulation", "overtime", "all"):
        s = exp[slice_name]
        lines.append(f"  {slice_name:<10} {s['n_rows']:>6} rows / {s['n_games']:>3} games   " + "  ".join(f"{v} {s['log_loss'][v]}" for v in VARIANTS))
        for v in VARIANTS[1:]:
            d = s["vs_baseline"][v]
            if d["mean"] is None:
                continue
            lines.append(f"      {v:<11} vs baseline {d['mean']:+.5f}  [{d['lo90']:+.5f}, {d['hi90']:+.5f}]  P(>0) {d['frac_positive']}")
    return "\n".join(lines)


class _Scorer:
    """Picklable ``score_summary`` closure for the process pool; errors come back as text."""

    def __init__(self, scale: float, clamp: Optional[float], ot_seconds: Optional[int]):
        self.scale, self.clamp, self.ot_seconds = scale, clamp, ot_seconds

    def __call__(self, summary: dict) -> Any:
        try:
            return score_summary(summary, scale=self.scale, clamp=self.clamp, ot_seconds=self.ot_seconds)
        except Exception as e:
            return repr(e)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--week", type=int, nargs="+", default=[1], help="one or more weeks, pooled")
    ap.add_argument("--espn", nargs="*", help="ESPN college event ids instead of the whole week")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--scale", type=float, default=DEFAULT_SCALE)
    ap.add_argument("--clamp", type=float, default=DEFAULT_CLAMP)
    ap.add_argument("--ot-seconds", type=int, default=DEFAULT_OT_SECONDS)
    ap.add_argument("--bootstrap", type=int, default=1000)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1), help="processes for the model scoring (~1.5 s per game single-threaded)")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--out", help="metrics-only JSON (tests/fixtures/results/college_experiment_p04.json)")
    args = ap.parse_args(argv)

    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    espn = ESPNClient(sport="ncaaf")
    if args.espn:
        games = [{"id": e, "name": e} for e in args.espn]
    else:
        games = []
        for week in args.week:
            sb = fp.cached_json(cache_dir, f"scoreboard_ncaaf_{args.season}_w{week}", lambda w=week: espn.scoreboard_week(args.season, w), args.offline)
            games.extend({"id": str(ev.get("id")), "name": ev.get("name")} for ev in sb.get("events") or [] if (((ev.get("status") or {}).get("type") or {}).get("name") == "STATUS_FINAL"))
    if args.limit:
        games = games[: args.limit]
    scored: list[dict[str, Any]] = []
    skipped: list[str] = []
    summaries: list[tuple[dict, dict]] = []
    for g in games:
        try:
            summaries.append((g, fp.cached_json(cache_dir, f"summary_{g['id']}", lambda gid=g["id"]: espn.summary(gid), args.offline)))
        except Exception as e:
            skipped.append(f"{g['name']}: {e!r}")
    scorer = _Scorer(args.scale, args.clamp, args.ot_seconds)
    results: Optional[list[Any]] = None
    if args.workers > 1 and len(summaries) > 1:
        try:  # fork keeps the loaded model; a sandbox may refuse the pool's semaphores -> serial
            with multiprocessing.get_context("fork").Pool(args.workers) as pool:
                results = pool.map(scorer, [s for _, s in summaries], chunksize=2)
        except (OSError, PermissionError, ValueError) as e:
            print(f"  (process pool unavailable: {e!r}; scoring serially)")
    if results is None:
        results = [scorer(s) for _, s in summaries]
    for (g, _), sc in zip(summaries, results):
        if isinstance(sc, str):
            skipped.append(f"{g['name']}: {sc}")
            continue
        if not sc["rows"]:
            skipped.append(f"{g['name']}: no in-play rows")
            continue
        scored.append(sc)
        print(f"  {sc['away']}@{sc['home']:<6} spread {sc['spread']} -> {sc['spreads']['rescale']}  rows {len(sc['rows']):>4} (OT {sc['n_ot_rows']})")
    exp = experiment(scored, B=args.bootstrap)
    params = {"scale": args.scale, "clamp": args.clamp, "ot_seconds": args.ot_seconds, "bootstrap": args.bootstrap}
    print(format_experiment(exp, params))
    for s in skipped:
        print(f"  skipped {s}")
    if args.out:
        payload = {"sport": "ncaaf", "season": args.season, "weeks": args.week if not args.espn else None, "espn_ids": args.espn, "params": params, "games": len(scored), "skipped": len(skipped), "no_spread_games": sum(1 for g in scored if g["spread"] is None), "results": exp}
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1)
            f.write("\n")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""``backtest`` flags for the honest replay: alignment, strata, placebos, offline cache,
extra models and multi-week pooling.

Loaded by the CLI plugin registry (``register(subparsers, existing_parsers)``); the handler
returned overrides the ``backtest`` dispatch. The module is also usable on its own::

    python -m arb_engine.cli_plugins.backtest_flags --week 1 --bar-mode both --slices --placebo --json out/w1.json
    python -m arb_engine.cli_plugins.backtest_flags --week 1 --offline ...      # must reproduce out/w1.json

Flags added: ``--bar-mode before|after|both`` (which Kalshi candle is the headline market;
``both`` is ``before`` plus the after-alignment report columns, which are always present),
``--slices`` (per-slice / per-class tables and class gaps), ``--placebo`` (the shifted and
shuffled simulations next to the two honest pairings), ``--offline`` (never fetch: fail on a
cache miss), ``--cache-dir``, ``--models a.json,b.json`` (score extra WP exports on the same
rows), ``--pool w1.json,w2.json`` (strata, intervals, games-needed and the NCAAF-style
walk-forward over persisted weeks), ``--spread-scale`` / ``--spread-clamp`` (the college
spread experiment), ``--min-cell``, ``--seed`` and ``--results-json`` (the metrics-only file
the docs tables are rendered from).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from dataclasses import asdict
from typing import Any, Callable, Optional

from ..backtest import HEADLINE_CLASSES, HISTORY_CACHE_DIR_DEFAULT, dump_results_json

BAR_MODE_CHOICES = ("before", "after", "both")


def add_base_flags(bt: argparse.ArgumentParser) -> None:
    """The flags the core CLI already defines (added only when the plugin builds the parser itself)."""
    bt.add_argument("--espn", help="ESPN event id (from `arb-engine games` keys / ESPN URLs)")
    bt.add_argument("--rh-home", help="Robinhood contract id for the home team (from the catalogue / fixtures)")
    bt.add_argument("--rh-away")
    bt.add_argument("--pm-home", help="Polymarket token id for the home outcome")
    bt.add_argument("--pm-away")
    bt.add_argument("--kalshi-home", help="override Kalshi ticker (default derived from teams + ET date)")
    bt.add_argument("--kalshi-away")
    bt.add_argument("--week", type=int, help="replay every finished game of this regular-season week and fit the blend weights")
    bt.add_argument("--season", type=int, default=2026)
    bt.add_argument("--sport", default="nfl", choices=["nfl", "ncaaf"])
    bt.add_argument("--limit", type=int, help="with --week: only the first N games")
    bt.add_argument("--no-polymarket", action="store_true", help="with --week: skip the Polymarket history lookup")
    bt.add_argument("--contracts", type=int, default=10, help="with --week: contracts per simulated STEAL/LOCK entry")
    bt.add_argument("--json", help="write the full replay to this file")


def add_flags(bt: argparse.ArgumentParser) -> None:
    """This item's flags (idempotent: skips any option the parser already has)."""
    have = {o for a in bt._actions for o in a.option_strings}

    def add(*names: str, **kw: Any) -> None:
        if names[0] not in have:
            bt.add_argument(*names, **kw)

    add("--bar-mode", default=None, choices=list(BAR_MODE_CHOICES), help="Kalshi candle alignment for the headline market: before (last candle ending <= play, default), after (first candle ending >= play), both")
    add("--slices", action="store_true", help="per-slice / per-play-class tables, |model - kalshi_before| and STEAL-qualifying counts by class")
    add("--placebo", action="store_true", help="add the +1-candle shift and game-label shuffle simulations next to the pre/before and post/after pairings")
    add("--offline", action="store_true", help="never fetch: every response must be in --cache-dir (fails on a miss)")
    add("--cache-dir", default=None, help=f"read-through cache for kalshi / polymarket / robinhood / ESPN responses (default {HISTORY_CACHE_DIR_DEFAULT}; ARB_HISTORY_CACHE_DIR)")
    add("--models", default=None, help="comma list of extra WP model JSON exports scored per row as model:<basename>")
    add("--pool", default=None, help="comma list (or glob) of persisted --json files: strata, intervals, games-needed and walk-forward over >= 2 weeks")
    add("--spread-scale", type=float, default=None, help="multiply the pre-game spread fed to the model (college experiment)")
    add("--spread-clamp", type=float, default=None, help="clamp |spread| fed to the model (college experiment)")
    add("--min-cell", type=int, default=50, help="strata cells with fewer rows show n only")
    add("--seed", type=int, default=0, help="bootstrap / shuffle seed")
    add("--results-json", default=None, help="write the metrics-only report (canonical JSON) for the docs tables")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="arb-engine backtest")
    add_base_flags(p)
    add_flags(p)
    return p


def register(subparsers: Any, existing_parsers: Optional[dict[str, argparse.ArgumentParser]] = None) -> dict[str, Callable[..., int]]:
    """CLI plugin entry point: extend the existing ``backtest`` subcommand (or create it) and
    override its handler."""
    bt = (existing_parsers or {}).get("backtest")
    if bt is None:
        bt = subparsers.add_parser("backtest", help="replay finished games play-by-play with public price history; score model/ESPN/venues vs the outcome")
        add_base_flags(bt)
    add_flags(bt)
    bt.set_defaults(func=handle_backtest)
    return {"backtest": handle_backtest}


def _cache_dir(args: argparse.Namespace, settings: Optional[dict[str, Any]]) -> Optional[str]:
    v = getattr(args, "cache_dir", None) or (settings or {}).get("history_cache_dir") or os.environ.get("ARB_HISTORY_CACHE_DIR") or HISTORY_CACHE_DIR_DEFAULT
    return v or None


def _load_extra_models(spec: Optional[str]) -> dict[str, Any]:
    if not spec:
        return {}
    from ..models.wp import load_model

    out = {}
    for path in [s.strip() for s in spec.split(",") if s.strip()]:
        out[os.path.splitext(os.path.basename(path))[0]] = load_model(path)
    return out


def _paths(spec: str) -> list[str]:
    out: list[str] = []
    for part in [s.strip() for s in spec.split(",") if s.strip()]:
        hits = sorted(glob.glob(part)) if any(c in part for c in "*?[") else [part]
        out.extend(hits)
    return out


def handle_backtest(args: argparse.Namespace, settings: Optional[dict[str, Any]] = None, out: Any = None) -> int:
    """The ``backtest`` handler with this item's options (``out`` = print target for tests)."""
    from ..backtest import GameReplayer, fit_blend_weights, pool_weeks, replay_week, simulate_pairings, simulate_steal, summarize, summarize_many, week_report
    from ..venues.espn import ESPNClient
    from ..venues.history import HistoryClient

    say = out if out is not None else print
    seed = int(getattr(args, "seed", 0) or 0)
    if getattr(args, "pool", None):
        paths = _paths(args.pool)
        if len(paths) < 1:
            say("--pool: no files matched")
            return 2
        rep = pool_weeks(paths, sport=args.sport, seed=seed)
        say(json.dumps({k: v for k, v in rep.items() if k in ("weeks", "n_games", "n_weeks", "pooled_inplay", "fit", "intervals", "walk_forward")}, indent=1, default=str))
        if getattr(args, "results_json", None):
            with open(args.results_json, "w", encoding="utf-8") as f:
                f.write(dump_results_json(rep))
            say(f"wrote {args.results_json}")
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(rep, f, indent=1, default=str)
            say(f"wrote {args.json}")
        return 0

    bar_mode = getattr(args, "bar_mode", None) or (settings or {}).get("backtest_bar_mode") or "before"
    primary = "after" if bar_mode == "after" else "before"
    history = HistoryClient(cache_dir=_cache_dir(args, settings), offline=bool(getattr(args, "offline", False)))
    replayer = GameReplayer(espn=ESPNClient(sport=args.sport), history=history, sport=args.sport, bar_mode=primary, extra_models=_load_extra_models(getattr(args, "models", None)), spread_scale=getattr(args, "spread_scale", None), spread_clamp=getattr(args, "spread_clamp", None))
    rh = {"home": args.rh_home, "away": args.rh_away} if args.rh_home and args.rh_away else None
    pm = {k: v for k, v in (("home", args.pm_home), ("away", args.pm_away)) if v} or None
    kt = {"home": args.kalshi_home, "away": args.kalshi_away} if args.kalshi_home and args.kalshi_away else None
    if args.week is not None:
        results, skipped = replay_week(args.season, args.week, replayer=replayer, polymarket=not args.no_polymarket, limit=args.limit, progress=say, sport=args.sport)
        fit = fit_blend_weights(results, sport=args.sport, play_classes=HEADLINE_CLASSES) if results else None
        fit_logit = fit_blend_weights(results, sport=args.sport, play_classes=HEADLINE_CLASSES, pool="logit") if results else None
        sims: list[dict[str, Any]] = []
        if results:
            sims = simulate_pairings(results, contracts=args.contracts, source="blend", lock=False, placebos=bool(getattr(args, "placebo", False)), seed=seed)
            sims += [simulate_steal(results, contracts=args.contracts, source="blend", lock=True, lock_fraction=lf, pairing="post_after", seed=seed) for lf in (0.0, 0.5, 1.0)]
            sims.append(simulate_steal(results, contracts=args.contracts, source="model", lock=False, pairing="post_after", seed=seed))
        report = week_report(results, fit, sims, seed=seed, slices=bool(getattr(args, "slices", False))) if results else {}
        if fit_logit and fit_logit.get("n"):
            report["fit_logit"] = fit_logit
        if report.get("strata") is not None and getattr(args, "min_cell", 50) != 50:
            from ..backtest import strata_tables

            report["strata"] = strata_tables(results, min_n=int(args.min_cell))
        say(summarize_many(results, skipped, fit, sims, report))
        say(f"cache: {history.cache_hits} hits, {history.cache_misses} misses ({history.cache_dir}{', offline' if history.offline else ''})")
        if getattr(args, "results_json", None):
            slim = dict(report)
            # ``logit_pool_available_live`` is an inspect.signature check on blended_fair, not a
            # replay number: keep it out of the persisted (byte-for-byte compared) report.
            slim["fit"] = {k: v for k, v in (fit or {}).items() if k != "logit_pool_available_live"} if fit else None
            if slim.get("fit_logit"):
                slim["fit_logit"] = {k: v for k, v in slim["fit_logit"].items() if k != "logit_pool_available_live"}
            slim.update({"season": args.season, "week": args.week, "sport": args.sport, "bar_mode": bar_mode})
            with open(args.results_json, "w", encoding="utf-8") as f:
                f.write(dump_results_json(slim))
            say(f"wrote {args.results_json}")
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump({"season": args.season, "week": args.week, "sport": args.sport, "bar_mode": bar_mode, "fit": fit, "fit_logit": fit_logit, "simulations": sims, "report": report, "skipped": skipped, "games": [asdict(r) for r in results]}, f, indent=1, default=str)
            say(f"wrote {args.json}")
        return 0
    if not args.espn:
        say("need --espn <event id>, --week N or --pool files")
        return 2
    res = replayer.replay(args.espn, rh_contracts=rh, pm_tokens=pm, kalshi_tickers=kt)
    say(summarize(res))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(asdict(res), f, indent=1, default=str)
        say(f"wrote {args.json}")
    return 0


if __name__ == "__main__":  # pragma: no cover - standalone use until the CLI registry loads plugins
    raise SystemExit(handle_backtest(build_parser().parse_args()))

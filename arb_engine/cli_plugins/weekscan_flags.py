"""CLI plugin: ``weekscan`` - look for arbitrage all week, every game, every market.

  weekscan [--sport nfl --sport ncaaf] [--every 120] [--fast 5] [--bankroll 500]
           [--record out/history.db] [--journal out/week_journal.jsonl] [--once]

A full sweep of every market on the executable venues every ``--every`` seconds, and a fast
watch (``--fast`` seconds) of the markets within ``--watch-margin`` of locking; alerts are the
game-day ARB / BIG ARB / ARB CLOSE tickets (strategy/weekscan.py). ``--once`` runs one sweep,
prints what it found and the closest markets, and exits.
"""
from __future__ import annotations

import argparse
from typing import Any, Optional


def run(args: argparse.Namespace, settings: Optional[dict[str, Any]] = None) -> int:
    from ..cli import build_adapters
    from ..scanner import resolve_executable_venues
    from ..strategy.alerts import Alerter
    from ..strategy.weekscan import WeekScanner

    settings = dict(settings or {})
    sports = args.sport or ["nfl", "ncaaf"]
    execv = resolve_executable_venues(settings, None) or {"kalshi", "robinhood"}
    venues = [v for v in ("kalshi", "robinhood") if v in execv]
    store = None
    if args.record:
        from ..store import Store

        store = Store(args.record)
    ws = WeekScanner(sports, build_adapters(venues, False), Alerter(journal_path=args.journal, quiet=args.quiet), settings,
                     store=store, bankroll=args.bankroll or None, executable_venues=set(venues), full_every_s=args.every,
                     fast_every_s=args.fast, watch_margin=args.watch_margin, max_watch=args.max_watch)
    if args.once:
        res = ws.full_sweep()
        for kind, text in res["found"]:
            print(f"*** {kind} ***\n{text}\n")
        rows = sorted(ws.watch.items(), key=lambda kv: -(kv[1]["margin"] or -9))[:15]
        print(f"{len(res['found'])} alerts; watching {res['watching']} markets within {args.watch_margin * 100:.0f}c of locking" + (":" if rows else ""))
        for k, w in rows:
            print(f"  {w['margin'] * 100:+.2f}c  {w['title']}  ({k})")
        return 0
    ws.run(duration=args.duration)
    return 0


def register(subparsers: Any, existing_parsers: Any = None) -> None:
    p = subparsers.add_parser("weekscan", help="look for arbs all week: every game, every market, full sweeps plus a fast watch of near-locks")
    p.add_argument("--sport", action="append", choices=["nfl", "ncaaf", "nba", "nhl", "mlb", "tennis"], help="repeatable; default nfl and ncaaf")
    p.add_argument("--every", type=float, default=120.0, help="seconds between full sweeps")
    p.add_argument("--fast", type=float, default=5.0, help="seconds between fast-watch refreshes")
    p.add_argument("--watch-margin", type=float, default=0.03, help="markets this close to locking (dollars per contract) are watched fast")
    p.add_argument("--max-watch", type=int, default=60, help="most markets on the fast watch")
    p.add_argument("--bankroll", type=float, default=0.0, help="size tickets to arb_stake_fraction of this")
    p.add_argument("--record", nargs="?", const="out/history.db", default=None, help="record arbs and watched markets to this database")
    p.add_argument("--journal", default="out/week_journal.jsonl")
    p.add_argument("--duration", type=float, default=None, help="stop after this many seconds (default: run until stopped)")
    p.add_argument("--quiet", action="store_true", help="no terminal bell / banner per alert")
    p.add_argument("--once", action="store_true", help="one sweep: print alerts and the closest markets, then exit")
    p.set_defaults(func=run)

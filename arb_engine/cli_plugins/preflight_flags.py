"""CLI plugin (L4): ``python -m arb_engine preflight`` — the pre-slate readiness report.

    preflight --sport nfl --date 2026-09-20 [--bridge http://127.0.0.1:8765] [--json]
              [--limit 16] [--venue-timeout 75] [--bankroll 1000 --kelly 0.25]
              [--offline] [--out-dir out] [--ext-dir extension]

Exit code 0 on PASS or WARN, 2 on FAIL (``scripts/sunday.sh`` refuses to start on 2). The
checks themselves live in ``arb_engine.preflight`` and take injected clients; this module
only parses flags and builds the real ones (``preflight.build_clients``), so the report the
tests run on fixtures is byte-for-byte the one the launcher runs against the venues.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional


def cmd_preflight(args: Any, settings: Optional[dict[str, Any]] = None) -> int:
    from .. import preflight as pf
    from ..config import load_settings

    settings = dict(settings) if settings is not None else load_settings()
    date = args.date or pf.today_et()
    clients = pf.Clients() if args.offline else pf.build_clients(args.sport, bridge=not args.no_bridge, extension=True)
    if args.offline:
        clients.ext_checker = pf.run_extension_checker
    quiet_json = bool(args.json)

    def progress(c: pf.Check) -> None:
        if not quiet_json:
            lat = f"{c.latency_ms / 1000:6.2f}s" if c.latency_ms is not None else "      -"
            print(f"{c.status:<4} {c.name:<17} {lat}  {c.detail}", flush=True)

    if not quiet_json:
        print(f"# preflight {args.sport} {date}  (bridge {args.bridge}; limit {args.limit} games; venue timeout {args.venue_timeout:g}s)", flush=True)
    rep = pf.run_report(args.sport, date, settings, clients, bridge_url=args.bridge, out_dir=args.out_dir, ext_dir=args.ext_dir, limit=args.limit, venue_timeout_s=args.venue_timeout, bankroll=args.bankroll, kelly=args.kelly, progress=progress)
    if quiet_json:
        json.dump(rep.as_dict(), sys.stdout, indent=1, default=str)
        sys.stdout.write("\n")
    else:
        print(pf.format_report(rep).splitlines()[-1], flush=True)
    return rep.exit_code


def register(subparsers: Any, existing_parsers: dict[str, Any]) -> None:
    if "preflight" in (existing_parsers or {}):
        return None
    p = subparsers.add_parser("preflight", help="readiness report before a live slate: imports, WP model, settings, venue reachability + latency, ESPN slate, cross-venue matches, bridge, out/ + disk, extension; exit 2 on FAIL")
    p.add_argument("--sport", default="nfl", choices=["nfl", "ncaaf", "nba", "nhl"])
    p.add_argument("--date", default=None, help="slate date YYYY-MM-DD in ET (default: today in ET)")
    p.add_argument("--bridge", default="http://127.0.0.1:8765", help="bridge base URL for the /health check")
    p.add_argument("--no-bridge", action="store_true", help="skip the bridge check")
    p.add_argument("--json", action="store_true", help="print the report as JSON instead of the table")
    p.add_argument("--limit", type=int, default=16, help="games of the date to match across venues (default 16)")
    p.add_argument("--venue-timeout", type=float, default=75.0, help="seconds each venue fetch may take in the match check (default 75)")
    p.add_argument("--bankroll", type=float, default=None, help="bankroll the launcher will pass to live (sanity-checked with --kelly)")
    p.add_argument("--kelly", type=float, default=None, help="Kelly fraction the launcher will pass to live")
    p.add_argument("--offline", action="store_true", help="local checks only (python, imports, model, settings, out/, extension); network rows report WARN skipped")
    p.add_argument("--out-dir", default="out", help="directory the recorder / journals / logs write to")
    p.add_argument("--ext-dir", default="extension", help="unpacked extension folder for scripts/check_extension.py")
    p.set_defaults(func=cmd_preflight)
    return None

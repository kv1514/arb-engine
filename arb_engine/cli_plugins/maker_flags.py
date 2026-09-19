"""CLI plugin (P11): hedge-venue opt-in, hedge-cash cap and executable-venue wiring.

Loaded by ``arb_engine.cli``'s plugin registry (P01) which calls ``register(subparsers,
existing_parsers)`` and dispatches to the handlers it returns. Until that loader exists the
module is inert but importable, and ``register`` can be exercised against any argparse
sub-parser set (see tests/test_maker.py).

    maker --hedge-venues robinhood[,polymarket]   explicit hedge venues; naming polymarket is
                                                   the opt-in to a non-executable hedge leg
    maker --hedge-cash 250                         dollars of hand-executed hedges resting at once
    scan|live|maker --allowed-venues executable|all|<csv>
                                                   scan: non-executable venues stay as a fair-value
                                                   reference but are never an arb leg; live/maker:
                                                   they are not fetched at all (maker keeps Kalshi)

Why the maker handler is re-implemented here rather than patched: cli.py belongs to the
registry item, and the runner's new safety knobs (hedge venues, hedge cash, exchange-status
pause) must reach MakerConfig without editing it.
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
from typing import Any, Callable, Mapping, Optional

from .. import compliance

ALLOWED_HELP = "'executable' (data/venue_rules.json or EXECUTABLE_VENUES), 'all', or a comma list of venues"


def _parsers(subparsers: Any, existing: Any) -> dict[str, argparse.ArgumentParser]:
    if isinstance(existing, Mapping):
        return dict(existing)
    choices = getattr(subparsers, "choices", None)
    return dict(choices) if isinstance(choices, Mapping) else {}


def _has_option(parser: argparse.ArgumentParser, opt: str) -> bool:
    return any(opt in a.option_strings for a in parser._actions)


def register(subparsers: Any, existing_parsers: Any = None) -> dict[str, Callable[..., int]]:
    """Add the flags; return the handlers that override dispatch for maker/scan/live."""
    parsers = _parsers(subparsers, existing_parsers)
    handlers: dict[str, Callable[..., int]] = {}
    mk = parsers.get("maker")
    if mk is not None:
        if not _has_option(mk, "--hedge-venues"):
            mk.add_argument("--hedge-venues", default=None, help="comma list of hedge venues (default robinhood); include polymarket only if this account can execute there — it is not executable for US persons")
        if not _has_option(mk, "--hedge-cash"):
            mk.add_argument("--hedge-cash", type=float, default=None, help="max dollars of hand-executed hedge legs resting at once, sum of size x hedge ask (default MAKER_HEDGE_CASH or 250; 0 = unbounded)")
        if not _has_option(mk, "--allowed-venues"):
            mk.add_argument("--allowed-venues", default="executable", help=ALLOWED_HELP)
        handlers["maker"] = run_maker
    for name, wrap in (("scan", run_scan), ("live", run_live)):
        sp = parsers.get(name)
        if sp is None:
            continue
        if not _has_option(sp, "--allowed-venues"):
            sp.add_argument("--allowed-venues", default="all", help=ALLOWED_HELP + " (default all: reference prices from every venue)")
        handlers[name] = wrap
    return handlers


# ---- resolution helpers -----------------------------------------------------------------------
def allowed_venues(spec: Optional[str], settings: Optional[Mapping[str, Any]] = None) -> Optional[set[str]]:
    """None = no restriction ('all'); otherwise the executable set or the explicit list."""
    s = (spec or "executable").strip().lower()
    if s == "all":
        return None
    if s == "executable":
        return compliance.executable_venues(settings)
    return {v.strip() for v in s.split(",") if v.strip()}


def hedge_venues(spec: Optional[str], settings: Optional[Mapping[str, Any]] = None) -> tuple[str, ...]:
    """--hedge-venues > MAKER_HEDGE_VENUES / settings > MakerConfig default (robinhood)."""
    raw = spec
    if raw is None and settings:
        raw = settings.get("maker_hedge_venues")
    if raw is None:
        raw = os.environ.get("MAKER_HEDGE_VENUES")
    if raw is None or not str(raw).strip():
        from ..strategy.maker import MakerConfig

        return MakerConfig.hedge_venues
    return tuple(v.strip().lower() for v in str(raw).split(",") if v.strip())


def hedge_cash(value: Optional[float], settings: Optional[Mapping[str, Any]] = None) -> float:
    if value is not None:
        return float(value)
    raw = (settings or {}).get("maker_hedge_cash", None)
    if raw is None:
        raw = os.environ.get("MAKER_HEDGE_CASH")
    if raw is None or raw == "":
        from ..strategy.maker import MakerConfig

        return MakerConfig.hedge_cash
    return float(raw)


def _settings(args: argparse.Namespace, settings: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if settings is None:
        from ..config import settings_from_env

        settings = settings_from_env()
    out = dict(settings)
    if getattr(args, "gold", False):
        out["robinhood_gold"] = True
    return out


def _call(fn: Callable[..., Any], args: argparse.Namespace, settings: Optional[Mapping[str, Any]]) -> int:
    """Call a cli handler whether it takes (args) or (args, settings)."""
    try:
        n = len([p for p in inspect.signature(fn).parameters.values() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)])
    except (TypeError, ValueError):
        n = 1
    return int((fn(args, settings) if n >= 2 else fn(args)) or 0)


def _venue_list(args: argparse.Namespace) -> list[str]:
    return [v.strip() for v in str(getattr(args, "venues", "") or "").split(",") if v.strip()]


# ---- handlers -----------------------------------------------------------------------------------
def run_scan(args: argparse.Namespace, settings: Optional[Mapping[str, Any]] = None) -> int:
    """scan with non-executable venues kept as reference quotes but never as arb legs."""
    from .. import cli, scanner

    allowed = allowed_venues(getattr(args, "allowed_venues", "all"), settings)
    if allowed is None:
        return _call(cli.cmd_scan, args, settings)
    orig = cli.scan

    def scan_with_allowed(*a: Any, **kw: Any) -> Any:
        if "allowed_venues" in inspect.signature(scanner.scan).parameters:
            kw.setdefault("allowed_venues", set(allowed))
        return orig(*a, **kw)

    cli.scan = scan_with_allowed
    try:
        return _call(cli.cmd_scan, args, settings)
    finally:
        cli.scan = orig


def run_live(args: argparse.Namespace, settings: Optional[Mapping[str, Any]] = None) -> int:
    """live with venues the account cannot execute on dropped from the fetch list."""
    from .. import cli

    allowed = allowed_venues(getattr(args, "allowed_venues", "all"), settings)
    if allowed is not None:
        kept = [v for v in _venue_list(args) if v in allowed]
        if not kept:
            raise SystemExit(f"--allowed-venues leaves no venue to price (allowed: {','.join(sorted(allowed))})")
        args.venues = ",".join(kept)
    return _call(cli.cmd_live, args, settings)


def run_maker(args: argparse.Namespace, settings: Optional[Mapping[str, Any]] = None, runner_factory: Optional[Callable[..., Any]] = None) -> int:
    """cli.cmd_maker plus: --allowed-venues (default executable) narrows the fetch list but
    always keeps Kalshi; --hedge-venues is the explicit opt-in list (a non-executable venue
    in it is shouted once at start and on every alert); --hedge-cash caps resting hedge
    exposure. ``runner_factory`` lets tests build the runner without network adapters."""
    from ..matching.matcher import merge_snapshots
    from ..strategy.alerts import Alerter
    from ..strategy.broker import KalshiBroker, PaperBroker
    from ..strategy.maker import MakerConfig, MakerRunner, MarketFeed

    settings = _settings(args, settings)
    venues = _venue_list(args)
    if "kalshi" not in venues:
        raise SystemExit("the maker runner rests orders on Kalshi; include kalshi in --venues")
    allowed = allowed_venues(getattr(args, "allowed_venues", "executable"), settings)
    hv = hedge_venues(getattr(args, "hedge_venues", None), settings)
    if allowed is not None:
        keep = set(allowed) | {"kalshi"} | set(hv)   # an opted-in hedge venue must be fetched to be a hedge
        dropped = [v for v in venues if v not in keep]
        venues = [v for v in venues if v in keep]
        if dropped:
            print(f"not fetching {','.join(dropped)}: not executable for this account (--allowed-venues {args.allowed_venues}); --hedge-venues names an explicit opt-in")
    for v in hv:
        why = compliance.ineligible_reason(v, settings)
        if why:
            print(f"WARNING: hedge venue {why}. Fills will be alerted as NOT EXECUTABLE; drop it from --hedge-venues unless this account can trade there.")
    markets = tuple(m.strip() for m in args.markets.split(",") if m.strip())
    cfg = MakerConfig(sport=args.sport, market_types=markets, size=args.size, min_margin=args.min_margin, target_margin=args.target_margin, max_orders=args.max_orders, max_notional=args.max_notional, max_per_event=args.max_per_event, queue_ahead=not args.deep_queue, interval=args.interval, rescan=args.rescan, hedge_venues=hv, hedge_cash=hedge_cash(getattr(args, "hedge_cash", None), settings))
    if runner_factory is not None:
        runner = runner_factory(cfg, settings, venues)
    else:
        from ..cli import build_adapters
        from ..venues import KalshiClient, PolymarketAdapter, RobinhoodAdapter

        data_client = KalshiClient(env=os.environ.get("KALSHI_DATA_ENV", "prod"))
        adapters = build_adapters(venues, False, kalshi_client=data_client)

        def scan_fn():
            snaps = [a.fetch(args.sport) for a in adapters]
            failed = [s.venue for s in snaps if s.errors and not s.events]
            if failed:
                raise RuntimeError(f"venue fetch failed: {', '.join(failed)}")
            merged = merge_snapshots(snaps)
            return [me for me in merged.values() if len(me.quotes_by_venue) >= 2 and me.info.market_type in markets]

        feed = MarketFeed(data_client, RobinhoodAdapter() if "robinhood" in venues else None, PolymarketAdapter() if "polymarket" in venues else None)
        if args.mode == "paper":
            broker = PaperBroker()
        else:
            broker = KalshiBroker(KalshiClient(env="demo" if args.mode == "demo" else "prod"), confirm=args.confirm)
        runner = MakerRunner(cfg, feed, broker, Alerter(journal_path=args.journal), settings, scan_fn)
    print(f"mode={args.mode}  broker={runner.broker.name}  hedge_venues={','.join(runner.hedge_venues)}  hedge_cash=${cfg.hedge_cash:g}  journal={args.journal}  (Ctrl-C cancels all resting orders and exits)")
    try:
        runner.run(duration=args.duration, max_iterations=args.iterations)
    except KeyboardInterrupt:
        runner.shutdown()
    if getattr(args, "state", None):
        with open(args.state, "w", encoding="utf-8") as f:
            json.dump(runner.snapshot(), f, indent=1, default=str)
        print(f"wrote {args.state}")
    return 0

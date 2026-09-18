"""Command line: ``python -m arb_engine <command>``.

  scan     pull venues, merge events, print arbs / edges / max-buy prices
  quote    one event or team: every venue's price, fee, all-in, fair value, max buy
  fees     fee calculator for a venue
  kelly    size a +EV (non-arb) position
  kalshi   balance / positions / orders / order (dry-run unless --confirm)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from typing import Any, Optional

import os

from . import __version__
from .config import load_dotenv, settings_from_env
from .fees import KalshiFees, PolymarketFees, PolymarketUSFees, RobinhoodFees
from .quant.sizing import kelly_stake
from .scanner import ScanResult, scan

ALL_VENUES = ("kalshi", "polymarket", "robinhood")


def build_adapters(venues: list[str], with_books: bool, kalshi_client=None):
    from .venues import KalshiAdapter, PolymarketAdapter, RobinhoodAdapter

    out = []
    for v in venues:
        if v == "kalshi":
            out.append(KalshiAdapter(client=kalshi_client, with_books=with_books))
        elif v == "polymarket":
            out.append(PolymarketAdapter(with_books=with_books))
        elif v == "robinhood":
            out.append(RobinhoodAdapter())
        else:
            raise SystemExit(f"unknown venue {v}")
    return out


def _pct(x: Optional[float]) -> str:
    return "  -  " if x is None else f"{x * 100:5.1f}%"


def _p(x: Optional[float]) -> str:
    return "  -  " if x is None else f"{x:.3f}"


def print_scan(res: ScanResult, min_margin: float, limit: int, show_all: bool, include_live: bool = False, include_thin: bool = False) -> None:
    arbs = res.arbs(min_margin, include_live=include_live, include_thin=include_thin)
    live_n = sum(1 for e in res.events if e.live)
    by_type = {}
    for e in res.events:
        by_type[e.market_type] = by_type.get(e.market_type, 0) + 1
    print(f"# {res.sport.upper()} scan  venues={','.join(res.venues)}  events={len(res.events)} {by_type} (live/in-play: {live_n})  arbs(margin>{min_margin:.2%}{'' if include_live else ', pre-game only'})={len(arbs)}")
    for v, errs in res.errors.items():
        for e in errs:
            print(f"  ! {v}: {e}")
    rows = res.events if show_all else arbs
    for ev in rows[:limit]:
        m = ev.margin
        head = f"\n[{ev.market_type}] {ev.title}  [{ev.event_key}]  start={ev.start_time or '?'}  venues={','.join(ev.venues)}"
        if m is not None:
            head += f"\n  sum-of-asks={ev.gross_sum:.3f}  fee-adjusted margin={_pct(m)} per $1 payout"
            if ev.sized_arb:
                sa = ev.sized_arb
                head += f"  | fillable: {sa['contracts']:.0f} contracts -> profit ${sa['profit']:.2f} ({sa['roi']:.2%} on ${sa['total_cost']:.2f})"
            elif m > 0:
                head += "  | NOT fillable at quoted sizes"
        if ev.flags:
            head += f"  flags={','.join(ev.flags)}"
        if ev.live:
            head += "  (IN-PLAY: quotes move within seconds; treat cross-venue gaps as staleness)"
        print(head)
        for o in ev.outcomes:
            print(f"  {o.label:<22} fair={_p(o.fair)}  best={o.best_buy_venue or '-':<10} all-in={_p(o.best_buy_all_in)}  edge={_pct(o.edge_at_best)}")
            for vp in o.venues:
                ex = f"/{vp.exchange}" if vp.exchange and vp.venue == "robinhood" else ""
                tags = []
                if vp.mirror_of:
                    tags.append(f"same book as {vp.mirror_of}")
                if vp.stale:
                    tags.append("STALE")
                if vp.age_s is not None:
                    tags.append(f"last change {vp.age_s:.0f}s ago")
                print(f"      {vp.venue + ex:<20} ask={_p(vp.ask)} bid={_p(vp.bid)} fee/ct={_p(vp.fee_per_contract)} all-in={_p(vp.all_in)} max-buy(taker)={_p(vp.max_buy_price)} max-buy(maker)={_p(vp.max_buy_maker)} size={vp.ask_size if vp.ask_size is not None else '-'}{('  [' + ', '.join(tags) + ']') if tags else ''}")
        if ev.arb:
            legs = " + ".join(f"{l['venue']}:{l['label'] or l['outcome']}@{l['price']:.2f}(fee {l['fee']:.2f})" for l in ev.arb["legs"])
            print(f"  legs@{ev.arb['contracts']:.0f}: {legs}  => cost ${ev.arb['total_cost']:.2f} for ${ev.arb['payout']:.0f} payout, profit ${ev.arb['profit']:.2f}")


def cmd_scan(args: argparse.Namespace) -> int:
    settings = settings_from_env()
    if args.gold:
        settings["robinhood_gold"] = True
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    markets = {m.strip() for m in args.markets.split(",") if m.strip()}
    res = scan(args.sport, build_adapters(venues, False), settings=settings, contracts=args.contracts, target_margin=args.target_margin, only_cross_venue=args.cross_only, max_quote_age=args.max_quote_age, market_types=markets, depth_for_candidates=args.books, candidate_margin=args.candidate_margin)
    if args.json:
        payload = asdict(res)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1, default=str)
        print(f"wrote {args.json}")
    print_scan(res, args.min_margin, args.limit, args.all, include_live=args.include_live, include_thin=args.include_thin)
    return 0


def cmd_quote(args: argparse.Namespace) -> int:
    settings = settings_from_env()
    if args.gold:
        settings["robinhood_gold"] = True
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    markets = {m.strip() for m in args.markets.split(",") if m.strip()}
    res = scan(args.sport, build_adapters(venues, args.books), settings=settings, contracts=args.contracts, target_margin=args.target_margin, max_quote_age=args.max_quote_age, market_types=markets)
    needle = args.query.lower()
    hits = [e for e in res.events if needle in e.event_key.lower() or needle in e.title.lower()]
    if not hits:
        print(f"no event matching {args.query!r}")
        return 1
    res.events = hits
    print_scan(res, -1.0, len(hits), True, include_live=True)
    return 0


def cmd_fees(args: argparse.Namespace) -> int:
    venue = args.venue
    if venue == "kalshi":
        fm = KalshiFees(multiplier=KalshiFees.from_series({"fee_multiplier": args.multiplier}).multiplier, maker_fees=args.maker_fees, rounding=args.rounding)
    elif venue == "robinhood":
        fm = RobinhoodFees(gold=args.gold, exchange=args.exchange)
    elif venue == "polymarket":
        fm = PolymarketFees(rate=PolymarketFees().rate if args.rate is None else __import__("decimal").Decimal(str(args.rate)))
    elif venue == "polymarket_us":
        fm = PolymarketUSFees.for_date()
    else:
        raise SystemExit("venue must be kalshi|robinhood|polymarket|polymarket_us")
    fee = fm.fee(args.price, args.contracts, args.role)
    cost = args.price * args.contracts + float(fee)
    print(f"{venue}: {args.contracts} contracts @ {args.price:.4f} ({args.role}) -> fee ${fee}  all-in ${cost:.4f}  = {cost / args.contracts:.4f}/contract")
    return 0


def cmd_kelly(args: argparse.Namespace) -> int:
    r = kelly_stake(args.bankroll, args.fair, args.cost, args.fraction)
    print(json.dumps(r, indent=1))
    return 0


def cmd_rh_event(args: argparse.Namespace) -> int:
    from .eventlookup import EventAnalyzer

    settings = settings_from_env()
    if args.gold:
        settings["robinhood_gold"] = True
    res = EventAnalyzer().analyze_url(args.url, settings=settings, contracts=args.contracts, target_margin=args.target_margin)
    if args.json:
        print(json.dumps(res, indent=1, default=str))
        return 0
    if not res.get("ok"):
        print("error:", res.get("error"))
        return 1
    if not res.get("analysis"):
        print(res.get("note"))
        return 0
    from .scanner import EventReport, OutcomeReport, VenuePrice

    a = res["analysis"]
    if "lines" in a:
        reps = [EventReport(**{k: v for k, v in d.items() if k not in ("outcomes", "contract_id", "symbol")}, outcomes=[OutcomeReport(**{**o, "venues": [VenuePrice(**v) for v in o["venues"]]}) for o in d["outcomes"]]) for d in a["lines"]]
        fake = ScanResult(sport="nfl", fetched_at=0, venues=a["venues"], events=reps, errors={"lookup": a.get("errors", [])} if a.get("errors") else {})
        print(f"{res['event']['name']}  ({res['event']['game']}, {a['market_type']} lines: {len(reps)})")
        print_scan(fake, args.min_margin if hasattr(args, "min_margin") else -1.0, args.limit, args.all, include_live=True, include_thin=args.include_thin)
        return 0
    rep = EventReport(**{k: v for k, v in a.items() if k not in ("outcomes", "errors")}, outcomes=[OutcomeReport(**{**o, "venues": [VenuePrice(**v) for v in o["venues"]]}) for o in a["outcomes"]])
    fake = ScanResult(sport=res["event"]["sport"], fetched_at=0, venues=rep.venues, events=[rep], errors={"lookup": a.get("errors", [])} if a.get("errors") else {})
    print_scan(fake, -1.0, 1, True, include_live=True)
    return 0


def cmd_bridge(args: argparse.Namespace) -> int:
    from .bridge import serve

    serve(args.host, args.port)
    return 0


def cmd_maker(args: argparse.Namespace) -> int:
    from .matching.matcher import merge_snapshots
    from .strategy.alerts import Alerter
    from .strategy.broker import KalshiBroker, PaperBroker
    from .strategy.maker import MakerConfig, MakerRunner, MarketFeed
    from .venues import KalshiClient, PolymarketAdapter, RobinhoodAdapter

    settings = settings_from_env()
    if args.gold:
        settings["robinhood_gold"] = True
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    if "kalshi" not in venues:
        raise SystemExit("the maker runner rests orders on Kalshi; include kalshi in --venues")
    data_client = KalshiClient(env=os.environ.get("KALSHI_DATA_ENV", "prod"))
    adapters = build_adapters(venues, False, kalshi_client=data_client)
    markets = tuple(m.strip() for m in args.markets.split(",") if m.strip())

    def scan_fn():
        snaps = [a.fetch(args.sport) for a in adapters]
        failed = [s.venue for s in snaps if s.errors and not s.events]
        if failed:
            raise RuntimeError(f"venue fetch failed: {', '.join(failed)}")
        merged = merge_snapshots(snaps)
        return [me for me in merged.values() if len(me.quotes_by_venue) >= 2 and me.info.market_type in markets]

    feed = MarketFeed(data_client, RobinhoodAdapter(), PolymarketAdapter())
    if args.mode == "paper":
        broker = PaperBroker()
    else:
        env = "demo" if args.mode == "demo" else "prod"
        broker = KalshiBroker(KalshiClient(env=env), confirm=args.confirm)
    cfg = MakerConfig(sport=args.sport, market_types=markets, size=args.size, min_margin=args.min_margin, target_margin=args.target_margin, max_orders=args.max_orders, max_notional=args.max_notional, max_per_event=args.max_per_event, queue_ahead=not args.deep_queue, interval=args.interval, rescan=args.rescan)
    runner = MakerRunner(cfg, feed, broker, Alerter(journal_path=args.journal), settings, scan_fn)
    print(f"mode={args.mode}  broker={broker.name}  journal={args.journal}  (Ctrl-C cancels all resting orders and exits)")
    try:
        runner.run(duration=args.duration, max_iterations=args.iterations)
    except KeyboardInterrupt:
        runner.shutdown()
    if args.state:
        with open(args.state, "w", encoding="utf-8") as f:
            json.dump(runner.snapshot(), f, indent=1, default=str)
        print(f"wrote {args.state}")
    return 0


def cmd_kalshi(args: argparse.Namespace) -> int:
    from .execution.kalshi import KalshiExecutor

    ex = KalshiExecutor()
    if args.action == "balance":
        print(json.dumps(ex.client.balance(), indent=1))
    elif args.action == "positions":
        print(json.dumps(ex.client.positions(), indent=1))
    elif args.action == "orders":
        print(json.dumps(ex.client.orders(status="resting"), indent=1))
    elif args.action == "order":
        plan = ex.plan(ticker=args.ticker, action=args.side_action, side=args.side, count=args.count, price=args.price, post_only=args.post_only)
        print(json.dumps(ex.execute(plan, confirm=args.confirm), indent=1))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="arb-engine", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--sport", default="nfl", choices=["nfl", "tennis", "ncaaf", "nba", "nhl", "mlb"])
        sp.add_argument("--venues", default=",".join(ALL_VENUES))
        sp.add_argument("--contracts", type=float, default=100, help="reference size for fee rounding")
        sp.add_argument("--target-margin", type=float, default=0.0, help="required locked-in margin per $1 payout for max-buy prices")
        sp.add_argument("--books", action="store_true", help="second pass: fetch real order books for candidate events (margin above --candidate-margin) and re-size")
        sp.add_argument("--candidate-margin", type=float, default=-0.01, help="events at or above this top-of-book margin get real depth in the --books pass")
        sp.add_argument("--gold", action="store_true", help="price Robinhood commission at the Gold rate")
        sp.add_argument("--max-quote-age", type=float, default=600, help="seconds; snapshots older than this are excluded from arb legs")
        sp.add_argument("--markets", default="moneyline,spread,total", help="comma list of market types to include")

    s = sub.add_parser("scan", help="scan a sport across venues")
    common(s)
    s.add_argument("--min-margin", type=float, default=0.0)
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--all", action="store_true", help="print every event, not only arbs")
    s.add_argument("--cross-only", action="store_true", help="only events quoted on 2+ venues")
    s.add_argument("--include-live", action="store_true", help="count in-play events as arbs (default: pre-game only)")
    s.add_argument("--include-thin", action="store_true", help="count arbs that are not fillable for >= 1 contract at the quoted size")
    s.add_argument("--json", help="write full result to this file")
    s.set_defaults(func=cmd_scan)

    q = sub.add_parser("quote", help="show one event across venues (substring of key/title, e.g. PHI or 'Eagles')")
    common(q)
    q.add_argument("query")
    q.set_defaults(func=cmd_quote)

    f = sub.add_parser("fees", help="fee calculator")
    f.add_argument("--venue", required=True)
    f.add_argument("--price", type=float, required=True)
    f.add_argument("--contracts", type=float, default=100)
    f.add_argument("--role", default="taker", choices=["taker", "maker"])
    f.add_argument("--gold", action="store_true")
    f.add_argument("--exchange", default="rothera")
    f.add_argument("--multiplier", type=float, default=1.0)
    f.add_argument("--maker-fees", action="store_true", help="series charges maker fees (game markets do)")
    f.add_argument("--rounding", default="cent", choices=["cent", "centicent"])
    f.add_argument("--rate", type=float, default=None, help="polymarket feeSchedule rate override")
    f.set_defaults(func=cmd_fees)

    k = sub.add_parser("kelly", help="Kelly sizing for a +EV contract")
    k.add_argument("--bankroll", type=float, required=True)
    k.add_argument("--fair", type=float, required=True, help="your probability")
    k.add_argument("--cost", type=float, required=True, help="all-in cost per contract (price + fee)")
    k.add_argument("--fraction", type=float, default=0.25)
    k.set_defaults(func=cmd_kelly)

    re_ = sub.add_parser("rh-event", help="analyse one Robinhood event page URL across venues (what the overlay shows)")
    re_.add_argument("url")
    re_.add_argument("--contracts", type=float, default=100)
    re_.add_argument("--target-margin", type=float, default=0.0)
    re_.add_argument("--gold", action="store_true")
    re_.add_argument("--json", action="store_true")
    re_.add_argument("--all", action="store_true", help="print every line, not only arbs (line pages)")
    re_.add_argument("--limit", type=int, default=60)
    re_.add_argument("--min-margin", type=float, default=0.0)
    re_.add_argument("--include-thin", action="store_true")
    re_.set_defaults(func=cmd_rh_event)

    b = sub.add_parser("bridge", help="local HTTP bridge for the browser overlay (127.0.0.1:8765)")
    b.add_argument("--host", default="127.0.0.1")
    b.add_argument("--port", type=int, default=8765)
    b.set_defaults(func=cmd_bridge)

    mk = sub.add_parser("maker", help="rest Kalshi orders at arb-creating prices vs the cheapest hedge elsewhere; alert on fills")
    common(mk)
    mk.add_argument("--mode", default="paper", choices=["paper", "demo", "live"], help="paper = simulated fills from live prices (default); demo = real orders on Kalshi demo; live = prod (needs --confirm + ARB_LIVE_TRADING=1)")
    mk.add_argument("--confirm", action="store_true", help="required for demo/live order placement")
    mk.add_argument("--size", type=float, default=100, help="contracts per resting order")
    mk.add_argument("--min-margin", type=float, default=0.01, help="required margin per $1 if filled and hedged at the current ask")
    mk.add_argument("--max-orders", type=int, default=8)
    mk.add_argument("--max-notional", type=float, default=500.0, help="max dollars resting across all orders")
    mk.add_argument("--max-per-event", type=int, default=1)
    mk.add_argument("--deep-queue", action="store_true", help="also rest below the current best bid (default: only at/above it)")
    mk.add_argument("--interval", type=float, default=10.0, help="seconds between quote refreshes")
    mk.add_argument("--rescan", type=float, default=300.0, help="seconds between full cross-venue scans")
    mk.add_argument("--duration", type=float, default=3600.0, help="seconds to run")
    mk.add_argument("--iterations", type=int, default=None, help="stop after N loops (testing)")
    mk.add_argument("--journal", default="out/maker_journal.jsonl")
    mk.add_argument("--state", default=None, help="write watches/orders/fills JSON here on exit")
    mk.set_defaults(func=cmd_maker)

    ka = sub.add_parser("kalshi", help="authenticated Kalshi actions (demo env unless KALSHI_ENV=prod)")
    ka.add_argument("action", choices=["balance", "positions", "orders", "order"])
    ka.add_argument("--ticker")
    ka.add_argument("--side-action", default="buy", choices=["buy", "sell"])
    ka.add_argument("--side", default="yes", choices=["yes", "no"])
    ka.add_argument("--count", type=float, default=1)
    ka.add_argument("--price", type=float, help="price of the chosen side in dollars, e.g. 0.52")
    ka.add_argument("--post-only", action="store_true")
    ka.add_argument("--confirm", action="store_true", help="actually submit (otherwise dry-run)")
    ka.set_defaults(func=cmd_kalshi)

    args = p.parse_args(argv)
    return int(args.func(args) or 0)

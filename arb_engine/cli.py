"""Command line: ``python -m arb_engine <command>``.

  scan     pull venues, merge events, print arbs / edges / max-buy prices
  quote    one event or team: every venue's price, fee, all-in, fair value, max buy
  fees     fee calculator for a venue
  kelly    size a +EV (non-arb) position
  kalshi   balance / positions / orders / order (dry-run unless --confirm)
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
import pkgutil
import sys
import time
from dataclasses import asdict
from typing import Any, Callable, Optional

from . import __version__
from .config import load_dotenv, load_settings
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


def cmd_scan(args: argparse.Namespace, settings: Optional[dict[str, Any]] = None) -> int:
    settings = dict(settings) if settings is not None else load_settings()
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
    if args.record:
        from .store import Store

        st = Store(args.record)
        n = st.record_scan(res)
        st.close()
        print(f"recorded {n} rows to {args.record}")
    print_scan(res, args.min_margin, args.limit, args.all, include_live=args.include_live, include_thin=args.include_thin)
    return 0


def cmd_quote(args: argparse.Namespace, settings: Optional[dict[str, Any]] = None) -> int:
    settings = dict(settings) if settings is not None else load_settings()
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


def cmd_fees(args: argparse.Namespace, settings: Optional[dict[str, Any]] = None) -> int:
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


def cmd_kelly(args: argparse.Namespace, settings: Optional[dict[str, Any]] = None) -> int:
    r = kelly_stake(args.bankroll, args.fair, args.cost, args.fraction)
    print(json.dumps(r, indent=1))
    return 0


def cmd_rh_event(args: argparse.Namespace, settings: Optional[dict[str, Any]] = None) -> int:
    from .eventlookup import EventAnalyzer

    settings = dict(settings) if settings is not None else load_settings()
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


def cmd_bridge(args: argparse.Namespace, settings: Optional[dict[str, Any]] = None) -> int:
    from .bridge import serve

    serve(args.host, args.port)
    return 0


def cmd_maker(args: argparse.Namespace, settings: Optional[dict[str, Any]] = None) -> int:
    from .matching.matcher import merge_snapshots
    from .strategy.alerts import Alerter
    from .strategy.broker import KalshiBroker, PaperBroker
    from .strategy.maker import MakerConfig, MakerRunner, MarketFeed
    from .venues import KalshiClient, PolymarketAdapter, RobinhoodAdapter

    settings = dict(settings) if settings is not None else load_settings()
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


def _espn_state_fetcher(event_key: str, refresh_summary_every: float = 30.0):
    """Returns a zero-arg fetcher for the ESPN GameState of one event (scoreboard every call,
    summary — win-probability series + odds — at most every ``refresh_summary_every`` s)."""
    from .venues.espn import ESPNClient, ESPNFeed

    parts = event_key.split(":")
    feed = ESPNFeed(ESPNClient(sport=parts[0] if parts and parts[0] else "nfl"))
    date = parts[2] if len(parts) > 2 and parts[2] else None
    state = {"last_summary": 0.0, "cache": None}

    def fetch():
        import time as _t

        gs = feed.find(event_key, date)
        if gs is None:
            return state["cache"]
        if gs.status != "pre" and _t.time() - state["last_summary"] >= refresh_summary_every:
            try:
                gs = feed.enrich(gs)
                state["last_summary"] = _t.time()
            except Exception:
                pass
        elif state["cache"] is not None and state["cache"].event_id == gs.event_id:
            # keep the last enriched fields between summary refreshes — except the WP of a
            # suspect state (the guard nulled it on purpose; see venues/espn.py StateGuard)
            suspect = bool(getattr(gs, "suspect", False))
            for k in ("espn_home_wp", "espn_wp_series", "vegas_spread_home", "vegas_total", "odds_provider"):
                if suspect and k in ("espn_home_wp", "espn_wp_series"):
                    continue
                if getattr(gs, k, None) in (None, []) and getattr(state["cache"], k, None) not in (None, []):
                    setattr(gs, k, getattr(state["cache"], k))
        state["cache"] = gs
        return gs

    return fetch


def cmd_inplay(args: argparse.Namespace, settings: Optional[dict[str, Any]] = None) -> int:
    """Watch one Robinhood event with your open lots; alert on STEAL / LOCK NOW."""
    from .eventlookup import EventAnalyzer
    from .strategy.alerts import Alerter
    from .strategy.inplay import InplayWatcher, Lot

    settings = dict(settings) if settings is not None else load_settings()
    if args.gold:
        settings["robinhood_gold"] = True
    lots = [Lot.parse(p) for p in (args.position or [])]
    an = EventAnalyzer()

    def fetch():
        res = an.analyze_url(args.url, settings=settings, contracts=args.contracts)
        if not res.get("ok"):
            raise RuntimeError(res.get("error"))
        if "lines" in (res.get("analysis") or {}):
            raise RuntimeError("inplay watches a game-winner (moneyline) page; open the game's main event page")
        return an.last_event

    fetch_state = None
    if not args.no_espn:
        first = fetch()
        fetch_state = _espn_state_fetcher(first.event_key)
    store = None
    if args.record:
        from .store import Store

        store = Store(args.record)
    w = InplayWatcher(fetch, lots, Alerter(journal_path=args.journal), settings, steal_edge=args.steal_edge, target_margin=args.target_margin, fetch_state=fetch_state, store=store, bankroll=args.bankroll, kelly_fraction=args.kelly)
    if args.iterations == 1 or args.once:
        view = w.step()
        print(f"{view.title}  live={view.live}  cost=${view.total_cost:.2f}  payout_if={ {k: round(v, 1) for k, v in view.payout_if.items()} }" + (f"  locked P&L=${view.locked_pnl:.2f}" if view.balanced else ""))
        if view.game_line:
            print(f"  {view.game_line}")
        if view.fair_line:
            print(f"  {view.fair_line}")
        for sv in view.sides:
            print(f"  {sv.label:<20} held={sv.held:g} avg={_p(sv.avg_all_in)} fair={_p(sv.fair)} [mkt {_p(sv.market_p)} model {_p(sv.model_p)} espn {_p(sv.espn_p)}] best={sv.best_venue or '-'} ask={_p(sv.best_ask)} all-in={_p(sv.best_all_in)} edge={_pct(sv.steal_edge)}" + (f"  need={sv.need:g} lock<= {_p(sv.lock_price)} {'AVAILABLE' if sv.lock_available else ''}" if sv.need else ""))
        for a in view.actions:
            print("  ->", a)
        return 0
    print(f"watching {args.url} every {args.interval}s with {len(lots)} lot(s); espn={'on' if fetch_state else 'off'}; journal={args.journal}")
    w.run(interval=args.interval, duration=args.duration, max_iterations=args.iterations)
    return 0


def cmd_games(args: argparse.Namespace, settings: Optional[dict[str, Any]] = None) -> int:
    """This week's NFL games from ESPN with status, score, spread and the model's pre-game P(home)."""
    from .strategy.inplay import game_line, model_home_wp
    from .venues.espn import ESPNClient, ESPNFeed

    feed = ESPNFeed(ESPNClient(sport=args.sport))
    games = feed.games(args.date)
    print(f"{len(games)} games" + (f" on {args.date}" if args.date else " this week") + "  (spread = home line, DraftKings via ESPN; P(home) = our WP model)")
    for g in sorted(games, key=lambda x: (x.start_time or 0).timestamp() if x.start_time else 0):
        if args.live_only and g.status != "live":
            continue
        if g.status != "pre" and args.enrich:
            try:
                g = feed.enrich(g)
            except Exception:
                pass
        p_home = model_home_wp(g)
        line = game_line(g) or ""
        extra = f"  P(home) model={p_home:.2f}" if p_home is not None else ""
        if g.espn_home_wp is not None:
            extra += f" espn={g.espn_home_wp:.2f}"
        print(f"  [{g.status:<5}] {line}{extra}  key={g.event_key}  espn={g.event_id}")
    return 0


def cmd_backtest(args: argparse.Namespace, settings: Optional[dict[str, Any]] = None) -> int:
    """Replay a finished game with public price history and score every fair-value source."""
    from .backtest import GameReplayer, summarize

    rh = None
    if args.rh_home and args.rh_away:
        rh = {"home": args.rh_home, "away": args.rh_away}
    pm = None
    if args.pm_home or args.pm_away:
        pm = {k: v for k, v in (("home", args.pm_home), ("away", args.pm_away)) if v}
    kt = {"home": args.kalshi_home, "away": args.kalshi_away} if args.kalshi_home and args.kalshi_away else None
    if args.week is not None:
        from .backtest import fit_blend_weights, replay_week, simulate_steal, summarize_many

        results, skipped = replay_week(args.season, args.week, polymarket=not args.no_polymarket, limit=args.limit, progress=print, sport=args.sport)
        fit = fit_blend_weights(results, sport=args.sport) if results else None
        sims = [simulate_steal(results, contracts=args.contracts, source="blend", lock=True, lock_fraction=lf) for lf in (0.0, 0.5, 1.0)] + [simulate_steal(results, contracts=args.contracts, source="blend", lock=False), simulate_steal(results, contracts=args.contracts, source="model", lock=False)] if results else []
        print(summarize_many(results, skipped, fit, sims))
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump({"season": args.season, "week": args.week, "fit": fit, "simulations": sims, "skipped": skipped, "games": [asdict(r) for r in results]}, f, indent=1, default=str)
            print(f"wrote {args.json}")
        return 0
    if not args.espn:
        print("need --espn <event id> or --week N", file=sys.stderr)
        return 2
    res = GameReplayer(sport=args.sport).replay(args.espn, rh_contracts=rh, pm_tokens=pm, kalshi_tickers=kt)
    print(summarize(res))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(asdict(res), f, indent=1, default=str)
        print(f"wrote {args.json}")
    return 0


def cmd_live(args: argparse.Namespace, settings: Optional[dict[str, Any]] = None) -> int:
    """Price every live (and about-to-start) game at once: fair per side, cheapest venue, STEALs."""
    from .strategy.alerts import Alerter
    from .strategy.live import LiveSlate, format_tick

    settings = dict(settings) if settings is not None else load_settings()
    if args.gold:
        settings["robinhood_gold"] = True
    store = None
    if args.record:
        from .store import Store

        store = Store(args.record)
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    slate = LiveSlate(build_adapters(venues, False), settings=settings, sport=args.sport, steal_edge=args.steal_edge, target_margin=args.target_margin, pre_hours=args.pre_hours, alerter=Alerter(journal_path=args.journal), store=store, contracts=args.contracts, bankroll=args.bankroll, kelly_fraction=args.kelly, fast=getattr(args, "fast", 0.0) or 0.0)
    if args.once:
        print(format_tick(slate.tick()))
        return 0
    print(f"live slate: {args.sport} every {args.every}s, steal edge {args.steal_edge:.0%}, pre-game window {args.pre_hours}h, venues {','.join(venues)}; journal={args.journal}")
    slate.run(interval=args.every, duration=args.hours * 3600, max_iterations=args.iterations)
    return 0


def cmd_record(args: argparse.Namespace, settings: Optional[dict[str, Any]] = None) -> int:
    """Scan on a schedule and append every event + quote to SQLite (measures arb frequency)."""
    from .store import Store

    st = Store(args.db)
    n_runs = 0
    deadline = time.time() + args.hours * 3600 if args.hours else None
    try:
        while True:
            t0 = time.time()
            try:
                settings = dict(settings) if settings is not None else load_settings()
                if args.gold:
                    settings["robinhood_gold"] = True
                res = scan(args.sport, build_adapters([v.strip() for v in args.venues.split(",") if v.strip()], False), settings=settings, contracts=args.contracts, target_margin=args.target_margin, market_types={m.strip() for m in args.markets.split(",") if m.strip()}, depth_for_candidates=args.books)
                n = st.record_scan(res)
                arbs = sum(1 for e in res.events if e.fillable and not e.live)
                print(f"{time.strftime('%H:%M:%S')} {args.sport}: {len(res.events)} events, {arbs} fillable arbs, {n} rows -> {args.db}  ({time.time() - t0:.1f}s)", flush=True)
            except Exception as e:
                print(f"{time.strftime('%H:%M:%S')} scan failed: {e!r}", flush=True)
            n_runs += 1
            if args.once or (deadline and time.time() >= deadline):
                break
            time.sleep(max(1.0, args.every - (time.time() - t0)))
    except KeyboardInterrupt:
        pass
    st.close()
    print(f"{n_runs} scan(s) recorded")
    return 0


def cmd_stats(args: argparse.Namespace, settings: Optional[dict[str, Any]] = None) -> int:
    """Arb frequency / duration from a recorded SQLite file."""
    from .store import Store

    st = Store(args.db)
    fq = st.arb_frequency(args.sport, min_margin=args.min_margin)
    print(f"{fq['snapshots']} event snapshots in {args.db}" + (f" ({args.sport})" if args.sport else ""))
    print("market      hours-to-kickoff  snapshots  with arb   share   max margin  sized profit")
    for b in fq["buckets"]:
        mm = f"{b['max_margin']*100:.2f}%" if b["max_margin"] is not None else "   -  "
        print(f"{b['market_type']:<10} {b['bucket']:>16} {b['snapshots']:>10} {b['arb_snapshots']:>9}   {(b['arb_share'] or 0)*100:5.1f}%   {mm:>9}   ${b['profit_sum']:.2f}")
    eps = fq["episodes"]
    print(f"{len(eps)} arb episode(s)" + (f"; mean length {fq['episode_scans_mean']} scans / {fq['episode_seconds_mean']}s" if eps else ""))
    for e in sorted(eps, key=lambda x: -x["max_margin"])[: args.limit]:
        print(f"  {e['event_key']:<48} {e['market_type']:<9} {e['bucket']:>8}  {e['scans']:>3} scans  {e['end'] - e['start']:>6.0f}s  max {e['max_margin']*100:.2f}%")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(fq, f, indent=1, default=str)
        print(f"wrote {args.json}")
    st.close()
    return 0


def cmd_kalshi(args: argparse.Namespace, settings: Optional[dict[str, Any]] = None) -> int:
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


Handler = Callable[..., Any]


def _call_handler(handler: Handler, args: argparse.Namespace, settings: dict[str, Any]) -> int:
    """Dispatch ``handler(args, settings)``; a legacy one-argument ``handler(args)`` still works."""
    try:
        params = inspect.signature(handler).parameters
    except (TypeError, ValueError):  # builtins / C callables: assume the modern shape
        params = None
    if params is not None and len([q for q in params.values() if q.kind in (q.POSITIONAL_ONLY, q.POSITIONAL_OR_KEYWORD)]) < 2 and not any(q.kind == q.VAR_POSITIONAL for q in params.values()):
        return int(handler(args) or 0)
    return int(handler(args, settings) or 0)


def load_plugins(subparsers: argparse._SubParsersAction, parsers: dict[str, argparse.ArgumentParser], package: Any = None) -> dict[str, Handler]:
    """Import every public module in ``arb_engine.cli_plugins`` (name order) and call its
    ``register(subparsers, existing_parsers)``; collect ``{subcommand: handler}`` overrides.

    A plugin that raises is reported on stderr and skipped: one feature's bug must not take
    the whole CLI down, and its own tests exercise it directly. ``package`` is injectable so
    tests can point the loader at a scratch package; the plugin search path is the package's
    ``__path__``, which tests may also monkeypatch to an empty directory."""
    if package is None:
        from . import cli_plugins as package
    overrides: dict[str, Handler] = {}
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        if info.name.startswith("_"):
            continue
        qualname = f"{package.__name__}.{info.name}"
        try:
            mod = importlib.import_module(qualname)
            register = getattr(mod, "register", None)
            if register is None:
                continue
            ret = register(subparsers, parsers)
        except Exception as e:  # noqa: BLE001 - a broken plugin is reported, not fatal
            print(f"! cli plugin {qualname} skipped: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        for name, sp in subparsers.choices.items():
            parsers.setdefault(name, sp)
        if ret:
            overrides.update(ret)
    return overrides


def build_parser(plugins: bool = True) -> tuple[argparse.ArgumentParser, dict[str, Handler]]:
    """The root parser with every built-in subcommand, then plugin flags / subcommands.

    Returns the parser and the plugin handler overrides ``main`` consults before ``args.func``."""
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
    s.add_argument("--record", metavar="DB", nargs="?", const="out/history.db", help="append every event and quote to this SQLite file (bare flag: out/history.db)")
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

    ip = sub.add_parser("inplay", help="watch one game with your open lots: STEAL (below fair) and LOCK NOW (other side cheap enough to guarantee profit)")
    ip.add_argument("url", help="Robinhood game event URL")
    ip.add_argument("--position", action="append", help="lot you hold, venue:outcome:price:count[:exchange], e.g. robinhood:DEN:0.50:100 (repeatable)")
    ip.add_argument("--steal-edge", type=float, default=0.03, help="all-in must be this far below consensus fair to flag STEAL")
    ip.add_argument("--target-margin", type=float, default=0.0, help="extra locked margin required per $1 for LOCK")
    ip.add_argument("--contracts", type=float, default=100)
    ip.add_argument("--gold", action="store_true")
    ip.add_argument("--interval", type=float, default=5.0)
    ip.add_argument("--duration", type=float, default=4 * 3600)
    ip.add_argument("--iterations", type=int, default=None)
    ip.add_argument("--once", action="store_true", help="evaluate once and print")
    ip.add_argument("--journal", default="out/inplay_journal.jsonl")
    ip.add_argument("--no-espn", action="store_true", help="do not pull live game state / model (market consensus only)")
    ip.add_argument("--record", metavar="DB", nargs="?", const="out/history.db", help="append every tick (game state, sources, actions) to this SQLite file (bare flag: out/history.db)")
    ip.add_argument("--bankroll", type=float, help="dollars you are willing to deploy; STEAL alerts then say how many contracts (fractional Kelly, capped by depth)")
    ip.add_argument("--kelly", type=float, default=0.25, help="Kelly fraction for sizing (default 0.25)")
    ip.set_defaults(func=cmd_inplay)

    gm = sub.add_parser("games", help="this week's NFL games from ESPN: status, score, situation, spread, model P(home)")
    gm.add_argument("--date", default=None, help="YYYY-MM-DD (default: current week)")
    gm.add_argument("--live-only", action="store_true")
    gm.add_argument("--enrich", action="store_true", help="also pull each game's summary (ESPN win probability)")
    gm.add_argument("--sport", default="nfl", choices=["nfl", "ncaaf", "nba", "nhl"])
    gm.set_defaults(func=cmd_games)

    lv = sub.add_parser("live", help="price every live NFL game at once (model/market/ESPN fair, cheapest venue, STEAL flags)")
    lv.add_argument("--sport", default="nfl", choices=["nfl", "ncaaf", "nba", "nhl"])
    lv.add_argument("--every", type=float, default=10.0, help="seconds between ticks")
    lv.add_argument("--hours", type=float, default=8.0)
    lv.add_argument("--iterations", type=int)
    lv.add_argument("--once", action="store_true")
    lv.add_argument("--pre-hours", type=float, default=1.0, help="also show games starting within this many hours")
    lv.add_argument("--steal-edge", type=float, default=0.03)
    lv.add_argument("--target-margin", type=float, default=0.0)
    lv.add_argument("--venues", default="kalshi,polymarket,robinhood")
    lv.add_argument("--contracts", type=int, default=100)
    lv.add_argument("--gold", action="store_true")
    lv.add_argument("--record", metavar="DB", nargs="?", const="out/history.db", help="append every game tick to this SQLite file (bare flag: out/history.db)")
    lv.add_argument("--bankroll", type=float, help="dollars to deploy; STEAL alerts then include a contract count (fractional Kelly, capped by depth)")
    lv.add_argument("--kelly", type=float, default=0.25)
    lv.add_argument("--journal", default="out/live.jsonl")
    lv.add_argument("--fast", type=float, default=0.0, metavar="SECONDS", help="between full ticks, refresh Kalshi + Robinhood top of book for the live games every SECONDS (e.g. 1) and run the LAG / ARB signals on it")
    lv.set_defaults(func=cmd_live)

    rc = sub.add_parser("record", help="scan on a schedule and append every event + quote to SQLite (arb frequency by time-to-kickoff)")
    rc.add_argument("--sport", default="nfl")
    rc.add_argument("--every", type=float, default=300, help="seconds between scans (default 300)")
    rc.add_argument("--hours", type=float, help="stop after this many hours (default: run until Ctrl-C)")
    rc.add_argument("--once", action="store_true")
    rc.add_argument("--books", action="store_true", help="fetch depth for candidates (slower, sized arbs)")
    rc.add_argument("--db", default="out/history.db")
    rc.add_argument("--venues", default="kalshi,polymarket,robinhood")
    rc.add_argument("--markets", default="moneyline,spread,total")
    rc.add_argument("--contracts", type=int, default=100)
    rc.add_argument("--target-margin", type=float, default=0.0)
    rc.add_argument("--gold", action="store_true")
    rc.set_defaults(func=cmd_record)

    stt = sub.add_parser("stats", help="arb frequency / duration from a recorded SQLite file")
    stt.add_argument("--db", default="out/history.db")
    stt.add_argument("--sport")
    stt.add_argument("--min-margin", type=float, default=0.0)
    stt.add_argument("--limit", type=int, default=20, help="episodes to list")
    stt.add_argument("--json")
    stt.set_defaults(func=cmd_stats)

    bt = sub.add_parser("backtest", help="replay a finished NFL game play-by-play with public price history; score model/ESPN/venues vs the outcome")
    bt.add_argument("--espn", help="ESPN event id (from `arb-engine games` keys / ESPN URLs)")
    bt.add_argument("--rh-home", help="Robinhood contract id for the home team (from the catalogue / fixtures)")
    bt.add_argument("--rh-away")
    bt.add_argument("--pm-home", help="Polymarket token id for the home outcome")
    bt.add_argument("--pm-away")
    bt.add_argument("--kalshi-home", help="override Kalshi ticker (default derived from teams + ET date)")
    bt.add_argument("--kalshi-away")
    bt.add_argument("--week", type=int, help="replay every finished game of this NFL regular-season week (Kalshi + Polymarket + ESPN + model) and fit the blend weights")
    bt.add_argument("--season", type=int, default=2026)
    bt.add_argument("--sport", default="nfl", choices=["nfl", "ncaaf"])
    bt.add_argument("--limit", type=int, help="with --week: only the first N games")
    bt.add_argument("--no-polymarket", action="store_true", help="with --week: skip the Polymarket history lookup")
    bt.add_argument("--contracts", type=int, default=10, help="with --week: contracts per simulated STEAL/LOCK entry")
    bt.add_argument("--json", help="write the full replay to this file")
    bt.set_defaults(func=cmd_backtest)

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

    overrides = load_plugins(sub, dict(sub.choices)) if plugins else {}
    return p, overrides


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv()
    p, overrides = build_parser()
    args = p.parse_args(argv)
    settings = load_settings()
    handler = overrides.get(args.cmd) or args.func
    return _call_handler(handler, args, settings)

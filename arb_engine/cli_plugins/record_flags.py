"""CLI plugin (tick recording, ladder, event study, tick replay):

  backtest-ticks --db out/history.db --game DEN|KC --gates on|off|both   replay recorded ticks
  stats --convergence [--min-games 30]                                  STEAL ladder ratios / CLV
  clv --db out/history.db                                               ladder + pre-game anchors
  event-study --rows out/week1.json --trades kalshi=TICKER[:away][@GAME]  absorption of model moves

Registered through ``register(subparsers, existing_parsers)`` (the plugin loader contract of
``arb_engine.cli_plugins``); the module also imports cleanly when no loader exists. Recording
cadence is the ``live --every`` poll interval until a websocket feed exists, so the ladder's
first rung (+10 s) is only as fine as that interval.
"""

from __future__ import annotations

import argparse
import inspect
import json
from typing import Any, Callable, Optional

try:  # settings declared by the foundation item; optional so this module imports alone
    from ..config import declare_setting  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - P01 absent
    declare_setting = None
if declare_setting is not None:
    try:
        declare_setting("TRADES_CACHE_DIR", env="ARB_TRADES_CACHE_DIR", default="out/cache/trades", cast=str, doc="read-through cache for public trade tapes (event studies)")
        declare_setting("TICK_REPLAY_STALE_AFTER_S", env="ARB_TICK_REPLAY_STALE_AFTER_S", default=15.0, cast=float, doc="fallback feed-stale threshold for backtest-ticks when the live gates are absent")
    except Exception:
        pass


def _call(fn: Callable[..., Any], args: argparse.Namespace, settings: Optional[dict[str, Any]]) -> int:
    """Call an original handler with (args) or (args, settings), whichever it takes."""
    try:
        n = len(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        n = 1
    return int((fn(args, settings) if n >= 2 else fn(args)) or 0)


# ---- handlers --------------------------------------------------------------------------------

def cmd_backtest_ticks(args: argparse.Namespace, settings: Optional[dict[str, Any]] = None) -> int:
    from ..tickreplay import format_replay, replay_both, replay_ticks

    kw = {"steal_edge": args.steal_edge, "settings": settings or {}, "stale_after_s": args.stale_after}
    if args.gates == "both":
        res = replay_both(args.db, args.game, **kw)
    else:
        res = {args.gates: replay_ticks(args.db, args.game, gates=args.gates == "on", **kw)}
    print(format_replay(res))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=1, default=str)
        print(f"wrote {args.json}")
    return 0


def print_convergence(conv: dict[str, Any]) -> None:
    print(f"{conv['n']} STEAL observations over {conv['n_games']} game(s); {conv['complete_ladders']} with a complete ladder; ratios need >= {conv['min_games']} games per cell")
    print("  edge    gated     n  games  settled   toward/away +60s   +300s   CLV_bid +60s  +300s   CLV_mid +60s  +300s   P&L")
    for c in conv["cells"]:
        if c["ok"]:
            f = lambda k: ("   -  " if c.get(k) is None else f"{c[k]:6.3f}")  # noqa: E731
            ta = lambda o: f"{c[f'toward_{o}']}/{c[f'away_{o}']}"  # noqa: E731
            print(f"  {c['bucket']:<6} {'yes' if c['gated'] else 'no':>5} {c['n']:>5} {c['n_games']:>6} {c['n_settled']:>8}   {ta(60):>10} {ta(300):>7}   {f('clv_bid_60')} {f('clv_bid_300')}   {f('clv_mid_60')} {f('clv_mid_300')}   {f('pnl_settle_mean')}")
        else:
            print(f"  {c['bucket']:<6} {'yes' if c['gated'] else 'no':>5} {c['n']:>5} {c['n_games']:>6} {c['n_settled']:>8}   (fewer than {conv['min_games']} games: ratios withheld)")


def cmd_stats(args: argparse.Namespace, settings: Optional[dict[str, Any]] = None, original: Optional[Callable[..., Any]] = None) -> int:
    if not getattr(args, "convergence", False):
        if original is None:
            from ..cli import cmd_stats as original  # type: ignore[no-redef]
        return _call(original, args, settings)
    from ..store import Store

    st = Store(args.db)
    print_convergence(st.convergence(min_games=args.min_games))
    an = st.anomaly_counts()
    print(f"espn_ticks: {an['ticks']} rows / {an['games']} games; suspect {an['suspect']}, review-pending {an['review_pending']}; sources {an['by_source']}; episodes {an['episodes']}")
    st.close()
    return 0


def cmd_clv(args: argparse.Namespace, settings: Optional[dict[str, Any]] = None) -> int:
    from ..store import Store

    st = Store(args.db)
    rep = st.clv_report(min_games=args.min_games)
    print_convergence(rep)
    for a in rep["pregame_anchors"]:
        print(f"  pregame {a['event_key']:<40} kalshi mid {a['kalshi_mid']}  sportsbook P(home) {a['sportsbook_p_home']}  observations {a['observations']}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, default=str)
        print(f"wrote {args.json}")
    st.close()
    return 0


def game_label(g: dict[str, Any]) -> str:
    return str(g.get("final") or g.get("espn_event_id") or f"{g.get('away')}|{g.get('home')}")


def parse_trade_spec(spec: str) -> dict[str, Any]:
    """``venue=market[:away][@game]`` -> ``{venue, market, is_home, game}`` (``game`` None when
    unqualified). ``:away`` says the market's YES side is the away team; ``@game`` is a
    case-insensitive substring of the game's label (``final``, ESPN id, ``AWAY|HOME``)."""
    if "=" not in spec:
        raise SystemExit(f"--trades {spec!r}: expected venue=market[:away][@game]")
    venue, rest = spec.split("=", 1)
    rest, _, game = rest.partition("@")
    is_home = not rest.endswith(":away")
    market = rest[:-5] if not is_home else rest
    if venue not in ("kalshi", "polymarket"):
        raise SystemExit(f"unknown trades venue {venue!r} in --trades {spec!r} (kalshi or polymarket)")
    if not market:
        raise SystemExit(f"--trades {spec!r}: empty market")
    return {"venue": venue, "market": market, "is_home": is_home, "game": game or None}


def bind_trade_specs(games: list[dict[str, Any]], specs: list[dict[str, Any]]) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Pair every game with the specs that name it. An unqualified spec (no ``@game``) binds
    only when exactly one game is scored: a 16-game week file must not be scored against one
    ticker's tape. A ``@game`` selector must match exactly one game. Games without a spec are
    left out (the caller reports them)."""
    labels = [game_label(g) for g in games]
    haystacks = [" ".join(str(x) for x in (lb, g.get("espn_event_id"), f"{g.get('away')}|{g.get('home')}") if x).lower() for g, lb in zip(games, labels)]
    bound: list[tuple[dict[str, Any], list[dict[str, Any]]]] = [(g, []) for g in games]
    for sp in specs:
        if sp["game"] is None:
            if len(games) != 1:
                raise SystemExit(f"--trades {sp['venue']}={sp['market']} names no game but --rows holds {len(games)} games: add @<game> (one of {', '.join(labels)}) or --limit 1")
            bound[0][1].append(sp)
            continue
        needle = sp["game"].lower()
        hits = [i for i, h in enumerate(haystacks) if needle in h]
        if len(hits) != 1:
            raise SystemExit(f"--trades @{sp['game']} matches {len(hits)} games ({', '.join(labels[i] for i in hits) or 'none'}); recorded: {', '.join(labels)}")
        bound[hits[0]][1].append(sp)
    return bound


def cmd_event_study(args: argparse.Namespace, settings: Optional[dict[str, Any]] = None) -> int:
    """Score each game in ``--rows`` against the tapes its ``--trades`` specs name, then pool
    every game's scored events through ``summarize``. Tapes are fetched (or read back from
    the cache) over the game's row span plus an hour before and half an hour after."""
    from ..quant.eventstudy import event_study, format_study, load_games, summarize
    from ..venues.trades import TradesClient, as_home_prices

    games = load_games(args.rows)
    if args.limit:
        games = games[: args.limit]
    client = TradesClient(cache_dir=args.cache_dir, offline=args.offline)
    specs = [parse_trade_spec(s) for s in (args.trades or [])]
    pooled: list[dict[str, Any]] = []
    for g, bound in bind_trade_specs(games, specs):
        rows = g.get("rows") or []
        tss = [r["ts"] for r in rows if r.get("ts") is not None]
        if not rows or not tss:
            print(f"{game_label(g)}: no rows, skipped")
            continue
        if not bound:
            print(f"{game_label(g)}: no --trades spec names it, skipped")
            continue
        t0, t1 = min(tss) - 3600, max(tss) + 1800
        trades: dict[str, Any] = {}
        for sp in bound:
            tr = client.kalshi_trades(sp["market"], t0, t1) if sp["venue"] == "kalshi" else client.polymarket_trades(sp["market"], t0, t1)
            trades[sp["venue"]] = as_home_prices(tr, is_home=sp["is_home"])
        res = event_study(rows, trades, window=(args.window_pre, args.window_post), dwp_min=args.dwp_min, game=game_label(g))
        pooled.extend(res["events"])
        print(f"{res['game']}: {res['n_events']} events, {res['n_scored']} scored on {', '.join(sorted(trades))}")
    summary = summarize(pooled)
    print(format_study(summary))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "events": pooled}, f, indent=1, default=str)
        print(f"wrote {args.json}")
    return 0


# ---- registration ------------------------------------------------------------------------------

def register(subparsers: Any, existing_parsers: Any = None) -> dict[str, Callable[..., Any]]:
    existing = existing_parsers or {}
    get = existing.get if hasattr(existing, "get") else (lambda name, default=None: default)
    handlers: dict[str, Callable[..., Any]] = {}

    bt = subparsers.add_parser("backtest-ticks", help="replay recorded ticks (live --record) through the in-play watcher with the feed gates on / off: STEAL count, CLV_bid at +60 s / +5 min, settled P&L per policy")
    bt.add_argument("--db", default="out/history.db")
    bt.add_argument("--game", help="event key or a substring of it (e.g. 'DEN|KC'); optional when one game is recorded")
    bt.add_argument("--gates", choices=["on", "off", "both"], default="both")
    bt.add_argument("--steal-edge", type=float, default=0.03)
    bt.add_argument("--stale-after", type=float, default=15.0, help="fallback feed-stale seconds when the live gates are absent")
    bt.add_argument("--json")
    bt.set_defaults(func=cmd_backtest_ticks)
    handlers["backtest-ticks"] = cmd_backtest_ticks

    stt = get("stats")
    if stt is not None:
        stt.add_argument("--convergence", action="store_true", help="STEAL ladder: toward/away ratios and CLV per edge bucket (ratios only with >= --min-games games)")
        stt.add_argument("--min-games", type=int, default=30)
        original = stt.get_default("func")
        wrapped = lambda args, settings=None, _o=original: cmd_stats(args, settings, original=_o)  # noqa: E731
        stt.set_defaults(func=wrapped)
        handlers["stats"] = wrapped

    cl = subparsers.add_parser("clv", help="closing-line value of recorded STEALs: the observation ladder plus the pre-game sportsbook / Kalshi anchors")
    cl.add_argument("--db", default="out/history.db")
    cl.add_argument("--min-games", type=int, default=30)
    cl.add_argument("--json")
    cl.set_defaults(func=cmd_clv)
    handlers["clv"] = cmd_clv

    es = subparsers.add_parser("event-study", help="how fast venues absorb large model moves: replay rows (backtest --json) x public trade tapes (cached under out/cache/trades)")
    es.add_argument("--rows", required=True, help="backtest --week --json output (games[].rows)")
    es.add_argument("--trades", action="append", help="venue=market[:away][@game], e.g. kalshi=KXNFLGAME-26SEP14DETBUF-BUF@DET|BUF or polymarket=<token id>:away; repeatable; @game (a substring of the game's label) is required when --rows holds several games")
    es.add_argument("--window-pre", type=float, default=-60, help="seconds before the play within which the pre-event print must fall (older prints are stale: the event is dropped; widen for thin tapes)")
    es.add_argument("--window-post", type=float, default=900)
    es.add_argument("--dwp-min", type=float, default=0.05)
    es.add_argument("--limit", type=int)
    es.add_argument("--cache-dir", default="out/cache/trades")
    es.add_argument("--offline", action="store_true", help="fail instead of fetching when the trade tape is not cached")
    es.add_argument("--json")
    es.set_defaults(func=cmd_event_study)
    handlers["event-study"] = cmd_event_study
    return handlers

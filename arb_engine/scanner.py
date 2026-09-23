"""Scan: pull every venue for a sport, merge by event, price fees, find arbs and edges.

Correctness rules this module enforces (see ``quant.arbitrage`` for the math):

* **Tie-aware margin.** Every report carries ``tie_margin`` beside ``margin``; when the
  chosen legs settle a tie differently (Kalshi $0.50 vs Rothera $0 / $1) the event is
  flagged ``tie-rule-mismatch:<venue>=<payout>,...``.
* **Rothera NO leg.** ``scan()`` asks the Robinhood adapter for the NO side of each Rothera
  contract (setting ``rothera_no_leg``, default on) so Kalshi YES-A + Rothera NO-A, which
  pays $1.50 on a tie, competes with Rothera YES-B, which pays $0.50. Same-book de-dupe
  keys on ``(book_id, side)`` so the NO leg survives; mirrors of another venue's book
  (Robinhood's Kalshi-routed quotes) still collapse into the direct venue. The consensus
  fair value is computed on the YES-side view (``fair_value_view``), so turning the NO leg
  on does not move ``fair`` / ``edge``.
* **Per-venue tick and minimum size.** Max-buy prices are computed on the market's own
  grid (``meta.tick``: Polymarket tails quote at $0.001) and sizing honours ``meta.min_size``
  (Polymarket: 5 shares); an arb whose legs cannot all be placed is ``below-min-size``.
* **Signal-only rows.** A venue that is not executable (``executable_venues``, from the
  compliance table when that module exists) keeps its
  row for the fair-value signal (``ineligible="not executable"``) but never becomes a leg;
  the event is flagged ``signal-only:<venue>``.
* **Gated pairs.** ``thin``, ``below-min-size`` and the settlement-registry gates
  (``walkover-exposed``, ``tier:*``, ``thin-book``) keep an event out of ``arbs()`` unless
  ``include_thin``.
"""

from __future__ import annotations

import inspect
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from typing import Any, Iterable, Optional

from .fees.registry import fee_model_for_quote
from .matching.matcher import MergedEvent, merge_snapshots
from .models import OutcomeQuote, VenueSnapshot
from .quant.arbitrage import ArbResult, Leg, best_leg_per_outcome, evaluate, max_price_for_leg, min_size_for_legs, size_for_budget, size_from_books, tick_for_quote
from .quant.fairvalue import consensus_fair_value

try:  # settings registry (P01); the scanner must import without it
    from .config import declare_setting as _declare_setting, setting as _setting_lookup
except ImportError:  # pragma: no cover - exercised when config.py predates declare_setting
    _declare_setting = None
    _setting_lookup = None


def _as_bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


SETTING_DOCS = {
    "rothera_no_leg": ("ROTHERA_NO_LEG", True, _as_bool, "scan(): emit the NO side of Rothera game contracts as its own leg (tie-aware hedge)"),
    "line_fair": ("LINE_FAIR", False, _as_bool, "scan(): attach quant.lines fair values to spread/total events when that module exists"),
}
if _declare_setting is not None:
    for _k, (_env, _default, _cast, _doc) in SETTING_DOCS.items():
        try:
            _declare_setting(_k, env=_env, default=_default, cast=_cast, doc=_doc)
        except Exception:  # already declared elsewhere or a stricter registry: not fatal
            pass


def setting(settings: Optional[dict[str, Any]], key: str, default: Any = None) -> Any:
    """Settings dict -> environment (``SETTING_DOCS`` env name) -> default; delegates to
    ``config.setting`` when the registry exists so declared defaults/casts apply."""
    settings = settings or {}
    if key in settings and settings[key] is not None:
        return settings[key]
    if _setting_lookup is not None:
        try:
            v = _setting_lookup(settings, key)
            if v is not None:
                return v
        except Exception:
            pass
    env, doc_default, cast, _ = SETTING_DOCS.get(key, (None, None, None, None))
    if env and os.environ.get(env) is not None:
        raw = os.environ[env]
        return cast(raw) if cast else raw
    return default if default is not None else doc_default


@dataclass
class VenuePrice:
    venue: str
    market_id: str
    ask: Optional[float]
    bid: Optional[float]
    ask_size: Optional[float]
    fee_per_contract: Optional[float]
    all_in: Optional[float]
    max_buy_price: Optional[float]  # highest TAKER price here that still arbs vs best hedge elsewhere
    url: Optional[str]
    exchange: Optional[str] = None
    max_buy_maker: Optional[float] = None  # same for a resting (maker) order: lower/no fee, so a higher price works
    age_s: Optional[float] = None  # seconds since the venue last changed this quote (informational)
    mirror_of: Optional[str] = None  # this quote is a re-sale of another venue's book (e.g. robinhood -> kalshi)
    stale: bool = False
    ineligible: Optional[str] = None  # "not executable": shown for the signal, never a leg
    tie_payout: Optional[float] = None  # dollars per contract on a tie (0.5 unless the adapter says)
    side: Optional[str] = None  # "yes" / "no" contract on the venue when known
    tick: Optional[float] = None  # price grid the max-buy prices sit on


@dataclass
class OutcomeReport:
    outcome: str
    label: str
    fair: Optional[float]
    venues: list[VenuePrice]
    best_buy_venue: Optional[str]
    best_buy_all_in: Optional[float]
    edge_at_best: Optional[float]  # fair - all_in


@dataclass
class EventReport:
    event_key: str
    title: str
    sport: str
    start_time: Optional[str]
    venues: list[str]
    outcomes: list[OutcomeReport]
    arb: Optional[dict]  # ArbResult as dict at reference size
    sized_arb: Optional[dict]  # depth-limited ArbResult when books were fetched
    gross_sum: Optional[float]
    margin: Optional[float]
    tie_rule: str
    flags: list[str] = field(default_factory=list)
    live: bool = False
    market_type: str = "moneyline"
    line: Optional[float] = None
    fillable: bool = False  # positive margin AND enough depth for min_size contracts
    tie_margin: Optional[float] = None  # margin if the game ties (legs' tie payouts minus cost)
    tie_payout_total: Optional[float] = None  # dollars per contract set on a tie
    line_fair: Optional[dict] = None  # quant.lines fair values when the line_fair setting is on


@dataclass
class ScanResult:
    sport: str
    fetched_at: float
    venues: list[str]
    events: list[EventReport]
    errors: dict[str, list[str]]

    def arbs(self, min_margin: float = 0.0, include_live: bool = False, include_thin: bool = False) -> list[EventReport]:
        """Executable arbs: positive margin, not live, fresh, fillable and not gated
        (``thin``, ``below-min-size``, ``walkover-exposed``, ``tier:*``, ``thin-book``) unless
        ``include_thin``."""
        return [e for e in self.events if e.margin is not None and e.margin > min_margin and (include_live or not e.live) and "stale-quote" not in e.flags and (include_thin or (e.fillable and not is_gated(e.flags)))]


GATED_FLAGS = ("thin", "thin-book", "below-min-size", "walkover-exposed")
GATED_FLAG_PREFIXES = ("tier:",)


def is_gated(flags: Iterable[str]) -> bool:
    """Flags that keep an event out of ``ScanResult.arbs()`` by default."""
    return any(f in GATED_FLAGS or f.startswith(GATED_FLAG_PREFIXES) for f in flags)


def _arb_to_dict(r: Optional[ArbResult]) -> Optional[dict]:
    return asdict(r) if r is not None else None


def _dedupe_same_book(quotes: list[OutcomeQuote]) -> tuple[list[OutcomeQuote], dict[str, str]]:
    """Keep one quote per underlying order book *and side* (the freshest).

    A book that is quoted directly (``venue == book_id``, e.g. Kalshi) collapses entirely
    into the direct venue: Robinhood's Kalshi-routed YES and NO quotes are re-sales of that
    same book and are kept for the fee comparison only. A book reached only through a
    reseller (Rothera via Robinhood) keeps one quote per ``meta.side``: the YES on team B and
    the NO on team A are different contracts with different tie payouts, so both may be
    legs. Returns (tradable quotes, {market_id: venue it mirrors})."""
    by_book: dict[str, list[OutcomeQuote]] = {}
    for q in quotes:
        by_book.setdefault(q.book_id, []).append(q)
    keep: list[OutcomeQuote] = []
    mirrors: dict[str, str] = {}
    for book, qs in by_book.items():
        if len(qs) == 1:
            keep.append(qs[0])
            continue
        direct = [q for q in qs if q.venue == book]
        if direct:
            # The direct venue is where the leg would actually be placed (and is cheaper), so
            # it wins; mirrors are kept for the fee comparison only.
            qs_sorted = sorted(qs, key=lambda q: (0 if q.venue == book else 1, -(q.quote_time or 0)))
            keep.append(qs_sorted[0])
            for other in qs_sorted[1:]:
                mirrors[other.venue_market_id] = qs_sorted[0].venue
            continue
        by_side: dict[Optional[str], list[OutcomeQuote]] = {}
        for q in qs:
            by_side.setdefault(q.meta.get("side"), []).append(q)
        for side_qs in by_side.values():
            side_sorted = sorted(side_qs, key=lambda q: -(q.quote_time or 0))
            keep.append(side_sorted[0])
            for other in side_sorted[1:]:
                mirrors[other.venue_market_id] = side_sorted[0].venue
    return keep, mirrors


def fair_value_view(quotes_by_venue: dict[str, list[OutcomeQuote]]) -> dict[str, list[OutcomeQuote]]:
    """``consensus_fair_value`` expects one quote per outcome per venue and keeps the *last*
    one it sees, so the Rothera NO legs (``meta.side="no"``, outcome = the other team) would
    silently replace the YES quotes and make the fair value depend on quote order and on a
    single contract's book. Give it the YES-side view: a NO quote only stands in for an
    outcome that has no YES quote at that venue (the mid is then ``1 - YES`` of the other
    contract, the same inference the devig already makes)."""
    out: dict[str, list[OutcomeQuote]] = {}
    for venue, qs in quotes_by_venue.items():
        yes_outcomes = {q.outcome for q in qs if q.meta.get("side") != "no" and not q.meta.get("no_of")}
        out[venue] = [q for q in qs if (q.meta.get("side") != "no" and not q.meta.get("no_of")) or q.outcome not in yes_outcomes]
    return out


UNRESTRICTED = {"all", "*"}


def resolve_executable_venues(settings: Optional[dict[str, Any]], explicit: Optional[Iterable[str]] = None) -> Optional[set[str]]:
    """Venues an order can actually be placed on: an explicit set, else the
    ``executable_venues`` setting (set / list / comma string; ``"all"`` = unrestricted;
    ``None`` = unset), else the compliance table (``arb_engine.compliance``) when it exists,
    else no restriction."""
    if explicit is not None:
        return set(explicit)
    settings = settings or {}
    v = settings.get("executable_venues")
    if v is not None:  # None = unset (config.load_settings emits every declared key): fall through to the table
        names = {x.strip().lower() for x in v.split(",") if x.strip()} if isinstance(v, str) else {str(x).strip().lower() for x in v}
        if names & UNRESTRICTED:  # an explicit "all" / "*" is the only way to say "no restriction"
            return None
        return names
    try:
        from .compliance import executable_venues as _executable_venues  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        v = _executable_venues(settings)
    except Exception:
        return None
    return None if v is None else set(v)


def registry_settlement_flags(legs: list[Leg], arb: Optional[ArbResult], info: Any, settings: Optional[dict[str, Any]]) -> list[str]:
    """Settlement-registry flags for the chosen legs (``matching.settlement_rules``, a later
    item): ``pair_flags`` per leg pair and, for tennis, ``tennis_pair_flags`` on the arb.
    Empty when the registry is not importable; a registry that raises is reported as
    ``settlement-rules-error`` rather than silently trusted."""
    try:
        from .matching.settlement_rules import pair_flags, tennis_pair_flags  # type: ignore[import-not-found]
    except ImportError:
        return []
    out: list[str] = []
    quotes = [l.quote for l in legs if l.quote is not None]
    try:
        for a, b in combinations(quotes, 2):
            out.extend(pair_flags(a, b, info.sport, info.market_type) or [])
        if info.sport == "tennis" and arb is not None:
            out.extend(tennis_pair_flags(arb, settings or {}) or [])
    except Exception:
        out.append("settlement-rules-error")
    return list(dict.fromkeys(str(f) for f in out))


def _available(leg: Leg) -> Optional[float]:
    q = leg.quote
    if q is None:
        return None
    if q.book is not None and q.book.asks:
        return float(sum(l.size for l in q.book.asks))
    return float(q.ask_size) if q.ask_size else None


def tie_mismatch_flag(legs: list[Leg]) -> Optional[str]:
    """``tie-rule-mismatch:kalshi=0.5,robinhood=0`` when the chosen legs settle a tie
    differently; None when they agree (or there is only one leg)."""
    if len({float(l.tie_payout) for l in legs}) < 2:
        return None
    return "tie-rule-mismatch:" + ",".join(f"{l.venue}={float(l.tie_payout):g}" for l in legs)


def settlement_mismatches(info: Any, quotes_by_venue: dict) -> list[str]:
    """'settlement-mismatch:<case>' for every case (walkover, retirement, …) where the venues
    holding quotes settle differently — a hedge across them is not a hedge in that case."""
    rules = {v: (info.venues.get(v) or {}).get("settlement") for v in quotes_by_venue if isinstance(info.venues.get(v), dict)}
    rules = {v: r for v, r in rules.items() if r}
    if len(rules) < 2:
        return []
    out = []
    for case in sorted({k for r in rules.values() for k in r}):
        vals = {r.get(case) for r in rules.values()}
        if len(vals) > 1:
            out.append(f"settlement-mismatch:{case}")
    return out


def analyze_event(me: MergedEvent, settings: dict[str, Any], contracts: float = 100, target_margin: float = 0.0, allowed_venues: Optional[set[str]] = None, max_quote_age: float = 600.0, now: Optional[float] = None, min_size: float = 1.0, executable_venues: Optional[set[str]] = None, budget: Optional[float] = None) -> EventReport:
    """One merged event -> fee-aware report. ``allowed_venues`` drops other venues entirely;
    ``executable_venues`` (None = unrestricted) keeps the others as signal-only rows."""
    info = me.info
    now = now or time.time()
    fee_for = lambda q: fee_model_for_quote(q, settings)  # noqa: E731
    live = bool(info.in_play) or bool(info.start_time and info.start_time.timestamp() <= now)

    all_by_outcome: dict[str, list[OutcomeQuote]] = {o: me.quotes_for_outcome(o) for o in info.outcomes}
    signal_only: set[str] = set()
    for qs in all_by_outcome.values():
        for q in qs:
            # Eligibility is the resolved executable set alone: Gamma's ``restricted`` flag is
            # venue-wide (every captured sports market carries it), so compliance folds it into
            # the default table and an explicit executable_venues override may still allow the venue.
            if executable_venues is not None and q.venue not in executable_venues:
                signal_only.add(q.venue)
    tradable: dict[str, list[OutcomeQuote]] = {}
    mirrors: dict[str, str] = {}
    stale_ids: set[str] = set()
    for o, qs in all_by_outcome.items():
        kept, m = _dedupe_same_book(qs)
        mirrors.update(m)
        fresh = []
        for q in kept:
            # Staleness is about *our* snapshot age (e.g. a cached fetch), not how long the
            # venue's best quote has been resting — a quiet pre-game book is not stale.
            if q.ts and now - q.ts > max_quote_age:
                stale_ids.add(q.venue_market_id)
                continue
            if q.venue in signal_only:
                continue
            fresh.append(q)
        tradable[o] = fresh

    legs = best_leg_per_outcome(tradable, fee_for, contracts=contracts, allowed_venues=allowed_venues)
    complete = len(legs) == len(info.outcomes)
    arb = evaluate(legs, contracts) if complete else None
    # Depth check: the largest size (book depth, else top-of-book size, else unlimited) that
    # still clears the target margin, on the legs' minimum-size floor. A tail quote backed by
    # 0.01 contracts is not an arb, nor is a 3-share Polymarket leg (5-share minimum).
    # ``budget`` (the operator's bankroll) caps the size in dollars, fees included, so the
    # sized result an alert prints is the order that can actually be paid for.
    sized = size_for_budget(legs, budget, min_margin=target_margin) if complete and arb and arb.is_arb else None
    fillable = sized is not None and sized.contracts >= min_size
    below_min = bool(arb and arb.is_arb) and any(l.min_size and (_available(l) is not None) and _available(l) < float(l.min_size) for l in legs)  # type: ignore[arg-type]
    fair = consensus_fair_value(fair_value_view(me.quotes_by_venue), info.outcomes, venue_weights=settings.get("venue_weights"))

    outcomes: list[OutcomeReport] = []
    flags: list[str] = []
    if live:
        flags.append("live")
    if info.tie_rule == "unknown" and info.sport in ("nfl", "ncaaf"):
        flags.append("tie-rule-unverified")
    flags.extend(settlement_mismatches(info, me.quotes_by_venue))
    flags.extend(registry_settlement_flags(legs, arb, info, settings))
    if complete:
        mismatch = tie_mismatch_flag(legs)
        if mismatch:
            flags.append(mismatch)
    for o in info.outcomes:
        others = [l for l in legs if l.outcome != o]
        hedgeable = complete and len(others) == len(info.outcomes) - 1
        vps: list[VenuePrice] = []
        best_venue, best_all_in = None, None
        for q in all_by_outcome[o]:
            if allowed_venues and q.venue not in allowed_venues:
                continue
            fm = fee_for(q)
            tick = tick_for_quote(q)
            fee_pc = fm.per_contract(q.ask, contracts) if q.ask is not None else None
            all_in = (q.ask + fee_pc) if (q.ask is not None and fee_pc is not None) else None
            max_buy = max_price_for_leg(others, fm, contracts, target_margin, tick=tick) if hedgeable else None
            max_buy_maker = max_price_for_leg(others, fm, contracts, target_margin, tick=tick, role="maker") if hedgeable else None
            is_stale = q.venue_market_id in stale_ids
            ineligible = "not executable" if q.venue in signal_only else None
            tp = q.meta.get("tie_payout")
            vps.append(VenuePrice(venue=q.venue, market_id=q.venue_market_id, ask=q.ask, bid=q.bid, ask_size=q.ask_size, fee_per_contract=fee_pc, all_in=all_in, max_buy_price=max_buy, url=q.url, exchange=q.meta.get("exchange") or q.fee_params.get("exchange"), max_buy_maker=max_buy_maker, age_s=q.age, mirror_of=mirrors.get(q.venue_market_id), stale=is_stale, ineligible=ineligible, tie_payout=float(tp) if tp is not None else None, side=q.meta.get("side"), tick=tick))
            if all_in is not None and not is_stale and ineligible is None and (best_all_in is None or all_in < best_all_in):
                best_venue, best_all_in = q.venue, all_in
        fv = fair.get(o)
        edge = (fv.fair - best_all_in) if (fv and fv.fair is not None and best_all_in is not None) else None
        outcomes.append(OutcomeReport(outcome=o, label=info.labels.get(o, o), fair=fv.fair if fv else None, venues=sorted(vps, key=lambda v: (v.all_in is None, v.all_in or 9)), best_buy_venue=best_venue, best_buy_all_in=best_all_in, edge_at_best=edge))
    if stale_ids:
        flags.append("stale-quote")
    for v in sorted(signal_only):
        flags.append(f"signal-only:{v}")
    if below_min:
        flags.append("below-min-size")  # a leg's venue minimum order exceeds what is on offer
    if arb and arb.is_arb and not fillable:
        flags.append("thin")  # positive margin at the reference size but not fillable for min_size contracts
    if arb and arb.is_arb:
        books_in_arb = {l.quote.book_id if l.quote else l.venue for l in legs}
        if len(books_in_arb) == 1:
            flags.append("single-book-arb")  # both sides on one book: usually a stale snapshot
    return EventReport(
        event_key=me.event_key, title=info.title(), sport=info.sport, start_time=info.start_time.isoformat() if info.start_time else None,
        venues=me.venues, outcomes=outcomes, arb=_arb_to_dict(arb), sized_arb=_arb_to_dict(sized), gross_sum=arb.gross_sum if arb else None,
        margin=arb.margin if arb else None, tie_rule=info.tie_rule, flags=flags, live=live, market_type=info.market_type, line=info.line,
        fillable=fillable, tie_margin=arb.tie_margin if arb else None, tie_payout_total=arb.tie_payout_total if arb else None,
    )


def cross_book_pairs(me: MergedEvent, settings: dict[str, Any], contracts: float = 100) -> list[dict[str, Any]]:
    """Audit view for a two-outcome event: every two-leg combination across *different*
    books (a Rothera YES-B and a Rothera NO-A are separate rows) with its margin and tie
    margin, best first. This is what ``tests/fixtures/results/arb_fixture_p09.json`` counts
    (Kalshi x Rothera pairs whose tie margin is negative, NO legs that dominate)."""
    info = me.info
    if len(info.outcomes) != 2:
        return []
    fee_for = lambda q: fee_model_for_quote(q, settings)  # noqa: E731
    a, b = info.outcomes
    out: list[dict[str, Any]] = []
    for qa in me.quotes_for_outcome(a):
        if qa.ask is None:
            continue
        for qb in me.quotes_for_outcome(b):
            if qb.ask is None or qb.book_id == qa.book_id:
                continue
            r = evaluate([Leg.from_quote(a, qa, fee_for(qa)), Leg.from_quote(b, qb, fee_for(qb))], contracts)
            out.append({
                "event_key": me.event_key,
                "legs": [{"venue": q.venue, "book": q.book_id, "outcome": q.outcome, "market_id": q.venue_market_id, "side": q.meta.get("side"), "ask": q.ask, "tie_payout": lr.tie_payout} for q, lr in ((qa, r.legs[0]), (qb, r.legs[1]))],
                "margin": r.margin, "tie_margin": r.tie_margin, "tie_payout_total": r.tie_payout_total, "is_arb": r.is_arb,
            })
    return sorted(out, key=lambda x: -x["margin"])


def _fetch(ad: Any, sport: str, emit_no_side: bool) -> VenueSnapshot:
    """Adapters that know about the NO side get asked for it; the others are called as before."""
    try:
        params = inspect.signature(ad.fetch).parameters
    except (TypeError, ValueError):
        params = {}
    if "emit_no_side" in params:
        return ad.fetch(sport, emit_no_side=emit_no_side)
    return ad.fetch(sport)


def attach_line_fair(reports: list[EventReport], selected: list[MergedEvent], settings: dict[str, Any]) -> None:
    """Opt-in (setting ``line_fair``): ask ``quant.lines.line_fair_for_event`` (a later item)
    for spread/total fair values, feeding it the moneyline consensus of the same game, and
    attach the result plus an ``ml-spread-gap`` flag when it reports one. A missing module is
    a no-op; a failing call is reported as ``line-fair-error``."""
    try:
        from .quant.lines import line_fair_for_event  # type: ignore[import-not-found]
    except ImportError:
        return
    ml_fair: dict[str, dict[str, Optional[float]]] = {}
    for r in reports:
        if r.market_type == "moneyline":
            ml_fair[r.event_key] = {o.outcome: o.fair for o in r.outcomes}
    for me, r in zip(selected, reports):
        if r.market_type not in ("spread", "total"):
            continue
        game_key = ":".join(r.event_key.split(":")[:3])
        try:
            res = line_fair_for_event(me.info, moneyline_p=ml_fair.get(game_key), spread_home=r.line if r.market_type == "spread" else None, total=r.line if r.market_type == "total" else None, state=None)
        except Exception:
            r.flags.append("line-fair-error")
            continue
        if not isinstance(res, dict):
            continue
        r.line_fair = res
        gap = res.get("ml_spread_gap") or res.get("ml-spread-gap")
        for f in res.get("flags") or []:
            if f not in r.flags:
                r.flags.append(str(f))
        if gap and "ml-spread-gap" not in r.flags:
            r.flags.append("ml-spread-gap")


def scan(sport: str, adapters: Iterable[Any], settings: Optional[dict[str, Any]] = None, contracts: float = 100, target_margin: float = 0.0, allowed_venues: Optional[set[str]] = None, only_cross_venue: bool = False, max_quote_age: float = 600.0, market_types: Optional[set[str]] = None, depth_for_candidates: bool = False, candidate_margin: float = -0.01, executable_venues: Optional[Iterable[str]] = None, emit_no_side: Optional[bool] = None, now: Optional[float] = None) -> ScanResult:
    """Two passes when ``depth_for_candidates``: top-of-book for everything, then real order
    books only for events whose margin is above ``candidate_margin`` (keeps Kalshi's
    rate limit happy: dozens of book requests instead of hundreds).

    ``executable_venues`` defaults to ``resolve_executable_venues(settings)``;
    ``emit_no_side`` to the ``rothera_no_leg`` setting (on)."""
    settings = settings or {}
    adapters = list(adapters)
    if emit_no_side is None:
        emit_no_side = _as_bool(setting(settings, "rothera_no_leg", True))
    exec_venues = resolve_executable_venues(settings, executable_venues)
    snapshots: list[VenueSnapshot] = []
    errors: dict[str, list[str]] = {}
    for ad in adapters:
        snap = _fetch(ad, sport, emit_no_side)
        snapshots.append(snap)
        if snap.errors:
            errors[snap.venue] = snap.errors
    merged = merge_snapshots(snapshots)
    reports: list[EventReport] = []
    now = time.time() if now is None else float(now)  # tests and fixture scripts pin the clock so pre-game stays pre-game
    selected: list[MergedEvent] = []
    for me in merged.values():
        if market_types and me.info.market_type not in market_types:
            continue
        if only_cross_venue and len(me.quotes_by_venue) < 2:
            continue
        selected.append(me)
        reports.append(analyze_event(me, settings, contracts=contracts, target_margin=target_margin, allowed_venues=allowed_venues, max_quote_age=max_quote_age, now=now, executable_venues=exec_venues))
    if depth_for_candidates:
        cand_keys = {r.event_key for r in reports if r.margin is not None and r.margin >= candidate_margin and not r.live}
        by_venue: dict[str, list[OutcomeQuote]] = {}
        for me in selected:
            if me.event_key in cand_keys:
                for v, qs in me.quotes_by_venue.items():
                    by_venue.setdefault(v, []).extend(qs)
        for ad in adapters:
            fn = getattr(ad, "attach_books_for", None)
            if fn and by_venue.get(ad.venue):
                errs: list[str] = []
                fn(by_venue[ad.venue], errs)
                if errs:
                    errors.setdefault(ad.venue, []).extend(errs[:5] + ([f"... {len(errs) - 5} more"] if len(errs) > 5 else []))
        reports = [analyze_event(me, settings, contracts=contracts, target_margin=target_margin, allowed_venues=allowed_venues, max_quote_age=max_quote_age, now=now, executable_venues=exec_venues) if me.event_key in cand_keys else r for me, r in zip(selected, reports)]
        for r in reports:
            if r.event_key in cand_keys:
                r.flags.append("depth-checked")
    if _as_bool(setting(settings, "line_fair", False)):
        attach_line_fair(reports, selected, settings)
    reports.sort(key=lambda r: (r.live, not r.fillable, -(r.margin if r.margin is not None else -9), r.start_time or ""))
    return ScanResult(sport=sport, fetched_at=now, venues=[s.venue for s in snapshots], events=reports, errors=errors)

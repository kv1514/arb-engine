"""Scan: pull every venue for a sport, merge by event, price fees, find arbs and edges."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .fees.registry import fee_model_for_quote
from .matching.matcher import MergedEvent, merge_snapshots
from .models import OutcomeQuote, VenueSnapshot
from .quant.arbitrage import ArbResult, Leg, best_leg_per_outcome, evaluate, max_price_for_leg, size_from_books
from .quant.fairvalue import consensus_fair_value


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


@dataclass
class ScanResult:
    sport: str
    fetched_at: float
    venues: list[str]
    events: list[EventReport]
    errors: dict[str, list[str]]

    def arbs(self, min_margin: float = 0.0, include_live: bool = False, include_thin: bool = False) -> list[EventReport]:
        return [e for e in self.events if e.margin is not None and e.margin > min_margin and (include_live or not e.live) and "stale-quote" not in e.flags and (include_thin or e.fillable)]


def _arb_to_dict(r: Optional[ArbResult]) -> Optional[dict]:
    return asdict(r) if r is not None else None


def _dedupe_same_book(quotes: list[OutcomeQuote]) -> tuple[list[OutcomeQuote], dict[str, str]]:
    """Keep one quote per underlying order book (the freshest; direct venue wins ties).
    Returns (tradable quotes, {market_id: venue it mirrors})."""
    by_book: dict[str, list[OutcomeQuote]] = {}
    for q in quotes:
        by_book.setdefault(q.book_id, []).append(q)
    keep: list[OutcomeQuote] = []
    mirrors: dict[str, str] = {}
    for book, qs in by_book.items():
        if len(qs) == 1:
            keep.append(qs[0])
            continue
        # The direct venue is where the leg would actually be placed (and is cheaper), so it
        # wins; mirrors are kept for the fee comparison only.
        qs_sorted = sorted(qs, key=lambda q: (0 if q.venue == book else 1, -(q.quote_time or 0)))
        keep.append(qs_sorted[0])
        for other in qs_sorted[1:]:
            mirrors[other.venue_market_id] = qs_sorted[0].venue
    return keep, mirrors


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


def analyze_event(me: MergedEvent, settings: dict[str, Any], contracts: float = 100, target_margin: float = 0.0, allowed_venues: Optional[set[str]] = None, max_quote_age: float = 600.0, now: Optional[float] = None, min_size: float = 1.0) -> EventReport:
    info = me.info
    now = now or time.time()
    fee_for = lambda q: fee_model_for_quote(q, settings)  # noqa: E731
    live = bool(info.in_play) or bool(info.start_time and info.start_time.timestamp() <= now)

    all_by_outcome: dict[str, list[OutcomeQuote]] = {o: me.quotes_for_outcome(o) for o in info.outcomes}
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
            fresh.append(q)
        tradable[o] = fresh

    legs = best_leg_per_outcome(tradable, fee_for, contracts=contracts, allowed_venues=allowed_venues)
    complete = len(legs) == len(info.outcomes)
    arb = evaluate(legs, contracts) if complete else None
    # Depth check: the largest size (book depth, else top-of-book size, else unlimited) that
    # still clears the target margin. A tail quote backed by 0.01 contracts is not an arb.
    sized = size_from_books(legs, min_margin=target_margin) if complete and arb and arb.is_arb else None
    fillable = sized is not None and sized.contracts >= min_size
    fair = consensus_fair_value(me.quotes_by_venue, info.outcomes, venue_weights=settings.get("venue_weights"))

    outcomes: list[OutcomeReport] = []
    flags: list[str] = []
    if live:
        flags.append("live")
    if info.tie_rule == "unknown" and info.sport in ("nfl", "ncaaf"):
        flags.append("tie-rule-unverified")
    flags.extend(settlement_mismatches(info, me.quotes_by_venue))
    for o in info.outcomes:
        others = [l for l in legs if l.outcome != o]
        hedgeable = complete and len(others) == len(info.outcomes) - 1
        vps: list[VenuePrice] = []
        best_venue, best_all_in = None, None
        for q in all_by_outcome[o]:
            if allowed_venues and q.venue not in allowed_venues:
                continue
            fm = fee_for(q)
            fee_pc = fm.per_contract(q.ask, contracts) if q.ask is not None else None
            all_in = (q.ask + fee_pc) if (q.ask is not None and fee_pc is not None) else None
            max_buy = max_price_for_leg(others, fm, contracts, target_margin) if hedgeable else None
            max_buy_maker = max_price_for_leg(others, fm, contracts, target_margin, role="maker") if hedgeable else None
            is_stale = q.venue_market_id in stale_ids
            vps.append(VenuePrice(venue=q.venue, market_id=q.venue_market_id, ask=q.ask, bid=q.bid, ask_size=q.ask_size, fee_per_contract=fee_pc, all_in=all_in, max_buy_price=max_buy, url=q.url, exchange=q.meta.get("exchange") or q.fee_params.get("exchange"), max_buy_maker=max_buy_maker, age_s=q.age, mirror_of=mirrors.get(q.venue_market_id), stale=is_stale))
            if all_in is not None and not is_stale and (best_all_in is None or all_in < best_all_in):
                best_venue, best_all_in = q.venue, all_in
        fv = fair.get(o)
        edge = (fv.fair - best_all_in) if (fv and fv.fair is not None and best_all_in is not None) else None
        outcomes.append(OutcomeReport(outcome=o, label=info.labels.get(o, o), fair=fv.fair if fv else None, venues=sorted(vps, key=lambda v: (v.all_in is None, v.all_in or 9)), best_buy_venue=best_venue, best_buy_all_in=best_all_in, edge_at_best=edge))
    if stale_ids:
        flags.append("stale-quote")
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
        fillable=fillable,
    )


def scan(sport: str, adapters: Iterable[Any], settings: Optional[dict[str, Any]] = None, contracts: float = 100, target_margin: float = 0.0, allowed_venues: Optional[set[str]] = None, only_cross_venue: bool = False, max_quote_age: float = 600.0, market_types: Optional[set[str]] = None, depth_for_candidates: bool = False, candidate_margin: float = -0.01) -> ScanResult:
    """Two passes when ``depth_for_candidates``: top-of-book for everything, then real order
    books only for events whose margin is above ``candidate_margin`` (keeps Kalshi's
    rate limit happy: dozens of book requests instead of hundreds)."""
    settings = settings or {}
    adapters = list(adapters)
    snapshots: list[VenueSnapshot] = []
    errors: dict[str, list[str]] = {}
    for ad in adapters:
        snap = ad.fetch(sport)
        snapshots.append(snap)
        if snap.errors:
            errors[snap.venue] = snap.errors
    merged = merge_snapshots(snapshots)
    reports: list[EventReport] = []
    now = time.time()
    selected: list[MergedEvent] = []
    for me in merged.values():
        if market_types and me.info.market_type not in market_types:
            continue
        if only_cross_venue and len(me.quotes_by_venue) < 2:
            continue
        selected.append(me)
        reports.append(analyze_event(me, settings, contracts=contracts, target_margin=target_margin, allowed_venues=allowed_venues, max_quote_age=max_quote_age, now=now))
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
        reports = [analyze_event(me, settings, contracts=contracts, target_margin=target_margin, allowed_venues=allowed_venues, max_quote_age=max_quote_age, now=now) if me.event_key in cand_keys else r for me, r in zip(selected, reports)]
        for r in reports:
            if r.event_key in cand_keys:
                r.flags.append("depth-checked")
    reports.sort(key=lambda r: (r.live, not r.fillable, -(r.margin if r.margin is not None else -9), r.start_time or ""))
    return ScanResult(sport=sport, fetched_at=now, venues=[s.venue for s in snapshots], events=reports, errors=errors)

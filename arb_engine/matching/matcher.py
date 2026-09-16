"""Merge per-venue snapshots into cross-venue events.

Event keys are deterministic (sport + canonical participants + Eastern date). Tennis
keys tolerate a one-day date drift between venues (Kalshi encodes the *scheduled* date in
the ticker while Polymarket/Robinhood use the UTC start time) by also indexing on the
participant pair and picking the closest date.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..models import EventInfo, OutcomeQuote, VenueSnapshot


@dataclass
class MergedEvent:
    event_key: str
    info: EventInfo
    quotes_by_venue: dict[str, list[OutcomeQuote]] = field(default_factory=dict)

    @property
    def venues(self) -> list[str]:
        return sorted(self.quotes_by_venue)

    def quotes_for_outcome(self, outcome: str) -> list[OutcomeQuote]:
        return [q for qs in self.quotes_by_venue.values() for q in qs if q.outcome == outcome]


def _split_key(key: str) -> tuple[str, str, str, str]:
    """'nfl:BUF|DET:2026-09-17:spread:BUF-1.5' -> (sport, participants, date, suffix)."""
    parts = key.split(":", 3)
    sport, participants, date = parts[0], parts[1], parts[2] if len(parts) > 2 else ""
    suffix = parts[3] if len(parts) > 3 else ""
    return sport, participants, date, suffix


def _days_apart(a: str, b: str) -> int:
    try:
        da = datetime.strptime(a, "%Y-%m-%d")
        db = datetime.strptime(b, "%Y-%m-%d")
    except ValueError:
        return 99
    return abs((da - db).days)


def merge_snapshots(snapshots: list[VenueSnapshot], date_tolerance_days: int = 1) -> dict[str, MergedEvent]:
    merged: dict[str, MergedEvent] = {}
    by_participants: dict[tuple[str, str], list[str]] = {}

    def register(key: str, info: EventInfo) -> str:
        sport, parts, date, suffix = _split_key(key)
        if key in merged:
            return key
        # Look for an existing event with the same participants (and market/line) within the
        # date tolerance.
        for existing in by_participants.get((sport, parts, suffix), []):
            _, _, edate, _ = _split_key(existing)
            if _days_apart(date, edate) <= date_tolerance_days:
                return existing
        merged[key] = MergedEvent(event_key=key, info=info)
        by_participants.setdefault((sport, parts, suffix), []).append(key)
        return key

    for snap in snapshots:
        for key, info in snap.events.items():
            canonical = register(key, info)
            me = merged[canonical]
            # Fill in missing metadata from later venues.
            if info.start_time is not None and (me.info.start_time is None or info.start_time < me.info.start_time):
                me.info.start_time = info.start_time  # earliest scheduled time across venues
            if info.in_play:
                me.info.in_play = True
            elif me.info.in_play is None and info.in_play is not None:
                me.info.in_play = info.in_play
            for o, label in info.labels.items():
                me.info.labels.setdefault(o, label)
            me.info.venues.update(info.venues)
            if me.info.tie_rule == "unknown" and info.tie_rule != "unknown":
                me.info.tie_rule = info.tie_rule
        for q in snap.quotes:
            sport, parts, date, suffix = _split_key(q.event_key)
            canonical = q.event_key if q.event_key in merged else None
            if canonical is None:
                for existing in by_participants.get((sport, parts, suffix), []):
                    _, _, edate, _ = _split_key(existing)
                    if _days_apart(date, edate) <= date_tolerance_days:
                        canonical = existing
                        break
            if canonical is None:
                continue
            q.event_key = canonical
            merged[canonical].quotes_by_venue.setdefault(q.venue, []).append(q)
    return merged

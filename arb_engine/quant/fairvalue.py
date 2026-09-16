"""Consensus fair value for an outcome from several venues' quotes.

1. Each venue's two-way (or N-way) mids are de-vigged multiplicatively so its own
   probabilities sum to 1 (removes the half-spread skew).
2. Venues are combined with weights = ``venue_weight / max(spread, min_spread)`` so tight,
   liquid books dominate; user-configurable per venue.
3. Optional sportsbook probabilities (already de-vigged) enter with their own weight.

The result is a probability, i.e. the fair *price* of the $1-payout contract. Edge for a
buy is ``fair - all_in_cost``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from ..models import OutcomeQuote

DEFAULT_VENUE_WEIGHTS = {
    "polymarket": 1.0,
    "kalshi": 1.0,
    "robinhood": 0.7,  # Rothera books are younger/thinner; Kalshi-routed RH quotes are Kalshi's book anyway
    "polymarket_us": 0.8,
    "sportsbook": 1.5,
}


@dataclass
class FairValue:
    outcome: str
    fair: Optional[float]
    by_venue: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    n_sources: int = 0


def _devig_venue(mids: Mapping[str, float]) -> dict[str, float]:
    s = sum(v for v in mids.values() if v is not None)
    if s <= 0:
        return {}
    return {k: v / s for k, v in mids.items() if v is not None}


def consensus_fair_value(
    quotes_by_venue: Mapping[str, Sequence[OutcomeQuote]],
    outcomes: Sequence[str],
    venue_weights: Optional[Mapping[str, float]] = None,
    sportsbook_probs: Optional[Mapping[str, float]] = None,
    min_spread: float = 0.01,
) -> dict[str, FairValue]:
    """``quotes_by_venue``: venue -> quotes for this event (one per outcome)."""
    weights = dict(DEFAULT_VENUE_WEIGHTS)
    if venue_weights:
        weights.update(venue_weights)
    per_outcome: dict[str, dict[str, float]] = {o: {} for o in outcomes}
    w_used: dict[str, dict[str, float]] = {o: {} for o in outcomes}

    for venue, quotes in quotes_by_venue.items():
        mids = {q.outcome: q.mid for q in quotes if q.mid is not None and q.outcome in per_outcome}
        if len(mids) < len(outcomes):
            # Two-way market with only one side quoted: infer the other side from 1 - mid.
            if len(outcomes) == 2 and len(mids) == 1 and len(set(outcomes)) == 2:
                (o1, m1), = mids.items()
                o2 = [o for o in outcomes if o != o1][0]
                mids[o2] = 1.0 - m1
            else:
                continue
        probs = _devig_venue(mids)
        spreads = {q.outcome: (q.spread if q.spread is not None else 0.05) for q in quotes}
        for o, p in probs.items():
            spread = max(spreads.get(o, 0.05), min_spread)
            w = weights.get(venue, 0.5) / spread
            per_outcome[o][venue] = p
            w_used[o][venue] = w

    if sportsbook_probs:
        for o, p in sportsbook_probs.items():
            if o in per_outcome:
                per_outcome[o]["sportsbook"] = float(p)
                w_used[o]["sportsbook"] = weights.get("sportsbook", 1.5) / min_spread

    out: dict[str, FairValue] = {}
    raw: dict[str, Optional[float]] = {}
    for o in outcomes:
        vals = per_outcome[o]
        ws = w_used[o]
        if not vals:
            raw[o] = None
            continue
        tw = sum(ws.values())
        raw[o] = sum(vals[v] * ws[v] for v in vals) / tw if tw else None
    # Final renormalisation so the outcomes sum to 1.
    known = {o: v for o, v in raw.items() if v is not None}
    total = sum(known.values())
    for o in outcomes:
        fair = None
        if raw[o] is not None and total > 0 and len(known) == len(outcomes):
            fair = raw[o] / total
        elif raw[o] is not None:
            fair = raw[o]
        out[o] = FairValue(outcome=o, fair=fair, by_venue=dict(per_outcome[o]), weights=dict(w_used[o]), n_sources=len(per_outcome[o]))
    return out

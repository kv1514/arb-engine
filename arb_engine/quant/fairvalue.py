"""Consensus fair value for an outcome from several venues' quotes.

1. Each venue's two-way (or N-way) mids are de-vigged multiplicatively so its own
   probabilities sum to 1 (removes the half-spread skew).
2. Venues are combined with weights = ``venue_weight / max(spread, min_spread)`` so tight,
   liquid books dominate; user-configurable per venue.
3. Optional sportsbook probabilities (already de-vigged) enter with their own weight. A
   plain float per outcome keeps the historical weight ``sportsbook / min_spread``; a
   ``{"fair", "fair_min", "fair_max"}`` mapping (``SportsbookProbs.as_mapping``) carries the
   de-vig disagreement range, which is treated like a bid/ask spread:
   ``weight = sportsbook / max(min_spread, fair_max - fair_min)``.
4. Optional ``line_prior`` — the margin-model fair from ``quant.lines`` — enters with the
   ``line_prior`` weight only when supplied (opt-in until the replay table shows it beats the
   exchange mid in play; see ``scripts/eval_lines.py``).

The result is a probability, i.e. the fair *price* of the $1-payout contract. Edge for a
buy is ``fair - all_in_cost``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from ..models import OutcomeQuote

DEFAULT_VENUE_WEIGHTS = {
    "polymarket": 1.0,
    "kalshi": 1.0,
    "robinhood": 0.7,  # Rothera books are younger/thinner; Kalshi-routed RH quotes are Kalshi's book anyway
    "polymarket_us": 0.8,
    "sportsbook": 1.5,
    "line_prior": 0.8,  # margin-model fair for spreads/totals; opt-in via consensus_fair_value(line_prior=...)
}


@dataclass
class FairValue:
    outcome: str
    fair: Optional[float]
    by_venue: dict[str, float] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    n_sources: int = 0
    # (fair_min, fair_max) of the sportsbook de-vig across methods when a range was supplied.
    sportsbook_range: Optional[tuple[float, float]] = None


def _devig_venue(mids: Mapping[str, float]) -> dict[str, float]:
    s = sum(v for v in mids.values() if v is not None)
    if s <= 0:
        return {}
    return {k: v / s for k, v in mids.items() if v is not None}


def _sportsbook_entry(v: Any) -> tuple[Optional[float], Optional[tuple[float, float]]]:
    """A sportsbook value is a float, or a mapping with ``fair`` and optional ``fair_min``/``fair_max``."""
    if isinstance(v, Mapping):
        fair = v.get("fair", v.get("p"))
        if fair is None:
            return None, None
        lo, hi = v.get("fair_min"), v.get("fair_max")
        rng = (float(lo), float(hi)) if lo is not None and hi is not None else None
        return float(fair), rng
    return (float(v), None) if v is not None else (None, None)


def consensus_fair_value(
    quotes_by_venue: Mapping[str, Sequence[OutcomeQuote]],
    outcomes: Sequence[str],
    venue_weights: Optional[Mapping[str, float]] = None,
    sportsbook_probs: Optional[Mapping[str, Any]] = None,
    min_spread: float = 0.01,
    line_prior: Optional[Mapping[str, Optional[float]]] = None,
) -> dict[str, FairValue]:
    """``quotes_by_venue``: venue -> quotes for this event (one per outcome).

    ``sportsbook_probs`` / ``line_prior`` map outcome -> probability (see the module doc for
    the range form); both are optional and change nothing when absent."""
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

    sb_range: dict[str, tuple[float, float]] = {}
    if sportsbook_probs:
        for o, v in sportsbook_probs.items():
            p, rng = _sportsbook_entry(v)
            if o in per_outcome and p is not None:
                per_outcome[o]["sportsbook"] = p
                spread = max(min_spread, rng[1] - rng[0]) if rng else min_spread
                w_used[o]["sportsbook"] = weights.get("sportsbook", 1.5) / spread
                if rng:
                    sb_range[o] = rng
    if line_prior:
        for o, p in line_prior.items():
            if o in per_outcome and p is not None:
                per_outcome[o]["line_prior"] = float(p)
                w_used[o]["line_prior"] = weights.get("line_prior", 0.8) / min_spread

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
        out[o] = FairValue(outcome=o, fair=fair, by_venue=dict(per_outcome[o]), weights=dict(w_used[o]), n_sources=len(per_outcome[o]), sportsbook_range=sb_range.get(o))
    return out

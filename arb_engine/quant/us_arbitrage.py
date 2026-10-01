"""Read-only, conservative NFL cross-book price opportunities.

This is NOT an executor or proof that two orders can fill atomically. Costs use
Decimal, real displayed top-of-book liquidity and a per-leg cash cap. Unknown or
different settlement terms produce *conditional* candidates, never a verified
payoff. Polymarket US is explicitly included here, not in existing live/maker
execution paths; global Polymarket remains subject to the eligibility table.
"""

from __future__ import annotations

import math
import time
from copy import deepcopy
from decimal import Decimal
from itertools import product
from typing import Any, Mapping, Optional

from ..compliance import executable_venues
from ..fees.base import D
from ..fees.registry import fee_model_for_quote
from ..matching.matcher import merge_snapshots
from ..matching.settlement_rules import pair_flags, rule_for_quote
from ..models import OutcomeQuote, VenueSnapshot

US_VENUES = frozenset({"kalshi", "robinhood", "polymarket_us"})


def _finite(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool):
        return None
    try:
        number = D(value)
    except (TypeError, ValueError, ArithmeticError):
        return None
    return number if number.is_finite() else None


def _quote_ok(q: OutcomeQuote, now: float, max_age_s: float) -> bool:
    """No metadata marks, missing depth, crossed/stale/future or failed reads."""
    ask, bid, size, observed = map(_finite, (q.ask, q.bid, q.ask_size, q.ts))
    if any(v is None for v in (ask, bid, size, observed)):
        return False
    if not (0 < bid <= ask < 1 and size > 0 and 0 <= D(now) - observed <= D(max_age_s)):
        return False
    meta = q.meta or {}
    refreshed = meta.get("refreshed")
    if meta.get("arb_ineligible") or not (refreshed is True or type(refreshed) is int and refreshed == 1):
        return False
    receipt, request = _finite(meta.get("obs_ts")), _finite(meta.get("req_ts"))
    if receipt is None or receipt != observed or request is None or request > observed or meta.get("approx_time"):
        return False
    quote_time = _finite(q.quote_time)
    if q.quote_time is not None and (quote_time is None or not 0 <= observed - quote_time <= 10):
        return False
    return bool(q.book_id and q.venue_market_id)


def _cost(q: OutcomeQuote, settings: Mapping[str, Any], n: int) -> tuple[Decimal, Decimal, Decimal]:
    model = fee_model_for_quote(q, settings)
    price = D(q.ask)
    fee = model.fee(price, n, "taker")
    # Kalshi/RH may round/minimum-charge each fill. A one-contract charge is a
    # conservative bound for this whole-contract, single-price ticket. PM US's
    # documented cumulative adjustment caps an order at its rounded exact fee.
    bound = fee if q.venue == "polymarket_us" else max(fee, n * model.fee(price, 1, "taker"))
    if not fee.is_finite() or not bound.is_finite() or fee < 0 or bound < fee:
        raise ValueError("invalid taker fee")
    return fee, bound, price * n + bound


def _settlement_gates(a: OutcomeQuote, b: OutcomeQuote) -> tuple[list[str], Optional[Decimal]]:
    gates = list(pair_flags(a, b, "nfl", "moneyline"))
    ties: list[Decimal] = []
    for q in (a, b):
        rule = rule_for_quote(q, "nfl", "moneyline")
        if rule is None:
            gates.append(f"settlement-unverified:{q.venue}")
        else:
            if rule.get("status") != "verbatim":
                gates.append(f"settlement-unverified:{q.venue}")
            for name in ("tie", "cancelled", "postponed", "ot_included"):
                if rule.get(name) is None:
                    gates.append(f"settlement-unknown:{q.venue}:{name}")
            if rule.get("cancelled") in ("fair_price", "vwap_1w"):
                gates.append(f"discretionary-settlement:{q.venue}")
        # Never use quant.arbitrage's compatibility default of half for unknown.
        payout = _finite(q.meta.get("tie_payout"))
        if payout is None and rule and rule.get("status") == "verbatim" and rule.get("tie") == "half":
            payout = Decimal("0.5")
        elif payout is not None and rule and rule.get("tie") == "half" and payout != Decimal("0.5"):
            gates.append(f"tie-rule-contradiction:{q.venue}")
        if payout is None or not 0 <= payout <= 1:
            gates.append(f"tie-payout-unknown:{q.venue}")
        else:
            ties.append(payout)
    return sorted(set(gates)), sum(ties, Decimal(0)) if len(ties) == 2 else None


def _quotes(quotes: list[OutcomeQuote], now: float, max_age_s: float) -> list[OutcomeQuote]:
    # Select the latest causal receipt BEFORE validating liquidity. A failed
    # newer response invalidates the older book; equal-time conflicting rows
    # cannot be resolved by choosing the cheaper price or the larger size.
    grouped = {}
    for q in quotes:
        at = _finite(q.ts)
        if at is None or at < 0 or at > D(now):
            continue
        key = (q.venue, q.book_id, q.venue_market_id, str(q.meta.get("side", "")), q.outcome)
        grouped.setdefault(key, []).append(q)
    selected = []
    for rows in grouped.values():
        latest = max(D(q.ts) for q in rows)
        current = [q for q in rows if D(q.ts) == latest]
        if all(q == current[0] for q in current) and _quote_ok(current[0], now, max_age_s):
            selected.append(current[0])
    return sorted(selected, key=lambda q: (q.venue, q.book_id, q.venue_market_id, str(q.meta.get("side", "")), q.outcome))


def find_candidates(snapshots: list[VenueSnapshot], settings: Optional[Mapping[str, Any]] = None, *,
                    now: Optional[float] = None, contracts: int = 100, side_cap: float = 25,
                    max_age_s: float = 6, min_margin: float = 0) -> dict[str, Any]:
    """Find fee-positive, displayed-size price pairs without orders or alerts.

    Strict same Eastern-date keys (no one-day merge tolerance), pre-game full NFL
    moneylines only. ``verified-payoff`` requires known, compatible registry terms;
    it still does not claim an atomic fill or demonstrated realised profit.
    ``conditional`` means positive win-case math but missing/different rules or a
    losing tie case. All candidates use whole contracts and at most $25 per leg
    by default, INCLUDING a conservative fee bound. No rebate is assumed here,
    even if a volume-rebate setting is used elsewhere (rebates are paid later).
    """
    now = time.time() if now is None else float(now)
    if not math.isfinite(now):
        raise ValueError("now must be finite")
    if isinstance(contracts, bool) or int(contracts) != contracts or not 1 <= contracts <= 10000:
        raise ValueError("contracts must be an integer in [1, 10000]")
    cap, age, margin_floor = map(_finite, (side_cap, max_age_s, min_margin))
    if cap is None or cap <= 0 or age is None or age <= 0 or margin_floor is None or margin_floor < 0:
        raise ValueError("caps/age must be finite positive; margin must be finite nonnegative")
    settings = dict(settings or {})
    settings["polymarket_us_volume_rebate"] = 0
    eligible = executable_venues(settings, with_adapter_only=False) & US_VENUES
    errors = {s.venue: list(s.errors) for s in snapshots if s.errors}
    scoped = deepcopy(snapshots)
    identities = {}
    for snap in scoped:
        for key, info in snap.events.items():
            identities.setdefault(key, set()).add((info.sport, info.market_type, tuple(sorted(info.outcomes)),
                                                  info.start_time, bool(info.in_play)))
        valid = [q for q in snap.quotes if q.venue == snap.venue and q.event_key in snap.events and
                 q.outcome in snap.events[q.event_key].outcomes]
        if len(valid) != len(snap.quotes):
            errors.setdefault(snap.venue, []).append("wrongly scoped quote rows excluded")
        snap.quotes = valid
    # The general merger fills in EventInfo and normalises quote keys in place.
    merged = merge_snapshots(scoped, date_tolerance_days=0)
    candidates: list[dict[str, Any]] = []
    for me in merged.values():
        info = me.info
        if len(identities.get(me.event_key, set())) != 1:
            errors.setdefault('identity', []).append(f'{me.event_key}: conflicting venue game identity or kickoff')
            continue
        if info.sport != "nfl" or info.market_type != "moneyline" or len(set(info.outcomes)) != 2 or len(info.outcomes) != 2:
            continue
        if info.in_play or info.start_time is None or info.start_time.timestamp() <= now:
            continue
        by_outcome = [_quotes([q for q in me.quotes_for_outcome(o) if q.venue in eligible], now, float(age)) for o in info.outcomes]
        for a, b in product(*by_outcome):
            if a.book_id == b.book_id:
                continue
            minimums = [_finite(q.meta.get("min_size", 1)) for q in (a, b)]
            if any(v is None or v <= 0 for v in minimums):
                continue
            minimum = math.ceil(max(minimums))
            maximum = min(int(contracts), math.floor(min(D(a.ask_size), D(b.ask_size))),
                          math.floor(cap / max(D(a.ask), D(b.ask))))
            selected = None
            try:
                for n in range(maximum, minimum - 1, -1):
                    costs = [_cost(q, settings, n) for q in (a, b)]
                    total = sum((c[2] for c in costs), Decimal(0))
                    if all(c[2] <= cap for c in costs) and (D(n) - total) / n > margin_floor:
                        selected = n, costs, total
                        break
            except (TypeError, ValueError, ArithmeticError) as exc:
                errors.setdefault("calculation", []).append(f"{me.event_key}: {exc}")
            if selected is None:
                continue
            n, costs, total = selected
            gates, tie_payout = _settlement_gates(a, b)
            gates.extend(f"fee-unverified:{q.venue}" for q in (a, b) if q.fee_params.get("fee_multiplier_assumed"))
            tie_profit = tie_payout * n - total if tie_payout is not None else None
            if tie_profit is not None and tie_profit <= 0:
                gates.append("loses-or-breaks-even-on-tie")
            candidates.append({
                "event_key": me.event_key, "classification": "conditional" if gates else "verified-payoff",
                "contracts": n, "total_cost": float(total), "profit": float(D(n) - total),
                "margin": float((D(n) - total) / n), "tie_profit": float(tie_profit) if tie_profit is not None else None,
                "gates": sorted(set(gates)), "execution": "read-only; fills are not atomic",
                "legs": [{"venue": q.venue, "book_id": q.book_id, "market_id": q.venue_market_id,
                          "outcome": q.outcome, "side": q.meta.get("side"), "price": q.ask,
                          "fee": float(c[0]), "fee_bound": float(c[1]), "cost": float(c[2]),
                          "observed_at": q.ts, "displayed_size": q.ask_size, "url": q.url}
                         for q, c in zip((a, b), costs)],
            })
    candidates.sort(key=lambda c: (-c["profit"], c["event_key"], repr(c["legs"])))
    return {"candidates": candidates, "errors": errors, "fetched_at": now,
            "venues": sorted({s.venue for s in snapshots}), "events": len(merged)}

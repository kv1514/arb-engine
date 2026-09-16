"""Position sizing.

For a $1-payout contract bought at an all-in cost ``k`` (price + fee per contract) with
true win probability ``q``, net odds are ``b = (1 - k) / k`` and the Kelly fraction is

    f* = (q * b - (1 - q)) / b = (q - k) / (1 - k)

i.e. the edge divided by the counterparty's share. Pure arbitrage has no variance, so
Kelly does not apply — size arbs by available depth (see ``quant.arbitrage.size_from_books``).
"""

from __future__ import annotations

from typing import Sequence


def kelly_fraction(fair_prob: float, all_in_cost: float, fraction: float = 1.0) -> float:
    q = float(fair_prob)
    k = float(all_in_cost)
    if not (0 < k < 1) or not (0 <= q <= 1):
        return 0.0
    f = (q - k) / (1.0 - k)
    return max(0.0, f * float(fraction))


def kelly_stake(bankroll: float, fair_prob: float, all_in_cost: float, fraction: float = 0.25) -> dict:
    f = kelly_fraction(fair_prob, all_in_cost, fraction)
    stake = float(bankroll) * f
    contracts = int(stake // float(all_in_cost)) if all_in_cost > 0 else 0
    return {
        "fraction": f,
        "stake": stake,
        "contracts": contracts,
        "edge": float(fair_prob) - float(all_in_cost),
        "ev_per_contract": float(fair_prob) - float(all_in_cost),
    }


def equal_payout_stakes(decimal_odds: Sequence[float], total_stake: float) -> list[float]:
    """Split ``total_stake`` across legs with decimal odds so every outcome pays the same."""
    inv = [1.0 / float(d) for d in decimal_odds]
    s = sum(inv)
    return [float(total_stake) * x / s for x in inv]

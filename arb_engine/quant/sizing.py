"""Position sizing.

For a $1-payout contract bought at an all-in cost ``k`` (price + fee per contract) with
true win probability ``q``, net odds are ``b = (1 - k) / k`` and the Kelly fraction is

    f* = (q * b - (1 - q)) / b = (q - k) / (1 - k)

i.e. the edge divided by the counterparty's share. Pure arbitrage has no variance, so
Kelly does not apply — size arbs by available depth (see ``quant.arbitrage.size_from_books``).

``hedge_kelly`` is the other direction: you already hold one side and ask how much of the
*other* side to buy. Kelly on the two-state wealth (A wins / B wins) has a closed form,
derived in the function's docstring.
"""

from __future__ import annotations

from typing import Optional, Sequence


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


def hedge_kelly(bankroll: float, held: float, avg_cost: float, hedge_all_in: float, fair_p: float, fraction: float = 1.0) -> dict:
    """How many contracts of the *other* side to buy against ``held`` contracts of side A.

    Wealth before the hedge is ``X = bankroll - held*avg_cost + held`` if A wins and
    ``Y = bankroll - held*avg_cost`` if B wins (``bankroll`` includes the cash already spent).
    Buying ``n`` of B at all-in ``h`` gives ``X - n*h`` / ``Y + n*(1 - h)``. Maximising
    ``p*log(X - n h) + (1 - p)*log(Y + n (1 - h))`` in ``n`` gives

        n* = [(1 - p)(1 - h) X - p h Y] / (h (1 - h))

    clamped to ``[0, held]``: more than ``held`` would be a directional bet on B (size that
    with ``kelly_stake``), and a negative n* says the hedge is dearer than its fair value, so
    hold. At ``p + h = 1`` (hedge priced exactly at fair) n* = held: Kelly removes all
    variance it can buy at no cost, and any cheaper hedge is a lock, so it also fills.

    A hedge at ``h >= 1 - avg_cost`` cannot lock anything (both outcomes then cost more than
    $1), so it is reported as 0 contracts — that is not a hedge, it is a new position, and
    the STEAL sizing decides about those. ``fraction`` scales n* before the clamp (fractional
    Kelly, same convention as ``kelly_stake``).
    """
    b, n_held, c, h, p = float(bankroll), float(held), float(avg_cost), float(hedge_all_in), float(fair_p)
    out = {"contracts": 0, "raw": 0.0, "fraction_of_held": 0.0, "locks": False, "wealth_if_a": None, "wealth_if_b": None}
    if n_held <= 0 or not (0 < h < 1) or not (0 <= p <= 1) or not (0 < c < 1):
        return out
    out["locks"] = h < 1.0 - c
    if not out["locks"]:
        return out
    y = b - n_held * c
    x = y + n_held
    if x <= 0 or y + n_held * (1 - h) <= 0:
        return out  # bankroll does not cover the position; nothing sensible to size
    raw = ((1 - p) * (1 - h) * x - p * h * y) / (h * (1 - h))
    n = min(max(raw * float(fraction), 0.0), n_held)
    out.update({"raw": raw, "contracts": int(n + 1e-9), "fraction_of_held": n / n_held, "wealth_if_a": x - n * h, "wealth_if_b": y + n * (1 - h)})
    return out


def equal_payout_stakes(decimal_odds: Sequence[float], total_stake: float) -> list[float]:
    """Split ``total_stake`` across legs with decimal odds so every outcome pays the same."""
    inv = [1.0 / float(d) for d in decimal_odds]
    s = sum(inv)
    return [float(total_stake) * x / s for x in inv]

"""Odds formats and de-vigging.

A binary prediction-market contract that pays $1 is priced at its implied probability, so
``price == implied probability`` and ``decimal odds == 1 / price``. Sportsbook lines carry
an overround (vig) that has to be removed before their probabilities are comparable.
"""

from __future__ import annotations

from math import log, exp
from typing import Iterable, Sequence


def american_to_decimal(american: float) -> float:
    a = float(american)
    if a == 0:
        raise ValueError("american odds cannot be 0")
    return 1.0 + (a / 100.0 if a > 0 else 100.0 / -a)


def decimal_to_american(decimal_odds: float) -> float:
    d = float(decimal_odds)
    if d <= 1.0:
        raise ValueError("decimal odds must exceed 1.0")
    return round((d - 1.0) * 100.0, 2) if d >= 2.0 else round(-100.0 / (d - 1.0), 2)


def implied_from_decimal(decimal_odds: float) -> float:
    return 1.0 / float(decimal_odds)


def decimal_from_prob(prob: float) -> float:
    if not 0 < prob < 1:
        raise ValueError("probability must be in (0, 1)")
    return 1.0 / prob


def overround(implied: Iterable[float]) -> float:
    """Sum of implied probabilities minus 1 (0 = fair, 0.045 = 4.5% vig)."""
    return sum(float(p) for p in implied) - 1.0


def devig_multiplicative(implied: Sequence[float]) -> list[float]:
    """Normalise so probabilities sum to 1 (assumes vig is spread proportionally)."""
    s = sum(implied)
    if s <= 0:
        raise ValueError("implied probabilities must sum to a positive number")
    return [float(p) / s for p in implied]


def devig_power(implied: Sequence[float], tol: float = 1e-12, max_iter: int = 200) -> list[float]:
    """Power method: find k with sum(p_i ** k) == 1. Better than multiplicative for
    favourite-longshot bias (longshots carry proportionally more vig)."""
    ps = [float(p) for p in implied]
    if any(p <= 0 or p >= 1 for p in ps):
        return devig_multiplicative(ps)
    lo, hi = 0.5, 5.0
    f = lambda k: sum(p ** k for p in ps) - 1.0  # noqa: E731
    if f(lo) < 0:  # already under-round; scale up instead
        return devig_multiplicative(ps)
    while f(hi) > 0 and hi < 1e3:
        hi *= 2
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    k = (lo + hi) / 2
    out = [p ** k for p in ps]
    s = sum(out)
    return [p / s for p in out]


def devig_shin(implied: Sequence[float], tol: float = 1e-12, max_iter: int = 200) -> list[float]:
    """Shin (1993) method: models a fraction z of insider bettors. For n outcomes,
    p_i = (sqrt(z^2 + 4 (1 - z) q_i^2 / B) - z) / (2 (1 - z)) where B = sum(q)."""
    qs = [float(p) for p in implied]
    n = len(qs)
    B = sum(qs)
    if n < 2 or B <= 1.0:
        return devig_multiplicative(qs)
    lo, hi = 0.0, 0.5

    def total(z: float) -> float:
        return sum((((z * z + 4.0 * (1.0 - z) * q * q / B) ** 0.5) - z) / (2.0 * (1.0 - z)) for q in qs)

    # total(z) is decreasing in z; bisect for total(z) == 1.
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        if total(mid) > 1.0:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    z = (lo + hi) / 2
    out = [(((z * z + 4.0 * (1.0 - z) * q * q / B) ** 0.5) - z) / (2.0 * (1.0 - z)) for q in qs]
    s = sum(out)
    return [p / s for p in out]


__all__ = [
    "american_to_decimal", "decimal_to_american", "implied_from_decimal", "decimal_from_prob",
    "overround", "devig_multiplicative", "devig_power", "devig_shin", "log", "exp",
]

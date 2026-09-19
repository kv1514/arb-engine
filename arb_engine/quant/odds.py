"""Odds formats and de-vigging.

A binary prediction-market contract that pays $1 is priced at its implied probability, so
``price == implied probability`` and ``decimal odds == 1 / price``. Sportsbook lines carry
an overround (vig) that has to be removed before their probabilities are comparable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import log, exp
from typing import Any, Iterable, Optional, Sequence


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


def devig_additive(implied: Sequence[float]) -> list[float]:
    """Subtract the overround equally from every outcome. Simple and common in the
    literature, but it can push a long shot below zero; such cells are floored at a hair
    above 0 and the vector renormalised (the caller sees the disagreement via ``devig_range``)."""
    ps = [float(p) for p in implied]
    n = len(ps)
    if n == 0:
        raise ValueError("implied probabilities required")
    adj = (sum(ps) - 1.0) / n
    out = [max(1e-9, p - adj) for p in ps]
    s = sum(out)
    return [p / s for p in out]


def shin_z_two_way(q1: float, q2: float) -> Optional[float]:
    """Closed-form Shin insider share for two outcomes: with ``B = q1 + q2`` and ``d = q1 - q2``,
    ``z = (B - 1)(B - d^2) / (B (1 - d^2))``. Derived by eliminating the square roots in
    ``sum_i p_i(z) = 1``; ``devig_shin`` bisects the same equation for any n and the two agree
    to 1e-9 (``tests/test_odds.py``). None when there is no overround or |d| >= 1."""
    B, d = float(q1) + float(q2), float(q1) - float(q2)
    if B <= 1.0 or abs(d) >= 1.0:
        return None
    z = (B - 1.0) * (B - d * d) / (B * (1.0 - d * d))
    return z if 0.0 <= z < 1.0 else None


def devig_shin_closed(implied: Sequence[float]) -> list[float]:
    """Shin de-vig using the closed-form ``z`` for n == 2, bisection otherwise."""
    qs = [float(p) for p in implied]
    if len(qs) != 2:
        return devig_shin(qs)
    z = shin_z_two_way(qs[0], qs[1])
    if z is None:
        return devig_shin(qs)
    B = sum(qs)
    out = [(((z * z + 4.0 * (1.0 - z) * q * q / B) ** 0.5) - z) / (2.0 * (1.0 - z)) for q in qs]
    s = sum(out)
    return [p / s for p in out]


DEVIG_METHODS = {
    "multiplicative": devig_multiplicative,
    "power": devig_power,
    "additive": devig_additive,
    "shin": devig_shin_closed,
}


def devig(implied: Sequence[float], method: str = "power") -> list[float]:
    """De-vig by name: multiplicative | power | additive | shin | auto."""
    if method == "auto":
        return devig_auto(implied)
    try:
        fn = DEVIG_METHODS[method]
    except KeyError:
        raise ValueError(f"unknown de-vig method {method!r}; choose from {sorted(DEVIG_METHODS)} or 'auto'") from None
    return fn(implied)


def devig_auto(implied: Sequence[float]) -> list[float]:
    """Shin when its insider share is well defined (the method that best explains the
    favourite-longshot bias in sportsbook closes), else power, else multiplicative. An
    under-round or degenerate vector always falls back to multiplicative."""
    ps = [float(p) for p in implied]
    if len(ps) < 2 or any(p <= 0 or p >= 1 for p in ps) or sum(ps) <= 1.0:
        return devig_multiplicative(ps)
    if len(ps) == 2 and shin_z_two_way(ps[0], ps[1]) is not None:
        return devig_shin_closed(ps)
    if len(ps) > 2:
        return devig_shin(ps)
    return devig_power(ps)


def devig_range(implied: Sequence[float], methods: Sequence[str] = ("multiplicative", "power", "additive", "shin")) -> dict[str, Any]:
    """Every method's fair vector plus the per-outcome min/max across them: the spread of
    the de-vig disagreement, widest on heavy favourites, is a real uncertainty a consumer
    should carry (``consensus_fair_value`` treats it like a bid/ask spread)."""
    by: dict[str, list[float]] = {m: devig(implied, m) for m in methods}
    n = len(list(implied))
    return {
        "by_method": by,
        "fair_min": [min(by[m][i] for m in methods) for i in range(n)],
        "fair_max": [max(by[m][i] for m in methods) for i in range(n)],
        "overround": overround(implied),
    }


@dataclass(frozen=True)
class SportsbookProbs:
    """De-vigged two-way sportsbook probabilities with the cross-method disagreement."""

    home: float
    away: float
    fair_min: float          # min over methods of P(home)
    fair_max: float          # max over methods of P(home)
    method: str
    overround: float
    by_method: dict[str, float] = field(default_factory=dict)  # method -> P(home)

    @property
    def range(self) -> float:
        return self.fair_max - self.fair_min

    def as_mapping(self, home: str, away: str) -> dict[str, dict[str, float]]:
        """Shape ``consensus_fair_value(sportsbook_probs=...)`` accepts, range attached."""
        return {
            home: {"fair": self.home, "fair_min": self.fair_min, "fair_max": self.fair_max},
            away: {"fair": self.away, "fair_min": 1.0 - self.fair_max, "fair_max": 1.0 - self.fair_min},
        }


def sportsbook_probs_from_moneylines(home_ml: float, away_ml: float, method: str = "power") -> SportsbookProbs:
    """American moneylines (e.g. ``-245`` / ``+200``) -> de-vigged P(home)/P(away) by ``method``,
    with ``fair_min``/``fair_max`` for P(home) across all four methods."""
    implied = [implied_from_decimal(american_to_decimal(home_ml)), implied_from_decimal(american_to_decimal(away_ml))]
    rng = devig_range(implied)
    chosen = devig(implied, method)
    return SportsbookProbs(
        home=chosen[0], away=chosen[1], fair_min=rng["fair_min"][0], fair_max=rng["fair_max"][0],
        method=method, overround=rng["overround"], by_method={m: v[0] for m, v in rng["by_method"].items()},
    )


__all__ = [
    "american_to_decimal", "decimal_to_american", "implied_from_decimal", "decimal_from_prob",
    "overround", "devig_multiplicative", "devig_power", "devig_shin", "devig_additive", "devig_shin_closed",
    "shin_z_two_way", "devig", "devig_auto", "devig_range", "DEVIG_METHODS", "SportsbookProbs",
    "sportsbook_probs_from_moneylines", "log", "exp",
]

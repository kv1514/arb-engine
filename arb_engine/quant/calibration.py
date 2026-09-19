"""Evaluation statistics for win-probability models — standard library only.

Plays inside one game are not independent (a model that is wrong about a game is wrong
for sixty consecutive plays), so every interval here resamples *games*, never plays:

* :func:`per_game_loss_diff` collapses play-level (p_a, p_b, y) rows to one mean loss
  difference per game, and :func:`bootstrap_paired` gives the game-cluster percentile
  interval for that difference (the "is model A better than B" question that
  ``docs/MODEL.md`` tables must answer with an interval, not a point).
* :func:`games_needed` inverts the same sd to say how many more games a comparison needs.
* :func:`pav_isotonic` / :func:`corp_decomposition` split the Brier score into
  miscalibration (MCB), discrimination (DSC) and uncertainty (UNC) following
  Dimitriadis, Gneiting & Jordan (2021): ``brier = MCB - DSC + UNC`` exactly.
* :func:`reliability_band` is the reliability diagram with a game-bootstrap band per bin.
* :func:`extreme_path_audit` checks the tails along whole paths: at the first moment a
  model claims >= q (or <= 1-q) the outcome frequency should equal the claimed value if
  the path is a martingale (optional stopping), so ``excess = claimed - realized`` is the
  overconfidence at the moment the model first becomes extreme.
"""

from __future__ import annotations

import math
import random
from statistics import NormalDist
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Union

Row = Union[Sequence[Any], Mapping[str, Any]]
LossFn = Callable[[float, float], float]

_EPS = 1e-12


def brier_loss(p: float, y: float) -> float:
    return (p - y) ** 2


def log_loss(p: float, y: float) -> float:
    q = min(max(p, _EPS), 1.0 - _EPS)
    return -(y * math.log(q) + (1.0 - y) * math.log(1.0 - q))


LOSSES: dict[str, LossFn] = {"brier": brier_loss, "log": log_loss}


def _loss_fn(loss: Union[str, LossFn]) -> LossFn:
    return LOSSES[loss] if isinstance(loss, str) else loss


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _sd(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _quantile(sorted_xs: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile (numpy default) on an already sorted list."""
    if not sorted_xs:
        return float("nan")
    pos = q * (len(sorted_xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


# ---- paired comparison, game-clustered ---------------------------------------------------
def per_game_loss_diff(rows_by_game: Mapping[Any, Sequence[Row]], a: Any = 0, b: Any = 1, y: Any = 2, loss: Union[str, LossFn] = "brier") -> dict[Any, float]:
    """Mean ``loss(p_a, y) - loss(p_b, y)`` per game (negative = candidate ``a`` is better).

    Rows are tuples ``(p_a, p_b, y)`` (``a``/``b``/``y`` are indices) or mappings (``a``/``b``/
    ``y`` are keys, e.g. ``a="model", b="market", y="home_win"``). Games without rows or with
    a missing probability on either side are skipped, so the two candidates are always
    scored on exactly the same plays."""
    fn = _loss_fn(loss)
    out: dict[Any, float] = {}
    for game, rows in rows_by_game.items():
        diffs = []
        for r in rows:
            pa, pb, yy = r[a], r[b], r[y]
            if pa is None or pb is None or yy is None:
                continue
            diffs.append(fn(float(pa), float(yy)) - fn(float(pb), float(yy)))
        if diffs:
            out[game] = _mean(diffs)
    return out


def bootstrap_paired(diffs_by_game: Union[Mapping[Any, float], Sequence[float]], B: int = 1000, seed: int = 0, level: float = 0.90) -> dict[str, Any]:
    """Game-cluster percentile bootstrap of a per-game statistic.

    Resamples games with replacement ``B`` times (seeded, so tables are reproducible) and
    returns the population ``mean`` of the diffs with the ``lo90``/``hi90`` percentile
    interval of the resampled means (keys stay ``lo90``/``hi90`` whatever ``level`` is, plus
    ``level`` itself), ``frac_positive`` (share of replicates with mean > 0),
    ``n_games`` and ``sd_per_game`` (sample sd of the per-game diffs — the input to
    :func:`games_needed`)."""
    diffs = list(diffs_by_game.values()) if isinstance(diffs_by_game, Mapping) else list(diffs_by_game)
    n = len(diffs)
    if n == 0:
        return {"mean": float("nan"), "lo90": float("nan"), "hi90": float("nan"), "frac_positive": float("nan"), "n_games": 0, "sd_per_game": float("nan"), "level": level, "B": B}
    rng = random.Random(seed)
    means = []
    for _ in range(B):
        acc = 0.0
        for _ in range(n):
            acc += diffs[rng.randrange(n)]
        means.append(acc / n)
    means.sort()
    alpha = (1.0 - level) / 2.0
    return {
        "mean": _mean(diffs),
        "lo90": _quantile(means, alpha),
        "hi90": _quantile(means, 1.0 - alpha),
        "frac_positive": sum(1 for m in means if m > 0) / B,
        "n_games": n,
        "sd_per_game": _sd(diffs),
        "level": level,
        "B": B,
    }


def games_needed(delta: float, sd_per_game: float, power: float = 0.8, alpha: float = 0.1) -> int:
    """Games required for a paired comparison to detect a per-game loss difference of
    ``|delta|`` with the given power at a two-sided ``alpha`` (defaults match the 90%
    interval above): ``n = ((z_{1-alpha/2} + z_{power}) * sd / delta)^2``, rounded up."""
    if delta == 0:
        raise ValueError("delta must be non-zero")
    if sd_per_game <= 0:
        return 1
    nd = NormalDist()
    z = nd.inv_cdf(1.0 - alpha / 2.0) + nd.inv_cdf(power)
    return int(math.ceil((z * sd_per_game / abs(delta)) ** 2))


# ---- calibration --------------------------------------------------------------------------
def pav_isotonic(p: Sequence[float], y: Sequence[float], weights: Optional[Sequence[float]] = None) -> list[float]:
    """Pool-adjacent-violators isotonic regression of ``y`` on ``p``.

    Returns the fitted (non-decreasing in ``p``) value for every input row, in input
    order. Rows with identical ``p`` are pooled first so ties always share one fitted
    value, which is what makes the CORP decomposition an exact identity."""
    if len(p) != len(y):
        raise ValueError("p and y must have the same length")
    if not p:
        return []
    w = [1.0] * len(p) if weights is None else [float(x) for x in weights]
    order = sorted(range(len(p)), key=lambda i: p[i])
    # blocks: [sum_w, sum_wy, first_index_in_order, last_index_in_order]
    blocks: list[list[float]] = []
    i = 0
    while i < len(order):
        j = i
        sw = swy = 0.0
        while j < len(order) and p[order[j]] == p[order[i]]:
            sw += w[order[j]]
            swy += w[order[j]] * y[order[j]]
            j += 1
        blocks.append([sw, swy, i, j - 1])
        while len(blocks) >= 2 and blocks[-2][1] * blocks[-1][0] > blocks[-1][1] * blocks[-2][0]:
            b = blocks.pop()
            a = blocks[-1]
            a[0] += b[0]
            a[1] += b[1]
            a[3] = b[3]
        i = j
    fitted = [0.0] * len(p)
    for sw, swy, lo, hi in blocks:
        v = swy / sw if sw else 0.0
        for k in range(int(lo), int(hi) + 1):
            fitted[order[k]] = v
    return fitted


def corp_decomposition(p: Sequence[float], y: Sequence[float]) -> dict[str, float]:
    """CORP Brier decomposition ``brier = MCB - DSC + UNC`` (Dimitriadis et al. 2021).

    ``MCB`` = score lost to miscalibration (brier minus the brier of the isotonic
    recalibration), ``DSC`` = discrimination gained over the climatological forecast,
    ``UNC`` = ``ybar (1 - ybar)``. All in Brier units, so 0.01 of MCB is one Brier point."""
    if not p:
        return {"MCB": float("nan"), "DSC": float("nan"), "UNC": float("nan"), "brier": float("nan"), "n": 0}
    n = len(p)
    ybar = sum(y) / n
    brier = sum((pi - yi) ** 2 for pi, yi in zip(p, y)) / n
    c = pav_isotonic(p, y)
    brier_c = sum((ci - yi) ** 2 for ci, yi in zip(c, y)) / n
    unc = ybar * (1.0 - ybar)
    return {"MCB": brier - brier_c, "DSC": unc - brier_c, "UNC": unc, "brier": brier, "n": n}


def reliability_band(p: Sequence[float], y: Sequence[float], groups: Sequence[Any], B: int = 200, seed: int = 0, bins: Union[int, Sequence[float]] = 10, level: float = 0.90) -> list[dict[str, Any]]:
    """Reliability diagram with a group (game) bootstrap band on the observed frequency.

    One dict per bin: ``lo``/``hi`` edges, ``n`` rows, ``mean_p`` (claimed), ``obs``
    (realized), ``obs_lo90``/``obs_hi90`` from resampling groups with replacement, and
    ``gap = mean_p - obs`` (positive = overconfident in that bin). Empty bins are kept
    with ``n=0`` and NaN statistics so callers can print a fixed table."""
    if not (len(p) == len(y) == len(groups)):
        raise ValueError("p, y and groups must have the same length")
    edges = [i / bins for i in range(bins + 1)] if isinstance(bins, int) else [float(e) for e in bins]
    nb = len(edges) - 1

    def bin_of(x: float) -> int:
        for k in range(nb):
            if x < edges[k + 1] or k == nb - 1:
                return k
        return nb - 1  # pragma: no cover

    by_group: dict[Any, list[int]] = {}
    for i, g in enumerate(groups):
        by_group.setdefault(g, []).append(i)
    gids = list(by_group)
    bins_idx = [bin_of(float(x)) for x in p]

    def obs_per_bin(rows: Iterable[int]) -> list[Optional[float]]:
        s = [0.0] * nb
        c = [0] * nb
        for i in rows:
            s[bins_idx[i]] += y[i]
            c[bins_idx[i]] += 1
        return [s[k] / c[k] if c[k] else None for k in range(nb)]

    base = obs_per_bin(range(len(p)))
    rng = random.Random(seed)
    reps: list[list[Optional[float]]] = []
    for _ in range(B):
        rows: list[int] = []
        for _ in range(len(gids)):
            rows.extend(by_group[gids[rng.randrange(len(gids))]])
        reps.append(obs_per_bin(rows))
    alpha = (1.0 - level) / 2.0
    out = []
    for k in range(nb):
        idx = [i for i in range(len(p)) if bins_idx[i] == k]
        vals = sorted(r[k] for r in reps if r[k] is not None)
        out.append({
            "lo": edges[k],
            "hi": edges[k + 1],
            "n": len(idx),
            "mean_p": _mean([float(p[i]) for i in idx]) if idx else float("nan"),
            "obs": base[k] if base[k] is not None else float("nan"),
            "obs_lo90": _quantile(vals, alpha) if vals else float("nan"),
            "obs_hi90": _quantile(vals, 1.0 - alpha) if vals else float("nan"),
            "gap": (_mean([float(p[i]) for i in idx]) - base[k]) if idx and base[k] is not None else float("nan"),
        })
    return out


# ---- extreme paths --------------------------------------------------------------------------
def extreme_path_audit(paths: Iterable[Any], thresholds: Sequence[float] = (0.9, 0.95, 0.99)) -> list[dict[str, Any]]:
    """Tail check along whole game paths (one ``(probabilities, outcome)`` pair per game;
    mappings with ``p``/``y`` keys are accepted too).

    For each threshold ``q`` and side: take the games whose path first reaches ``>= q``
    (``side="high"``) or ``<= 1-q`` (``side="low"``); ``claimed`` is the mean probability
    at that first crossing, ``realized`` the mean outcome among those games and
    ``excess = claimed - realized``. For a calibrated martingale the optional stopping
    theorem makes ``excess`` zero in expectation, so a positive high-side excess (or a
    negative low-side one) is overconfidence exactly where an in-play STEAL/LOCK signal
    would fire. ``n_hit`` counts games that crossed; ``n`` all games."""
    seqs: list[tuple[list[float], float]] = []
    for item in paths:
        if isinstance(item, Mapping):
            seqs.append(([float(v) for v in item["p"]], float(item["y"])))
        else:
            ps, yv = item
            seqs.append(([float(v) for v in ps], float(yv)))
    out = []
    for q in thresholds:
        for side in ("high", "low"):
            claimed: list[float] = []
            realized: list[float] = []
            for ps, yv in seqs:
                for v in ps:
                    if (side == "high" and v >= q) or (side == "low" and (1.0 - v) >= q):
                        claimed.append(v)
                        realized.append(yv)
                        break
            n_hit = len(claimed)
            c = _mean(claimed) if n_hit else float("nan")
            r = _mean(realized) if n_hit else float("nan")
            out.append({"q": q, "side": side, "n": len(seqs), "n_hit": n_hit, "claimed": c, "realized": r, "excess": (c - r) if n_hit else float("nan")})
    return out

"""Grouped cross-validation folds for play-level data — standard library only.

Plays from one game must never straddle a train/test split (the score, clock and spread
of adjacent plays leak the outcome), so folds are built on *games* and then expanded to
row indices. ``scripts/train_wp_model.py`` imports this at training time and the
evaluation code imports it for out-of-fold tables, so it deliberately has no numpy.

* :func:`grouped_folds` — k folds, every game in exactly one, fold sizes balanced by
  row count (LPT greedy on a seeded shuffle), so a 5-fold split of 1,300 games lands
  within a few plays of equal.
* :func:`loso_folds` — leave-one-game-out, one fold per game in first-appearance order.
* :func:`fold_train_test` — the complementary (train, test) index lists for fold ``i``.
* :class:`OOFLedger` / :func:`oof_record` — bookkeeping of out-of-fold losses per
  candidate and fold so the selection table can be printed and serialised.
"""

from __future__ import annotations

import json
import random
from typing import Any, Hashable, Optional, Sequence


def _games_in_order(game_ids: Sequence[Hashable]) -> dict[Hashable, list[int]]:
    by_game: dict[Hashable, list[int]] = {}
    for i, g in enumerate(game_ids):
        by_game.setdefault(g, []).append(i)
    return by_game


def grouped_folds(game_ids: Sequence[Hashable], k: int = 5, seed: int = 0) -> list[list[int]]:
    """``k`` lists of row indices; every game's rows land in exactly one fold.

    Games are shuffled with ``seed`` (deterministic), sorted by size descending and each
    assigned to the currently smallest fold, which keeps fold *row* counts within a few
    percent of each other even when games differ in length. Indices inside a fold are
    sorted so slicing a play table by fold preserves play order."""
    if k < 1:
        raise ValueError("k must be >= 1")
    by_game = _games_in_order(game_ids)
    if k > len(by_game):
        raise ValueError(f"k={k} exceeds the number of distinct games ({len(by_game)})")
    games = list(by_game)
    random.Random(seed).shuffle(games)
    games.sort(key=lambda g: -len(by_game[g]))  # stable: ties keep the shuffled order
    folds: list[list[int]] = [[] for _ in range(k)]
    sizes = [0] * k
    for g in games:
        j = min(range(k), key=lambda i: (sizes[i], i))
        folds[j].extend(by_game[g])
        sizes[j] += len(by_game[g])
    return [sorted(f) for f in folds]


def loso_folds(game_ids: Sequence[Hashable]) -> list[list[int]]:
    """Leave-one-game-out: one fold per distinct game, in order of first appearance."""
    return [sorted(rows) for rows in _games_in_order(game_ids).values()]


def fold_train_test(folds: Sequence[Sequence[int]], i: int) -> tuple[list[int], list[int]]:
    """(train, test) row indices for fold ``i``: test is fold ``i``, train is every other fold."""
    test = list(folds[i])
    train = sorted(idx for j, f in enumerate(folds) if j != i for idx in f)
    return train, test


class OOFLedger:
    """Out-of-fold loss bookkeeping: ``record`` one (candidate, fold) cell at a time, then
    ``table()`` for the row-weighted mean per candidate and ``best()`` for the winner."""

    def __init__(self) -> None:
        self.cells: dict[str, dict[int, tuple[float, int]]] = {}

    def record(self, candidate: str, fold: int, loss: float, n: int = 1) -> None:
        self.cells.setdefault(candidate, {})[int(fold)] = (float(loss), int(n))

    def folds(self) -> list[int]:
        return sorted({f for cells in self.cells.values() for f in cells})

    def table(self) -> dict[str, dict[str, Any]]:
        """Per candidate: ``mean`` (row-weighted over folds), ``per_fold`` {fold: loss},
        ``n_rows`` and ``n_folds``. Candidates missing a fold are still reported so an
        incomplete run is visible instead of silently winning on the easy folds."""
        out: dict[str, dict[str, Any]] = {}
        for cand, cells in self.cells.items():
            n_rows = sum(n for _, n in cells.values())
            mean = sum(l * n for l, n in cells.values()) / n_rows if n_rows else float("nan")
            out[cand] = {"mean": mean, "per_fold": {f: cells[f][0] for f in sorted(cells)}, "n_rows": n_rows, "n_folds": len(cells)}
        return out

    def best(self, require_folds: Optional[int] = None) -> Optional[str]:
        """Lowest mean loss among candidates that covered ``require_folds`` folds (default:
        every fold seen by any candidate)."""
        need = len(self.folds()) if require_folds is None else require_folds
        rows = [(v["mean"], c) for c, v in self.table().items() if v["n_folds"] >= need]
        return min(rows)[1] if rows else None

    def to_json(self) -> str:
        return json.dumps({"table": self.table(), "folds": self.folds()}, indent=1, sort_keys=True)


_DEFAULT_LEDGER = OOFLedger()


def oof_record(candidate: str, fold: int, loss: float, n: int = 1, ledger: Optional[OOFLedger] = None) -> OOFLedger:
    """Record one out-of-fold loss cell and return the ledger it went into (the module
    default when none is given, so a training script can call this from a loop and read
    ``oof_record.ledger.table()`` at the end)."""
    lg = ledger if ledger is not None else _DEFAULT_LEDGER
    lg.record(candidate, fold, loss, n)
    return lg


oof_record.ledger = _DEFAULT_LEDGER  # type: ignore[attr-defined]

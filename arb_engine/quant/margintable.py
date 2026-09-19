"""Margin-of-victory tables from nflverse ``games.csv`` (pure functions, deterministic output).

Two tables feed :mod:`arb_engine.quant.lines`:

* ``build_margin_dist`` — P(favourite margin = k | closing spread), one bucket per
  half-point spread, as raw integer counts. NFL margins pile up on key numbers (3, 7, 6,
  10) so a normal alone misprices half-point moves through them; the empirical lattice
  captures the lumps and ``EmpiricalMargin`` shrinks each bucket to the normal with weight
  ``n / (n + n0)`` so thin buckets (big spreads) do not overfit.
* ``build_sigma`` — the standard deviation of ``margin - spread`` and ``total - total_line``
  overall and per season, with standard errors, plus the observed tie rate. These are the
  parameters of the normal fallback and of the in-play scaling ``sd = sigma * sqrt(frac)``.

Training window: only *complete* seasons enter a table. A season with any unplayed row of
an accepted game type (the one in progress when the file was downloaded) is excluded unless
``max_season`` is given explicitly, so the games the engine is evaluated or traded on this
season can never be in the tables that price them (the WP model draws the same line at
2016-2024). The excluded seasons are listed in the ``source`` block.

Determinism: every number is rounded before serialisation and ``dump_json`` sorts keys, so
the same rows always produce the same bytes (``tests/test_margintable.py`` proves it on the
trimmed fixture). The ``source`` block records the sha256 and row count of the CSV so a
committed table can be traced to the file that built it.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping, Optional, Sequence

SCHEMA = 1
NFLVERSE_GAMES_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"
DEFAULT_MIN_SEASON = 2016   # extra point moved to the 15 in 2015; OT shortened 2017; modern key-number mix
DEFAULT_SHRINK_N0 = 50      # bucket weight n/(n+50): a 50-game bucket is half empirical, half normal
GAME_TYPES = ("REG", "WC", "DIV", "CON", "SB")


def _f(x: Any) -> Optional[float]:
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_games_csv(text: str) -> list[dict[str, str]]:
    """Rows of an nflverse ``games.csv`` (or the trimmed fixture) as dicts."""
    return list(csv.DictReader(io.StringIO(text)))


def bucket_key(spread: float) -> str:
    """Half-point bucket label for a favourite spread: 3.0 -> '3', 2.5 -> '2.5'."""
    v = round(abs(float(spread)) * 2) / 2
    return f"{v:g}"


def incomplete_seasons(rows: Iterable[Mapping[str, Any]], game_types: Sequence[str] = GAME_TYPES) -> list[int]:
    """Seasons with at least one unplayed game of an accepted type (no ``result`` yet)."""
    out: set[int] = set()
    for r in rows:
        season = _f(r.get("season"))
        if season is None or (game_types and r.get("game_type") not in game_types):
            continue
        if _f(r.get("result")) is None:
            out.add(int(season))
    return sorted(out)


def usable_rows(rows: Iterable[Mapping[str, Any]], min_season: int = DEFAULT_MIN_SEASON, game_types: Sequence[str] = GAME_TYPES, max_season: Optional[int] = None) -> list[dict[str, Any]]:
    """Finished games with a closing spread: ``[{season, result, spread_line, total, total_line, overtime}]``.

    ``max_season`` None drops every incomplete season (the one in progress), so the current
    season's finished games never leak into a table that is scored on them; pass it
    explicitly to override. nflverse conventions: ``result = home_score - away_score``;
    ``spread_line`` is positive when the *home* team is favoured."""
    rows = list(rows)
    skip = set(incomplete_seasons(rows, game_types)) if max_season is None else set()
    out: list[dict[str, Any]] = []
    for r in rows:
        season = _f(r.get("season"))
        result, spread = _f(r.get("result")), _f(r.get("spread_line"))
        if season is None or result is None or spread is None:
            continue
        if int(season) < min_season or int(season) in skip or (max_season is not None and int(season) > max_season):
            continue
        if game_types and r.get("game_type") not in game_types:
            continue
        out.append({"season": int(season), "game_id": r.get("game_id", ""), "result": result, "spread_line": spread, "total": _f(r.get("total")), "total_line": _f(r.get("total_line")), "overtime": _f(r.get("overtime"))})
    return out


def favourite_margin(result: float, spread_line: float) -> int:
    """Margin from the favourite's side (home margin when the game is a pick-em)."""
    return int(round(result)) if spread_line >= 0 else -int(round(result))


def _sd(xs: Sequence[float]) -> tuple[Optional[float], Optional[float]]:
    """Population standard deviation and its large-sample standard error sd/sqrt(2(n-1))."""
    n = len(xs)
    if n < 2:
        return None, None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / n
    sd = math.sqrt(var)
    return sd, sd / math.sqrt(2 * (n - 1))


def _r(x: Optional[float], nd: int = 6) -> Optional[float]:
    return None if x is None else round(float(x), nd)


def source_block(text: str, rows: Sequence[Mapping[str, Any]], used: Sequence[Mapping[str, Any]], name: str = "games.csv", max_season: Optional[int] = None) -> dict[str, Any]:
    seasons = sorted({int(r["season"]) for r in used})
    return {
        "file": name, "url": NFLVERSE_GAMES_URL,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "rows": len(rows), "rows_used": len(used),
        "seasons": [seasons[0], seasons[-1]] if seasons else [],
        "max_season": max_season,
        "excluded_incomplete_seasons": [] if max_season is not None else [s for s in incomplete_seasons(rows) if not seasons or s > seasons[0]],
    }


def build_margin_dist(rows: Iterable[Mapping[str, Any]], min_season: int = DEFAULT_MIN_SEASON, sport: str = "nfl", n0: int = DEFAULT_SHRINK_N0, max_season: Optional[int] = None) -> dict[str, Any]:
    """``{"buckets": {"3": {"n", "mean", "pmf": {"-7": count, ...}}, ...}, "all": {...}}``.

    ``pmf`` values are raw counts of the favourite's margin; keeping counts (not
    frequencies) makes the shrinkage weight recoverable and the file diff-friendly."""
    used = usable_rows(rows, min_season, max_season=max_season)
    counts: dict[str, Counter] = defaultdict(Counter)
    all_counts: Counter = Counter()
    for r in used:
        m = favourite_margin(r["result"], r["spread_line"])
        counts[bucket_key(r["spread_line"])][m] += 1
        all_counts[m] += 1

    def block(c: Counter) -> dict[str, Any]:
        n = sum(c.values())
        mean = sum(k * v for k, v in c.items()) / n if n else 0.0
        return {"n": n, "mean": _r(mean, 4), "pmf": {str(k): c[k] for k in sorted(c)}}

    seasons = sorted({r["season"] for r in used})
    return {
        "schema": SCHEMA, "sport": sport, "min_season": min_season, "max_season": seasons[-1] if seasons else None, "shrink_n0": n0,
        "orientation": "favourite margin (home margin for pick-ems); bucket = |closing spread| to the half point",
        "buckets": {k: block(counts[k]) for k in sorted(counts, key=float)},
        "all": block(all_counts),
    }


def build_sigma(rows: Iterable[Mapping[str, Any]], min_season: int = 1999, sport: str = "nfl", ref_min_season: int = DEFAULT_MIN_SEASON, max_season: Optional[int] = None) -> dict[str, Any]:
    """Residual sigma of ``margin - spread`` and ``total - total_line`` per season and pooled.

    ``margin.sigma`` / ``total.sigma`` are the pooled values from ``ref_min_season`` on (the
    ones the engine uses); ``by_season`` keeps every season with its SE so drift is visible.
    ``overtime`` carries the OT rate and P(tie | overtime), the tie mass of an OT state."""
    used = usable_rows(rows, min_season, max_season=max_season)
    by_season: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in used:
        by_season[r["season"]].append(r)

    def stats(rs: Sequence[Mapping[str, Any]], key_a: str, key_b: str) -> dict[str, Any]:
        xs = [r[key_a] - r[key_b] for r in rs if r.get(key_a) is not None and r.get(key_b) is not None]
        sd, se = _sd(xs)
        return {"n": len(xs), "mean_resid": _r(sum(xs) / len(xs), 4) if xs else None, "sigma": _r(sd, 4), "se": _r(se, 4)}

    ref = [r for r in used if r["season"] >= ref_min_season]
    ties = sum(1 for r in ref if r["result"] == 0)
    ot = [r for r in ref if r.get("overtime") == 1]
    ot_ties = sum(1 for r in ot if r["result"] == 0)
    seasons = sorted({r["season"] for r in ref})
    out = {
        "schema": SCHEMA, "sport": sport, "ref_min_season": ref_min_season, "ref_max_season": seasons[-1] if seasons else None,
        "margin": {**stats(ref, "result", "spread_line"), "by_season": {str(s): stats(by_season[s], "result", "spread_line") for s in sorted(by_season)}},
        "total": {**stats(ref, "total", "total_line"), "by_season": {str(s): stats(by_season[s], "total", "total_line") for s in sorted(by_season)}},
        "tie_rate": _r(ties / len(ref), 6) if ref else None,
        "ties": ties,
        "overtime": {"n_ot": len(ot), "ot_rate": _r(len(ot) / len(ref), 6) if ref else None, "ties_in_ot": ot_ties, "tie_rate_given_ot": _r(ot_ties / len(ot), 6) if ot else None},
    }
    return out


def dump_json(obj: Any) -> str:
    """Canonical serialisation: sorted keys, one-space indent, trailing newline."""
    return json.dumps(obj, sort_keys=True, indent=1, ensure_ascii=False) + "\n"


def build_tables(text: str, name: str = "games.csv", min_season: int = DEFAULT_MIN_SEASON, sport: str = "nfl", max_season: Optional[int] = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Both tables with their ``source`` block from the CSV text (what the script writes).
    ``max_season`` None = every complete season (the in-progress one is excluded)."""
    rows = parse_games_csv(text)
    dist = build_margin_dist(rows, min_season=min_season, sport=sport, max_season=max_season)
    sig = build_sigma(rows, sport=sport, ref_min_season=min_season, max_season=max_season)
    dist["source"] = source_block(text, rows, usable_rows(rows, min_season, max_season=max_season), name, max_season)
    sig["source"] = source_block(text, rows, usable_rows(rows, 1999, max_season=max_season), name, max_season)
    return dist, sig


__all__ = [
    "SCHEMA", "NFLVERSE_GAMES_URL", "DEFAULT_MIN_SEASON", "DEFAULT_SHRINK_N0", "parse_games_csv", "bucket_key",
    "usable_rows", "incomplete_seasons", "favourite_margin", "build_margin_dist", "build_sigma", "build_tables", "dump_json", "source_block",
]

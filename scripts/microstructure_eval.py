#!/usr/bin/env python3
"""Frozen, game-blocked evaluation for H1/H2/H3/H4 microstructure candidates.

The script consumes only recorded JSON/JSONL rows.  It never talks to a venue and never
creates an alert or order.  Test-fold openings are append-only in ``out/eval_log.jsonl``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "tests/fixtures/microstructure/manifest.json"
DEFAULT_LOG = ROOT / "out/eval_log.jsonl"


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def spec_hash(spec: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(spec).encode()).hexdigest()


def validate_manifest(manifest: dict[str, Any]) -> None:
    seen: dict[str, str] = {}
    for fold in ("discovery", "test"):
        for game in manifest.get(fold, []):
            key = game["event_key"] if isinstance(game, dict) else str(game)
            if key in seen:
                raise ValueError(f"game {key} occurs in both {seen[key]} and {fold}")
            seen[key] = fold
    for game in manifest.get("test", []):
        kickoff = str(game.get("kickoff", "")) if isinstance(game, dict) else ""
        if kickoff and kickoff < "2026-10-08":  # NFL week 5 Thursday kickoff
            raise ValueError("test fold begins at NFL week-5 kickoff")


def ridge_fit(xs: list[float], ys: list[float], penalty: float = 1.0) -> tuple[float, float]:
    if not xs:
        return 0.0, 0.0
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    denom = sum((x - mx) ** 2 for x in xs) + penalty
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    return my - slope * mx, slope


def game_block_bootstrap(game_values: dict[str, list[float]], statistic: Callable[[list[float]], float] | None = None,
                         draws: int = 2000, seed: int = 20260920) -> dict[str, float]:
    statistic = statistic or (lambda xs: sum(xs) / len(xs))
    games = sorted(game_values)
    if not games:
        return {"point": math.nan, "lo": math.nan, "hi": math.nan}
    pooled = [x for game in games for x in game_values[game]]
    rng = random.Random(seed)
    samples = []
    for _ in range(draws):
        chosen = [rng.choice(games) for _ in games]
        values = [x for game in chosen for x in game_values[game]]
        samples.append(statistic(values))
    samples.sort()
    return {"point": statistic(pooled), "lo": samples[int(.025 * draws)], "hi": samples[min(draws - 1, int(.975 * draws))]}


deterministic_bootstrap = game_block_bootstrap


def decision(metrics: dict[str, Any]) -> str:
    skill = metrics.get("skill_ci") or {}
    pnl = metrics.get("net_pnl_ci") or {}
    if skill.get("lo") is None or skill.get("lo") <= 0 <= skill.get("hi", 0):
        return "reject"
    if (pnl.get("lo") is None or pnl.get("lo") <= 0 <= pnl.get("hi", 0)
            or metrics.get("fill_rate", 0) < .60 or metrics.get("test_games", 0) < 30
            or metrics.get("deduped_triggers", 0) < 200 or metrics.get("top_game_share", 1) > .40):
        return "shadow"
    robust = metrics.get("robust_l3_h05") or {}
    if robust.get("net_pnl_ci", {}).get("lo", 0) > 0 and robust.get("positive_game_share", 0) >= .60:
        return "alert-only"
    return "shadow"


def guard_test_open(log_path: Path, current_hash: str, reopen_reason: str | None = None) -> None:
    prior = []
    if log_path.exists():
        prior = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    opened = [r for r in prior if r.get("fold") == "test"]
    if opened and opened[-1].get("spec_hash") != current_hash and not reopen_reason:
        raise RuntimeError('test fold was opened under a different spec; use --reopen-test "<reason>"')
    if reopen_reason is not None and not reopen_reason.strip():
        raise ValueError("--reopen-test requires a non-empty reason")


def append_log(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(canonical(row) + "\n")


def synthetic_report() -> dict[str, Any]:
    fixture = json.loads((ROOT / "tests/fixtures/results/momentum_synthetic_40.json").read_text())
    skill = fixture["persistence_mean_absolute_error"] - fixture["mean_absolute_error"]
    return {"experiment": "microstructure", "candidate": "H1_momentum_prototype", "fold": "synthetic",
            "mae": round(fixture["mean_absolute_error"], 3),
            "persistence_mae": round(fixture["persistence_mean_absolute_error"], 3),
            "skill_vs_persistence": skill, "decision": "reject" if skill <= 0 else "continue",
            "note": "price-forecast diagnostic only; no fills or P&L"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--fold", choices=("discovery", "test", "synthetic"), default="synthetic")
    ap.add_argument("--reopen-test")
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--results", type=Path)
    args = ap.parse_args(argv)
    manifest = json.loads(args.manifest.read_text()) if args.manifest.exists() else {"discovery": [], "test": []}
    validate_manifest(manifest)
    spec = manifest.get("spec") or {"version": 1, "primary": "H3@30s", "horizons": [5, 15, 30, 60], "bootstrap": 2000, "seed": 20260920}
    digest = spec_hash(spec)
    if args.fold == "test":
        guard_test_open(args.log, digest, args.reopen_test)
    report = synthetic_report() if args.fold == "synthetic" else {"experiment": "microstructure", "fold": args.fold, "spec_hash": digest, "status": "no-recorded-games" if not manifest.get(args.fold) else "manifest-frozen"}
    append_log(args.log, {"fold": args.fold, "spec_hash": digest, "reopen_reason": args.reopen_test})
    if args.results:
        args.results.parent.mkdir(parents=True, exist_ok=True)
        args.results.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

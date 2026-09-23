#!/usr/bin/env python3
"""Frozen, game-blocked evaluation of H1 momentum / H2 dip-recovery / H3 lead-lag / H4 arb.

    python3 scripts/microstructure_eval.py --db out/history.db --fold discovery [--results FILE]
    python3 scripts/microstructure_eval.py --db out/history.db --fold validation
    python3 scripts/microstructure_eval.py --db out/history.db --fold test [--reopen-test "why"]
    python3 scripts/microstructure_eval.py --fold synthetic

Reads recorded ticks read-only (quant/microdata.load_db); never talks to a venue, never
creates an alert or an order. Folds are whole games (tests/fixtures/microstructure/
manifest.json): *discovery* = the games the LAG rule was designed on, *validation* = later
games before ``test_from``, *test* = games from ``test_from`` on, defined by rule before any
was recorded. Every run is appended to ``out/eval_log.jsonl``; the test fold refuses a spec
that differs from the one it was first opened under unless ``--reopen-test`` says why.

Per hypothesis and horizon, as trades from quant/paperexec (IOC at the decision ask after the
latency, sold to the bid at h, both fees, missed fills counted, unsold-and-unsettled trades
excluded):

* H1 momentum: buy a contract that just rose (trigger, dmid_30 > 0).
* H2 dip: buy a contract that just fell (trigger, dmid_30 < 0).
* H3 lead-lag: buy the follower when an independent fresh book moved >= 5c in 30 s, the
  follower moved < half as much, and the leader's mid sits >= 2c above the follower's.
* H4 arbitrage: executable books (Kalshi, Robinhood) on independent books whose all-in asks
  for both outcomes sum below $1; both legs simulated with their own latency (a person on
  Robinhood: 15 s), leg failures unwound.
* Baselines: B0 unchanged price (forecast error), B1 a buy at every unconditional sample (the
  cost hurdle), B3 ridge dmid_h ~ dmid_30, B4 ridge dmid_h ~ leader gap, scored
  leave-one-game-out (a game's forecast never sees its own data).

Uncertainty is a game-level block bootstrap (a game's trades move together); the decision
rules are in ``decision``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_MANIFEST = ROOT / "tests/fixtures/microstructure/manifest.json"
DEFAULT_LOG = ROOT / "out/eval_log.jsonl"
EXECUTABLE = ("kalshi", "robinhood")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def spec_hash(spec: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(spec).encode()).hexdigest()


def _date(key: str) -> str:
    for tag in (":spread:", ":total:"):
        key = key.split(tag, 1)[0]
    return key.rsplit(":", 1)[-1]


def validate_manifest(manifest: dict[str, Any]) -> None:
    seen: dict[str, str] = {}
    for fold in ("discovery", "validation", "test"):
        for game in manifest.get(fold, []) or []:
            key = game["event_key"] if isinstance(game, dict) else str(game)
            if key in seen:
                raise ValueError(f"game {key} occurs in both {seen[key]} and {fold}")
            seen[key] = fold
    test_from = manifest.get("test_from", "2026-10-08")
    for game in manifest.get("discovery", []) or []:
        key = game["event_key"] if isinstance(game, dict) else str(game)
        if _date(key) >= test_from:
            raise ValueError(f"discovery game {key} is on or after test_from {test_from}")


def fold_of(manifest: dict[str, Any], event_key: str) -> str:
    """Whole-game fold for any recorded game key (lines follow their game)."""
    game = event_key
    for tag in (":spread:", ":total:"):
        game = game.split(tag, 1)[0]
    disc = {g["event_key"] if isinstance(g, dict) else g for g in manifest.get("discovery", [])}
    if game in disc:
        return "discovery"
    return "test" if _date(game) >= manifest.get("test_from", "2026-10-08") else "validation"


# ---- statistics ---------------------------------------------------------------------
def ridge_fit(xs: list[float], ys: list[float], penalty: float = 1.0) -> tuple[float, float]:
    if not xs:
        return 0.0, 0.0
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    denom = sum((x - mx) ** 2 for x in xs) + penalty * len(xs) * 1e-4
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom if denom else 0.0
    return my - slope * mx, slope


def game_block_bootstrap(game_values: dict[str, list[float]], statistic: Optional[Callable[[list[float]], float]] = None,
                         draws: int = 2000, seed: int = 20260920, ci: float = 0.90) -> dict[str, float]:
    """Resample whole games; returns point, lo, hi of a central ``ci`` interval."""
    statistic = statistic or (lambda xs: sum(xs) / len(xs))
    games = sorted(g for g, v in game_values.items() if v)
    if not games:
        return {"point": math.nan, "lo": math.nan, "hi": math.nan}
    pooled = [x for g in games for x in game_values[g]]
    rng = random.Random(seed)
    samples = []
    for _ in range(draws):
        values = [x for g in (rng.choice(games) for _ in games) for x in game_values[g]]
        samples.append(statistic(values))
    samples.sort()
    a = (1 - ci) / 2
    return {"point": statistic(pooled), "lo": samples[int(a * draws)], "hi": samples[min(draws - 1, int((1 - a) * draws))]}


deterministic_bootstrap = game_block_bootstrap


def summarize(trades: dict[str, list[Optional[float]]], attempts: dict[str, int], seed: int = 20260920, draws: int = 2000) -> dict[str, Any]:
    """Per-contract net returns by game -> the §9 metrics. ``None`` = missed or unresolved."""
    done = {g: [x for x in xs if x is not None] for g, xs in trades.items()}
    n = sum(len(v) for v in done.values())
    tried = sum(attempts.values())
    out: dict[str, Any] = {"attempts": tried, "trades": n, "games": sum(1 for v in done.values() if v),
                           "fill_rate": (n / tried) if tried else None}
    if not n:
        return out
    per_game = {g: sum(v) for g, v in done.items() if v}
    total = sum(per_game.values())
    out["mean_ret"] = game_block_bootstrap(done, seed=seed, draws=draws)
    out["hit_rate"] = sum(1 for v in done.values() for x in v if x > 0) / n
    out["positive_game_share"] = sum(1 for v in per_game.values() if v > 0) / len(per_game)
    out["top_game_share"] = (max(abs(v) for v in per_game.values()) / sum(abs(v) for v in per_game.values())) if total or per_game else None
    eq, peak, dd = 0.0, 0.0, 0.0
    for g in sorted(per_game):
        eq += per_game[g]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    out["max_drawdown_per_contract"] = dd
    return out


def decision(metrics: dict[str, Any]) -> str:
    """Pre-registered rules (docs/MODEL.md, "Microstructure experiment").

    reject      forecast skill CI not above 0 (includes it or lies below it)
    shadow      skill ok but net P&L CI not above 0, fill rate < 60 %, < 30 games or
                < 200 trades, or one game > 40 % of |P&L|
    alert-only  shadow passed and the stressed run (3 s latency, haircut 0.5) keeps the P&L
                CI above 0 with >= 60 % of games positive
    """
    skill = metrics.get("skill_ci") or {}
    pnl = metrics.get("net_pnl_ci") or {}
    lo = skill.get("lo")
    if lo is None or not math.isfinite(lo) or lo <= 0:
        return "reject"
    plo = pnl.get("lo")
    if (plo is None or not math.isfinite(plo) or plo <= 0 or (metrics.get("fill_rate") or 0) < .60
            or metrics.get("test_games", 0) < 30 or metrics.get("deduped_triggers", 0) < 200
            or (metrics.get("top_game_share") if metrics.get("top_game_share") is not None else 1) > .40):
        return "shadow"
    robust = metrics.get("robust_l3_h05") or {}
    rlo = (robust.get("net_pnl_ci") or {}).get("lo")
    if rlo is not None and rlo > 0 and robust.get("positive_game_share", 0) >= .60:
        return "alert-only"
    return "shadow"


def holm(pvalues: dict[str, float], alpha: float = 0.10) -> dict[str, bool]:
    order = sorted(pvalues, key=pvalues.get)
    out, m, stop = {}, len(order), False
    for i, k in enumerate(order):
        stop = stop or pvalues[k] > alpha / (m - i)
        out[k] = not stop
    return out


def boot_p(values: dict[str, list[float]], seed: int, draws: int = 2000) -> float:
    """One-sided p that the mean is <= 0, by game bootstrap."""
    games = sorted(g for g, v in values.items() if v)
    if not games:
        return 1.0
    rng = random.Random(seed)
    le = 0
    for _ in range(draws):
        vals = [x for g in (rng.choice(games) for _ in games) for x in values[g]]
        le += (sum(vals) / len(vals)) <= 0
    return (le + 1) / (draws + 1)


# ---- test-fold discipline -----------------------------------------------------------
def guard_test_open(log_path: Path, current_hash: str, reopen_reason: Optional[str] = None) -> None:
    prior = []
    if log_path.exists():
        prior = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    opened = [r for r in prior if r.get("fold") == "test"]
    if reopen_reason is not None and not reopen_reason.strip():
        raise ValueError("--reopen-test requires a non-empty reason")
    if opened and opened[0].get("spec_hash") != current_hash and not reopen_reason:
        raise RuntimeError('test fold was first opened under a different spec; use --reopen-test "<reason>"')


def append_log(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(canonical(row) + "\n")


# ---- the experiment -----------------------------------------------------------------
def _game(key: str) -> str:
    for tag in (":spread:", ":total:"):
        key = key.split(tag, 1)[0]
    return key


def hypothesis_trades(samples: list[dict[str, Any]], h: int) -> dict[str, tuple[dict[str, list], dict[str, int]]]:
    """name -> (per-game per-contract returns, per-game attempts)."""
    out: dict[str, tuple[dict[str, list], dict[str, int]]] = {}
    rules = {
        "B1_buy_any": lambda s: s["kind"] == "unconditional",
        "H1_momentum": lambda s: s["kind"] == "trigger" and (s["dmid_30"] or 0) > 0,
        "H2_dip": lambda s: s["kind"] == "trigger" and (s["dmid_30"] or 0) < 0,
        "H3_leadlag": lambda s: s["kind"] == "unconditional" and s.get("venue") in EXECUTABLE and s.get("leader_dmid_30") is not None
        and abs(s["leader_dmid_30"]) >= .05 and s["leader_dmid_30"] > 0 and abs(s.get("dmid_30") or 0) < .5 * abs(s["leader_dmid_30"])
        and (s.get("gap_leader") or 0) >= .02,
    }
    for name, rule in rules.items():
        rets: dict[str, list] = defaultdict(list)
        tries: dict[str, int] = defaultdict(int)
        for s in samples:
            if s.get("venue") not in EXECUTABLE or not rule(s):
                continue
            g = _game(s["event_key"])
            tries[g] += 1
            rets[g].append(s.get(f"ret_long_{h}"))
        out[name] = (dict(rets), dict(tries))
    return out


def forecast_skill(samples: list[dict[str, Any]], h: int, feature: str, seed: int, draws: int) -> dict[str, Any]:
    """Leave-one-game-out ridge dmid_h ~ feature vs unchanged price: MAE skill by game."""
    rows = [s for s in samples if s["kind"] == "unconditional" and s.get(f"dmid_{h}_fwd") is not None and s.get(feature) is not None]
    by: dict[str, list] = defaultdict(list)
    for s in rows:
        by[_game(s["event_key"])].append((s[feature], s[f"dmid_{h}_fwd"]))
    if len(by) < 2:
        return {"n": len(rows), "games": len(by)}
    err_m: dict[str, list[float]] = {}
    err_0: dict[str, list[float]] = {}
    betas = []
    for g in by:
        train = [p for k, v in by.items() if k != g for p in v]
        a, b = ridge_fit([x for x, _ in train], [y for _, y in train])
        betas.append(b)
        err_m[g] = [abs(y - (a + b * x)) for x, y in by[g]]
        err_0[g] = [abs(y) for _, y in by[g]]
    diff = {g: [e0 - em for e0, em in zip(err_0[g], err_m[g])] for g in by}
    n = sum(len(v) for v in err_m.values())
    return {"n": n, "games": len(by), "mae_model": sum(x for v in err_m.values() for x in v) / n,
            "mae_persistence": sum(x for v in err_0.values() for x in v) / n,
            "skill_ci": game_block_bootstrap(diff, seed=seed, draws=draws), "beta_median": sorted(betas)[len(betas) // 2]}


def arb_scan(rows: list[dict[str, Any]], fee_for_row: Callable, latency_k: float, latency_rh: float, seed_n: int = 10,
             cooldown_s: float = 30.0) -> tuple[dict[str, list], dict[str, int]]:
    """H4: moments where independent executable books' all-in asks for both outcomes sum < $1."""
    from arb_engine.quant.microdata import contract_key, is_observation, observation_time
    from arb_engine.quant.paperexec import two_leg_arb

    by_event: dict[str, list] = defaultdict(list)
    for r in rows:
        if r.get("venue") in EXECUTABLE and (r.get("side") or "yes") == "yes" and is_observation(r):
            by_event[r["event_key"]].append((observation_time(r)[0], r))
    rets: dict[str, list] = defaultdict(list)
    tries: dict[str, int] = defaultdict(int)
    for ev, obs in by_event.items():
        obs.sort(key=lambda x: x[0])
        latest: dict[tuple, tuple[float, dict]] = {}
        series: dict[tuple, list] = defaultdict(list)
        for t, r in obs:
            series[contract_key(r)].append(dict(r, obs_ts=t))
        last_fire = -math.inf
        for t, r in obs:
            latest[contract_key(r)] = (t, r)
            fresh = [(k, x) for k, x in latest.items() if t - x[0] <= 6.0]
            outcomes = sorted({k[2] for k, _ in fresh})
            if len(outcomes) != 2 or t - last_fire < cooldown_s:
                continue
            best = None
            for ka, (ta, ra) in fresh:
                for kb, (tb, rb) in fresh:
                    if ka[2] != outcomes[0] or kb[2] != outcomes[1] or ka[1] == kb[1]:
                        continue
                    fa, fb = fee_for_row(ra), fee_for_row(rb)
                    if fa is None or fb is None:
                        continue
                    cost = float(ra["ask"]) + float(fb.fee(rb["ask"], seed_n, "taker")) / seed_n + float(rb["ask"]) + float(fa.fee(ra["ask"], seed_n, "taker")) / seed_n
                    if cost < 1.0 and (best is None or cost < best[0]):
                        best = (cost, ka, ra, fa, kb, rb, fb)
            if best is None:
                continue
            last_fire = t
            _, ka, ra, fa, kb, rb, fb = best
            la = latency_rh if ra.get("venue") == "robinhood" else latency_k
            lb = latency_rh if rb.get("venue") == "robinhood" else latency_k
            res = two_leg_arb([x for x in series[ka] if x["obs_ts"] > t], [x for x in series[kb] if x["obs_ts"] > t], t,
                              float(ra["ask"]), float(rb["ask"]), seed_n, fa, fb, latency_a_s=la, latency_b_s=lb)
            g = _game(ev)
            tries[g] += 1
            rets[g].append(float(res.pnl) / seed_n if res.pnl is not None and (res.matched or res.unwound) else None)
    return dict(rets), dict(tries)


def evaluate(data: dict[str, Any], spec: dict[str, Any], latency_s: float, legacy: bool) -> dict[str, Any]:
    from arb_engine.quant.microdata import _default_fee, build, settlement_values

    seed, draws = spec.get("seed", 20260920), spec.get("bootstrap", 2000)
    settle = settlement_values(data["rows"], data["finals"])
    samples = build(data["rows"], espn=data["espn"], prints=data["prints"], sample="all", settlement=settle,
                    latency_s=latency_s, entry_tol_s=2.0, ref_contracts=spec.get("ref_contracts", 10))
    report: dict[str, Any] = {"latency_s": latency_s, "legacy_timestamps": legacy, "games": len({_game(k) for k in data["event_keys"]}),
                              "samples": len(samples), "triggers": sum(1 for s in samples if s["kind"] == "trigger"),
                              "finals": len(data["finals"]), "horizons": {}}
    pvals: dict[str, float] = {}
    for h in spec.get("horizons", [5, 15, 30, 60]):
        unc = [s for s in samples if s["kind"] == "unconditional"]
        cover = sum(1 for s in unc if s.get(f"dmid_{h}_fwd") is not None) / len(unc) if unc else None
        hr: dict[str, Any] = {"coverage": cover}
        for name, (rets, tries) in hypothesis_trades(samples, h).items():
            hr[name] = summarize(rets, tries, seed=seed, draws=draws)
            if name.startswith("H"):
                pvals[f"{name}@{h}"] = boot_p({g: [x for x in v if x is not None] for g, v in rets.items()}, seed, draws)
        hr["B3_ridge_dmid30"] = forecast_skill(samples, h, "dmid_30", seed, draws)
        hr["B4_ridge_gap"] = forecast_skill(samples, h, "gap_leader", seed, draws)
        report["horizons"][str(h)] = hr
    rets, tries = arb_scan(data["rows"], _default_fee, latency_s, spec.get("manual_leg_latency_s", 15))
    report["H4_arb"] = summarize(rets, tries, seed=seed, draws=draws)
    report["holm_pass"] = holm(pvals)
    primary = spec.get("primary", "H3@30s").replace("s", "")
    report["primary"] = {"hypothesis": primary, "p_mean_le_0": pvals.get(primary.replace("H3", "H3_leadlag"))}
    return report


def synthetic_report() -> dict[str, Any]:
    fixture = json.loads((ROOT / "tests/fixtures/results/momentum_synthetic_40.json").read_text())
    skill = fixture["persistence_mean_absolute_error"] - fixture["mean_absolute_error"]
    return {"experiment": "microstructure", "candidate": "H1_momentum_prototype", "fold": "synthetic",
            "mae": round(fixture["mean_absolute_error"], 3),
            "persistence_mae": round(fixture["persistence_mean_absolute_error"], 3),
            "skill_vs_persistence": skill, "decision": "reject" if skill <= 0 else "continue",
            "note": "price-forecast diagnostic only; no fills or P&L"}


def _round(x: Any) -> Any:
    if isinstance(x, float):
        return round(x, 5) if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _round(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_round(v) for v in x]
    return x


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Frozen, game-blocked microstructure evaluation (read-only; no alerts, no orders).")
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--fold", choices=("discovery", "validation", "test", "synthetic"), default="synthetic")
    ap.add_argument("--db", default=str(ROOT / "out/history.db"), help="recorder database (opened read-only)")
    ap.add_argument("--latency", type=float, help="order latency in seconds (default: spec; legacy 5 s data uses legacy_latency_s)")
    ap.add_argument("--reopen-test")
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--results", type=Path)
    args = ap.parse_args(argv)
    manifest = json.loads(args.manifest.read_text()) if args.manifest.exists() else {"discovery": [], "test_from": "2026-10-08"}
    validate_manifest(manifest)
    spec = manifest.get("spec") or {"version": 2, "primary": "H3@30s", "horizons": [5, 15, 30, 60], "bootstrap": 2000, "seed": 20260920}
    digest = spec_hash(spec)
    if args.fold == "test":
        guard_test_open(args.log, digest, args.reopen_test)
    if args.fold == "synthetic":
        report = synthetic_report()
    else:
        from arb_engine.quant.microdata import load_db
        import sqlite3

        con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        keys = [r[0] for r in con.execute("select distinct event_key from inplay_ticks where l1_json is not null and event_key like 'nfl:%'")]
        con.close()
        keys = sorted(k for k in keys if fold_of(manifest, k) == args.fold and ":spread:" not in k and ":total:" not in k)
        if not keys:
            report = {"experiment": "microstructure", "fold": args.fold, "spec_hash": digest, "status": "no-recorded-games"}
        else:
            data = load_db(args.db, event_keys=keys)
            legacy = all(r.get("approx_time") for r in data["rows"][:2000])
            latency = args.latency if args.latency is not None else (spec.get("legacy_latency_s", 5) if legacy else (spec.get("latencies_s") or [1])[0])
            report = {"experiment": "microstructure", "fold": args.fold, "spec_hash": digest, **evaluate(data, spec, latency, legacy)}
            if args.fold == "discovery":
                report["caveat"] = ("discovery games designed the LAG rule: descriptive, not evidence; "
                                    "legacy 5 s timestamps" if legacy else "discovery games designed the LAG rule: descriptive, not evidence")
    append_log(args.log, {"fold": args.fold, "spec_hash": digest, "reopen_reason": args.reopen_test})
    report = _round(report)
    if args.results:
        args.results.parent.mkdir(parents=True, exist_ok=True)
        args.results.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

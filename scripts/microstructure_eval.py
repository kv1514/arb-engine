#!/usr/bin/env python3
"""Frozen, game-blocked evaluation of H1 momentum / H2 dip-recovery / H3 lead-lag / H4 arb.

    python3 scripts/microstructure_eval.py --db out/history.db --fold discovery [--results FILE] [--freeze FILE]
    python3 scripts/microstructure_eval.py --db out/history.db --fold validation [--freeze FILE]
    python3 scripts/microstructure_eval.py --db out/history.db --fold test --frozen FILE [--reopen-test "why"]
    python3 scripts/microstructure_eval.py --fold synthetic

Reads recorded ticks read-only (quant/microdata.load_db); never talks to a venue, never
creates an alert or an order. Folds are whole games with all their markets
(tests/fixtures/microstructure/manifest.json): *discovery* = every NFL game dated
2026-09-20/21 (``discovery_dates``; the enumerated list is the record, the dates are the
rule), *validation* = later games before ``test_from``, *test* = games dated on or after
``test_from`` (the verified week-5 kickoff), defined by rule before any was recorded.

Candidates, each scored as IOC paper orders (quant/paperexec via quant/microdata: the ask at
decision time as the limit, the book met after the latency, displayed size x haircut, sold to
the bid at h, both fees; missed, filled, closed, settled and unresolved counted apart):

* B0 persistence: the price does not move (the forecast every model must beat).
* B1 cost hurdle: a buy at every unconditional sample.
* B2 sign rules: H1 momentum (buy an up ``trigger``), H2 dip (buy a down ``trigger``), H2
  recovery (buy an observed bounce, microdata ``recovery``), H3 LAG (buy the follower when an
  independent fresh book moved >= 5c in 30 s, the follower < half as much, the leader's mid
  >= 2c above), and M_prototype (strategy/momentum.MomentumTracker at its defaults: buy when
  it says ``rising``). Unconditional-sample rules fire once per contract per 60 s.
* B3 ridge dmid_h ~ dmid_30 and B4 ridge dmid_h ~ leader gap: fitted chronologically (the
  earlier games of the fold, or the earlier folds) and frozen before they are scored; the
  test fold only ever reads a frozen artifact (``--frozen``), never refits.
* H4 arbitrage: executable books whose all-in asks for both outcomes sum below $1; each leg
  meets its own book after its own latency; scored win case, tie case and worst case.

Uncertainty: whole-game block bootstrap (2000 draws, fixed seed). H3 at 30 s is primary; the
secondary family (``spec.secondary``) is Holm-corrected. Every run appends an audit record
(the hash of the complete effective spec: manifest spec, fold policy, sampling constants,
code and frozen models) to out/eval_log.jsonl; a test run under a spec other than the one
the test fold was first opened with needs ``--reopen-test "<reason>"`` and is reported as
exploratory.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import random
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_MANIFEST = ROOT / "tests/fixtures/microstructure/manifest.json"
DEFAULT_LOG = ROOT / "out/eval_log.jsonl"
DEFAULT_FROZEN = ROOT / "out/micro_frozen_models.json"
EXECUTABLE = ("kalshi", "robinhood")
CODE_FILES = ("arb_engine/quant/microdata.py", "arb_engine/quant/paperexec.py", "arb_engine/strategy/momentum.py",
              "scripts/microstructure_eval.py")
DEFAULT_SPEC: dict[str, Any] = {"version": 3, "primary": "H3@30s", "horizons": [5, 15, 30, 60], "bootstrap": 2000, "seed": 20260920}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def spec_hash(spec: Any) -> str:
    return hashlib.sha256(canonical(spec).encode()).hexdigest()


def _game(key: str) -> str:
    for tag in (":spread:", ":total:"):
        key = key.split(tag, 1)[0]
    return key


def _date(key: str) -> str:
    return _game(key).rsplit(":", 1)[-1]


def _enum(manifest: dict[str, Any], fold: str) -> set[str]:
    return {g["event_key"] if isinstance(g, dict) else str(g) for g in manifest.get(fold, []) or []}


def validate_manifest(manifest: dict[str, Any]) -> None:
    seen: dict[str, str] = {}
    for fold in ("discovery", "validation", "test"):
        for key in _enum(manifest, fold):
            if key in seen:
                raise ValueError(f"game {key} occurs in both {seen[key]} and {fold}")
            seen[key] = fold
    test_from = manifest.get("test_from", "2026-10-08")
    for key in _enum(manifest, "discovery"):
        if _date(key) >= test_from:
            raise ValueError(f"discovery game {key} is on or after test_from {test_from}")
    for d in manifest.get("discovery_dates") or []:
        if d >= test_from:
            raise ValueError(f"discovery date {d} is on or after test_from {test_from}")
    kick = manifest.get("test_kickoff")
    if kick and kick.get("et_date") and kick["et_date"] != test_from:
        raise ValueError(f"test_from {test_from} is not the verified kickoff's date {kick['et_date']}")


def fold_of(manifest: dict[str, Any], event_key: str) -> str:
    """Whole-game fold for any recorded key (lines follow their game). Discovery is the
    enumerated games *and* every game on a discovery date (a game missing from the list
    stays discovery); test is every game dated on or after ``test_from``."""
    game = _game(event_key)
    if game in _enum(manifest, "discovery") or _date(game) in set(manifest.get("discovery_dates") or []):
        return "discovery"
    return "test" if _date(game) >= manifest.get("test_from", "2026-10-08") else "validation"


def fold_policy(manifest: dict[str, Any]) -> dict[str, Any]:
    return {"discovery": sorted(_enum(manifest, "discovery")), "discovery_dates": sorted(manifest.get("discovery_dates") or []),
            "test_from": manifest.get("test_from", "2026-10-08"), "test_kickoff": manifest.get("test_kickoff"),
            "rule": "whole games with all their markets; discovery by list or date; test = ET date >= test_from; else validation"}


def effective_spec(manifest: dict[str, Any], frozen: Optional[dict[str, Any]] = None, root: Path = ROOT) -> dict[str, Any]:
    """Everything that decides a result: the manifest spec, the fold policy, the sampling
    constants, the code that samples / executes / scores, and the frozen models."""
    from arb_engine.quant.microdata import effective_constants

    code = {f: hashlib.sha256((root / f).read_bytes()).hexdigest() for f in CODE_FILES if (root / f).exists()}
    return {"spec": manifest.get("spec") or DEFAULT_SPEC, "folds": fold_policy(manifest), "constants": effective_constants(),
            "code": code, "frozen_models": spec_hash(frozen) if frozen else None}


def component_hashes(eff: dict[str, Any]) -> dict[str, Any]:
    return {k: (spec_hash(v) if v is not None and not isinstance(v, str) else v) for k, v in eff.items()}


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


def _concentration(per_game: dict[str, float]) -> Optional[float]:
    tot = sum(abs(v) for v in per_game.values())
    return max(abs(v) for v in per_game.values()) / tot if tot else None


def _drawdown(chrono: Iterable[float]) -> float:
    eq = peak = dd = 0.0
    for x in chrono:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dd


def summarize(trades: dict[str, list[Optional[float]]], attempts: dict[str, int], seed: int = 20260920, draws: int = 2000,
              times: Optional[dict[str, list[float]]] = None) -> dict[str, Any]:
    """Per-contract net returns by game (``None`` = missed or unresolved: no return) -> metrics.
    ``resolved_rate`` is completed trades / attempts - a resolution rate, not a fill rate
    (``candidate_metrics`` counts fills). Drawdown follows ``times`` (decision times per game,
    aligned with ``trades``) when given, else games in kickoff order is unknown and it falls
    back to each game's total in key order."""
    done = {g: [x for x in xs if x is not None] for g, xs in trades.items()}
    n = sum(len(v) for v in done.values())
    tried = sum(attempts.values())
    out: dict[str, Any] = {"attempts": tried, "trades": n, "games": sum(1 for v in done.values() if v),
                           "resolved_rate": (n / tried) if tried else None}
    if not n:
        return out
    per_game = {g: sum(v) for g, v in done.items() if v}
    out["mean_ret"] = game_block_bootstrap(done, seed=seed, draws=draws)
    out["hit_rate"] = sum(1 for v in done.values() for x in v if x > 0) / n
    out["positive_game_share"] = sum(1 for v in per_game.values() if v > 0) / len(per_game)
    out["top_game_share"] = _concentration(per_game)
    if times:
        chrono = sorted((t, x) for g, xs in trades.items() for t, x in zip(times.get(g, []), xs) if x is not None)
        out["max_drawdown_per_contract"] = _drawdown(x for _, x in chrono)
    else:
        out["max_drawdown_per_contract"] = _drawdown(per_game[g] for g in sorted(per_game))
    return out


def decision(metrics: dict[str, Any], rules: Optional[dict[str, Any]] = None) -> str:
    """Pre-registered rules (docs/MODEL.md, "Microstructure experiment").

    reject      skill CI includes zero or lies below it
    shadow      skill ok but net P&L CI includes zero (or below), fill rate < 60 %, < 30 games,
                < 200 deduplicated decisions, or one game > 40 % of |P&L|
    alert-only  shadow passed and the stressed run (3 s latency, haircut 0.5, both fees) keeps
                the P&L CI above 0 with >= 60 % of games positive. Eligibility only: this
                report creates no alert.
    """
    r = {"min_fill_rate": .60, "min_games": 30, "min_decisions": 200, "max_game_share": .40, "min_positive_games": .60, **(rules or {})}
    skill = metrics.get("skill_ci") or {}
    pnl = metrics.get("net_pnl_ci") or {}
    lo = skill.get("lo")
    if lo is None or not math.isfinite(lo) or lo <= 0:
        return "reject"
    plo = pnl.get("lo")
    if (plo is None or not math.isfinite(plo) or plo <= 0 or (metrics.get("fill_rate") or 0) < r["min_fill_rate"]
            or metrics.get("test_games", 0) < r["min_games"] or metrics.get("deduped_triggers", 0) < r["min_decisions"]
            or (metrics.get("top_game_share") if metrics.get("top_game_share") is not None else 1) > r["max_game_share"]):
        return "shadow"
    robust = metrics.get("robust_l3_h05") or {}
    rlo = (robust.get("net_pnl_ci") or {}).get("lo")
    if rlo is not None and math.isfinite(rlo) and rlo > 0 and (robust.get("positive_game_share") or 0) >= r["min_positive_games"]:
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
def _log_rows(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


def guard_test_open(log_path: Path, current_hash: str, reopen_reason: Optional[str] = None) -> bool:
    """Refuse a test run under a spec other than the one the test fold was first opened with,
    unless a non-empty reason is given. Returns True when the run is exploratory (the test
    spec changed, or the run was explicitly reopened)."""
    if reopen_reason is not None and not reopen_reason.strip():
        raise ValueError("--reopen-test requires a non-empty reason")
    opened = [r for r in _log_rows(log_path) if r.get("fold") == "test"]
    changed = bool(opened) and opened[0].get("spec_hash") != current_hash
    if changed and not reopen_reason:
        raise RuntimeError('test fold was first opened under a different spec; use --reopen-test "<reason>"')
    return changed or bool(reopen_reason)


def append_log(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(canonical(row) + "\n")


def _git_head() -> Optional[str]:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5).stdout.strip() or None
    except Exception:
        return None


# ---- candidates ---------------------------------------------------------------------
def _h3(s: dict[str, Any]) -> bool:
    return (s["kind"] == "unconditional" and s.get("leader_dmid_30") is not None and abs(s["leader_dmid_30"]) >= .05
            and s["leader_dmid_30"] > 0 and abs(s.get("dmid_30") or 0) < .5 * abs(s["leader_dmid_30"]) and (s.get("gap_leader") or 0) >= .02)


RULES: dict[str, tuple[Callable[[dict[str, Any]], bool], bool]] = {   # name -> (rule, one per contract per cooldown)
    "B1_buy_any": (lambda s: s["kind"] == "unconditional", False),
    "H1_momentum": (lambda s: s["kind"] == "trigger" and (s["dmid_30"] or 0) > 0, False),
    "H2_dip": (lambda s: s["kind"] == "trigger" and (s["dmid_30"] or 0) < 0, False),
    "H2_recovery": (lambda s: s["kind"] == "recovery", False),
    "H3_leadlag": (_h3, True),
    "M_prototype": (lambda s: s["kind"] == "unconditional" and s.get("momentum_status") == "rising", True),
}


def select(samples: list[dict[str, Any]], rule: Callable[[dict[str, Any]], bool], cooldown_s: float = 0.0) -> list[dict[str, Any]]:
    """Executable decision points a rule fires on, once per contract per ``cooldown_s`` (a
    condition that persists for a minute is one decision, not twelve)."""
    last: dict[tuple, float] = {}
    out = []
    for s in sorted(samples, key=lambda x: x["t"]):
        if s.get("venue") not in EXECUTABLE or not rule(s):
            continue
        k = (s["event_key"], s["book_id"], s["outcome"], s["side"])
        if cooldown_s and s["t"] - last.get(k, -math.inf) < cooldown_s:
            continue
        last[k] = s["t"]
        out.append(s)
    return out


def candidate_metrics(sel: list[dict[str, Any]], h: int, seed: int, draws: int) -> dict[str, Any]:
    """Orders, fills, positions, labels, skill and P&L for one candidate at horizon h."""
    ex = [(s, s.get(f"exec_{h}") or {}) for s in sel]
    st = Counter(e.get("status") for _, e in ex)
    filled = [(s, e) for s, e in ex if (e.get("filled") or 0) > 0]
    resolved = [(s, e) for s, e in filled if e.get("pnl") is not None]
    out: dict[str, Any] = {
        "attempted_orders": len(ex), "filled_orders": len(filled), "filled_contracts": sum(e["filled"] for _, e in filled),
        "missed_orders": st.get("missed", 0), "closed_positions": st.get("closed", 0), "settled_positions": st.get("settled", 0),
        "unresolved_positions": st.get("unresolved", 0), "fill_rate": len(filled) / len(ex) if ex else None,
        "resolved_share_of_fills": len(resolved) / len(filled) if filled else None,
        "games": len({_game(s["event_key"]) for s in sel}), "fees": sum(e.get("fees") or 0.0 for _, e in resolved),
    }
    lab: dict[str, list[float]] = defaultdict(list)
    for s in sel:
        y = s.get(f"dmid_{h}_fwd")
        if y is not None:
            lab[_game(s["event_key"])].append(y)
    n_lab = sum(len(v) for v in lab.values())
    out["labelled"], out["missing_labels"] = n_lab, len(sel) - n_lab
    if n_lab:
        # A buy rule forecasts "up": its skill is the mean forward mid move it bought into.
        out["skill_ci"] = game_block_bootstrap(dict(lab), seed=seed, draws=draws)
        moved = [y for v in lab.values() for y in v if y != 0]
        out["directional_hit"] = sum(1 for y in moved if y > 0) / len(moved) if moved else None
    if not resolved:
        return out
    per_game_ret: dict[str, list[float]] = defaultdict(list)
    per_game_usd: dict[str, float] = defaultdict(float)
    for s, e in resolved:
        g = _game(s["event_key"])
        per_game_ret[g].append(e["pnl"] / e["filled"])
        per_game_usd[g] += e["pnl"]
    out["mean_ret"] = game_block_bootstrap(dict(per_game_ret), seed=seed, draws=draws)
    out["dollar_pnl"] = sum(per_game_usd.values())
    out["hit_rate"] = sum(1 for _, e in resolved if e["pnl"] > 0) / len(resolved)
    out["positive_game_share"] = sum(1 for v in per_game_usd.values() if v > 0) / len(per_game_usd)
    out["top_game_share"] = _concentration(dict(per_game_usd))
    out["max_drawdown_usd"] = _drawdown(e["pnl"] for s, e in sorted(resolved, key=lambda x: x[0]["t"]))
    return out


def decision_inputs(m: dict[str, Any], robust: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {"skill_ci": m.get("skill_ci"), "net_pnl_ci": m.get("mean_ret"), "fill_rate": m.get("fill_rate"),
            "test_games": m.get("games", 0), "deduped_triggers": m.get("attempted_orders", 0), "top_game_share": m.get("top_game_share"),
            "robust_l3_h05": {"net_pnl_ci": (robust or {}).get("mean_ret"), "positive_game_share": (robust or {}).get("positive_game_share")}}


# ---- forecasts (B0 / B3 / B4 / the momentum prototype) --------------------------------
def forecast_pairs(samples: list[dict[str, Any]], h: int, feature: str) -> list[tuple[str, float, float, float]]:
    """(game, t, x, y) on unconditional samples with the feature and a forward label."""
    return [(_game(s["event_key"]), s["t"], float(s[feature]), float(s[f"dmid_{h}_fwd"])) for s in samples
            if s["kind"] == "unconditional" and s.get(f"dmid_{h}_fwd") is not None and s.get(feature) is not None]


def chrono_split(samples: list[dict[str, Any]], share: float = 2 / 3) -> tuple[list[str], list[str]]:
    """Games in kickoff order (first decision time); the first ``share`` train, the rest score."""
    first: dict[str, float] = {}
    for s in samples:
        g = _game(s["event_key"])
        first[g] = min(first.get(g, math.inf), s["t"])
    order = sorted(first, key=lambda g: (first[g], g))
    k = max(1, min(len(order) - 1, int(math.ceil(share * len(order))))) if len(order) > 1 else len(order)
    return order[:k], order[k:]


def fit_models(samples: list[dict[str, Any]], horizons: Iterable[int]) -> dict[str, Any]:
    coef: dict[str, dict[str, list[float]]] = {"B3_ridge_dmid30": {}, "B4_ridge_gap": {}}
    for h in horizons:
        for name, feat in (("B3_ridge_dmid30", "dmid_30"), ("B4_ridge_gap", "gap_leader")):
            p = forecast_pairs(samples, h, feat)
            coef[name][str(h)] = list(ridge_fit([x for _, _, x, _ in p], [y for _, _, _, y in p]))
    return coef


def score_forecast(pairs: list[tuple[str, float, float, float]], predict: Callable[[float], float], seed: int, draws: int) -> dict[str, Any]:
    """MAE vs unchanged price (skill = persistence MAE - model MAE, by game), directional hit
    and calibration (realised on predicted: slope 1 = calibrated; quintile bins)."""
    if not pairs:
        return {"n": 0, "games": 0}
    by: dict[str, list[float]] = defaultdict(list)
    em = e0 = 0.0
    pp = []
    for g, _, x, y in pairs:
        f = predict(x)
        pp.append((f, y))
        em += abs(y - f)
        e0 += abs(y)
        by[g].append(abs(y) - abs(y - f))
    n = len(pairs)
    moved = [(f, y) for f, y in pp if y != 0 and f != 0]
    mf, my = sum(f for f, _ in pp) / n, sum(y for _, y in pp) / n
    vf = sum((f - mf) ** 2 for f, _ in pp)
    slope = sum((f - mf) * (y - my) for f, y in pp) / vf if vf > 1e-18 else None
    bins = []
    srt = sorted(pp)
    for i in range(5):
        chunk = srt[i * n // 5:(i + 1) * n // 5]
        if chunk:
            bins.append([sum(f for f, _ in chunk) / len(chunk), sum(y for _, y in chunk) / len(chunk), len(chunk)])
    return {"n": n, "games": len(by), "mae_model": em / n, "mae_persistence": e0 / n, "skill_ci": game_block_bootstrap(dict(by), seed=seed, draws=draws),
            "directional_hit": sum(1 for f, y in moved if f * y > 0) / len(moved) if moved else None,
            "calibration": {"slope": slope, "bins": bins}}


def forecast_skill(samples: list[dict[str, Any]], h: int, feature: str, seed: int, draws: int,
                   coef: Optional[list[float]] = None, train: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """Chronological: coefficients from ``coef`` (frozen) or fitted on ``train`` (earlier data),
    scored on ``samples``. Never fitted on the samples it scores."""
    if coef is None:
        p = forecast_pairs(train or [], h, feature)
        coef = list(ridge_fit([x for _, _, x, _ in p], [y for _, _, _, y in p])) if p else [0.0, 0.0]
    a, b = coef
    out = score_forecast(forecast_pairs(samples, h, feature), lambda x: a + b * x, seed, draws)
    out["coef"] = [a, b]
    return out


def momentum_forecasts(rows: list[dict[str, Any]]) -> dict[tuple, dict[str, Any]]:
    """strategy/momentum.MomentumTracker at its defaults, fed every deduplicated observation in
    time order (one tracker per game): (event, book, outcome, side, t) -> its output."""
    from arb_engine.models import OutcomeQuote
    from arb_engine.quant.microdata import _dedupe, contract_key
    from arb_engine.strategy.momentum import MomentumTracker

    trackers: dict[str, MomentumTracker] = {}
    out: dict[tuple, dict[str, Any]] = {}
    for t, _, r in _dedupe(rows):
        k = contract_key(r)
        if k[3] != "yes":
            continue
        tr = trackers.setdefault(k[0], MomentumTracker())
        q = OutcomeQuote(str(r.get("venue")), str(r.get("venue_market_id") or ""), k[0], k[2], bid=float(r["bid"]), ask=float(r["ask"]),
                         bid_size=r.get("bid_size"), ask_size=r.get("ask_size"), ts=t, quote_time=None, meta={"side": "yes"}, book_id=k[1])
        for f in tr.observe(k[0], {q.venue: [q]}, t):
            out[(k[0], f["book_id"], f["outcome"], "yes", t)] = f
    return out


def momentum_forecast_score(fc: dict[tuple, dict[str, Any]], rows: list[dict[str, Any]], samples: list[dict[str, Any]],
                            seed: int, draws: int) -> dict[str, Any]:
    """The prototype's own projection (``projected_mid`` over its ``horizon_s`` <= 10 s) against
    the first refreshed mark in [t+H, t+H+max(1, 0.2H)], at the unconditional samples."""
    from arb_engine.quant.microdata import _mid, series_by_contract

    series = series_by_contract(rows)
    times = {k: [r["obs_ts"] for r in v] for k, v in series.items()}
    pairs = []
    for s in samples:
        if s["kind"] != "unconditional":
            continue
        f = fc.get((s["event_key"], s["book_id"], s["outcome"], s["side"], s["t"]))
        if not f:
            continue
        k = (s["event_key"], s["book_id"], s["outcome"], s["side"])
        H = float(f["horizon_s"])
        ts = times.get(k, [])
        i = bisect.bisect_left(ts, s["t"] + H)
        if i < len(ts) and ts[i] <= s["t"] + H + max(1.0, 0.2 * H):
            pairs.append((_game(s["event_key"]), s["t"], float(f["projected_mid"]) - float(f["mid"]), _mid(series[k][i]) - float(f["mid"])))
    out = score_forecast(pairs, lambda x: x, seed, draws)
    out["statuses"] = dict(Counter(f["status"] for f in fc.values()))
    return out


# ---- H3 extras, H4 ----------------------------------------------------------------------
def h3_lock_trades(samples: list[dict[str, Any]], rows: list[dict[str, Any]], fee_for_row: Callable, latency_s: float,
                   watch_s: float = 600.0, n: int = 10, tie_safe: bool = True, settle: Optional[dict] = None,
                   cooldown_s: float = 0.0) -> dict[str, Any]:
    """H3 + lock (strategy/laglock.py replayed): buy the follower on an H3 signal (IOC at the
    decision ask after the latency, fees in); then for ``watch_s`` watch the other outcome on
    the executable venues and lock with the first observation whose all-in makes the pair
    cost <= $1 *and* whose own IOC (same latency) fills - tie-safe pairs only when asked. An
    unlocked position is sold to the bid at the end of the watch (or settled). Returns the
    per-game per-contract P&L plus the lock conversion and the locked-only P&L."""
    from arb_engine.quant.microdata import series_by_contract, tie_value
    from arb_engine.quant.paperexec import ioc_entry, ioc_round_trip

    series = series_by_contract(rows)
    rets: dict[str, list] = defaultdict(list)
    tries: dict[str, int] = defaultdict(int)
    locked_only: dict[str, list] = defaultdict(list)
    hold: dict[str, list] = defaultdict(list)   # the same entries held for watch_s, never locked: the fair baseline
    waits: list[float] = []
    n_filled = n_locked = 0
    for s in select(samples, _h3, cooldown_s):
        g = _game(s["event_key"])
        tries[g] += 1
        key = (s["event_key"], s["book_id"], s["outcome"], s["side"])
        mine = [r for r in series.get(key, []) if r["obs_ts"] > s["t"]]
        fee = fee_for_row(mine[0]) if mine else None
        if fee is None:
            rets[g].append(None)
            continue
        entry, erow = ioc_entry(mine, s["t"], s["ask"], n, fee, latency_s)
        if entry.missed:
            rets[g].append(None)
            continue
        n_filled += 1
        sv = (settle or {}).get((s["event_key"], s["outcome"], s["side"]))
        hold[g].append(ioc_round_trip(mine, s["t"], s["ask"], n, fee, latency_s, horizon_s=watch_s, settlement=sv).pnl_per_contract)
        t_in = erow["obs_ts"]
        entry_all_in = (entry.entry_price * entry.filled + float(entry.entry_fee)) / entry.filled
        etie = tie_value(erow)
        comp_keys = [k for k in series if k[0] == s["event_key"] and k[2] != s["outcome"] and k[3] == "yes"]
        stream = sorted((r["obs_ts"], k, r) for k in comp_keys for r in series[k]
                        if t_in < r["obs_ts"] <= t_in + watch_s and r.get("venue") in EXECUTABLE)
        done = None
        for tt, k, r in stream:
            ask = r.get("ask")
            cfee = fee_for_row(r)
            if ask is None or cfee is None or (r.get("ask_size") is not None and float(r["ask_size"]) < entry.filled):
                continue
            c_all_in = float(ask) + float(cfee.fee(ask, entry.filled, "taker")) / entry.filled
            if entry_all_in + c_all_in > 1.0:
                continue
            if tie_safe:
                ct = tie_value(r)
                if etie is None or ct is None or etie + ct < 1.0 - 1e-9:
                    continue
            leg, lrow = ioc_entry([x for x in series[k] if x["obs_ts"] > tt], tt, float(ask), entry.filled, cfee, latency_s)
            if leg.missed or leg.filled < entry.filled:
                continue
            got = (leg.entry_price * leg.filled + float(leg.entry_fee)) / leg.filled
            done = 1.0 - entry_all_in - got
            waits.append(lrow["obs_ts"] - t_in)
            break
        if done is not None:
            n_locked += 1
            rets[g].append(done)
            locked_only[g].append(done)
            continue
        rets[g].append(hold[g][-1])
    return {"rets": dict(rets), "tries": dict(tries), "locked_only": dict(locked_only), "hold": dict(hold), "filled": n_filled, "locked": n_locked,
            "median_seconds_to_lock": sorted(waits)[len(waits) // 2] if waits else None}


def h3_by_grade(samples: list[dict[str, Any]], h: int, seed: int, draws: int, cooldown_s: float = 0.0) -> dict[str, Any]:
    """H3 split by the grades logged live: hard vs soft lag, agreement vs none."""
    groups = {"hard": lambda s: s.get("hard_lag") is True, "soft": lambda s: s.get("hard_lag") is False,
              "agree>=1": lambda s: (s.get("agree") or 0) >= 1, "agree=0": lambda s: (s.get("agree") or 0) == 0}
    sel = select(samples, _h3, cooldown_s)
    return {name: candidate_metrics([s for s in sel if keep(s)], h, seed, draws) for name, keep in groups.items()}


def arb_scan(rows: list[dict[str, Any]], fee_for_row: Callable, latency_k: float, latency_rh: float, seed_n: int = 10,
             cooldown_s: float = 30.0, haircut: float = 1.0) -> list[dict[str, Any]]:
    """H4: moments where independent executable books' all-in asks for both outcomes sum < $1
    (each quote fresh: <= 2 s on the fast-lane venues, <= 6 s elsewhere). Each leg meets its
    own book after its own latency; the win-case, tie-case and worst-case P&L are recorded
    (a pair whose tie payout is unknown is excluded, not guessed)."""
    from arb_engine.quant.microdata import TIE_PRIOR, contract_key, fresh_limit, is_observation, observation_time, tie_value
    from arb_engine.quant.paperexec import two_leg_arb

    by_event: dict[str, list] = defaultdict(list)
    for r in rows:
        if r.get("venue") in EXECUTABLE and is_observation(r):
            by_event[r["event_key"]].append((observation_time(r)[0], r))
    records: list[dict[str, Any]] = []
    for ev, obs in by_event.items():
        obs.sort(key=lambda x: x[0])
        latest: dict[tuple, tuple[float, dict]] = {}
        series: dict[tuple, list] = defaultdict(list)
        for t, r in obs:
            series[contract_key(r)].append(dict(r, obs_ts=t))
        last_fire = -math.inf
        for t, r in obs:
            latest[contract_key(r)] = (t, r)
            fresh = [(k, x) for k, x in latest.items() if t - x[0] <= fresh_limit(x[1])]
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
                    cost = float(ra["ask"]) + float(fa.fee(ra["ask"], seed_n, "taker")) / seed_n + float(rb["ask"]) + float(fb.fee(rb["ask"], seed_n, "taker")) / seed_n
                    if cost < 1.0 and (best is None or cost < best[0]):
                        best = (cost, ka, ra, fa, kb, rb, fb)
            if best is None:
                continue
            last_fire = t
            cost, ka, ra, fa, kb, rb, fb = best
            la = latency_rh if ra.get("venue") == "robinhood" else latency_k
            lb = latency_rh if rb.get("venue") == "robinhood" else latency_k
            ties = (tie_value(ra), tie_value(rb))
            res = two_leg_arb([x for x in series[ka] if x["obs_ts"] > t], [x for x in series[kb] if x["obs_ts"] > t], t,
                              float(ra["ask"]), float(rb["ask"]), seed_n, fa, fb, latency_a_s=la, latency_b_s=lb, haircut=haircut,
                              tie_payouts=ties)
            legs_filled = sum(1 for l in res.legs if l.filled)
            win = float(res.pnl) / seed_n if res.pnl is not None and (res.matched or res.unwound) else None
            tie = float(res.pnl_tie) / seed_n if res.pnl_tie is not None and (res.matched or res.unwound) else None
            p_tie = TIE_PRIOR.get(ev.split(":", 1)[0].lower(), 0.0)
            records.append({"game": _game(ev), "t": t, "margin": 1.0 - cost, "tie_safe": (sum(ties) >= 1.0 - 1e-9) if None not in ties else None,
                            "excluded": res.excluded or None, "legs_filled": legs_filled, "matched": res.matched, "unwound": res.unwound,
                            "unresolved": res.unresolved, "pnl_win": win, "pnl_tie": tie,
                            "pnl_worst": min(win, tie) if win is not None and tie is not None else win,
                            "pnl_ev": (1 - p_tie) * win + p_tie * tie if win is not None and tie is not None else win})
    return records


def arb_metrics(records: list[dict[str, Any]], seed: int, draws: int, field: str = "pnl_worst") -> dict[str, Any]:
    """Two-leg accounting: attempts, both legs filled (a locked set), one leg (unwound), none,
    unresolved; P&L per contract set on ``field`` (worst case = win or tie, the guaranteed)."""
    rets: dict[str, list] = defaultdict(list)
    tries: dict[str, int] = defaultdict(int)
    times: dict[str, list] = defaultdict(list)
    for r in records:
        tries[r["game"]] += 1
        rets[r["game"]].append(r[field])
        times[r["game"]].append(r["t"])
    out = summarize(dict(rets), dict(tries), seed=seed, draws=draws, times=dict(times))
    out.update({"excluded_unknown_tie": sum(1 for r in records if r["excluded"]), "both_legs_filled": sum(1 for r in records if r["legs_filled"] == 2),
                "one_leg_filled": sum(1 for r in records if r["legs_filled"] == 1), "no_leg_filled": sum(1 for r in records if r["legs_filled"] == 0 and not r["excluded"]),
                "locked_sets": sum(r["matched"] for r in records), "unwound_contracts": sum(r["unwound"] for r in records),
                "unresolved_contracts": sum(r["unresolved"] for r in records)})
    return out


# ---- the experiment -----------------------------------------------------------------
def _spec_with_defaults(spec: dict[str, Any]) -> dict[str, Any]:
    return {**DEFAULT_SPEC, "signal_cooldown_s": 60, "robust": {"latency_s": 3, "haircut": 0.5}, "manual_leg_latency_s": 15,
            "train_share": 2 / 3, "ref_contracts": 10, "secondary": [], "alpha": 0.10, **(spec or {})}


def evaluate(data: dict[str, Any], spec: dict[str, Any], latency_s: float, legacy: bool, fold: str = "discovery",
             frozen: Optional[dict[str, Any]] = None, train_samples: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """One fold. ``frozen`` = coefficients fixed before this fold (the test fold's only
    option); ``train_samples`` = earlier data to fit on (validation); neither = the fold's own
    earlier games train and its later games are scored (discovery)."""
    from arb_engine.quant.microdata import _default_fee, build, settlement_values

    spec = _spec_with_defaults(spec)
    seed, draws = spec["seed"], spec["bootstrap"]
    cd = float(spec["signal_cooldown_s"])
    horizons = spec["horizons"]
    settle = settlement_values(data["rows"], data["finals"])
    samples = build(data["rows"], espn=data["espn"], prints=data["prints"], sample="all", settlement=settle, horizons=horizons,
                    latency_s=latency_s, entry_tol_s=2.0, ref_contracts=spec["ref_contracts"])
    rob = spec["robust"]
    robust = build(data["rows"], sample="all", settlement=settle, horizons=horizons, latency_s=max(float(rob["latency_s"]), 0.0),
                   entry_tol_s=2.0, ref_contracts=spec["ref_contracts"], haircut=float(rob["haircut"]))
    fc = momentum_forecasts(data["rows"])
    for ss in (samples, robust):
        for s in ss:
            f = fc.get((s["event_key"], s["book_id"], s["outcome"], s["side"], s["t"]))
            s["momentum_status"] = f["status"] if f else None
    kinds = Counter(s["kind"] for s in samples)
    report: dict[str, Any] = {"latency_s": latency_s, "legacy_timestamps": legacy, "games": len({_game(k) for k in data["event_keys"]}),
                              "samples": len(samples), "decision_points": dict(kinds), "triggers": kinds.get("trigger", 0),
                              "finals": len(data["finals"]), "horizons": {},
                              "baselines": {"B0": "B0_persistence (unchanged price)", "B1": "B1_buy_any (cost hurdle)",
                                            "B2": ["H1_momentum", "H2_dip", "H2_recovery", "H3_leadlag", "M_prototype"],
                                            "B3": "B3_ridge_dmid30", "B4": "B4_ridge_gap"}}
    # Forecast models: frozen, else fitted on earlier data only.
    if frozen is not None:
        coef, fit_note, scored = frozen["coef"], {"from": "frozen", "trained_on": frozen.get("trained_on")}, samples
    elif train_samples is not None:
        coef, fit_note, scored = fit_models(train_samples, horizons), {"from": "earlier folds", "trained_on": sorted({_game(s["event_key"]) for s in train_samples})}, samples
    else:
        tr, sc = chrono_split(samples, float(spec["train_share"]))
        trs, scs = set(tr), set(sc)
        coef = fit_models([s for s in samples if _game(s["event_key"]) in trs], horizons)
        fit_note, scored = {"from": "earlier games of this fold", "trained_on": tr, "scored_on": sc}, [s for s in samples if _game(s["event_key"]) in scs]
    report["model_fit"] = {**fit_note, "coef": coef}
    pvals: dict[str, float] = {}
    decisions: dict[str, Any] = {}
    for h in horizons:
        unc = [s for s in samples if s["kind"] == "unconditional"]
        cover = sum(1 for s in unc if s.get(f"dmid_{h}_fwd") is not None) / len(unc) if unc else None
        hr: dict[str, Any] = {"coverage": cover, "missing_labels_unconditional": sum(1 for s in unc if s.get(f"dmid_{h}_fwd") is None)}
        b0 = forecast_pairs(scored, h, "dmid_30")
        hr["B0_persistence"] = {"n": len(b0), "mae": (sum(abs(y) for _, _, _, y in b0) / len(b0)) if b0 else None}
        for name, (rule, cool) in RULES.items():
            m = candidate_metrics(select(samples, rule, cd if cool else 0.0), h, seed, draws)
            rm = candidate_metrics(select(robust, rule, cd if cool else 0.0), h, seed, draws)
            m["robust_l3_h05"] = {k: rm.get(k) for k in ("mean_ret", "positive_game_share", "fill_rate", "attempted_orders")}
            hr[name] = m
            if name != "B1_buy_any":
                decisions[f"{name}@{h}"] = decision(decision_inputs(m, rm))
            per_game = defaultdict(list)
            for s in select(samples, rule, cd if cool else 0.0):
                e = s.get(f"exec_{h}") or {}
                if e.get("pnl") is not None and e.get("filled"):
                    per_game[_game(s["event_key"])].append(e["pnl"] / e["filled"])
            pvals[f"{name}@{h}"] = boot_p(dict(per_game), seed, draws)
        hr["H3_by_grade"] = h3_by_grade(samples, h, seed, draws, cd)
        hr["B3_ridge_dmid30"] = forecast_skill(scored, h, "dmid_30", seed, draws, coef=coef["B3_ridge_dmid30"][str(h)])
        hr["B4_ridge_gap"] = forecast_skill(scored, h, "gap_leader", seed, draws, coef=coef["B4_ridge_gap"][str(h)])
        for name in ("B3_ridge_dmid30", "B4_ridge_gap"):
            decisions[f"{name}@{h}"] = decision({"skill_ci": hr[name].get("skill_ci")})   # no orders: at best shadow
        report["horizons"][str(h)] = hr
    report["M_prototype_forecast"] = momentum_forecast_score(fc, data["rows"], samples, seed, draws)
    # H4: the guaranteed trade. Manual Robinhood leg (a person, 15 s) and both legs fast.
    manual = arb_scan(data["rows"], _default_fee, latency_s, spec["manual_leg_latency_s"])
    fast = arb_scan(data["rows"], _default_fee, latency_s, latency_s)
    stressed = arb_scan(data["rows"], _default_fee, max(float(rob["latency_s"]), latency_s), spec["manual_leg_latency_s"], haircut=float(rob["haircut"]))
    h4 = arb_metrics(manual, seed, draws)                                  # worst case: the guaranteed P&L
    h4["win_case"] = arb_metrics(manual, seed, draws, "pnl_win").get("mean_ret")
    h4["expected_with_tie_prior"] = arb_metrics(manual, seed, draws, "pnl_ev").get("mean_ret")
    h4["both_legs_fast"] = {**arb_metrics(fast, seed, draws), "win_case": arb_metrics(fast, seed, draws, "pnl_win").get("mean_ret"),
                            "expected_with_tie_prior": arb_metrics(fast, seed, draws, "pnl_ev").get("mean_ret")}
    h4["stressed_l3_h05"] = {**arb_metrics(stressed, seed, draws), "win_case": arb_metrics(stressed, seed, draws, "pnl_win").get("mean_ret")}
    h4["by_margin"] = {name: {**arb_metrics(sub, seed, draws), "win_case": arb_metrics(sub, seed, draws, "pnl_win").get("mean_ret")}
                       for name, lo, hi in (("<1c", 0.0, 0.01), ("1-3c", 0.01, 0.03), (">=3c", 0.03, 9.0))
                       for sub in [[r for r in manual if lo <= r["margin"] < hi]]}
    h4["by_tie"] = {name: arb_metrics([r for r in manual if r["tie_safe"] is want], seed, draws) for name, want in (("tie-safe", True), ("loses-on-tie", False))}
    report["H4_arb"] = h4
    pv4 = defaultdict(list)
    for r in manual:
        if r["pnl_worst"] is not None:
            pv4[r["game"]].append(r["pnl_worst"])
    pvals["H4_arb"] = boot_p(dict(pv4), seed, draws)
    lk = h3_lock_trades(samples, data["rows"], _default_fee, latency_s, watch_s=spec.get("lock_watch_s", 600), settle=settle, cooldown_s=cd)
    report["H3_lock"] = {**summarize(lk["rets"], lk["tries"], seed=seed, draws=draws), "entries_filled": lk["filled"], "locked": lk["locked"],
                         "lock_conversion": (lk["locked"] / lk["filled"]) if lk["filled"] else None,
                         "median_seconds_to_lock": lk["median_seconds_to_lock"],
                         "locked_only": summarize(lk["locked_only"], {g: len(v) for g, v in lk["locked_only"].items()}, seed=seed, draws=draws),
                         "hold_no_lock": summarize(lk["hold"], {g: len(v) for g, v in lk["hold"].items()}, seed=seed, draws=draws),
                         "note": ("locking mostly happens after the entry has moved in its favour: it turns winners into sure "
                                  "profit; compare with hold_no_lock (same entries, same watch, never locked)")}
    primary = spec["primary"].rstrip("s").replace("H3@", "H3_leadlag@")
    alpha = float(spec["alpha"])
    report["primary"] = {"hypothesis": primary, "p_mean_le_0": pvals.get(primary), "alpha": alpha,
                         "passes": (pvals.get(primary, 1.0) <= alpha)}
    fam = {k: pvals[k] for k in spec["secondary"] if k in pvals}
    report["secondary"] = {"family": list(spec["secondary"]), "p": fam, "holm_pass": holm(fam, alpha)}
    report["decisions"] = decisions
    report["fold_role"] = {"discovery": "descriptive: these games shaped the rules; not evidence",
                           "validation": "chronological check before the test; not the test",
                           "test": "the pre-registered test"}.get(fold, fold)
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
    if isinstance(x, (list, tuple)):
        return [_round(v) for v in x]
    return x


def _fold_keys(db: str, manifest: dict[str, Any], fold: str) -> list[str]:
    import sqlite3

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    keys = [r[0] for r in con.execute("select distinct event_key from inplay_ticks where l1_json is not null and event_key like 'nfl:%'")]
    con.close()
    return sorted(k for k in keys if fold_of(manifest, k) == fold and ":spread:" not in k and ":total:" not in k)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Frozen, game-blocked microstructure evaluation (read-only; no alerts, no orders).")
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--fold", choices=("discovery", "validation", "test", "synthetic"), default="synthetic")
    ap.add_argument("--db", default=str(ROOT / "out/history.db"), help="recorder database (opened read-only)")
    ap.add_argument("--latency", type=float, help="order latency in seconds (default: spec; legacy 5 s data uses legacy_latency_s)")
    ap.add_argument("--reopen-test")
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--results", type=Path)
    ap.add_argument("--freeze", type=Path, help="write the forecast models fitted on this fold and every earlier one (not with --fold test)")
    ap.add_argument("--frozen", type=Path, help="frozen forecast models for the test fold (required there)")
    args = ap.parse_args(argv)
    manifest = json.loads(args.manifest.read_text()) if args.manifest.exists() else {"discovery": [], "test_from": "2026-10-08"}
    validate_manifest(manifest)
    spec = _spec_with_defaults(manifest.get("spec") or DEFAULT_SPEC)
    frozen = None
    if args.fold == "test":
        if args.freeze:
            raise SystemExit("--freeze is not allowed on the test fold (models are frozen before it is opened)")
        path = args.frozen or DEFAULT_FROZEN
        if not path.exists():
            raise SystemExit(f"the test fold needs frozen models ({path}); freeze them on the validation fold first")
        frozen = json.loads(path.read_text())
    eff = effective_spec(manifest, frozen)
    digest = spec_hash(eff)
    exploratory = guard_test_open(args.log, digest, args.reopen_test) if args.fold == "test" else False
    if args.fold == "synthetic":
        report = synthetic_report()
    else:
        from arb_engine.quant.microdata import build, load_db

        keys = _fold_keys(args.db, manifest, args.fold)
        if not keys:
            report = {"experiment": "microstructure", "fold": args.fold, "status": "no-recorded-games"}
        else:
            data = load_db(args.db, event_keys=keys)
            legacy = all(r.get("approx_time") for r in data["rows"][:2000])
            latency = args.latency if args.latency is not None else (spec.get("legacy_latency_s", 5) if legacy else (spec.get("latencies_s") or [1])[0])
            train = None
            if args.fold == "validation":
                dkeys = _fold_keys(args.db, manifest, "discovery")
                if dkeys:
                    d = load_db(args.db, event_keys=dkeys)
                    train = build(d["rows"], sample="unconditional", horizons=spec["horizons"], fee_for_row=lambda r: None)
            report = {"experiment": "microstructure", "fold": args.fold,
                      **evaluate(data, spec, latency, legacy, fold=args.fold, frozen=frozen, train_samples=train)}
            if args.freeze:
                fit_on = (train or []) + [s for s in build(data["rows"], sample="unconditional", horizons=spec["horizons"], fee_for_row=lambda r: None)]
                art = {"coef": fit_models(fit_on, spec["horizons"]), "trained_on": sorted({_game(s["event_key"]) for s in fit_on}),
                       "folds": ["discovery"] + (["validation"] if args.fold == "validation" else []), "spec_hash_at_freeze": digest}
                args.freeze.parent.mkdir(parents=True, exist_ok=True)
                args.freeze.write_text(json.dumps(_round(art), indent=2, sort_keys=True) + "\n")
                report["frozen_models_written"] = {"path": str(args.freeze), "sha256": spec_hash(_round(art))}
            if legacy:
                report["caveat"] = "legacy 5 s timestamps: the book 1 s after a decision was never observed"
    report.update({"spec_hash": digest, "hashes": component_hashes(eff), "exploratory": exploratory})
    if exploratory:
        report["decisions"] = {k: f"exploratory:{v}" for k, v in (report.get("decisions") or {}).items()}
    report = _round(report)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    append_log(args.log, {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "git_head": _git_head(), "fold": args.fold,
                          "spec_hash": digest, "hashes": component_hashes(eff), "reopen_reason": args.reopen_test,
                          "exploratory": exploratory, "results_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
                          "argv": list(argv) if argv is not None else sys.argv[1:]})
    if args.results:
        args.results.parent.mkdir(parents=True, exist_ok=True)
        args.results.write_text(rendered)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

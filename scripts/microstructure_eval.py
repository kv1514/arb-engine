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
* B3 ridge dmid_h ~ dmid_30 and B4 ridge dmid_h ~ leader gap: on discovery, each scored game
  is forecast by a fit on the games that *ended before it started* (never a later or
  concurrent game); ``--freeze`` then fits all discovery games once and writes the frozen
  artifact that validation and test read - neither refits.
* H4 arbitrage: executable books whose all-in asks for both outcomes sum below $1; each leg
  meets its own book after its own latency. Only a pair with verified, compatible settlement
  rules and a tie payout of >= $1 is *guaranteed* (scored on its worse of win and tie); every
  other pair is *speculation*, scored on its win case and never called an arbitrage.

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
DEFAULT_FROZEN = ROOT / "tests/fixtures/microstructure/frozen_models.json"
DEFAULT_FROZEN_SPEC = ROOT / "tests/fixtures/microstructure/frozen_spec.json"
EXECUTABLE = ("kalshi", "robinhood")
CODE_FILES = (
    # the experiment
    "arb_engine/quant/microdata.py", "arb_engine/quant/paperexec.py", "arb_engine/strategy/momentum.py", "scripts/microstructure_eval.py",
    # fees, settlement and contract identity it relies on
    "arb_engine/fees/base.py", "arb_engine/fees/kalshi.py", "arb_engine/fees/polymarket.py", "arb_engine/fees/robinhood.py",
    "arb_engine/fees/registry.py", "arb_engine/matching/settlement_rules.py", "arb_engine/matching/normalize.py",
    "arb_engine/data/settlement_rules.json", "arb_engine/models/__init__.py",
)
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


FEE_PROBES = (("kalshi", {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1, "series": "KXNFLGAME"}, None),
              ("robinhood", {"exchange": "rothera"}, "rothera"), ("robinhood", {"exchange": "cdna"}, "cdna"),
              ("robinhood", {"exchange": "kalshi"}, "kalshi"),
              ("polymarket", {"feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True}, "feesEnabled": True}, None))


def fee_fingerprint() -> list[Any]:
    """The fees the experiment would charge, evaluated: the fee model ``microdata`` builds for
    each venue / exchange (code, registry dispatch and settings such as
    ROBINHOOD_ROTHERA_FEE_MODEL alike) on a grid of prices and counts."""
    from arb_engine.fees.registry import fee_model_for_quote
    from arb_engine.models import OutcomeQuote

    out = []
    for venue, params, exch in FEE_PROBES:
        q = OutcomeQuote(venue, "probe", "nfl:A|B:2026-01-01", "A", ask=.5, bid=.49, fee_params=dict(params), meta={"exchange": exch} if exch else {})
        fm = fee_model_for_quote(q)
        out.append([venue, exch, type(fm).__name__, [str(fm.fee(px, n, "taker")) for px in (.03, .25, .5, .77, .97) for n in (1, 10, 137)]])
    return out


def settlement_fingerprint() -> dict[str, Any]:
    """The settlement registry as the experiment reads it, per venue / exchange / sport / market."""
    from arb_engine.matching.settlement_rules import lookup

    out: dict[str, Any] = {}
    for venue, exch in (("kalshi", None), ("polymarket", None), ("robinhood", "rothera"), ("robinhood", "cdna"), ("robinhood", "kalshi")):
        for sport in ("nfl", "ncaaf"):
            for mt in ("moneyline", "spread", "total"):
                r = lookup(venue, sport, mt, exch)
                out[f"{venue}/{exch}/{sport}/{mt}"] = ({k: r.get(k) for k in ("tie", "postponed", "cancelled", "walkover", "retirement", "ot_included", "status")}
                                                       if r else None)
    return out


def effective_spec(manifest: dict[str, Any], frozen: Optional[dict[str, Any]] = None, root: Path = ROOT,
                   runtime: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Everything that decides a result: the manifest spec, the fold policy, the sampling
    constants, the code that samples / executes / scores (and the fee, settlement and
    identity code under it), the fees and settlement rules as evaluated (so a setting or a
    registry edit counts too), runtime overrides (``--latency``) and the frozen models."""
    from arb_engine.quant.microdata import effective_constants

    # A listed file that is missing is recorded as such - never silently left out of the hash.
    code = {f: hashlib.sha256((root / f).read_bytes()).hexdigest() if (root / f).exists() else "MISSING" for f in CODE_FILES}
    return {"spec": manifest.get("spec") or DEFAULT_SPEC, "folds": fold_policy(manifest), "constants": effective_constants(),
            "code": code, "fees": fee_fingerprint(), "settlement": settlement_fingerprint(), "runtime": dict(runtime or {}),
            "frozen_models": spec_hash(frozen) if frozen else None}


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
    (``candidate_metrics`` counts fills). Drawdown needs the decision times (``times``,
    aligned with ``trades``); without them it is reported as unsupported, never computed in
    some other order."""
    done = {g: [x for x in xs if x is not None] for g, xs in trades.items()}
    n = sum(len(v) for v in done.values())
    tried = sum(attempts.values())
    out: dict[str, Any] = {"attempts": tried, "trades": n, "games": sum(1 for v in done.values() if v),
                           "resolved_rate": (n / tried) if tried else None, "unsupported": {}}
    if not n:
        out["unsupported"]["mean_ret"] = "no resolved trade"
        return out
    per_game = {g: sum(v) for g, v in done.items() if v}
    out["mean_ret"] = game_block_bootstrap(done, seed=seed, draws=draws)
    out["hit_rate"] = sum(1 for v in done.values() for x in v if x > 0) / n
    out["positive_game_share"] = sum(1 for v in per_game.values() if v > 0) / len(per_game)
    out["top_game_share"] = _concentration(per_game)
    if times and all(len(times.get(g, [])) == len(xs) for g, xs in trades.items()):
        chrono = sorted((t, x) for g, xs in trades.items() for t, x in zip(times[g], xs) if x is not None)
        out["max_drawdown_per_contract"] = _drawdown(x for _, x in chrono)
    else:
        out["max_drawdown_per_contract"] = None
        out["unsupported"]["max_drawdown_per_contract"] = "no decision times: a drawdown needs chronological order"
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


def sign_flip_p(values: dict[str, list[float]], seed: int = 20260920, draws: int = 2000, exact_max_games: int = 16) -> float:
    """One-sided game-level sign-flip (randomization) test.

    H0: every game's total net return is symmetric about zero (so the mean is 0); H1: the mean
    is positive. The statistic is the pooled per-contract mean T = sum_g S_g / N (S_g = game
    g's total, N = all resolved trades) - the number the report's mean shows. Under H0 each
    game's total is as likely to be -S_g as S_g, so T* = sum_g e_g S_g / N with e_g = +-1.
    p = the share of T* >= T: over all 2^G sign vectors when G <= ``exact_max_games`` (exact;
    the smallest possible p is 2^-G), else over ``draws`` random vectors with a fixed seed plus
    the observed one. Games, not trades, are flipped: a game's trades share its news, and
    flipping them one by one would overstate the evidence. A game without a resolved trade
    contributes nothing; no data gives p = 1. (The old uncentered bootstrap share of means
    <= 0 was a confidence-interval diagnostic, not a p-value.)"""
    games = sorted(g for g, v in values.items() if v)
    if not games:
        return 1.0
    S = [float(sum(values[g])) for g in games]
    N = sum(len(values[g]) for g in games)
    T = sum(S) / N
    tol = 1e-12 * max(1.0, abs(T))
    G = len(S)
    if G <= exact_max_games:
        tot, flipped, ge = sum(S), [False] * G, 0
        ge += tot / N >= T - tol
        for k in range(1, 1 << G):                    # Gray code: one sign changes per step
            i = (k & -k).bit_length() - 1
            flipped[i] = not flipped[i]
            tot += -2 * S[i] if flipped[i] else 2 * S[i]
            ge += tot / N >= T - tol
        return ge / (1 << G)
    rng = random.Random(seed)
    ge = 1
    for _ in range(draws):
        ge += sum(x if rng.random() < .5 else -x for x in S) / N >= T - tol
    return ge / (draws + 1)


def _log_rows(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


def ledger_path(frozen_spec_path: Path) -> Path:
    """The canonical test-open ledger: beside the frozen spec record (committed with it), not
    wherever ``--log`` points, so choosing another audit log cannot reset the history."""
    return frozen_spec_path.parent / "test_open_ledger.jsonl"


def guard_test_open(ledger: Path, current_hash: str, reopen_reason: Optional[str] = None) -> bool:
    """Refuse a test run under a spec other than the one the test fold was first opened with,
    unless a non-empty reason is given. ``ledger`` holds one ``intent`` row per test run,
    written before any test data is read (older logs' ``fold == "test"`` rows count too).
    Returns True when the run is exploratory (the test spec changed, or it was reopened)."""
    if reopen_reason is not None and not reopen_reason.strip():
        raise ValueError("--reopen-test requires a non-empty reason")
    opened = [r for r in _log_rows(ledger) if r.get("event") == "intent" or (r.get("event") is None and r.get("fold") == "test")]
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
def _h3_direction(s: dict[str, Any], leader: Optional[dict[str, Any]] = None) -> int:
    """The registered H3 rule, symmetric. +1 = buy the follower (its independent leader rose);
    -1 = buy the follower's complement (its leader fell); 0 = no decision. A decision needs a
    leader that moved >= 5c in 30 s, a follower that moved less than half as much, and the
    leader's mid >= 2c beyond the follower's in the leader's direction."""
    ld = leader["dmid_30"] if leader is not None else s.get("leader_dmid_30")
    gap = leader["gap"] if leader is not None else s.get("gap_leader")
    if s.get("kind") != "unconditional" or ld is None or gap is None or abs(ld) < .05:
        return 0
    d = 1 if ld > 0 else -1
    if abs(s.get("dmid_30") or 0) >= .5 * abs(ld) or d * gap < .02:
        return 0
    return d


def _h3(s: dict[str, Any]) -> bool:
    return _h3_direction(s) != 0


def _h3_diag(level: str) -> Callable[[dict[str, Any]], int]:
    """The H3 rule on a looser leader (``leader_diag``): not registered, never tested."""
    def rule(s: dict[str, Any]) -> int:
        ld = (s.get("leader_diag") or {}).get(level)
        return _h3_direction(s, ld) if ld else 0
    return rule


def _dir_h1(s: dict[str, Any]) -> int:
    """Momentum, both ways: a rise buys the contract, a fall buys its complement."""
    if s.get("kind") != "trigger":
        return 0
    d = s.get("dmid_30") or 0
    return 1 if d > 0 else (-1 if d < 0 else 0)


# name -> (direction rule, one decision per economic exposure per signal_cooldown_s)
RULES: dict[str, tuple[Callable[[dict[str, Any]], int], bool]] = {
    "B1_buy_any": (lambda s: 1 if s["kind"] == "unconditional" else 0, False),
    "H1_momentum": (_dir_h1, True),
    "H2_dip": (lambda s: 1 if s["kind"] == "trigger" and (s["dmid_30"] or 0) < 0 else 0, True),
    "H2_recovery": (lambda s: 1 if s["kind"] == "recovery" else 0, True),
    "H3_leadlag": (_h3_direction, True),
    "M_prototype": (lambda s: 1 if s["kind"] == "unconditional" and s.get("momentum_status") == "rising" else 0, True),
}


def select(samples: list[dict[str, Any]], rule: Callable[[dict[str, Any]], Any], cooldown_s: float = 0.0) -> list[dict[str, Any]]:
    """Executable decision points a rule fires on, once per contract per ``cooldown_s``."""
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


def bought_contract(s: dict[str, Any], d: int) -> Optional[dict[str, Any]]:
    """What a decision buys: the contract itself (d = +1) or its executable complement (d = -1,
    ``microdata._complement``: same event, market and book, paying on the other outcome and,
    where a tie can happen, the rest of the tie). Never a short synthesised from a bid."""
    if d > 0:
        return {"key": (s["event_key"], s["book_id"], s["outcome"], s["side"]), "venue": s.get("venue"), "ask": s["ask"],
                "mid": s.get("mid", s["ask"]), "self": True}
    c = s.get("complement")
    if not c:
        return None
    return {"key": tuple(c["key"]), "venue": c.get("venue"), "ask": c["ask"], "mid": (c["bid"] + c["ask"]) / 2.0, "self": False}


def select_trades(samples: list[dict[str, Any]], rule: Callable[[dict[str, Any]], int], cooldown_s: float = 0.0
                  ) -> tuple[list[tuple[dict[str, Any], int, dict[str, Any]]], dict[str, Any]]:
    """(decision, direction, bought contract) in time order, plus the counts of what was not
    traded. One *economic exposure* - (event, book, the outcome the bought contract pays on) -
    trades once per ``cooldown_s``: a rise in KC and the mirror fall in DEN both mean "long
    KC" and are one trade, whichever contract signalled first. A fall with no executable
    complement is counted, not traded."""
    last: dict[tuple, float] = {}
    out = []
    unavailable: Counter = Counter()
    dup = 0
    for s in sorted(samples, key=lambda x: (x["t"], x["event_key"], x["book_id"], x["outcome"], x["side"], x["kind"])):
        if s.get("venue") not in EXECUTABLE:
            continue
        d = rule(s)
        if not d:
            continue
        bc = bought_contract(s, d)
        if bc is None or bc.get("venue") not in EXECUTABLE:
            unavailable[s.get("complement_missing") or "not-executable"] += 1
            continue
        exposure = (bc["key"][0], bc["key"][1], bc["key"][2])
        if cooldown_s and exposure in last and s["t"] - last[exposure] < cooldown_s:
            dup += 1
            continue
        last[exposure] = s["t"]
        out.append((s, d, bc))
    return out, {"complement_unavailable": dict(unavailable), "complement_unavailable_total": sum(unavailable.values()),
                 "same_exposure_dropped": dup, "long": sum(1 for _, d, _ in out if d > 0), "via_complement": sum(1 for _, d, _ in out if d < 0)}


class ExecCtx:
    """Paper execution of any contract at any latency / haircut from the recorded rows
    (``quant.paperexec`` through ``microdata._exec_trade``); Robinhood orders are placed by a
    person, so ``venue_latency`` overrides the latency for them."""

    def __init__(self, rows: list[dict[str, Any]], settle: dict[tuple, float], fee_for_row: Callable, n: int,
                 venue_latency: Optional[dict[str, float]] = None, entry_tol_s: float = 2.0) -> None:
        from arb_engine.quant.microdata import series_by_contract

        self.series = {k: [(r["obs_ts"], r) for r in v] for k, v in series_by_contract(rows).items()}
        self.times = {k: [t for t, _ in v] for k, v in self.series.items()}
        self.settle, self.fee_for_row, self.n = settle, fee_for_row, n
        self.venue_latency, self.entry_tol_s = dict(venue_latency or {}), entry_tol_s
        self._fees: dict[tuple, Any] = {}

    def latency(self, venue: Any, latency_s: float) -> float:
        return float(self.venue_latency.get(str(venue), latency_s))

    def fee(self, key: tuple) -> Any:
        if key not in self._fees:
            ser = self.series.get(key)
            self._fees[key] = self.fee_for_row(ser[0][1]) if ser else None
        return self._fees[key]

    def execute(self, key: tuple, venue: Any, t: float, ask: float, h: int, latency_s: float, haircut: float) -> dict[str, Any]:
        from arb_engine.quant.microdata import _exec_trade, exec_record

        lat = self.latency(venue, latency_s)
        ser = self.series.get(key)
        if not ser:
            rec = exec_record(None)
            rec["status"] = "no-observations"
        else:
            rec = exec_record(_exec_trade(ser, t, ask, h, self.fee(key), self.n, self.settle.get(key), lat, self.entry_tol_s, haircut,
                                          times=self.times[key]))
        rec["latency_s"] = lat
        return rec

    def forward(self, key: tuple, t: float, mid: float, h: int) -> Optional[float]:
        """The label: the first refreshed mark of the bought contract in [t+h, t+h+max(1, .2h)]."""
        from arb_engine.quant.microdata import _mid

        ts = self.times.get(key) or []
        i = bisect.bisect_left(ts, t + h)
        if i < len(ts) and ts[i] <= t + h + max(1.0, 0.2 * h):
            return _mid(self.series[key][i][1]) - mid
        return None


def realize(ctx: ExecCtx, s: dict[str, Any], d: int, bc: dict[str, Any], horizons: Iterable[int], latency_s: float, haircut: float,
            reuse: bool) -> dict[str, Any]:
    """A decision as the trade it makes: the bought contract's execution and label per horizon.
    ``reuse``: the build already executed this very order (self, same latency and haircut)."""
    tr: dict[str, Any] = {"t": s["t"], "event_key": s["event_key"], "direction": d, "bought": list(bc["key"]), "venue": bc["venue"]}
    for h in horizons:
        if reuse and bc["self"]:
            tr[f"exec_{h}"], tr[f"dmid_{h}_fwd"] = s.get(f"exec_{h}") or {}, s.get(f"dmid_{h}_fwd")
        else:
            tr[f"exec_{h}"] = ctx.execute(bc["key"], bc["venue"], s["t"], bc["ask"], h, latency_s, haircut)
            tr[f"dmid_{h}_fwd"] = ctx.forward(bc["key"], s["t"], bc["mid"], h)
    return tr


def candidate_metrics(sel: list[dict[str, Any]], h: int, seed: int, draws: int) -> dict[str, Any]:
    """Orders, fills, positions, labels, skill and P&L for one candidate at horizon h. Every
    metric that cannot be computed is None with its reason under ``unsupported``."""
    ex = [(s, s.get(f"exec_{h}") or {}) for s in sel]
    st = Counter(e.get("status") for _, e in ex)
    filled = [(s, e) for s, e in ex if (e.get("filled") or 0) > 0]
    resolved = [(s, e) for s, e in filled if e.get("pnl") is not None]
    uns: dict[str, str] = {}
    out: dict[str, Any] = {
        "attempted_orders": len(ex), "filled_orders": len(filled), "filled_contracts": sum(e["filled"] for _, e in filled),
        "missed_orders": st.get("missed", 0) + st.get("no-observations", 0) + st.get("no-fee-model", 0),
        "closed_positions": st.get("closed", 0), "settled_positions": st.get("settled", 0),
        "unresolved_positions": st.get("unresolved", 0), "fill_rate": len(filled) / len(ex) if ex else None,
        "resolved_share_of_fills": len(resolved) / len(filled) if filled else None,
        "games": len({_game(s["event_key"]) for s in sel}), "fees": sum(e.get("fees") or 0.0 for _, e in resolved),
        "unsupported": uns,
    }
    if not ex:
        uns["fill_rate"] = "no decisions"
    elif not filled:
        uns["resolved_share_of_fills"] = "no fills"
    lab: dict[str, list[float]] = defaultdict(list)
    for s in sel:
        y = s.get(f"dmid_{h}_fwd")
        if y is not None:
            lab[_game(s["event_key"])].append(y)
    n_lab = sum(len(v) for v in lab.values())
    out["labelled"], out["missing_labels"] = n_lab, len(sel) - n_lab
    if n_lab:
        # A buy forecasts "up" for what it bought: its skill is the mid move of that contract.
        out["skill_ci"] = game_block_bootstrap(dict(lab), seed=seed, draws=draws)
        moved = [y for v in lab.values() for y in v if y != 0]
        out["directional_hit"] = sum(1 for y in moved if y > 0) / len(moved) if moved else None
        if not moved:
            uns["directional_hit"] = "no labelled mid moved"
    else:
        out["skill_ci"], out["directional_hit"] = None, None
        uns["skill_ci"] = uns["directional_hit"] = "no labelled decision"
    if not resolved:
        for k in ("mean_ret", "dollar_pnl", "hit_rate", "positive_game_share", "top_game_share", "max_drawdown_usd", "fee_share_of_notional",
                  "fee_share_of_gross_edge"):
            out[k] = None
            uns[k] = "no resolved trade"
        return out
    per_game_ret: dict[str, list[float]] = defaultdict(list)
    per_game_usd: dict[str, float] = defaultdict(float)
    for s, e in resolved:
        g = _game(s["event_key"])
        per_game_ret[g].append(e["pnl"] / e["filled"])
        per_game_usd[g] += e["pnl"]
    out["mean_ret"] = game_block_bootstrap(dict(per_game_ret), seed=seed, draws=draws)
    out["dollar_pnl"] = sum(per_game_usd.values())
    out["gross_pnl_usd"] = out["dollar_pnl"] + out["fees"]
    notional = sum((e.get("entry_notional") or 0.0) + (e.get("exit_notional") or 0.0) for _, e in resolved)
    out["fee_share_of_notional"] = out["fees"] / notional if notional else None
    if not notional:
        uns["fee_share_of_notional"] = "no traded notional"
    out["fee_share_of_gross_edge"] = out["fees"] / out["gross_pnl_usd"] if out["gross_pnl_usd"] > 0 else None
    if out["gross_pnl_usd"] <= 0:
        uns["fee_share_of_gross_edge"] = "no gross edge before fees (fees cannot be a share of a loss)"
    out["hit_rate"] = sum(1 for _, e in resolved if e["pnl"] > 0) / len(resolved)
    out["positive_game_share"] = sum(1 for v in per_game_usd.values() if v > 0) / len(per_game_usd)
    out["top_game_share"] = _concentration(dict(per_game_usd))
    if out["top_game_share"] is None:
        uns["top_game_share"] = "zero P&L in every game"
    out["max_drawdown_usd"] = _drawdown(e["pnl"] for s, e in sorted(resolved, key=lambda x: (x[0]["t"], x[0]["event_key"])))
    return out


def decision_inputs(m: dict[str, Any], robust: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Exactly what ``decision`` reads; ``missing`` names every input it had to do without."""
    out = {"skill_ci": m.get("skill_ci"), "net_pnl_ci": m.get("mean_ret"), "fill_rate": m.get("fill_rate"),
           "test_games": m.get("games", 0), "deduped_triggers": m.get("attempted_orders", 0), "top_game_share": m.get("top_game_share"),
           "robust_l3_h05": {"net_pnl_ci": (robust or {}).get("mean_ret"), "positive_game_share": (robust or {}).get("positive_game_share")}}
    out["missing"] = sorted([k for k in ("skill_ci", "net_pnl_ci", "fill_rate", "top_game_share") if out[k] is None]
                            + [f"robust_l3_h05.{k}" for k, v in out["robust_l3_h05"].items() if v is None])
    return out


def forecast_pairs(samples: list[dict[str, Any]], h: int, feature: str) -> list[tuple[str, float, float, float]]:
    """(game, t, x, y) on unconditional samples with the feature and a forward label."""
    return [(_game(s["event_key"]), s["t"], float(s[feature]), float(s[f"dmid_{h}_fwd"])) for s in samples
            if s["kind"] == "unconditional" and s.get(f"dmid_{h}_fwd") is not None and s.get(feature) is not None]


def chrono_training(samples: list[dict[str, Any]], min_train_games: int = 3) -> dict[str, list[str]]:
    """Scored game -> the games it may be trained on: those whose last observation came
    before its first. A game is never trained on a later or concurrent game (the 1 pm games
    of a Sunday cannot train each other); a game with fewer than ``min_train_games`` earlier
    games is not scored."""
    span: dict[str, list[float]] = {}
    for s in samples:
        g = _game(s["event_key"])
        a = span.setdefault(g, [math.inf, -math.inf])
        a[0], a[1] = min(a[0], s["t"]), max(a[1], s["t"])
    out = {}
    for g, (first, _) in sorted(span.items(), key=lambda kv: (kv[1][0], kv[0])):
        train = sorted(h for h, (_, last) in span.items() if h != g and last < first)
        if len(train) >= min_train_games:
            out[g] = train
    return out


def fit_models(samples: list[dict[str, Any]], horizons: Iterable[int], counts: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Ridge coefficients per model and horizon; ``counts`` (if given) receives the number of
    training pairs - a model with none is [0, 0], i.e. no forecast (the unchanged price)."""
    coef: dict[str, dict[str, list[float]]] = {"B3_ridge_dmid30": {}, "B4_ridge_gap": {}}
    for h in horizons:
        for name, feat in (("B3_ridge_dmid30", "dmid_30"), ("B4_ridge_gap", "gap_leader")):
            p = forecast_pairs(samples, h, feat)
            coef[name][str(h)] = list(ridge_fit([x for _, _, x, _ in p], [y for _, _, _, y in p]))
            if counts is not None:
                counts.setdefault(name, {})[str(h)] = len(p)
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
    """Coefficients from ``coef`` (frozen) or fitted on ``train`` (earlier data), scored on
    ``samples``. Never fitted on the samples it scores."""
    if coef is None:
        p = forecast_pairs(train or [], h, feature)
        coef = list(ridge_fit([x for _, _, x, _ in p], [y for _, _, _, y in p])) if p else [0.0, 0.0]
    a, b = coef
    out = score_forecast(forecast_pairs(samples, h, feature), lambda x: a + b * x, seed, draws)
    out["coef"] = [a, b]
    return out


def forecast_skill_rolling(samples: list[dict[str, Any]], h: int, feature: str, seed: int, draws: int,
                           plan: dict[str, list[str]]) -> dict[str, Any]:
    """Discovery: each scored game forecast by a fit on its own earlier games (``plan``)."""
    by_game: dict[str, list] = defaultdict(list)
    for p in forecast_pairs(samples, h, feature):
        by_game[p[0]].append(p)
    preds, coefs = [], {}
    for g, train in plan.items():
        tp = [p for t in train for p in by_game.get(t, [])]
        if not tp or not by_game.get(g):
            continue
        a, b = ridge_fit([x for _, _, x, _ in tp], [y for _, _, _, y in tp])
        coefs[g] = [a, b]
        preds.extend((gg, t, a + b * x, y) for gg, t, x, y in by_game[g])
    out = score_forecast([(g, t, f, y) for g, t, f, y in preds], lambda x: x, seed, draws)
    out["coef_by_game"] = coefs
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
                   cooldown_s: float = 0.0, venue_latency: Optional[dict[str, float]] = None) -> dict[str, Any]:
    """H3 + lock (strategy/laglock.py replayed) with inventory accounting. Buy what the H3
    decision buys (IOC at the decision ask after the latency, fees in); then for ``watch_s``
    watch the contracts paying on the other outcome on the executable venues and send an IOC
    for the *unhedged remainder* whenever the pair costs <= $1 (tie-safe pairs only when
    asked). Every contract a hedge actually fills stays in the books - a partial hedge locks
    what it filled and the watch goes on for the rest; one order at a time. Whatever is still
    unhedged after the watch is sold to the bid (or settled) by the paper executor's own exit;
    if any of it is neither, the trade is unresolved (None), never valued."""
    from decimal import Decimal

    from arb_engine.quant.microdata import series_by_contract, tie_value
    from arb_engine.quant.paperexec import ioc_entry, ioc_round_trip

    series = series_by_contract(rows)
    vl = dict(venue_latency or {})
    rets: dict[str, list] = defaultdict(list)
    times: dict[str, list] = defaultdict(list)
    tries: dict[str, int] = defaultdict(int)
    locked_only: dict[str, list] = defaultdict(list)
    hold: dict[str, list] = defaultdict(list)
    waits: list[float] = []
    inv = Counter()
    decisions, _ = select_trades(samples, _h3_direction, cooldown_s)
    for s, d, bc in decisions:
        g, t = _game(s["event_key"]), s["t"]
        tries[g] += 1
        times[g].append(t)
        key = tuple(bc["key"])
        mine = [r for r in series.get(key, []) if r["obs_ts"] > t]
        fee = fee_for_row(mine[0]) if mine else None
        if fee is None:
            rets[g].append(None)
            continue
        lat = float(vl.get(str(bc["venue"]), latency_s))
        entry, erow = ioc_entry(mine, t, bc["ask"], n, fee, lat)
        if entry.missed or not entry.filled:
            rets[g].append(None)
            continue
        inv["entries_filled"] += 1
        sv = (settle or {}).get(key)
        hold[g].append(ioc_round_trip(mine, t, bc["ask"], n, fee, lat, horizon_s=watch_s, settlement=sv).pnl_per_contract)
        held = entry.filled
        cost = Decimal(str(entry.entry_price)) * held + entry.entry_fee
        t_in = erow["obs_ts"]
        etie = tie_value(erow)
        comp_keys = [k for k in series if k[0] == key[0] and k[2] != key[2]]      # rows are normalized: any side
        stream = sorted((r["obs_ts"], k, r) for k in comp_keys for r in series[k]
                        if t_in < r["obs_ts"] <= t_in + watch_s and r.get("venue") in EXECUTABLE)
        locked, hedge_cost, busy_until = 0, Decimal("0"), -math.inf
        for tt, k, r in stream:
            remaining = held - locked
            if remaining <= 0:
                break
            if tt < busy_until:
                continue                                    # the previous hedge order is still out
            ask, cfee = r.get("ask"), fee_for_row(r)
            if ask is None or cfee is None:
                continue
            c_all_in = float(ask) + float(cfee.fee(ask, remaining, "taker")) / remaining
            if float(cost) / held + c_all_in > 1.0:
                continue
            if tie_safe:
                ct = tie_value(r)
                if etie is None or ct is None or etie + ct < 1.0 - 1e-9:
                    continue
            leg_lat = float(vl.get(str(r.get("venue")), latency_s))
            leg, lrow = ioc_entry([x for x in series[k] if x["obs_ts"] > tt], tt, float(ask), remaining, cfee, leg_lat)
            inv["hedge_orders"] += 1
            busy_until = lrow["obs_ts"] if lrow is not None else tt + leg_lat + 2.0
            if leg.missed or not leg.filled:
                continue
            locked += leg.filled
            hedge_cost += Decimal(str(leg.entry_price)) * leg.filled + leg.entry_fee
            inv["hedge_contracts"] += leg.filled
            if leg.filled < remaining:
                inv["partial_hedges"] += 1
            if locked == held:
                waits.append(lrow["obs_ts"] - t_in)
        rest = held - locked
        proceeds = exit_fee = Decimal("0")
        unresolved = 0
        if rest:
            ex = ioc_round_trip(mine, t, bc["ask"], rest, fee, lat, horizon_s=watch_s, settlement=sv)
            if ex.filled < rest:                            # cannot happen (rest <= held at the same book); never silently
                unresolved = rest - ex.filled
            proceeds = sum((Decimal(str(p)) * c for _, p, c, _ in ex.exits), Decimal("0")) + (Decimal(str(ex.settle_value)) * ex.settled if ex.settled else Decimal("0"))
            exit_fee = ex.exit_fee
            unresolved += ex.unresolved
        inv["locked_contracts"] += locked
        inv["unhedged_contracts_at_watch_end"] += rest
        if locked == held:
            inv["fully_locked"] += 1
        elif locked:
            inv["partly_locked"] += 1
        if unresolved:
            inv["unresolved"] += 1
            rets[g].append(None)
            continue
        pnl = Decimal(locked) + proceeds - exit_fee - cost - hedge_cost
        per = float(pnl / held)
        rets[g].append(per)
        if locked == held:
            locked_only[g].append(per)
    return {"rets": dict(rets), "times": dict(times), "tries": dict(tries), "locked_only": dict(locked_only), "hold": dict(hold),
            "filled": inv["entries_filled"], "locked": inv["fully_locked"], "inventory": dict(inv),
            "median_seconds_to_lock": sorted(waits)[len(waits) // 2] if waits else None}


def h3_by_grade(samples: list[dict[str, Any]], h: int, seed: int, draws: int, cooldown_s: float = 0.0,
                trades: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """H3 split by the grades logged live: hard vs soft lag, agreement vs none (on the realized
    trades when given, so a complement buy is graded by the decision that made it)."""
    groups = {"hard": lambda s: s.get("hard_lag") is True, "soft": lambda s: s.get("hard_lag") is False,
              "agree>=1": lambda s: (s.get("agree") or 0) >= 1, "agree=0": lambda s: (s.get("agree") or 0) == 0}
    if trades is None:
        trades = [dict(s, _decision=s) for s, _, _ in select_trades(samples, _h3_direction, cooldown_s)[0]]
    return {name: candidate_metrics([tr for tr in trades if keep(tr["_decision"])], h, seed, draws) for name, keep in groups.items()}


def arb_scan(rows: list[dict[str, Any]], fee_for_row: Callable, latency_k: float, latency_rh: float, seed_n: int = 10,
             cooldown_s: float = 30.0, haircut: float = 1.0) -> list[dict[str, Any]]:
    """H4: moments where independent executable books' all-in asks for both outcomes sum < $1
    (each quote fresh: <= 2 s on the fast-lane venues, <= 6 s elsewhere). Each leg meets its
    own book after its own latency; the win-case, tie-case and worst-case P&L are recorded
    (a pair whose tie payout is unknown is excluded, not guessed)."""
    from arb_engine.quant.microdata import TIE_PRIOR, contract_key, fresh_limit, is_observation, observation_time, settlement_relation, tie_value
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
            rel = settlement_relation(ra, rb)          # every case but the tie (the tie is priced by ``ties``)
            compat = True if rel == "identical" else (False if rel == "mismatch" else None)
            res = two_leg_arb([x for x in series[ka] if x["obs_ts"] > t], [x for x in series[kb] if x["obs_ts"] > t], t,
                              float(ra["ask"]), float(rb["ask"]), seed_n, fa, fb, latency_a_s=la, latency_b_s=lb, haircut=haircut,
                              tie_payouts=ties, settlement_compatible=compat, book_id_a=ka[1], book_id_b=kb[1])
            legs_filled = sum(1 for l in res.legs if l.filled)
            win = float(res.pnl) / seed_n if res.pnl is not None and (res.matched or res.unwound) else None
            tie = float(res.pnl_tie) / seed_n if res.pnl_tie is not None and (res.matched or res.unwound) else None
            p_tie = TIE_PRIOR.get(ev.split(":", 1)[0].lower(), 0.0)
            tie_safe = (sum(ties) >= 1.0 - 1e-9) if None not in ties else None
            records.append({"game": _game(ev), "t": t, "margin": 1.0 - cost, "tie_safe": tie_safe, "settlement": rel,
                            "class": "guaranteed-eligible" if compat is True and tie_safe is True else "speculation",
                            "guaranteed_result": bool(res.guaranteed),
                            "excluded": res.excluded or None, "legs_filled": legs_filled, "matched": res.matched, "unwound": res.unwound,
                            "unresolved": res.unresolved, "pnl_win": win, "pnl_tie": tie,
                            "pnl_worst": min(win, tie) if win is not None and tie is not None else win,
                            "pnl_ev": (1 - p_tie) * win + p_tie * tie if win is not None and tie is not None else win})
    return records


def h4_report(records: list[dict[str, Any]], seed: int, draws: int) -> dict[str, Any]:
    """Guaranteed-eligible pairs on their worst case; everything else as speculation on its
    win case (plus the tie case and the tie-odds expectation, for scale) - never mixed."""
    elig = [r for r in records if r["class"] == "guaranteed-eligible"]
    spec = [r for r in records if r["class"] == "speculation"]
    return {"signals": len(records), "by_settlement": dict(Counter(r["settlement"] for r in records)),
            "by_tie": dict(Counter("tie-safe" if r["tie_safe"] else ("loses-on-tie" if r["tie_safe"] is False else "unknown") for r in records)),
            "excluded": dict(Counter(r["excluded"] for r in records if r["excluded"])),
            "guaranteed": arb_metrics(elig, seed, draws, "pnl_worst"),
            "speculation": {**arb_metrics(spec, seed, draws, "pnl_win"),
                            "tie_case": arb_metrics(spec, seed, draws, "pnl_tie").get("mean_ret"),
                            "expected_with_tie_prior": arb_metrics(spec, seed, draws, "pnl_ev").get("mean_ret")}}


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
            "manual_venues": ["robinhood"], "latencies_s": [1, 3], "haircuts": [1.0, 0.5], "train_share": 2 / 3, "ref_contracts": 10,
            "secondary": [], "alpha": 0.10, **(spec or {})}


GRID_KEYS = ("attempted_orders", "filled_orders", "fill_rate", "resolved_share_of_fills", "mean_ret", "positive_game_share", "dollar_pnl",
             "unsupported")


def _cadence(ctx: ExecCtx) -> Optional[float]:
    """The median time between consecutive observations of a contract (the recorder's cadence)."""
    gaps = sorted(b - a for ts in ctx.times.values() for a, b in zip(ts, ts[1:]) if b > a)
    return gaps[len(gaps) // 2] if gaps else None


def _per_game_returns(trades: list[dict[str, Any]], h: int) -> dict[str, list[float]]:
    out: dict[str, list[float]] = defaultdict(list)
    for tr in trades:
        e = tr.get(f"exec_{h}") or {}
        if e.get("pnl") is not None and e.get("filled"):
            out[_game(tr["event_key"])].append(e["pnl"] / e["filled"])
    return dict(out)


def evaluate(data: dict[str, Any], spec: dict[str, Any], latency_s: float, legacy: bool, fold: str = "discovery",
             frozen: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """One fold. ``frozen`` = the discovery-frozen forecast coefficients (validation and test);
    without it (discovery) each scored game is fitted on the games that ended before it.

    Execution: single-leg orders at ``latency_s`` (a Robinhood order - placed by a person - at
    ``manual_leg_latency_s``), then every decision again under each registered latency x
    haircut (``latencies_s`` x ``haircuts``); the promotion rule's stress input is L=3 s,
    haircut 0.5 from that grid."""
    from arb_engine.quant.microdata import _default_fee, build, settlement_values

    spec = _spec_with_defaults(spec)
    seed, draws = spec["seed"], spec["bootstrap"]
    cd = float(spec["signal_cooldown_s"])
    horizons = list(spec["horizons"])
    manual = float(spec["manual_leg_latency_s"])
    venue_latency = {str(v): manual for v in spec["manual_venues"]}
    settle = settlement_values(data["rows"], data["finals"])
    samples = build(data["rows"], espn=data["espn"], prints=data["prints"], sample="all", settlement=settle, horizons=horizons,
                    latency_s=latency_s, entry_tol_s=2.0, ref_contracts=spec["ref_contracts"], venue_latency=venue_latency)
    print_counts = dict(getattr(build, "print_counts", {}) or {})
    fc = momentum_forecasts(data["rows"])
    for smp in samples:
        f = fc.get((smp["event_key"], smp["book_id"], smp["outcome"], smp["side"], smp["t"]))
        smp["momentum_status"] = f["status"] if f else None
    ctx = ExecCtx(data["rows"], settle, _default_fee, int(spec["ref_contracts"]), venue_latency)
    cadence = _cadence(ctx)
    grid = [(float(L), float(hc)) for L in spec["latencies_s"] for hc in spec["haircuts"]]
    gname = lambda L, hc: f"L{L:g}_h{hc:g}"   # noqa: E731
    kinds = Counter(x["kind"] for x in samples)
    report: dict[str, Any] = {"latency_s": latency_s, "legacy_timestamps": legacy, "games": len({_game(k) for k in data["event_keys"]}),
                              "samples": len(samples), "decision_points": dict(kinds), "triggers": kinds.get("trigger", 0),
                              "finals": len(data["finals"]), "prints": print_counts, "horizons": {},
                              "execution": {"latency_s": latency_s, "venue_latency": venue_latency, "cadence_s": cadence,
                                            "registered_grid": [gname(L, hc) for L, hc in grid], "robust_for_decisions": "L3_h0.5",
                                            "unsupported_latencies": [gname(L, hc) for L, hc in grid if cadence and cadence > L + 2.0]},
                              "baselines": {"B0": "B0_persistence (unchanged price)", "B1": "B1_buy_any (cost hurdle)",
                                            "B2": ["H1_momentum", "H2_dip", "H2_recovery", "H3_leadlag", "M_prototype"],
                                            "B3": "B3_ridge_dmid30", "B4": "B4_ridge_gap"}}
    # Forecast models: the discovery-frozen artifact, else (discovery itself) per game on the
    # games that ended before it.
    plan = None
    if frozen is not None:
        coef, fit_note, scored = frozen["coef"], {"from": "frozen", "trained_on": frozen.get("trained_on")}, samples
    else:
        plan = chrono_training(samples, int(spec.get("min_train_games", 3)))
        coef = None
        fit_note = {"from": "games that ended before each scored game", "plan": plan}
        scored = [x for x in samples if _game(x["event_key"]) in plan]
    report["model_fit"] = {**fit_note, "coef": coef}
    # Decisions -> trades (the bought contract: itself, or its executable complement), once per
    # economic exposure per cooldown; then each trade under the main execution and the grid.
    sels = {name: select_trades(samples, rule, cd if cool else 0.0) for name, (rule, cool) in RULES.items()}
    main = {name: [dict(realize(ctx, x, d, bc, horizons, latency_s, 1.0, reuse=True), _decision=x) for x, d, bc in sel]
            for name, (sel, _) in sels.items()}
    grid_h = {name: (horizons if name != "B1_buy_any" else [30 if 30 in horizons else horizons[0]]) for name in RULES}
    gtr = {name: {(L, hc): [realize(ctx, x, d, bc, grid_h[name], L, hc, reuse=False) for x, d, bc in sels[name][0]] for L, hc in grid}
           for name in RULES}
    pvals: dict[str, float] = {}
    decisions: dict[str, Any] = {}
    dinputs: dict[str, Any] = {}
    for h in horizons:
        unc = [x for x in samples if x["kind"] == "unconditional"]
        cover = sum(1 for x in unc if x.get(f"dmid_{h}_fwd") is not None) / len(unc) if unc else None
        hr: dict[str, Any] = {"coverage": cover, "missing_labels_unconditional": sum(1 for x in unc if x.get(f"dmid_{h}_fwd") is None)}
        b0 = forecast_pairs(scored, h, "dmid_30")
        hr["B0_persistence"] = {"n": len(b0), "mae": (sum(abs(y) for _, _, _, y in b0) / len(b0)) if b0 else None}
        for name in RULES:
            m = candidate_metrics(main[name], h, seed, draws)
            m["selection"] = sels[name][1]
            m["grid"] = {}
            robust = None
            for L, hc in grid:
                if h not in grid_h[name]:
                    m["grid"][gname(L, hc)] = {"unsupported": {"all": f"the grid is computed at {grid_h[name]} s for this candidate"}}
                    continue
                gm = candidate_metrics(gtr[name][(L, hc)], h, seed, draws)
                if cadence and cadence > L + 2.0:
                    gm["unsupported"]["latency"] = (f"recorder cadence {cadence:.1f} s exceeds latency + entry tolerance "
                                                    f"({L:g} + 2 s): orders at this latency cannot meet an observed book")
                m["grid"][gname(L, hc)] = {k: gm.get(k) for k in GRID_KEYS}
                if (L, hc) == (float(spec["robust"]["latency_s"]), float(spec["robust"]["haircut"])):
                    robust = gm
            hr[name] = m
            if name != "B1_buy_any":
                inp = decision_inputs(m, robust)
                if robust is None:
                    inp["missing"] = sorted(set(inp["missing"]) | {"robust_l3_h05 (not computed at this horizon)"})
                dinputs[f"{name}@{h}"] = inp
                decisions[f"{name}@{h}"] = decision(inp)
            pvals[f"{name}@{h}"] = sign_flip_p(_per_game_returns(main[name], h), seed, draws)
        hr["H3_by_grade"] = h3_by_grade(samples, h, seed, draws, cd, trades=main["H3_leadlag"])
        if plan is None:
            hr["B3_ridge_dmid30"] = forecast_skill(scored, h, "dmid_30", seed, draws, coef=coef["B3_ridge_dmid30"][str(h)])
            hr["B4_ridge_gap"] = forecast_skill(scored, h, "gap_leader", seed, draws, coef=coef["B4_ridge_gap"][str(h)])
        else:
            hr["B3_ridge_dmid30"] = forecast_skill_rolling(samples, h, "dmid_30", seed, draws, plan)
            hr["B4_ridge_gap"] = forecast_skill_rolling(samples, h, "gap_leader", seed, draws, plan)
        # H3 under looser settlement rules: diagnostics, outside the decisions and every test.
        hr["H3_diagnostics"] = {}
        for lvl in ("tie_matched", "any_settlement"):
            sel, st = select_trades(samples, _h3_diag(lvl), cd)
            hr["H3_diagnostics"][lvl] = {**candidate_metrics([realize(ctx, x, d, bc, [h], latency_s, 1.0, reuse=True) for x, d, bc in sel],
                                                             h, seed, draws), "selection": st}
        for name in ("B3_ridge_dmid30", "B4_ridge_gap"):
            decisions[f"{name}@{h}"] = decision({"skill_ci": hr[name].get("skill_ci")})   # no orders: at best shadow
        report["horizons"][str(h)] = hr
    report["decision_inputs"] = dinputs
    report["M_prototype_forecast"] = momentum_forecast_score(fc, data["rows"], samples, seed, draws)
    # H4: the guaranteed trade. Manual Robinhood leg (a person) and both legs fast.
    rob = spec["robust"]
    manual_recs = arb_scan(data["rows"], _default_fee, latency_s, manual)
    fast = arb_scan(data["rows"], _default_fee, latency_s, latency_s)
    stressed = arb_scan(data["rows"], _default_fee, max(float(rob["latency_s"]), latency_s), manual, haircut=float(rob["haircut"]))
    h4 = h4_report(manual_recs, seed, draws)
    h4["both_legs_fast"] = h4_report(fast, seed, draws)
    h4["stressed_l3_h05"] = h4_report(stressed, seed, draws)
    h4["by_margin"] = {name: h4_report([r for r in manual_recs if lo <= r["margin"] < hi], seed, draws)
                       for name, lo, hi in (("<1c", 0.0, 0.01), ("1-3c", 0.01, 0.03), (">=3c", 0.03, 9.0))}
    report["H4_arb"] = h4
    pv4: dict[str, list[float]] = defaultdict(list)          # H4 is tested on guaranteed-eligible pairs only
    for r in manual_recs:
        if r["class"] == "guaranteed-eligible" and r["pnl_worst"] is not None:
            pv4[r["game"]].append(r["pnl_worst"])
    pvals["H4_arb"] = sign_flip_p(dict(pv4), seed, draws)
    lk = h3_lock_trades(samples, data["rows"], _default_fee, latency_s, watch_s=spec.get("lock_watch_s", 600), settle=settle,
                        cooldown_s=cd, venue_latency=venue_latency)
    report["H3_lock"] = {**summarize(lk["rets"], lk["tries"], seed=seed, draws=draws, times=lk["times"]), "entries_filled": lk["filled"],
                         "locked": lk["locked"], "inventory": lk["inventory"],
                         "lock_conversion": (lk["locked"] / lk["filled"]) if lk["filled"] else None,
                         "median_seconds_to_lock": lk["median_seconds_to_lock"],
                         "locked_only": summarize(lk["locked_only"], {g: len(v) for g, v in lk["locked_only"].items()}, seed=seed, draws=draws),
                         "hold_no_lock": summarize(lk["hold"], {g: len(v) for g, v in lk["hold"].items()}, seed=seed, draws=draws),
                         "note": ("locking mostly happens after the entry has moved in its favour: it turns winners into sure "
                                  "profit; compare with hold_no_lock (same entries, same watch, never locked)")}
    primary = spec["primary"].rstrip("s").replace("H3@", "H3_leadlag@")
    alpha = float(spec["alpha"])
    report["primary"] = {"hypothesis": primary, "p_value": pvals.get(primary), "test": "game-level sign-flip, one-sided (mean > 0)",
                         "alpha": alpha, "passes": (pvals.get(primary, 1.0) <= alpha)}
    family = [k for k in spec["secondary"] if k != primary]      # the primary is tested alone, never in the family
    fam = {k: pvals[k] for k in family if k in pvals}
    report["secondary"] = {"family": family, "p": fam, "holm_pass": holm(fam, alpha), "test": "game-level sign-flip, one-sided; Holm"}
    report["decisions"] = decisions
    report["fold_role"] = {"discovery": "descriptive: these games shaped the rules; not evidence",
                           "validation": "chronological check before the test; not the test",
                           "test": "the pre-registered test"}.get(fold, fold)
    return _strip_private(report)


def _strip_private(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _strip_private(v) for k, v in x.items() if not str(k).startswith("_")}
    if isinstance(x, list):
        return [_strip_private(v) for v in x]
    return x


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
    ap.add_argument("--freeze", type=Path, help="discovery only: fit the forecast models on every discovery game and write the frozen artifact")
    ap.add_argument("--frozen", type=Path, help="the discovery-frozen forecast models (validation and test read them; default the fixture)")
    ap.add_argument("--freeze-spec", type=Path, nargs="?", const=DEFAULT_FROZEN_SPEC,
                    help="record the current effective spec hash (with the frozen models) as frozen: the test fold opens only under it")
    ap.add_argument("--frozen-spec", type=Path, default=DEFAULT_FROZEN_SPEC, help="the frozen spec record the test fold checks")
    args = ap.parse_args(argv)
    manifest = json.loads(args.manifest.read_text()) if args.manifest.exists() else {"discovery": [], "test_from": "2026-10-08"}
    validate_manifest(manifest)
    spec = _spec_with_defaults(manifest.get("spec") or DEFAULT_SPEC)
    if args.reopen_test is not None and not args.reopen_test.strip():
        raise ValueError("--reopen-test requires a non-empty reason")
    frozen = None
    if args.freeze and args.fold != "discovery":
        raise SystemExit("--freeze is discovery only: the models are fitted on discovery and frozen before validation and test")
    if args.fold in ("validation", "test") or args.freeze_spec:
        path = args.frozen or DEFAULT_FROZEN
        if not path.exists():
            raise SystemExit(f"no frozen models at {path}: run --fold discovery --freeze {path} first")
        frozen = json.loads(path.read_text())
    runtime = {"latency_override": args.latency}        # a CLI override changes results, so it is part of the spec
    eff = effective_spec(manifest, frozen, runtime=runtime)
    digest = spec_hash(eff)
    ledger = ledger_path(args.frozen_spec)
    if args.freeze_spec:
        if args.fold == "test":
            raise SystemExit("--freeze-spec cannot be combined with --fold test")
        rec = {"spec_hash": digest, "hashes": component_hashes(eff), "frozen_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "git_head": _git_head(), "test_ledger": ledger.name,
               "note": "the test fold opens only under this hash; a different one needs --reopen-test and is exploratory"}
        args.freeze_spec.parent.mkdir(parents=True, exist_ok=True)
        args.freeze_spec.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")
        append_log(args.log, {"utc": rec["frozen_utc"], "event": "freeze-spec", "fold": args.fold, "spec_hash": digest,
                              "hashes": rec["hashes"], "git_head": rec["git_head"], "frozen_spec": str(args.freeze_spec),
                              "argv": list(argv) if argv is not None else sys.argv[1:]})
        print(json.dumps(rec, indent=2, sort_keys=True))
        return 0
    exploratory = False
    if args.fold == "test":
        if not args.frozen_spec.exists():
            raise SystemExit(f"the test fold is closed until the discovery implementation and spec hash are frozen ({args.frozen_spec}; --freeze-spec)")
        fz = json.loads(args.frozen_spec.read_text())
        changed = fz.get("spec_hash") != digest
        if changed and not (args.reopen_test or "").strip():
            raise SystemExit("the effective spec differs from the frozen one; the test fold stays closed (a change after opening needs --reopen-test)")
        exploratory = guard_test_open(ledger, digest, args.reopen_test) or changed
        import uuid

        run_id = uuid.uuid4().hex
        # Durable intent, before a single test row is read: a crash or a different --log cannot
        # erase the fact that the test fold was opened under this spec.
        append_log(ledger, {"event": "intent", "run_id": run_id, "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "spec_hash": digest, "reopen_reason": args.reopen_test, "exploratory": exploratory, "audit_log": str(args.log),
                            "git_head": _git_head(), "argv": list(argv) if argv is not None else sys.argv[1:]})
    try:
        report = _run_fold(args, manifest, spec, frozen)
    except BaseException as exc:
        if args.fold == "test":
            append_log(ledger, {"event": "failed", "run_id": run_id, "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "error": repr(exc)[:500]})
        raise
    report.update({"spec_hash": digest, "hashes": component_hashes(eff), "exploratory": exploratory})
    if exploratory:
        report["decisions"] = {k: f"exploratory:{v}" for k, v in (report.get("decisions") or {}).items()}
    report = _round(report)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    results_sha = hashlib.sha256(rendered.encode()).hexdigest()
    append_log(args.log, {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "git_head": _git_head(), "fold": args.fold,
                          "spec_hash": digest, "hashes": component_hashes(eff), "reopen_reason": args.reopen_test,
                          "exploratory": exploratory, "results_sha256": results_sha,
                          "argv": list(argv) if argv is not None else sys.argv[1:]})
    if args.results:
        args.results.parent.mkdir(parents=True, exist_ok=True)
        args.results.write_text(rendered)
    if args.fold == "test":
        append_log(ledger, {"event": "completed", "run_id": run_id, "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "results_sha256": results_sha})
    print(rendered, end="")
    return 0


def _run_fold(args: Any, manifest: dict[str, Any], spec: dict[str, Any], frozen: Optional[dict[str, Any]]) -> dict[str, Any]:
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
            report = {"experiment": "microstructure", "fold": args.fold,
                      **evaluate(data, spec, latency, legacy, fold=args.fold, frozen=frozen)}
            if args.freeze:
                fit_on = build(data["rows"], sample="unconditional", horizons=spec["horizons"], fee_for_row=lambda r: None)
                n_train: dict[str, Any] = {}
                art = {"coef": fit_models(fit_on, spec["horizons"], n_train), "n_train": n_train, "trained_on": sorted({_game(s["event_key"]) for s in fit_on}),
                       "folds": ["discovery"], "note": "fitted once on every discovery game; validation and test read it, never refit"}
                args.freeze.parent.mkdir(parents=True, exist_ok=True)
                args.freeze.write_text(json.dumps(_round(art), indent=2, sort_keys=True) + "\n")
                report["frozen_models_written"] = {"path": str(args.freeze), "sha256": spec_hash(_round(art))}
            if legacy:
                report["caveat"] = "legacy 5 s timestamps: the book 1 s after a decision was never observed"
    return report


if __name__ == "__main__":
    raise SystemExit(main())

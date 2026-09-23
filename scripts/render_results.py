#!/usr/bin/env python3
"""Render the docs' results tables from the committed metrics fixtures.

Every number in README.md / docs/*.md that comes from a replay or a measurement script
lives in ``tests/fixtures/results/<name>.json`` (metrics only, canonical JSON). This script
turns each fixture into Markdown tables and keeps the docs' copies in sync through marker
blocks::

    <!-- results:nfl_w1_pooled -->
    | source | ... |
    <!-- /results:nfl_w1_pooled -->

    python scripts/render_results.py --list              # block names and their fixtures
    python scripts/render_results.py --print nfl_w1_fit  # one rendered block
    python scripts/render_results.py --check             # every doc block equals its render (CI)
    python scripts/render_results.py --write             # rewrite the blocks in place

One renderer per fixture shape; output is deterministic (fixed decimals, fixed row order,
sorted keys wherever the fixture's order is not meaningful). ``tests/test_docs_results.py``
imports this module and runs ``--check`` without a subprocess. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "tests" / "fixtures" / "results"
DOC_PATHS = ("README.md", "docs/*.md", "docs/results/*.md")

BLOCK_RE = re.compile(r"<!-- results:([a-z0-9_]+) -->\n(.*?)<!-- /results:\1 -->", re.S)


# ---- formatting helpers -----------------------------------------------------------------------

def f4(v: Any) -> str:
    return f"{v:.4f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else "–"


def f3(v: Any) -> str:
    return f"{v:.3f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else "–"


def f2(v: Any) -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else "–"


def signed4(v: Any) -> str:
    return f"{v:+.4f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else "–"


def pct(v: Any, digits: int = 1) -> str:
    """ROI / share as a signed percentage: +3.5%."""
    return f"{v * 100:+.{digits}f}%" if isinstance(v, (int, float)) and not isinstance(v, bool) else "–"


def share(v: Any, digits: int = 1) -> str:
    """Unsigned share: 88.5%."""
    return f"{v * 100:.{digits}f}%" if isinstance(v, (int, float)) and not isinstance(v, bool) else "–"


def ci(lo: Any, hi: Any, fmt: Callable[[Any], str] = signed4) -> str:
    if lo is None or hi is None:
        return "–"
    return f"[{fmt(lo)}, {fmt(hi)}]"


def ci2(d: Optional[dict]) -> str:
    if not d or d.get("lo") is None:
        return "–"
    return f"[{d['lo']:+.2f}, {d['hi']:+.2f}]"


def excludes_zero(lo: Any, hi: Any) -> str:
    if lo is None or hi is None:
        return "–"
    return "yes" if (lo > 0 or hi < 0) else "no"


def n(v: Any) -> str:
    return f"{v:,}" if isinstance(v, int) and not isinstance(v, bool) else ("–" if v is None else str(v))


def table(header: list[str], rows: Iterable[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(lines) + "\n"


def load(name: str) -> dict:
    with open(RESULTS_DIR / f"{name}.json", encoding="utf-8") as f:
        return json.load(f)


# ---- replay weeks (replay_nfl_2026_w1, replay_ncaaf_2026_w2) --------------------------------

POOLED_ROWS = (
    ("model", "model (state before the play)"),
    ("model_after", "model (state after the play)"),
    ("espn", "ESPN win probability"),
    ("kalshi_before", "Kalshi mid, last candle before the play"),
    ("kalshi_after", "Kalshi mid, first candle after the play"),
    ("polymarket", "Polymarket last trade"),
    ("market", "market consensus"),
    ("blend", "blend 0.30 market / 0.55 model / 0.15 ESPN"),
    ("blend_after", "blend, post-play state and candle"),
    ("kalshi_tight", "Kalshi, book ≤ 4¢ wide"),
    ("model_when_tight", "model on those same plays"),
)

INTERVAL_ROWS = (
    ("model_minus_kalshi_before", "model − Kalshi (before the play)"),
    ("model_minus_kalshi_after", "model − Kalshi (after the play)"),
    ("model_after_minus_kalshi_after", "model after − Kalshi after"),
    ("blend_minus_model", "blend (current weights) − model"),
    ("espn_minus_model", "ESPN − model"),
    ("best_grid_minus_current", "best grid weights − current weights"),
)

STRATA_SOURCES = (("model", "model"), ("model_after", "model after"), ("espn", "ESPN"), ("kalshi_before", "Kalshi before"), ("kalshi_after", "Kalshi after"), ("blend", "blend"), ("blend_after", "blend after"))

PAIRING_LABELS = {
    "pre_before": "pre-play fair vs candle before (honest, conservative)",
    "post_after": "post-play fair vs candle after (honest, executable)",
    "pre_after": "pre-play fair vs candle after (leaky, for comparison)",
    "shift": "placebo: fair vs one candle later still",
}
LOCK_LABELS = ("lock when guaranteed ≥ 0% of hold EV (break-even)", "lock when guaranteed ≥ 50% of hold EV", "lock when guaranteed ≥ 100% of hold EV")


def _pooled(d: dict) -> str:
    rows = []
    for key, label in POOLED_ROWS:
        s = d["pooled_scrimmage"].get(key)
        if not s or not s.get("n"):
            continue
        ip = d["pooled_inplay"].get(key) or {}
        al = d["pooled_all"].get(key) or {}
        rows.append([label, n(s["games"]), n(s["n"]), f4(s["log_loss"]), f4(s["brier"]), f4(ip.get("log_loss")), f4(al.get("log_loss"))])
    return table(["source (P(home) per play)", "games", "scrimmage plays", "log-loss", "Brier", "log-loss, all in-play rows", "log-loss, all rows"], rows)


def _intervals(d: dict) -> str:
    rows = []
    for key, label in INTERVAL_ROWS:
        v = d["intervals"].get(key)
        if not v or v.get("mean") is None:
            continue
        rows.append([label, signed4(v["mean"]), ci(v["lo90"], v["hi90"]), excludes_zero(v["lo90"], v["hi90"]), n(v["n_games"]), n(v.get("games_needed_at_observed"))])
    return table(["paired log-loss difference (in-play scrimmage)", "mean", "90% game-cluster interval", "excludes zero", "games", "games needed at this effect"], rows)


def _fit(d: dict) -> str:
    rows = []
    for key in ("fit", "fit_logit"):
        f = d.get(key)
        if not f or not f.get("n"):
            continue
        b, c, co = f["best"], f["current"], f["corners"]
        rows.append([f.get("pool", "linear"), n(f["n"]), f"{b['market']:.2f} / {b['model']:.2f} / {b['espn']:.2f}", f4(b["log_loss"]), f"{c['market']:.2f} / {c['model']:.2f} / {c['espn']:.2f}", f4(c["log_loss"]), f4(co["market"]), f4(co["model"]), f4(co["espn"])])
    return table(["pool", "plays (all three sources)", "best market / model / ESPN", "log-loss", "current weights", "log-loss", "market only", "model only", "ESPN only"], rows)


def _slices(d: dict) -> str:
    st = d["strata"]
    rows = []
    for name in ("q1", "q2", "q3", "q4_early", "q4_late", "ot"):
        cell = st["by_slice"].get(name)
        if not cell:
            continue
        rows.append([name, n(cell["n"])] + [f4((cell.get(k) or {}).get("log_loss")) for k, _ in STRATA_SOURCES])
    return table(["slice", "rows"] + [lbl for _, lbl in STRATA_SOURCES], rows)


def _classes(d: dict) -> str:
    st = d["strata"]["by_play_class"]
    gaps = d.get("class_gaps") or {}
    rows = []
    for name in sorted(st):
        cell = st[name]
        g = gaps.get(name) or {}
        steal = g.get("steal") or {}
        rows.append([name, n(cell["n"])] + [f4((cell.get(k) or {}).get("log_loss")) for k in ("model", "espn", "kalshi_before", "kalshi_after", "blend")] + [f4(g.get("mean_abs_gap")), f"{steal.get('0.03', 0)} / {steal.get('0.05', 0)} / {steal.get('0.08', 0)}"])
    return table(["play class", "rows", "model", "ESPN", "Kalshi before", "Kalshi after", "blend", "mean \\|model − Kalshi before\\|", "STEAL-qualifying rows at 3% / 5% / 8%"], rows)


def _sim_rows(label: str, sim: dict) -> list[list[str]]:
    rows = []
    for edge in sorted(sim["by_edge"], key=float):
        v = sim["by_edge"][edge]
        rows.append([label, f"{float(edge):.2f}", n(v["games_traded"]), n(v["locks"]), n(v["wins"]), n(v["losses"]), f2(v["cost"]), f"{v['pnl']:+.2f}", pct(v["roi"]), ci2(v.get("pnl_per_game_90"))])
    return rows


def _steal(d: dict) -> str:
    rows: list[list[str]] = []
    for sim in d.get("simulations", []):
        if sim.get("lock") or sim.get("source") != "blend":
            continue
        label = "placebo: games' outcomes shuffled (post-play fair vs candle after)" if sim.get("placebo") == "shuffle" else PAIRING_LABELS.get(sim.get("pairing"), str(sim.get("pairing")))
        rows.extend(_sim_rows(label, sim))
    return table(["pairing (fair = blend, hold to settlement)", "edge", "games", "locks", "wins", "losses", "staked $", "P&L $", "ROI", "90% interval, P&L per game"], rows)


def _lock(d: dict) -> str:
    rows: list[list[str]] = []
    lock_i = 0
    for sim in d.get("simulations", []):
        if sim.get("lock"):
            # the harness emits the lock variants in the order lock_fraction = 0.0, 0.5, 1.0
            label = LOCK_LABELS[lock_i] if lock_i < len(LOCK_LABELS) else f"lock variant {lock_i + 1}"
            lock_i += 1
            rows.extend(_sim_rows(label, sim))
        elif sim.get("source") == "model" and not sim.get("placebo"):
            rows.extend(_sim_rows("fair = model, hold to settlement", sim))
    return table(["rule (post-play fair vs candle after)", "edge", "games", "locks", "wins", "losses", "staked $", "P&L $", "ROI", "90% interval, P&L per game"], rows)


def render_replay(prefix: str, d: dict) -> dict[str, str]:
    return {
        f"{prefix}_pooled": _pooled(d),
        f"{prefix}_intervals": _intervals(d),
        f"{prefix}_fit": _fit(d),
        f"{prefix}_slices": _slices(d),
        f"{prefix}_classes": _classes(d),
        f"{prefix}_steal": _steal(d),
        f"{prefix}_lock": _lock(d),
    }


# ---- P05: WP rules table checks ---------------------------------------------------------------

def render_week1_p05(d: dict) -> dict[str, str]:
    si, ny, kp, tr, ot, kn = d["spread_inversion"], d["neutral_yardline"], d["kickoff_pending_synth"], d["try_synth"], d["ot"], d["kneel"]
    rows = [
        ["spread inversion", "max \\|error\\| over the pre-game table (points)", f3(si["max_abs_error_pts"])],
        ["spread inversion", "integer-grid table monotone / grid points", f"{'yes' if si['table_monotone'] else 'no'} / {si['grid_points']}"],
    ]
    for season in sorted(ny["yardline_by_season"]):
        rows.append(["neutral yardline", f"season {season}: yardline_100 / pre-game Δ at −3 / late one-score kickoff-pending Δ", f"{ny['yardline_by_season'][season]:.0f} / {ny['pregame_delta_at_minus3'][season]:+.6f} / {ny['late_one_score_kickoff_pending_delta'][season]:+.6f}"])
    rows += [
        ["kickoff-pending state", "synthetic rows / mean receiver shift / mean \\|shift\\|", f"{kp['rows']} / {kp['mean_receiver_shift']:+.5f} / {kp['mean_abs_shift']:.5f}"],
        ["try state", "rows / two-point rows / bracket violations / mean \\|shift\\| vs 1st & goal from the 2", f"{tr['rows']} / {tr['two_point_rows']} / {tr['bracket_violations']} / {tr['mean_abs_shift_vs_first_and_goal_from_2']:.6f}"],
        ["overtime", "rows / outputs exactly 0 or 1 / P(home) for a tie at 0:00", f"{ot['rows']} / {ot['exact_0_or_1']} / {ot['tie_at_zero']:.6f}"],
        ["kneel floor", "rows / lifted with the flag off / lifted with the flag on / flag default", f"{kn['rows']} / {kn['lifted_flag_off']} / {kn['lifted_flag_on']} / {'on' if kn['flag_default'] else 'off'}"],
    ]
    return {"week1_p05_rules": table(["rule", "check", "value"], rows)}


# ---- P04: college spread-rescale experiment ---------------------------------------------------

def render_college_experiment_p04(d: dict) -> dict[str, str]:
    p = d["params"]
    rows = []
    for phase in ("regulation", "overtime", "all"):
        r = d["results"][phase]
        ll = r["log_loss"]
        rows.append([phase, n(r["n_games"]), n(r["n_rows"]), f4(ll["baseline"]), f4(ll["clamp"]), f4(ll["rescale"]), f4(ll["rescale_ot"])])
    t1 = table(["phase", "games", "rows", "baseline (NFL model as is)", f"clamp \\|spread\\| ≤ {p['clamp']}", f"rescale × {p['scale']}", f"rescale + OT clock ({p['ot_seconds']} s)"], rows)
    rows2 = []
    for phase in ("regulation", "all"):
        for variant in ("clamp", "rescale", "rescale_ot"):
            v = d["results"][phase]["vs_baseline"][variant]
            rows2.append([phase, variant, signed4(v["mean"]), ci(v["lo90"], v["hi90"]), excludes_zero(v["lo90"], v["hi90"]), n(v["n_games"])])
    t2 = table(["phase", "variant − baseline (log-loss)", "mean", "90% game-cluster interval", "excludes zero", "games"], rows2)
    return {"college_experiment_p04": t1, "college_experiment_p04_intervals": t2}


# ---- P04: ESPN win-probability alignment ------------------------------------------------------

def render_espn_wp_alignment_p04(d: dict) -> dict[str, str]:
    p = d["pooled"]
    rows = [
        ["games / plays / scoring plays scored", f"{p['games']} / {p['n_plays']} / {p['n']}"],
        ["share of ESPN WP jumps that land before the play's own entry", share(p["share_jump_before"])],
        ["mean \\|jump\\| between the previous entry and the play's entry", f4(p["mean_jump_before"])],
        ["mean \\|jump\\| between the play's entry and the next", f4(p["mean_jump_after"])],
        ["model (pre-play state) closer to ESPN's post-play number", share(p["model_share_closer_to_post"])],
        ["mean \\|model − ESPN\\| scored pre-play / post-play", f"{f4(p['mean_abs_delta_pre'])} / {f4(p['mean_abs_delta_post'])}"],
        ["rows that needed the fallback alignment", share(p["fallback_share"])],
        ["verdict", f"ESPN's entry is the state **{p['alignment']}** the play"],
    ]
    t1 = table(["pooled (NFL 2026 week 1)", "value"], rows)
    rows2 = [[g["game"], n(g["n"]), share(g["share_jump_before"]), f4(g["mean_jump_before"]), f4(g["mean_jump_after"]), g["alignment"]] for g in d["games"]]
    t2 = table(["game", "scoring plays", "jump lands before the entry", "mean \\|jump\\| before", "mean \\|jump\\| after", "alignment"], rows2)
    return {"espn_wp_alignment_p04": t1, "espn_wp_alignment_p04_games": t2}


# ---- P04: feed parity ---------------------------------------------------------------------------

def render_feed_parity_w1_p04(d: dict) -> dict[str, str]:
    p = d["pooled"]
    rows = []
    for field in ("possession", "down", "distance", "yardline_100", "home_timeouts", "away_timeouts", "clock"):
        v = p[field]
        rows.append([field + (f" (±{d['clock_tolerance']} s)" if field == "clock" else ""), n(v["n"]), n(v["agree"]), share(v["rate"], 2)])
    a = p["_alignment"]
    t1 = table(["field (ESPN-derived replay state vs nflverse pbp)", "aligned plays compared", "agree", "rate"], rows)
    rows2 = [
        ["games / aligned plays", f"{a['games']} / {a['matched']}"],
        ["ESPN plays unmatched / nflverse plays unmatched", f"{a['espn_unmatched']} / {a['nfl_unmatched']}"],
        ["administrative rows skipped (ESPN / nflverse)", f"{a['espn_admin']} / {a['nfl_admin']}"],
        ["plays aligned one row off (scoring plays carry the post-play clock)", n(a["shifted"])],
        ["play-id match rate", share(a["id_match_rate"], 2)],
        ["wall-clock anomalies", n(p["wallclock_anomalies"])],
        ["timeouts source", d["timeouts"]],
    ]
    t2 = table(["alignment", "value"], rows2)
    rows3 = [[g["game"], n(g["possession"]["n"]), share(g["possession"]["rate"], 1), share(g["down"]["rate"], 1), share(g["yardline_100"]["rate"], 1), share(g["home_timeouts"]["rate"], 1), share(g["clock"]["rate"], 1), n(g["wallclock_anomalies"])] for g in d["games"]]
    t3 = table(["game", "plays", "possession", "down", "yardline", "home timeouts", "clock", "wall-clock anomalies"], rows3)
    return {"feed_parity_w1_p04": t1, "feed_parity_w1_p04_alignment": t2, "feed_parity_w1_p04_games": t3}


# ---- P10: fee-model flip ------------------------------------------------------------------------

def render_fee_flip_p10(d: dict) -> dict[str, str]:
    b = d["baseline"]
    rows = []
    for sport in sorted(d["fixtures"]):
        for key in sorted(d["fixtures"][sport]):
            v = d["fixtures"][sport][key]
            exch, model = key.split(":", 1)
            rows.append([sport, exch, model, n(v["events"]), n(v["robinhood_rows"]), n(v["rows_on_exchange"]), n(v["rows_moved"]), n(v["margins_improved"]), n(v["margin_sign_flips"]), f"{v['arbs_before']} → {v['arbs_after']}", f"{pct(v['max_margin_before'], 2)} → {pct(v['max_margin_after'], 2)}"])
    t = table(["fixture scan", "exchange", f"model (defaults: Rothera {b['rothera_fee_model']}, CDNA {b['cdna_fee_model']})", "events", "Robinhood rows", "rows on that exchange", "rows moved", "margins improved", "sign flips", "arbs before → after", "best margin before → after"], rows)
    return {"fee_flip_p10": t}


# ---- P11: eligibility impact -----------------------------------------------------------------

def render_eligibility_p11(d: dict) -> dict[str, str]:
    rows = []
    for sport in sorted(d["sources"]):
        a, m = d["sources"][sport]["arbs"], d["sources"][sport]["maker_hedges"]
        before = ", ".join(f"{k} {v}" for k, v in sorted(m["by_hedge_venue_before"].items())) or "–"
        after = ", ".join(f"{k} {v}" for k, v in sorted(m["by_hedge_venue_after"].items())) or "–"
        rows.append([sport, n(a["snapshots"]), n(a["arbs_before"]), n(a["arbs_before_with_non_executable_leg"]), n(a["arbs_after"]), n(m["watches_before"]), n(m["watches_before_non_executable_hedge"]), share(m["share_before_non_executable"]), before, after])
    t = table(["fixture scan", "snapshots", "arbs before", "with a non-executable leg", "arbs after", "maker watches before", "hedge not executable", "share", "hedge venues before", "hedge venues after"], rows)
    s = d["summary"]
    t2 = table(["summary (executable venues: " + ", ".join(d["executable_venues"]) + ")", "value"], [
        ["arbs before → after", f"{s['arbs_before']} → {s['arbs_after']}"],
        ["arbs before with a non-executable leg", n(s["arbs_before_with_non_executable_leg"])],
        ["share of arbs that relied on a non-executable leg", share(s["share_before_non_executable"])],
        ["arbs surviving", share(s["share_after_of_before"])],
    ])
    return {"eligibility_p11": t, "eligibility_p11_summary": t2}


# ---- P13: lines evaluation --------------------------------------------------------------------

def render_lines_eval_p13(d: dict) -> dict[str, str]:
    rows = []
    for key in ("spread/pre", "spread/inplay", "total/pre", "total/inplay"):
        t = d["table"][key]
        cells = []
        for pred in ("normal", "empirical", "kalshi_before", "kalshi"):
            v = t[pred]
            cells.append("–" if v.get("log_loss") is None else f"{v['log_loss']:.4f} / {v['brier']:.4f}")
        rows.append([key.replace("/", " · "), n(t["kalshi"]["n"]), n(d["unpaired_rows"].get(key))] + cells)
    t1 = table(["market · phase", "paired rows", "unpaired rows dropped", "normal (log-loss / Brier)", "empirical", "Kalshi mid before the play", "Kalshi mid after the play"], rows)
    dv, sg, tb = d["devig"], d["sigma"], d["tables"]
    t2 = table(["inputs", "value"], [
        ["games scored / skipped", f"{d['games_scored']} / {d['games_skipped']}"],
        ["margin σ / total σ / tie mass (pre-game)", f"{sg['margin']:.4f} / {sg['total']:.4f} / {sg['tie_mass']:.6f}"],
        ["margin table seasons / σ seasons / rows used", f"{tb['seasons'][0]}–{tb['seasons'][1]} / {tb['sigma_seasons'][0]}–{tb['sigma_seasons'][1]} / {tb['rows_used']}"],
        ["seasons excluded as incomplete", ", ".join(str(s) for s in tb["excluded_incomplete_seasons"])],
        ["de-vig methods on the closing moneylines: games / heavy favourites / range across methods (mean, max)", f"{dv['n']} / {dv['n_heavy']} / {dv['range_heavy_mean']:.4f}, {dv['range_heavy_max']:.4f}"],
        ["mean \\|de-vigged close − spread-implied\\| (points)", f2(dv["abs_gap_vs_close_mean"])],
    ])
    return {"lines_eval_p13": t1, "lines_eval_p13_inputs": t2}


# ---- P09: tie-aware arb metrics on the fixture scans -------------------------------------------

def render_arb_fixture_p09(d: dict) -> dict[str, str]:
    sports = sorted(d["fixtures"])
    rows = []
    for key in sorted(d["definitions"]):
        rows.append([key.replace("_", " ")] + [n(d["fixtures"][s].get(key)) for s in sports] + [d["definitions"][key]])
    return {"arb_fixture_p09": table(["metric"] + [f"{s.upper()} fixture" for s in sports] + ["definition"], rows)}


def render_micro_synthetic(d: dict) -> dict[str, str]:
    return {"micro_synthetic": table(["candidate", "MAE", "persistence MAE", "skill", "decision"], [[d["candidate"], f3(d["mae"]), f3(d["persistence_mae"]), signed4(d["skill_vs_persistence"]), d["decision"]]])}


def render_micro_discovery(d: dict) -> dict[str, str]:
    """Discovery fold of scripts/microstructure_eval.py: per-contract net return (both fees,
    IOC at the decision ask after the latency) with the game-bootstrap 90 % interval."""
    def cell(m: dict) -> str:
        mr = m.get("mean_ret") or {}
        if mr.get("point") is None:
            return "-"
        return f"{mr['point'] * 100:+.1f}c [{mr['lo'] * 100:+.1f}, {mr['hi'] * 100:+.1f}] ({m['trades']}/{m['attempts']})"
    rows = []
    for h in ("5", "15", "30", "60"):
        hr = d["horizons"][h]
        rows.append([f"{h} s", cell(hr["B1_buy_any"]), cell(hr["H1_momentum"]), cell(hr["H2_dip"]), cell(hr["H3_leadlag"])])
    trades = table(["horizon", "B1 buy at random", "H1 momentum", "H2 dip", "H3 lead-lag"], rows)
    fc = []
    for h in ("5", "15", "30", "60"):
        for name, label in (("B3_ridge_dmid30", "B3 ridge on dmid_30"), ("B4_ridge_gap", "B4 ridge on leader gap")):
            m = d["horizons"][h][name]
            sk = m.get("skill_ci") or {}
            fc.append([f"{h} s", label, f"{m['mae_model'] * 100:.3f}c", f"{m['mae_persistence'] * 100:.3f}c", f"[{sk['lo'] * 100:+.3f}, {sk['hi'] * 100:+.3f}]c"])
    forecast = table(["horizon", "model", "MAE", "unchanged-price MAE", "skill 90 % CI"], fc)
    a = d["H4_arb"]
    ar = a.get("mean_ret") or {}
    arb = table(["attempts", "completed", "fill rate", "mean per contract", "90 % CI", "games positive"],
                [[str(a["attempts"]), str(a["trades"]), f"{a['fill_rate']:.0%}", f"{ar['point'] * 100:+.2f}c", f"[{ar['lo'] * 100:+.2f}, {ar['hi'] * 100:+.2f}]c", f"{a['positive_game_share']:.0%}"]])
    return {"micro_discovery_trades": trades, "micro_discovery_forecast": forecast, "micro_discovery_arb": arb}


# ---- registry ---------------------------------------------------------------------------------

RENDERERS: dict[str, Callable[[dict], dict[str, str]]] = {
    "replay_nfl_2026_w1": lambda d: render_replay("nfl_w1", d),
    "replay_ncaaf_2026_w2": lambda d: render_replay("ncaaf_w2", d),
    "week1_p05": render_week1_p05,
    "college_experiment_p04": render_college_experiment_p04,
    "espn_wp_alignment_p04": render_espn_wp_alignment_p04,
    "feed_parity_w1_p04": render_feed_parity_w1_p04,
    "fee_flip_p10": render_fee_flip_p10,
    "eligibility_p11": render_eligibility_p11,
    "lines_eval_p13": render_lines_eval_p13,
    "arb_fixture_p09": render_arb_fixture_p09,
    "micro_synthetic": render_micro_synthetic,
    "micro_discovery": render_micro_discovery,
}


def fixture_paths() -> dict[str, Path]:
    return {name: RESULTS_DIR / f"{name}.json" for name in RENDERERS}


def render_all() -> dict[str, tuple[str, str]]:
    """block name -> (fixture name, rendered Markdown)."""
    out: dict[str, tuple[str, str]] = {}
    for fixture, fn in RENDERERS.items():
        for block, text in fn(load(fixture)).items():
            if block in out:
                raise ValueError(f"block {block!r} rendered by two fixtures")
            out[block] = (fixture, text)
    return out


def doc_files(root: Path = ROOT) -> list[Path]:
    files: list[Path] = []
    for pat in DOC_PATHS:
        files.extend(sorted(root.glob(pat)))
    return files


def find_blocks(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in BLOCK_RE.finditer(text)]


def check_text(text: str, rendered: dict[str, tuple[str, str]]) -> list[str]:
    """Problems in one document: unknown block names and blocks whose body differs."""
    problems = []
    for name, body in find_blocks(text):
        if name not in rendered:
            problems.append(f"unknown results block {name!r}")
        elif body != rendered[name][1]:
            problems.append(f"block {name!r} is stale (run: python scripts/render_results.py --write)")
    return problems


def check(root: Path = ROOT) -> dict[str, list[str]]:
    rendered = render_all()
    return {str(p.relative_to(root)): probs for p in doc_files(root) if (probs := check_text(p.read_text(encoding="utf-8"), rendered))}


def rewrite_text(text: str, rendered: dict[str, tuple[str, str]]) -> str:
    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name not in rendered:
            raise KeyError(f"unknown results block {name!r}")
        return f"<!-- results:{name} -->\n{rendered[name][1]}<!-- /results:{name} -->"
    return BLOCK_RE.sub(sub, text)


def write(root: Path = ROOT) -> list[str]:
    rendered = render_all()
    changed = []
    for p in doc_files(root):
        old = p.read_text(encoding="utf-8")
        new = rewrite_text(old, rendered)
        if new != old:
            p.write_text(new, encoding="utf-8")
            changed.append(str(p.relative_to(root)))
    return changed


def used_blocks(root: Path = ROOT) -> dict[str, list[str]]:
    """block name -> documents that carry it."""
    out: dict[str, list[str]] = {}
    for p in doc_files(root):
        for name, _ in find_blocks(p.read_text(encoding="utf-8")):
            out.setdefault(name, []).append(str(p.relative_to(root)))
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="verify every marker block in README.md and docs/ equals its rendered table")
    ap.add_argument("--write", action="store_true", help="rewrite the marker blocks in place")
    ap.add_argument("--list", action="store_true", help="list block names, their fixture and where they are used")
    ap.add_argument("--print", dest="print_block", metavar="BLOCK", help="print one rendered block")
    args = ap.parse_args(argv)
    if args.print_block:
        rendered = render_all()
        if args.print_block not in rendered:
            print(f"unknown block {args.print_block!r}; known: {', '.join(sorted(rendered))}", file=sys.stderr)
            return 2
        sys.stdout.write(rendered[args.print_block][1])
        return 0
    if args.list:
        rendered, used = render_all(), used_blocks()
        for name in sorted(rendered):
            print(f"{name:<36} {rendered[name][0]:<26} {', '.join(used.get(name, [])) or '(unused)'}")
        return 0
    if args.write:
        changed = write()
        print("rewrote: " + (", ".join(changed) if changed else "nothing (all blocks current)"))
        return 0
    if args.check:
        problems = check()
        for path, probs in problems.items():
            for p in probs:
                print(f"{path}: {p}")
        print("results blocks: " + ("OK" if not problems else f"{sum(len(v) for v in problems.values())} problem(s)"))
        return 1 if problems else 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

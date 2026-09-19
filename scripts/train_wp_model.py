#!/usr/bin/env python3
"""Train the in-game NFL win-probability model on nflverse play-by-play.

    python3 scripts/train_wp_model.py                 # 2016-2024 train, 2025 held out
    python3 scripts/train_wp_model.py --rounds 200 --depth 4 --eta 0.05 --min-child-weight 20   # the shipped defaults
    python3 scripts/train_wp_model.py --data-dir out/data --quick   # subsample for a smoke run

What it does
------------
1. Downloads ``play_by_play_<year>.csv.gz`` from the nflverse-data GitHub release into
   ``--data-dir`` (default ``$TMPDIR/nflpbp``) with ``curl -L --compressed`` when missing.
2. Parses the CSVs with the standard library (gzip + csv), keeps only the columns the
   model needs, caches them per season as ``.npz`` so re-runs are seconds, not minutes.
3. Builds nflfastR's ``vegas_wp`` feature set from the possession team's perspective
   (see ``arb_engine/models/wp.py`` for the list), labels a play 1 when the posteam won
   (ties excluded), fits a numpy logistic regression (features + squares + pairwise
   interactions) as a smooth baseline, then trains XGBoost ``binary:logistic`` trees on
   top of its margin (``base_margin`` stacking; ``--no-stack`` for plain trees) and
   scores the held-out season: log-loss, Brier, a 10-bin calibration table, and the same
   numbers for nflfastR's own ``vegas_wp`` on the same rows.
4. Exports the trees (from ``Booster.get_dump(dump_format="json")``, compacted to flat
   per-tree arrays), ``base_score`` and the feature list to
   ``arb_engine/data/nfl_wp_model.json``, plus metrics to ``nfl_wp_model.meta.json``, and
   checks the standard-library walker reproduces ``Booster.predict`` on the test rows.

If XGBoost cannot be imported (no OpenMP runtime on macOS without Homebrew, for
example) the script first tries to re-exec itself with ``DYLD_LIBRARY_PATH`` pointing at
a ``libomp.dylib`` bundled in an installed wheel (scikit-learn ships one), and if that
also fails it trains a numpy logistic regression on the same features plus squared and
pairwise-interaction terms and says so loudly in the output and in the meta file.

Dependencies (training only; inference is stdlib): numpy, and ideally xgboost.
    pip3 install --use-deprecated=legacy-certs numpy xgboost scikit-learn
"""

from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from arb_engine.models.wp import FEATURES, WinProbModel  # noqa: E402

RELEASE_URL = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{year}.csv.gz"
DEFAULT_DATA_DIR = Path(os.environ.get("TMPDIR") or "out/data") / "nflpbp"
MODEL_PATH = REPO / "arb_engine" / "data" / "nfl_wp_model.json"
META_PATH = REPO / "arb_engine" / "data" / "nfl_wp_model.meta.json"

# Columns we pull out of the 370-column CSV. Everything else is ignored at parse time.
NUMERIC_COLS = [
    "score_differential", "game_seconds_remaining", "half_seconds_remaining", "qtr", "down",
    "ydstogo", "yardline_100", "posteam_timeouts_remaining", "defteam_timeouts_remaining",
    "spread_line", "result", "vegas_wp", "wp", "week",
]
TEXT_COLS = ["game_id", "home_team", "away_team", "posteam", "defteam", "season_type"]


# --------------------------------------------------------------------------------------
# Optional dependencies
# --------------------------------------------------------------------------------------

def _import_numpy():
    try:
        import numpy as np  # noqa: F401
        return np
    except ImportError:
        sys.exit("numpy is required for training: pip3 install --use-deprecated=legacy-certs numpy")


def _try_import_xgboost():
    """Return the xgboost module or None; re-exec with a bundled libomp if that is the blocker."""
    try:
        import xgboost as xgb
        return xgb
    except ImportError:
        return None
    except Exception as exc:  # XGBoostError: libomp missing on macOS
        msg = str(exc)
        if "libomp" in msg and sys.platform == "darwin" and not os.environ.get("ARB_WP_REEXEC"):
            candidates = []
            for sp in sys.path:
                if sp and os.path.isdir(sp):
                    candidates += glob.glob(os.path.join(sp, "*", ".dylibs", "libomp.dylib"))
                    candidates += glob.glob(os.path.join(sp, "*", "lib", "libomp.dylib"))
            if candidates:
                libdir = os.path.dirname(candidates[0])
                print(f"[xgboost] libomp.dylib missing; re-executing with DYLD_LIBRARY_PATH={libdir}", flush=True)
                env = dict(os.environ)
                env["DYLD_LIBRARY_PATH"] = libdir + (":" + env["DYLD_LIBRARY_PATH"] if env.get("DYLD_LIBRARY_PATH") else "")
                env["ARB_WP_REEXEC"] = "1"
                os.execve(sys.executable, [sys.executable] + sys.argv, env)
        print(f"[xgboost] import failed ({msg.splitlines()[0] if msg else exc!r}); will fall back to logistic regression", flush=True)
        return None


# --------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------

def download(year: int, data_dir: Path) -> Path:
    path = data_dir / f"play_by_play_{year}.csv.gz"
    if path.exists() and path.stat().st_size > 1_000_000:
        return path
    data_dir.mkdir(parents=True, exist_ok=True)
    url = RELEASE_URL.format(year=year)
    print(f"[download] {url}", flush=True)
    tmp = path.with_suffix(".part")
    subprocess.run(["curl", "-sSL", "--compressed", "-o", str(tmp), url], check=True)
    if tmp.stat().st_size < 1_000_000:
        raise RuntimeError(f"download of {url} looks truncated ({tmp.stat().st_size} bytes)")
    tmp.rename(path)
    return path


def parse_season(path: Path, np):
    """Parse one season's CSV into dict-of-arrays (numeric float arrays, text object arrays)."""
    num = {c: [] for c in NUMERIC_COLS}
    txt = {c: [] for c in TEXT_COLS}
    with gzip.open(path, "rt", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        idx = {c: header.index(c) for c in NUMERIC_COLS + TEXT_COLS if c in header}
        missing = [c for c in NUMERIC_COLS + TEXT_COLS if c not in idx]
        if missing:
            raise RuntimeError(f"{path.name} lacks columns {missing}")
        nidx = [(c, idx[c]) for c in NUMERIC_COLS]
        tidx = [(c, idx[c]) for c in TEXT_COLS]
        for row in reader:
            for c, i in nidx:
                v = row[i]
                num[c].append(float(v) if v not in ("", "NA") else math.nan)
            for c, i in tidx:
                txt[c].append(row[i])
    out = {c: np.asarray(v, dtype=np.float64) for c, v in num.items()}
    out.update({c: np.asarray(v, dtype=object) for c, v in txt.items()})
    return out


def load_season(year: int, data_dir: Path, np):
    cache = data_dir / f"wp_cols_{year}.npz"
    if cache.exists():
        with np.load(cache, allow_pickle=True) as z:
            return {k: z[k] for k in z.files}
    path = download(year, data_dir)
    t0 = time.time()
    cols = parse_season(path, np)
    np.savez_compressed(cache, **cols)
    print(f"[parse] {path.name}: {len(cols['game_id'])} rows in {time.time() - t0:.1f}s", flush=True)
    return cols


def build_features(cols, np, regulation_only: bool = True):
    """Return (X, y, vegas_wp, game_id, keep_mask) for one season."""
    n = len(cols["game_id"])
    posteam = cols["posteam"]
    home = cols["home_team"]
    away = cols["away_team"]
    defteam = cols["defteam"]
    game_id = cols["game_id"]

    # receive_2h_ko (nflfastR): 1 in the first half when the posteam is the team that
    # kicked off to open the game == defteam of the game's first play with a posteam.
    first_def = {}
    for i in range(n):
        g = game_id[i]
        if g not in first_def and defteam[i]:
            first_def[g] = defteam[i]
    receive = np.zeros(n)
    for i in range(n):
        if cols["qtr"][i] <= 2 and posteam[i] and posteam[i] == first_def.get(game_id[i]):
            receive[i] = 1.0

    is_home = np.array([1.0 if p and p == h else 0.0 for p, h in zip(posteam, home)])
    has_pos = np.array([bool(p) for p in posteam])
    gsr = cols["game_seconds_remaining"]
    hsr = cols["half_seconds_remaining"]
    sd = cols["score_differential"]
    down = cols["down"]
    ydstogo = cols["ydstogo"]
    yl = cols["yardline_100"]
    pto = cols["posteam_timeouts_remaining"]
    dto = cols["defteam_timeouts_remaining"]
    spread_home = cols["spread_line"]           # nflverse: positive = home favoured
    result = cols["result"]                     # home score minus away score

    keep = (
        has_pos & np.isfinite(gsr) & np.isfinite(hsr) & np.isfinite(sd) & np.isfinite(down)
        & np.isfinite(ydstogo) & np.isfinite(yl) & np.isfinite(pto) & np.isfinite(dto)
        & np.isfinite(spread_home) & np.isfinite(result) & (result != 0) & (down >= 1) & (down <= 4)
        & (gsr >= 0) & (gsr <= 3600)
    )
    if regulation_only:
        keep &= cols["qtr"] <= 4

    posteam_spread = np.where(is_home == 1, spread_home, -spread_home)
    elapsed_share = (3600.0 - gsr) / 3600.0
    decay = np.exp(-4.0 * elapsed_share)
    spread_time = posteam_spread * decay
    diff_time_ratio = sd / decay

    X = np.column_stack([sd, gsr, hsr, receive, spread_time, diff_time_ratio, down, ydstogo, yl, pto, dto, is_home])
    y = np.where(is_home == 1, result > 0, result < 0).astype(np.float64)
    vegas = cols["vegas_wp"]
    return X[keep], y[keep], vegas[keep], game_id[keep], keep


# --------------------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------------------

def log_loss(y, p, np):
    p = np.clip(p, 1e-15, 1 - 1e-15)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def brier(y, p, np):
    return float(np.mean((p - y) ** 2))


def calibration_table(y, p, np, bins: int = 10):
    edges = np.linspace(0, 1, bins + 1)
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & ((p < hi) if hi < 1 else (p <= hi))
        cnt = int(m.sum())
        rows.append({
            "bin": f"{lo:.1f}-{hi:.1f}",
            "n": cnt,
            "pred_mean": float(p[m].mean()) if cnt else None,
            "actual_rate": float(y[m].mean()) if cnt else None,
        })
    return rows


def ece(table) -> float:
    total = sum(r["n"] for r in table)
    return sum(r["n"] / total * abs(r["pred_mean"] - r["actual_rate"]) for r in table if r["n"])


def print_table(title: str, table):
    print(f"\n{title}")
    print(f"{'bin':>9} {'n':>8} {'pred':>8} {'actual':>8} {'gap':>8}")
    for r in table:
        if r["n"]:
            print(f"{r['bin']:>9} {r['n']:>8} {r['pred_mean']:>8.3f} {r['actual_rate']:>8.3f} {r['actual_rate'] - r['pred_mean']:>+8.3f}")
        else:
            print(f"{r['bin']:>9} {0:>8}")


# --------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------

def train_xgboost(xgb, np, Xtr, ytr, Xte, yte, args, base_tr=None, base_te=None):
    """Boost trees on the labels; with ``base_tr``/``base_te`` (logistic margins) the trees
    learn only the residual on top of that smooth baseline (stacking)."""
    params = {
        "objective": "binary:logistic",
        "eval_metric": "logloss",
        "eta": args.eta,
        "max_depth": args.depth,
        "min_child_weight": args.min_child_weight,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "gamma": args.gamma,
        "lambda": args.reg_lambda,
        "tree_method": "hist",
        "base_score": 0.5,     # margin 0 — the walker uses logit(base_score)
        "seed": 2024,
        "nthread": max(1, os.cpu_count() or 1),
    }
    if args.monotone:
        # posteam lead, lead scaled by time, and being favoured never *lower* WP.
        mono = ["0"] * len(FEATURES)
        for name in ("score_differential", "diff_time_ratio", "spread_time"):
            mono[FEATURES.index(name)] = "1"
        params["monotone_constraints"] = "(" + ",".join(mono) + ")"
    dtr = xgb.DMatrix(Xtr, label=ytr, feature_names=list(FEATURES))
    dte = xgb.DMatrix(Xte, label=yte, feature_names=list(FEATURES))
    if base_tr is not None:
        dtr.set_base_margin(base_tr)
        dte.set_base_margin(base_te)
    t0 = time.time()
    booster = xgb.train(params, dtr, num_boost_round=args.rounds, evals=[(dte, "heldout")], verbose_eval=50)
    print(f"[xgboost] trained {args.rounds} rounds in {time.time() - t0:.0f}s", flush=True)
    return booster, params, dte


def export_xgboost(booster, params, np) -> dict:
    """get_dump(json) -> compact per-tree arrays the stdlib walker understands."""
    dumps = booster.get_dump(dump_format="json", with_stats=False)
    fidx = {name: i for i, name in enumerate(FEATURES)}
    trees = []
    for d in dumps:
        root = json.loads(d)
        nodes = []          # nodeid-ordered list of raw nodes

        def walk(node):
            nodes.append(node)
            for ch in node.get("children", []):
                walk(ch)

        walk(root)
        nodes.sort(key=lambda nd: nd["nodeid"])
        ids = {nd["nodeid"]: i for i, nd in enumerate(nodes)}
        f, t, y, n, m, v = [], [], [], [], [], []
        for nd in nodes:
            if "leaf" in nd:
                f.append(-1); t.append(0.0); y.append(-1); n.append(-1); m.append(-1); v.append(float(nd["leaf"]))
            else:
                f.append(fidx[nd["split"]]); t.append(float(nd["split_condition"]))
                y.append(ids[nd["yes"]]); n.append(ids[nd["no"]]); m.append(ids[nd["missing"]]); v.append(0.0)
        trees.append({"f": f, "t": t, "y": y, "n": n, "m": m, "v": v})
    cfg = json.loads(booster.save_config())
    raw = str(cfg["learner"]["learner_model_param"]["base_score"]).strip()
    # xgboost >= 2 reports it as "5E-1"; xgboost 3.x as a vector string "[5E-1]".
    base_score = float(json.loads(raw)[0]) if raw.startswith("[") else float(raw)
    return {
        "format": "arb-engine-nfl-wp/1",
        "model_type": "xgboost",
        "features": list(FEATURES),
        "base_score": base_score,
        "trees": trees,
        "params": {k: v for k, v in params.items() if k != "nthread"},
    }


def _expand_terms(nfeat: int):
    terms = [[i] for i in range(nfeat)]
    terms += [[i, i] for i in range(nfeat)]
    terms += [[i, j] for i in range(nfeat) for j in range(i + 1, nfeat)]
    return terms


def _design(Xz, terms, np):
    cols = [np.ones(len(Xz))]
    for term in terms:
        c = np.ones(len(Xz))
        for i in term:
            c = c * Xz[:, i]
        cols.append(c)
    return np.column_stack(cols)


def train_logistic(np, Xtr, ytr, iters: int = 60, l2: float = 1e-3):
    """Newton / IRLS logistic regression on standardized features + squares + interactions."""
    mean = Xtr.mean(axis=0)
    std = Xtr.std(axis=0)
    std[std == 0] = 1.0
    terms = _expand_terms(Xtr.shape[1])
    A = _design((Xtr - mean) / std, terms, np)
    w = np.zeros(A.shape[1])
    reg = l2 * np.eye(A.shape[1]); reg[0, 0] = 0
    for it in range(iters):
        z = A @ w
        p = 1 / (1 + np.exp(-z))
        g = A.T @ (p - ytr) + reg @ w
        s = p * (1 - p)
        H = (A * s[:, None]).T @ A + reg
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    return {
        "format": "arb-engine-nfl-wp/1",
        "model_type": "logistic",
        "features": list(FEATURES),
        "logistic": {"mean": mean.tolist(), "std": std.tolist(), "intercept": float(w[0]), "coef": w[1:].tolist(), "terms": terms},
    }


def logistic_margin(payload, X, np):
    lr = payload["logistic"]
    A = _design((X - np.asarray(lr["mean"])) / np.asarray(lr["std"]), lr["terms"], np)
    w = np.concatenate([[lr["intercept"]], lr["coef"]])
    return A @ w


def predict_logistic(payload, X, np):
    return 1 / (1 + np.exp(-logistic_margin(payload, X, np)))


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--train-seasons", default="2016-2024")
    ap.add_argument("--test-season", type=int, default=2025)
    ap.add_argument("--rounds", type=int, default=200)
    ap.add_argument("--depth", type=int, default=4)
    ap.add_argument("--eta", type=float, default=0.05)
    ap.add_argument("--min-child-weight", type=float, default=20.0)
    ap.add_argument("--gamma", type=float, default=0.0)
    ap.add_argument("--reg-lambda", type=float, default=1.0)
    ap.add_argument("--monotone", action="store_true", help="add monotone constraints on lead/spread features (costs ~0.003 log-loss on 2025)")
    ap.add_argument("--force-logistic", action="store_true", help="skip XGBoost and train the logistic fallback")
    ap.add_argument("--no-stack", dest="stack", action="store_false", help="plain XGBoost instead of trees stacked on the logistic margin")
    ap.add_argument("--quick", action="store_true", help="subsample training rows 10x for a smoke test")
    ap.add_argument("--out", type=Path, default=MODEL_PATH)
    ap.add_argument("--meta-out", type=Path, default=META_PATH)
    ap.add_argument("--max-mb", type=float, default=15.0, help="shrink rounds until the JSON is under this size")
    args = ap.parse_args()

    np = _import_numpy()
    xgb = None if args.force_logistic else _try_import_xgboost()

    lo, hi = (int(s) for s in args.train_seasons.split("-"))
    train_years = list(range(lo, hi + 1))
    Xs, ys = [], []
    n_games_train = 0
    for year in train_years:
        cols = load_season(year, args.data_dir, np)
        X, y, _, gids, _ = build_features(cols, np)
        n_games_train += len(set(gids.tolist()))
        Xs.append(X); ys.append(y)
    Xtr = np.vstack(Xs); ytr = np.concatenate(ys)
    if args.quick:
        rng = np.random.default_rng(0)
        pick = rng.random(len(ytr)) < 0.1
        Xtr, ytr = Xtr[pick], ytr[pick]
    cols = load_season(args.test_season, args.data_dir, np)
    Xte, yte, vegas_te, gids_te, _ = build_features(cols, np)
    print(f"[data] train rows {len(ytr):,} ({n_games_train} games, seasons {lo}-{hi}); test rows {len(yte):,} ({len(set(gids_te.tolist()))} games, {args.test_season})")
    print(f"[data] posteam win rate train {ytr.mean():.3f} test {yte.mean():.3f}")

    baseline_metrics = None
    if xgb is not None:
        base_tr = base_te = None
        lr_payload = None
        if args.stack:
            print("[stack] fitting the logistic baseline first (features + squares + pairwise interactions)", flush=True)
            lr_payload = train_logistic(np, Xtr, ytr)
            base_tr = logistic_margin(lr_payload, Xtr, np)
            base_te = logistic_margin(lr_payload, Xte, np)
            p_lr = 1 / (1 + np.exp(-base_te))
            baseline_metrics = {"log_loss": log_loss(yte, p_lr, np), "brier": brier(yte, p_lr, np), "ece": ece(calibration_table(yte, p_lr, np))}
            print(f"[stack] logistic baseline alone on {args.test_season}: log-loss {baseline_metrics['log_loss']:.5f}  Brier {baseline_metrics['brier']:.5f}", flush=True)
        booster, params, dte = train_xgboost(xgb, np, Xtr, ytr, Xte, yte, args, base_tr, base_te)
        payload = export_xgboost(booster, params, np)
        if lr_payload is not None:
            payload["base_logistic"] = lr_payload["logistic"]
        p_te = booster.predict(dte)
        # Shrink if the JSON would be too large.
        while args.max_mb and len(json.dumps(payload)) > args.max_mb * 1e6 and len(payload["trees"]) > 50:
            payload["trees"] = payload["trees"][: int(len(payload["trees"]) * 0.8)]
            print(f"[export] JSON over {args.max_mb} MB, keeping {len(payload['trees'])} trees", flush=True)
        if len(payload["trees"]) != args.rounds:
            p_te = booster.predict(dte, iteration_range=(0, len(payload["trees"])))
        stacked = " stacked on a logistic baseline" if lr_payload is not None else ""
        model_desc = f"xgboost {xgb.__version__} binary:logistic, {len(payload['trees'])} trees, depth {args.depth}, eta {args.eta}{stacked}"
    else:
        print("[fallback] training numpy logistic regression (features + squares + pairwise interactions)", flush=True)
        payload = train_logistic(np, Xtr, ytr)
        p_te = predict_logistic(payload, Xte, np)
        model_desc = "numpy logistic regression fallback (standardized features + squares + pairwise interactions)"

    p_te = np.asarray(p_te, dtype=np.float64)
    ok_v = np.isfinite(vegas_te)
    metrics = {
        "model": {
            "log_loss": log_loss(yte, p_te, np),
            "brier": brier(yte, p_te, np),
            "calibration": calibration_table(yte, p_te, np),
        },
        "nflfastr_vegas_wp": {
            "log_loss": log_loss(yte[ok_v], vegas_te[ok_v], np),
            "brier": brier(yte[ok_v], vegas_te[ok_v], np),
            "calibration": calibration_table(yte[ok_v], vegas_te[ok_v], np),
            "rows": int(ok_v.sum()),
        },
    }
    if baseline_metrics:
        metrics["logistic_baseline"] = baseline_metrics
    metrics["model"]["ece"] = ece(metrics["model"]["calibration"])
    metrics["nflfastr_vegas_wp"]["ece"] = ece(metrics["nflfastr_vegas_wp"]["calibration"])
    # Same-row comparison (vegas_wp can be NA on a handful of plays).
    metrics["model"]["log_loss_on_vegas_rows"] = log_loss(yte[ok_v], p_te[ok_v], np)
    metrics["model"]["brier_on_vegas_rows"] = brier(yte[ok_v], p_te[ok_v], np)
    corr = float(np.corrcoef(p_te[ok_v], vegas_te[ok_v])[0, 1])
    mad = float(np.mean(np.abs(p_te[ok_v] - vegas_te[ok_v])))

    print(f"\n[heldout {args.test_season}] {model_desc}")
    print(f"  model      log-loss {metrics['model']['log_loss']:.5f}  Brier {metrics['model']['brier']:.5f}  ECE {metrics['model']['ece']:.4f}")
    print(f"  vegas_wp   log-loss {metrics['nflfastr_vegas_wp']['log_loss']:.5f}  Brier {metrics['nflfastr_vegas_wp']['brier']:.5f}  ECE {metrics['nflfastr_vegas_wp']['ece']:.4f}  (n={ok_v.sum():,})")
    print(f"  model on the same rows: log-loss {metrics['model']['log_loss_on_vegas_rows']:.5f}  Brier {metrics['model']['brier_on_vegas_rows']:.5f}")
    print(f"  corr(model, vegas_wp) {corr:.4f}; mean |diff| {mad:.4f}")
    print_table("Calibration — this model", metrics["model"]["calibration"])
    print_table("Calibration — nflfastR vegas_wp", metrics["nflfastr_vegas_wp"]["calibration"])

    # Export + parity check with the standard-library walker.
    payload["meta"] = {
        "trained_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "train_seasons": f"{lo}-{hi}",
        "test_season": args.test_season,
        "train_rows": int(len(ytr)),
        "test_rows": int(len(yte)),
        "model": model_desc,
        "heldout": {k: {kk: vv for kk, vv in v.items() if kk != "calibration"} for k, v in metrics.items()},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    size_mb = args.out.stat().st_size / 1e6
    print(f"\n[export] {args.out} ({size_mb:.2f} MB)")

    model = WinProbModel(json.loads(args.out.read_text(encoding="utf-8")))
    rng = np.random.default_rng(1)
    sample = rng.choice(len(yte), size=min(3000, len(yte)), replace=False)
    diffs = []
    for i in sample:
        feats = dict(zip(FEATURES, Xte[i].tolist()))
        diffs.append(abs(model.predict_posteam_wp(feats) - float(p_te[i])))
    parity = float(max(diffs))
    print(f"[parity] stdlib walker vs trainer on {len(sample)} rows: max |diff| {parity:.2e}")
    if parity > 1e-4:
        print("WARNING: walker disagrees with the trainer; do not ship this export", file=sys.stderr)
        return 1

    # Pre-game sanity: spread-implied probabilities (ESPN sign: negative = home favoured).
    from arb_engine.models.wp import home_win_probability
    pregame = {}
    for spread in (-10, -7, -3, -1, 0, 1, 3, 7):
        pregame[str(spread)] = round(home_win_probability(home_score=0, away_score=0, game_seconds_remaining=3600, vegas_spread_home=spread, model=model), 4)
    print("[pregame] P(home) by home spread:", pregame, f"symmetry P(-3)+P(+3)={pregame['-3'] + pregame['3']:.3f}")

    meta = {
        **payload["meta"],
        "file": args.out.name,
        "size_mb": round(size_mb, 3),
        "params": payload.get("params"),
        "features": list(FEATURES),
        "metrics": metrics,
        "corr_with_vegas_wp": corr,
        "mean_abs_diff_vs_vegas_wp": mad,
        "walker_parity_max_abs_diff": parity,
        "pregame_home_wp_by_spread": pregame,
        "data": {"source": RELEASE_URL, "filters": "posteam present; down 1-4; finite clock/field/timeouts/spread; result != 0; qtr <= 4 (regulation only)"},
    }
    with open(args.meta_out, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(f"[export] {args.meta_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

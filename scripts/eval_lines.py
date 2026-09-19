#!/usr/bin/env python3
"""Bracketed evaluation of line fair values against Kalshi mids on settled NFL games.

    python scripts/eval_lines.py --season 2026 --week 2 [--limit 4] [--offline]
    python -m arb_engine lines-eval --week 2          # same thing through the CLI plugin

For every finished game of the week: the DraftKings close (spread / total / moneylines) from
the ESPN summary, the play timeline (wall-clock stamped), and the Kalshi 1-minute candles of
the spread market at the closing line and the total market at the closing total. Each play
is scored on two settled binary outcomes — favourite covers, game goes over — defined on the
*Kalshi market's* line: the ticker at an integer close (SEA -3 -> SEA3) is "wins by over
2.5", so every column, the model included, is scored at ``ceil(close) - 0.5`` (a half-point
close is unchanged) and there are no pushes. Scoring the model at the integer close while the
mid prices the half-point below would compare two different binaries (a 9% key-number lump
at 7 sits between them). Three predictors:

* ``normal``     NormalMargin / NormalTotal (pre-game, and in play with the sqrt(frac) scaling)
* ``empirical``  EmpiricalMargin (pre-game only; in play it falls back to the normal, shown as '-')
* ``kalshi``     the Kalshi mid (rows are paired: every column is scored on the plays where the
                 mid exists under both alignments), read under two alignments: ``kalshi`` (first candle ending at
                 or after the play, i.e. *after* the play is public — the optimistic bracket)
                 and ``kalshi_before`` (last candle ending at or before the play — what a taker
                 could actually have hit). The truth lies between the brackets.

Pooled log-loss / Brier per market x phase x predictor x alignment go to
``out/lines_eval_w<N>.txt`` and, metrics only, ``tests/fixtures/results/lines_eval_p13.json``.
The de-vig disagreement range on the closing moneylines is reported for heavy favourites.

Network calls go through a read-through JSON cache (``out/lines_cache``); ``--offline`` serves
only from it and skips anything missing, so a run is reproducible once cached. Nothing here
is imported by the engine; the library lives in ``arb_engine/quant/lines.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arb_engine.quant.lines import EmpiricalMargin, NormalMargin, NormalTotal, default_params, spread_from_p  # noqa: E402
from arb_engine.quant.odds import sportsbook_probs_from_moneylines  # noqa: E402
from arb_engine.venues.espn import ESPNClient  # noqa: E402
from arb_engine.venues.history import Bar, HistoryClient, bar_at, espn_timeline  # noqa: E402
from arb_engine.venues.http import HttpClient  # noqa: E402

ALIGNMENTS = ("kalshi_before", "kalshi")
GAME_SECONDS = 3600
HEAVY_FAVOURITE_ML = 300


class CachingHttp:
    """Read-through JSON cache in front of ``HttpClient`` keyed by the full URL + params.

    Only GETs are cached (everything the evaluation needs). ``offline`` raises on a miss so
    the caller can skip the game instead of hitting the network."""

    def __init__(self, cache_dir: Path, offline: bool = False, http: Optional[HttpClient] = None):
        self.dir, self.offline = cache_dir, offline
        self.http = http or HttpClient(rate_limit=6)
        self.hits = self.misses = 0
        self.dir.mkdir(parents=True, exist_ok=True)

    def _key(self, url: str, params: Optional[dict]) -> Path:
        blob = url + "?" + json.dumps(params or {}, sort_keys=True)
        return self.dir / (hashlib.sha256(blob.encode("utf-8")).hexdigest() + ".json")

    def get(self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None, raw: bool = False) -> Any:
        p = self._key(url, params)
        if p.exists():
            self.hits += 1
            with open(p, encoding="utf-8") as f:
                return f.read() if raw else json.load(f)
        if self.offline:
            raise RuntimeError(f"offline: cache miss for {url}")
        self.misses += 1
        data = self.http.get(url, params=params, headers=headers, raw=raw)
        with open(p, "w", encoding="utf-8") as f:
            if raw:
                f.write(data)
            else:
                json.dump(data, f)
        return data


def bar_before(bars: list[Bar], ts: float, max_gap: float = 900.0) -> Optional[Bar]:
    """Last candle ending at or before ``ts`` (P03's ``kalshi_before``). ``history.bar_at``
    already returns exactly this for any non-'kalshi' mode; call it when it advertises the
    mode, else do it here so the script runs against a pre-P03 history module."""
    try:
        return bar_at(bars, ts, "kalshi_before", max_gap)
    except Exception:
        cand = [b for b in bars if b.ts <= ts]
        b = cand[-1] if cand else None
        return b if b and abs(b.ts - ts) <= max_gap else None


def _mid(b: Optional[Bar]) -> Optional[float]:
    return b.mid if b is not None else None


def _kalshi_code(replayer: Any, home: str, away: str, kickoff: Any) -> str:
    """``26SEP17DETBUF`` from GameReplayer.kalshi_tickers (home ticker = KXNFLGAME-<code>-<HOME>)."""
    home_t, _ = replayer.kalshi_tickers(home, away, kickoff)
    return home_t.split("-")[1]


def kalshi_line(close: float) -> float:
    """The half-point line the Kalshi market at ``close`` actually settles on: ``ceil(close) - 0.5``
    (5.5 -> 5.5, 3.0 -> 2.5, 47.0 -> 46.5)."""
    return math.ceil(float(close)) - 0.5


def line_tickers(code: str, fav_k: str, spread_abs: float, total: Optional[float]) -> dict[str, Optional[str]]:
    """Kalshi uses the ceiling of the half-point line in the ticker suffix (floor_strike = line - 0.5)."""
    out: dict[str, Optional[str]] = {"spread": f"KXNFLSPREAD-{code}-{fav_k}{int(math.ceil(spread_abs))}" if spread_abs > 0 else None}
    out["total"] = f"KXNFLTOTAL-{code}-{int(math.ceil(total))}" if total is not None else None
    return out


def _pickcenter(summary: dict) -> dict[str, Any]:
    """DraftKings close: home spread (negative = home favoured), total, moneylines."""
    for pc in summary.get("pickcenter") or []:
        sp = pc.get("spread")
        if sp is None:
            continue
        home_odds, away_odds = pc.get("homeTeamOdds") or {}, pc.get("awayTeamOdds") or {}
        fav_home = bool(home_odds.get("favorite"))
        return {
            "spread_home": -abs(float(sp)) if fav_home else abs(float(sp)),
            "total": float(pc["overUnder"]) if pc.get("overUnder") is not None else None,
            "home_ml": home_odds.get("moneyLine"), "away_ml": away_odds.get("moneyLine"),
            "provider": (pc.get("provider") or {}).get("name"),
        }
    return {}


class Scorer:
    def __init__(self) -> None:
        self.rows: dict[tuple, list[tuple[float, int]]] = {}
        self.unpaired: dict[tuple, int] = {}  # (market, phase) -> rows dropped for lack of a Kalshi mid

    def add(self, key: tuple, p: Optional[float], y: int) -> None:
        if p is None or not (0.0 <= p <= 1.0):
            return
        self.rows.setdefault(key, []).append((min(1 - 1e-6, max(1e-6, p)), y))

    def metrics(self, key: tuple) -> dict[str, Any]:
        rows = self.rows.get(key) or []
        if not rows:
            return {"n": 0, "log_loss": None, "brier": None}
        ll = -sum(y * math.log(p) + (1 - y) * math.log(1 - p) for p, y in rows) / len(rows)
        br = sum((p - y) ** 2 for p, y in rows) / len(rows)
        return {"n": len(rows), "log_loss": round(ll, 4), "brier": round(br, 4)}


def evaluate_game(game: dict, espn: ESPNClient, history: HistoryClient, replayer: Any, scorer: Scorer, sigma: Optional[float], pre_minutes: int, post_minutes: int) -> dict[str, Any]:
    from arb_engine.backtest import KALSHI_TICKER_CODES
    from arb_engine.matching.teams import team_code

    summary = espn.summary(game["id"])
    plays, meta = espn_timeline(summary)
    home, away = team_code("nfl", meta["home"]) or meta["home"], team_code("nfl", meta["away"]) or meta["away"]
    close = _pickcenter(summary)
    info: dict[str, Any] = {"id": game["id"], "name": game.get("name"), "home": home, "away": away, "close": close, "plays": len(plays)}
    if not plays or close.get("spread_home") is None:
        info["skipped"] = "no plays" if not plays else "no closing spread"
        return info
    kickoff = meta.get("kickoff")
    spread_home = float(close["spread_home"])
    total = close.get("total")
    fav = home if spread_home <= 0 else away
    fav_k = KALSHI_TICKER_CODES.get(fav, fav)
    code = _kalshi_code(replayer, home, away, kickoff or plays[0].ts)
    tick = line_tickers(code, fav_k, abs(spread_home), total)
    t0 = min(plays[0].ts, kickoff.timestamp() if kickoff else plays[0].ts) - pre_minutes * 60
    t1 = plays[-1].ts + post_minutes * 60
    bars: dict[str, list[Bar]] = {}
    for mkt, t in tick.items():
        if t:
            try:
                bars[mkt] = history.kalshi_candles(t, int(t0), int(t1))
            except Exception as e:  # offline miss / unknown market
                bars[mkt] = []
                info.setdefault("errors", []).append(f"{mkt}: {e}")
    info["tickers"], info["candles"] = tick, {k: len(v) for k, v in bars.items()}

    # Settled outcomes on the Kalshi market's half-point line (no pushes): the model keeps the
    # close as its centre but is scored on the same binary as the mid.
    line_fav = kalshi_line(abs(spread_home)) if abs(spread_home) > 0 else None
    total_line = kalshi_line(total) if total is not None else None
    fav_margin = (meta["home_score"] - meta["away_score"]) * (1 if fav == home else -1)
    y_cover = None if line_fav is None else int(fav_margin > line_fav)
    y_over = None if total_line is None else int((meta["home_score"] + meta["away_score"]) > total_line)
    info["outcomes"] = {"fav_margin": fav_margin, "cover": y_cover, "total_points": meta["home_score"] + meta["away_score"], "over": y_over}
    info["scored_lines"] = {"spread": line_fav, "total": total_line}
    sig_m, sig_t, tie = default_params("nfl")
    sig_m = sigma if sigma is not None else sig_m

    def score_point(phase: str, ts: float, margin_home: int, points: int, gsr: Optional[int]) -> None:
        frac = 1.0 if phase == "pre" else max(0.0, min(1.0, (gsr or 0) / GAME_SECONDS))
        # Home-oriented distributions; the favourite covers when its margin exceeds the line.
        if phase == "pre":
            normal = NormalMargin.from_spread(spread_home, sig_m)
            emp: Optional[EmpiricalMargin] = EmpiricalMargin.from_table(None, spread_home, sig_m)
            tot = NormalTotal.from_line(total, sig_t) if total is not None else None
        else:
            normal = NormalMargin.in_play(margin_home, spread_home, frac, sig_m)
            emp = None
            tot = NormalTotal.in_play(points, total, frac, sig_t) if total is not None else None
        p_cover = (lambda d: d.p_gt(line_fav) if fav == home else d.p_lt(-line_fav))  # noqa: E731
        # Paired scoring: a row counts only when the Kalshi mid exists under BOTH alignments, so
        # every column is scored on the same plays (unpaired pools flatter whichever side has
        # rows the market did not quote, e.g. decided garbage time).
        if y_cover is not None:
            mids = {al: _mid(bar_before(bars.get("spread", []), ts) if al == "kalshi_before" else bar_at(bars.get("spread", []), ts, "kalshi")) for al in ALIGNMENTS}
            if all(m is not None for m in mids.values()):
                scorer.add(("spread", phase, "normal", "-"), p_cover(normal), y_cover)
                if emp is not None:
                    scorer.add(("spread", phase, "empirical", "-"), p_cover(emp), y_cover)
                for al, m in mids.items():
                    scorer.add(("spread", phase, "kalshi", al), m, y_cover)
            else:
                scorer.unpaired[("spread", phase)] = scorer.unpaired.get(("spread", phase), 0) + 1
        if y_over is not None and tot is not None:
            mids = {al: _mid(bar_before(bars.get("total", []), ts) if al == "kalshi_before" else bar_at(bars.get("total", []), ts, "kalshi")) for al in ALIGNMENTS}
            if all(m is not None for m in mids.values()):
                scorer.add(("total", phase, "normal", "-"), tot.p_gt(total_line), y_over)
                for al, m in mids.items():
                    scorer.add(("total", phase, "kalshi", al), m, y_over)
            else:
                scorer.unpaired[("total", phase)] = scorer.unpaired.get(("total", phase), 0) + 1

    pre_ts = (kickoff.timestamp() if kickoff else plays[0].ts) - 5 * 60
    score_point("pre", pre_ts, 0, 0, None)
    n_inplay = 0
    for p in plays:
        if p.game_seconds_remaining is None or p.game_seconds_remaining <= 0:
            continue
        if p.period >= 4 and (p.clock_seconds or 0) == 0:
            continue
        score_point("inplay", p.ts, p.home_score - p.away_score, p.home_score + p.away_score, p.game_seconds_remaining)
        n_inplay += 1
    info["n_inplay"] = n_inplay
    if close.get("home_ml") is not None and close.get("away_ml") is not None:
        try:
            sp = sportsbook_probs_from_moneylines(float(close["home_ml"]), float(close["away_ml"]))
            info["devig"] = {"home": round(sp.home, 4), "range": round(sp.range, 4), "overround": round(sp.overround, 4), "ml_spread": spread_from_p(sp.home, sig_m), "gap_vs_close": round(spread_from_p(sp.home, sig_m) - spread_home, 2), "heavy": max(abs(float(close["home_ml"])), abs(float(close["away_ml"]))) >= HEAVY_FAVOURITE_ML}
        except Exception as e:
            info["devig_error"] = str(e)
    return info


def build_report(season: int, week: int, games: list[dict[str, Any]], scorer: Scorer, sigma_used: tuple[float, float, float]) -> tuple[str, dict[str, Any]]:
    keys = [("spread", "pre"), ("spread", "inplay"), ("total", "pre"), ("total", "inplay")]
    cols = [("normal", "-"), ("empirical", "-"), ("kalshi", "kalshi_before"), ("kalshi", "kalshi")]
    lines = [f"lines-eval season {season} week {week}: {sum(1 for g in games if not g.get('skipped'))} games scored, {sum(1 for g in games if g.get('skipped'))} skipped",
             f"sigma margin {sigma_used[0]}, total {sigma_used[1]}, tie mass {sigma_used[2]}",
             "", f"{'market/phase':<16}" + "".join(f"{(s if a == '-' else a):>20}" for s, a in cols), f"{'':<16}" + "".join(f"{'n / logloss / brier':>20}" for _ in cols)]
    table: dict[str, dict[str, Any]] = {}
    for mkt, phase in keys:
        row = f"{mkt + '/' + phase:<16}"
        table[f"{mkt}/{phase}"] = {}
        for src, al in cols:
            m = scorer.metrics((mkt, phase, src, al))
            table[f"{mkt}/{phase}"][src if al == "-" else al] = m
            row += f"{'-':>20}" if not m["n"] else f"{m['n']:>6} {m['log_loss']:>6.4f} {m['brier']:>6.4f}"
        lines.append(row)
    if scorer.unpaired:
        lines.append("rows dropped (no Kalshi mid under both alignments): " + ", ".join(f"{m}/{ph} {n}" for (m, ph), n in sorted(scorer.unpaired.items())))
    heavy = [g["devig"] for g in games if g.get("devig", {}).get("heavy")]
    all_dv = [g["devig"] for g in games if g.get("devig")]
    devig = {"n": len(all_dv), "n_heavy": len(heavy), "range_heavy_max": max((d["range"] for d in heavy), default=None), "range_heavy_mean": round(sum(d["range"] for d in heavy) / len(heavy), 4) if heavy else None, "abs_gap_vs_close_mean": round(sum(abs(d["gap_vs_close"]) for d in all_dv) / len(all_dv), 2) if all_dv else None}
    lines += ["", f"de-vig disagreement on closing moneylines: {devig['n']} games, {devig['n_heavy']} heavy favourites (|ml| >= {HEAVY_FAVOURITE_ML}); range max {devig['range_heavy_max']} mean {devig['range_heavy_mean']}; |moneyline-implied spread - close| mean {devig['abs_gap_vs_close_mean']} pts", ""]
    for g in games:
        if g.get("skipped"):
            lines.append(f"  {g.get('name')}: skipped ({g['skipped']})")
        else:
            lines.append(f"  {g.get('name')}: close {g['close'].get('spread_home')} / {g['close'].get('total')} scored at {g.get('scored_lines')} candles {g.get('candles')} inplay rows {g.get('n_inplay')} outcomes {g.get('outcomes')}" + (f" errors {g['errors']}" if g.get("errors") else ""))
    lines.append("")
    lines.append("reading: every column is scored on the Kalshi market's half-point line (ceil(close) - 0.5; integer closes shift half a point, no pushes). kalshi_before (last candle ending <= play) is what a taker could hit; kalshi (first candle ending >= play) already contains the play. The fair beats the mid only if it wins against BOTH brackets in play.")
    tables = _table_window()
    lines.append(f"margin tables: seasons {tables.get('seasons')} (excluded incomplete {tables.get('excluded_incomplete_seasons')}); the evaluated season is never in the tables that price it.")
    metrics = {"item": "P13", "season": season, "week": week, "games_scored": sum(1 for g in games if not g.get("skipped")), "games_skipped": sum(1 for g in games if g.get("skipped")), "sigma": {"margin": sigma_used[0], "total": sigma_used[1], "tie_mass": sigma_used[2]}, "table": table, "devig": devig, "alignments": list(ALIGNMENTS), "unpaired_rows": {f"{m}/{ph}": n for (m, ph), n in sorted(scorer.unpaired.items())}, "paired": True,
               "scoring": "kalshi market line: ceil(close) - 0.5 for spread and total, truth and every predictor alike; no pushes", "tables": tables}
    return "\n".join(lines), metrics


def _table_window() -> dict[str, Any]:
    """Training window of the committed margin tables (so the results fixture states what the
    empirical column and the pooled sigma were fit on)."""
    from arb_engine.quant.lines import load_margin_table, load_sigma_table

    dist, sig = load_margin_table("nfl") or {}, load_sigma_table() or {}
    src = dist.get("source") or {}
    return {"seasons": src.get("seasons"), "excluded_incomplete_seasons": src.get("excluded_incomplete_seasons"), "rows_used": src.get("rows_used"), "sha256": src.get("sha256"), "sigma_seasons": (sig.get("source") or {}).get("seasons"), "sigma_ref_min_season": sig.get("ref_min_season"), "sigma_ref_max_season": sig.get("ref_max_season")}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(ap)
    args = ap.parse_args(argv)
    return run(args)


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--limit", type=int, help="only the first N finished games of the week")
    ap.add_argument("--offline", action="store_true", help="serve every request from the cache; skip games with misses")
    ap.add_argument("--sigma", type=float, help="override the margin sigma (default: data/margin_sigma.json)")
    ap.add_argument("--pre-minutes", type=int, default=30)
    ap.add_argument("--post-minutes", type=int, default=10)
    ap.add_argument("--out-dir", default=str(ROOT / "out"))
    ap.add_argument("--results", default=str(ROOT / "tests" / "fixtures" / "results" / "lines_eval_p13.json"), help="metrics-only JSON ('' to skip)")


def run(args: argparse.Namespace) -> int:
    from arb_engine.backtest import GameReplayer, week_games

    out_dir = Path(args.out_dir)
    http = CachingHttp(out_dir / "lines_cache", offline=args.offline)
    espn, history = ESPNClient(http=http), HistoryClient(http=http)  # type: ignore[arg-type]
    replayer = GameReplayer(espn=espn, history=history)
    scorer = Scorer()
    try:
        games = week_games(espn, args.season, args.week)
    except Exception as e:
        print(f"scoreboard unavailable: {e}", file=sys.stderr)
        return 2
    if args.limit:
        games = games[: args.limit]
    results: list[dict[str, Any]] = []
    for g in games:
        try:
            info = evaluate_game(g, espn, history, replayer, scorer, args.sigma, args.pre_minutes, args.post_minutes)
        except Exception as e:
            info = {"id": g.get("id"), "name": g.get("name"), "skipped": str(e)}
        results.append(info)
        print(f"{info.get('name'):<40} {'skipped: ' + info['skipped'] if info.get('skipped') else 'candles ' + str(info.get('candles')) + ' inplay ' + str(info.get('n_inplay'))}")
    sig = default_params("nfl")
    report, metrics = build_report(args.season, args.week, results, scorer, (args.sigma if args.sigma is not None else sig[0], sig[1], sig[2]))
    print()
    print(report)
    out_dir.mkdir(parents=True, exist_ok=True)
    txt = out_dir / f"lines_eval_w{args.week}.txt"
    txt.write_text(report + "\n", encoding="utf-8")
    print(f"wrote {txt} (cache hits {http.hits}, misses {http.misses})")
    if args.results and metrics["games_scored"]:
        rp = Path(args.results)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps(metrics, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {rp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

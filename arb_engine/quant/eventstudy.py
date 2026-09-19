"""Event study: how fast does each venue absorb a large model move, and how often does the
ESPN feed reverse itself?

Input rows are the replay harness's ``games[].rows`` (``arb_engine.backtest``: one dict per
play with ``ts``, ``model_p`` (pre-play), optionally ``model_after_p``, ``espn_p``,
``kalshi_before_p`` / ``kalshi_after_p`` (or the older ``kalshi_p``), ``play_class``,
``slice``, ``period``, ``home_score`` / ``away_score``). An *event* is a play whose model move
``dWP = model_after_p - model_p`` (or the next row's ``model_p`` minus this one's) is at
least ``dwp_min`` in absolute value.

Trades are ``{venue: [(ts, P(home), size)]}`` (``venues.trades.as_home_prices``) so both
venues are scored on the same probability. For each event and venue:

    pre       = last print at or before the play's wall-clock ``t0``, no older than
                ``t0 + window[0]`` (default 60 s): an older print is a stale pre-price whose
                drift since would be attributed to the event, so the event is dropped instead
    P(+k)     = last print at or before ``t0 + k`` (k = 30 s, 120 s, 300 s, 900 s)
    full      = P(+900) - pre                      (the repricing over the window)
    absorbed_k = (P(+k) - pre) / full              (fraction of it already in the price)

Events without a print inside ``(t0 + window[0], t0]`` or after ``t0``, or with
``|full| < 0.005``, are dropped (nothing to absorb; widen ``--window-pre`` for a thin tape).
Cells: venue x bucket, bucket = (mover fav|dog, |dWP| band, quarter) — the team whose WP
rose is the *mover*, a favourite when its pre-play WP was >= 0.5. Two regressions per venue
at the regression offset (120 s when scored, else the offset nearest it): Mincer-Zarnowitz
of ``P(+900)`` on ``P(+120)`` (slope 1, intercept 0 = the 2-minute price is an unbiased
forecast of the 15-minute price) and the *underreaction* slope of ``full`` on
``P(+120) - pre`` (> 1 = the first two minutes under-shoot the eventual move; a linear
15-minute repricing gives 7.5).

``reversal_episodes`` reads recorded ``espn_ticks`` rows and returns score decreases, score
changes that arrived before the play feed advanced, clock reversals, and suspect /
review-pending runs — the StateGuard anomalies the live gates are meant to catch.
"""

from __future__ import annotations

import json
from bisect import bisect_right
from typing import Any, Iterable, Optional

OFFSETS = (30, 120, 300, 900)
DWP_BANDS = ((0.05, 0.10, "5-10%"), (0.10, 0.20, "10-20%"), (0.20, 1.01, "20%+"))
MIN_FULL_MOVE = 0.005


# ---- events ----------------------------------------------------------------------------------

def _quarter(period: Any) -> str:
    try:
        p = int(period)
    except (TypeError, ValueError):
        return "?"
    return f"Q{p}" if 1 <= p <= 4 else ("OT" if p > 4 else "?")


def _band(x: float, bands: Iterable[tuple[float, float, str]] = DWP_BANDS) -> Optional[str]:
    return next((b[2] for b in bands if b[0] <= x < b[1]), None)


def events_from_rows(rows: Iterable[dict[str, Any]], dwp_min: float = 0.05) -> list[dict[str, Any]]:
    """Plays whose model move is at least ``dwp_min``: ``[{ts, dwp, p_pre, p_post, mover, band,
    quarter, scoring, play_class, text}]`` in time order."""
    rs = [r for r in rows if r.get("ts") is not None and r.get("model_p") is not None]
    rs.sort(key=lambda r: r["ts"])
    out: list[dict[str, Any]] = []
    for i, r in enumerate(rs):
        p_pre = float(r["model_p"])
        p_post = r.get("model_after_p")
        if p_post is None and i + 1 < len(rs):
            p_post = rs[i + 1].get("model_p")
        if p_post is None:
            continue
        dwp = float(p_post) - p_pre
        if abs(dwp) < dwp_min:
            continue
        home_moved = dwp > 0
        p_mover = p_pre if home_moved else 1.0 - p_pre
        hs0, as0 = r.get("home_score"), r.get("away_score")
        nxt = rs[i + 1] if i + 1 < len(rs) else None
        scoring = bool(r.get("scoring_play")) or (r.get("play_class") in ("score", "td", "fg", "try", "try_synth")) or (nxt is not None and hs0 is not None and (nxt.get("home_score"), nxt.get("away_score")) != (hs0, as0))
        out.append({"ts": float(r["ts"]), "dwp": round(dwp, 4), "p_pre": p_pre, "p_post": float(p_post), "mover": "fav" if p_mover >= 0.5 else "dog", "band": _band(abs(dwp)), "quarter": _quarter(r.get("period")), "scoring": scoring, "play_class": r.get("play_class"), "text": (r.get("text") or "")[:120]})
    return out


# ---- prices around an event ----------------------------------------------------------------

def _series(trades: Iterable[Any]) -> tuple[list[float], list[float]]:
    """(ts[], price[]) sorted, from Trade objects or (ts, price[, size]) tuples."""
    pts = []
    for t in trades:
        if isinstance(t, (tuple, list)):
            pts.append((float(t[0]), float(t[1])))
        else:
            pts.append((float(getattr(t, "ts")), float(getattr(t, "price"))))
    pts.sort()
    return [p[0] for p in pts], [p[1] for p in pts]


def price_at(ts: list[float], px: list[float], t: float) -> Optional[float]:
    """Last print at or before ``t`` (None before the first print)."""
    i = bisect_right(ts, t)
    return px[i - 1] if i else None


def absorption(ts: list[float], px: list[float], t0: float, window: tuple[float, float] = (-60, 900), offsets: Iterable[int] = OFFSETS) -> Optional[dict[str, Any]]:
    """Absorbed fractions of one venue's repricing after ``t0`` (None when nothing to score:
    no print in the pre-window ``[t0 + window[0], t0]``, none after ``t0`` within
    ``window[1]``, or a full move under ``MIN_FULL_MOVE``)."""
    i_after = bisect_right(ts, t0)
    if not i_after or ts[i_after - 1] < t0 + window[0]:
        return None  # no print, or only a stale one, before the event
    pre = px[i_after - 1]
    if i_after >= len(ts) or ts[i_after] > t0 + window[1]:
        return None  # no print inside the window
    p_end = price_at(ts, px, t0 + window[1])
    full = p_end - pre
    if abs(full) < MIN_FULL_MOVE:
        return None
    out: dict[str, Any] = {"pre": pre, "end": p_end, "full": round(full, 4), "prints_in_window": bisect_right(ts, t0 + window[1]) - i_after, "first_print_lag": round(ts[i_after] - t0, 3)}
    for k in offsets:
        pk = price_at(ts, px, t0 + k)
        out[f"p_{k}"] = pk
        out[f"absorbed_{k}"] = round((pk - pre) / full, 4)
    return out


# ---- regressions ---------------------------------------------------------------------------------

def ols(xs: list[float], ys: list[float]) -> dict[str, Any]:
    n = len(xs)
    if n < 2:
        return {"n": n, "slope": None, "intercept": None, "r2": None}
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx <= 1e-12:
        return {"n": n, "slope": None, "intercept": None, "r2": None}
    b = sxy / sxx
    a = my - b * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    return {"n": n, "slope": round(b, 4), "intercept": round(a, 4), "r2": round(1 - ss_res / ss_tot, 4) if ss_tot > 1e-12 else None}


# ---- the study -------------------------------------------------------------------------------------

def _mean(xs: list[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 4) if xs else None


def regression_offset(offsets: Iterable[int], preferred: int = 120) -> int:
    """The scored offset the two regressions use: ``preferred`` when present, else the
    nearest one (so ``offsets=(30, 300, 900)`` regresses on the +30 s price, not a KeyError)."""
    offs = tuple(int(o) for o in offsets)
    if not offs:
        raise ValueError("event study needs at least one offset")
    return preferred if preferred in offs else min(offs, key=lambda o: (abs(o - preferred), o))


def summarize(scored: list[dict[str, Any]], offsets: Iterable[int] = OFFSETS) -> dict[str, Any]:
    """Per-venue tables from scored events (``event_study`` output's ``events``): absorbed
    fractions overall and per bucket, sign agreement with the model, and both regressions
    (keyed ``mincer_zarnowitz_<k>`` / ``underreaction_<k>`` with ``k = regression_offset``)."""
    offsets = tuple(int(o) for o in offsets)
    k_reg = regression_offset(offsets)
    venues = sorted({v for e in scored for v in e.get("venues", {})})
    out: dict[str, Any] = {"n_events": len(scored), "offsets": list(offsets), "regression_offset": k_reg, "venues": {}}
    for v in venues:
        rows = [(e, e["venues"][v]) for e in scored if e.get("venues", {}).get(v)]
        cells: dict[str, list[tuple[dict, dict]]] = {}
        for e, a in rows:
            for key in ("all", f"mover={e['mover']}", f"dwp={e['band']}", f"quarter={e['quarter']}", f"{e['mover']}|{e['band']}|{e['quarter']}"):
                cells.setdefault(key, []).append((e, a))
        table = {}
        for key, items in cells.items():
            cell: dict[str, Any] = {"n": len(items), "sign_agree": _mean([1.0 if (a["full"] > 0) == (e["dwp"] > 0) else 0.0 for e, a in items]), "first_print_lag_mean": _mean([a["first_print_lag"] for _, a in items])}
            for k in offsets:
                cell[f"absorbed_{k}"] = _mean([a[f"absorbed_{k}"] for _, a in items])
            table[key] = cell
        xs2 = [a[f"p_{k_reg}"] for _, a in rows]
        ys = [a["end"] for _, a in rows]
        out["venues"][v] = {"n": len(rows), "table": table, f"mincer_zarnowitz_{k_reg}": ols(xs2, ys), f"underreaction_{k_reg}": ols([a[f"p_{k_reg}"] - a["pre"] for _, a in rows], [a["full"] for _, a in rows])}
    return out


def event_study(rows: Iterable[dict[str, Any]], trades: dict[str, Iterable[Any]], window: tuple[float, float] = (-60, 900), dwp_min: float = 0.05, offsets: Iterable[int] = OFFSETS, game: Optional[str] = None) -> dict[str, Any]:
    """Score every large model move of one game against each venue's prints.

    ``trades`` maps venue -> prints as ``(ts, P(home)[, size])`` tuples or ``Trade`` objects
    whose ``price`` is already P(home). Returns ``{n (events scored on >= 1 venue), n_events
    (large moves found), events: [...], summary}`` — ``events`` carry per-venue absorption so
    several games can be pooled with ``summarize``.
    """
    offsets = tuple(offsets)
    series = {v: _series(tr) for v, tr in (trades or {}).items()}
    evs = events_from_rows(rows, dwp_min=dwp_min)
    scored: list[dict[str, Any]] = []
    for e in evs:
        per: dict[str, Any] = {}
        for v, (ts, px) in series.items():
            a = absorption(ts, px, e["ts"], window=window, offsets=offsets)
            if a is not None:
                per[v] = a
        scored.append({**e, "game": game, "venues": per})
    n_scored = sum(1 for e in scored if e["venues"])
    return {"game": game, "n": n_scored, "n_events": len(evs), "n_scored": n_scored, "window": list(window), "dwp_min": dwp_min, "events": scored, "summary": summarize(scored, offsets)}


def load_games(path: str) -> list[dict[str, Any]]:
    """``games[]`` of a ``backtest --week --json`` file (or a single replay's dict)."""
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if isinstance(doc, dict) and "games" in doc:
        return list(doc["games"])
    if isinstance(doc, dict) and "rows" in doc:
        return [doc]
    if isinstance(doc, list):
        return doc
    raise ValueError(f"{path}: not a replay JSON (expected games[] or rows[])")


# ---- ESPN feed reversals ------------------------------------------------------------------------

def reversal_episodes(espn_ticks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anomalies in recorded ``espn_ticks`` rows (dicts; any order). Kinds: ``score-decrease``,
    ``score-before-lastplay`` (score moved while ``last_play_id`` did not), ``clock-reversal``
    (same period, clock went up while live), ``suspect`` and ``review-pending`` runs.
    Consecutive ticks of one kind for one game merge into one episode; the open episode is
    tracked per (game, kind) because StateGuard sets ``suspect`` and ``review_pending``
    together, and two kinds alternating on the same ticks must not split each other's run."""
    by_game: dict[str, list[dict[str, Any]]] = {}
    for r in espn_ticks:
        by_game.setdefault(str(r.get("event_key")), []).append(r)
    episodes: list[dict[str, Any]] = []
    open_eps: dict[tuple[str, str], dict[str, Any]] = {}
    idx = 0   # position of the current tick within its game; an episode extends only from idx - 1

    def push(kind: str, key: str, r: dict[str, Any], detail: str) -> None:
        last = open_eps.get((key, kind))
        if last is not None and last["_idx"] == idx - 1:
            last["ts_end"], last["n_ticks"], last["_idx"] = r["ts"], last["n_ticks"] + 1, idx
            return
        open_eps[(key, kind)] = ep = {"kind": kind, "event_key": key, "ts_start": r["ts"], "ts_end": r["ts"], "n_ticks": 1, "detail": detail, "_idx": idx}
        episodes.append(ep)

    for key, rows in by_game.items():
        rows.sort(key=lambda r: r.get("ts") or 0)
        prev: Optional[dict[str, Any]] = None
        for idx, r in enumerate(rows):
            if r.get("suspect"):
                push("suspect", key, r, str(r.get("state_source") or ""))
            if r.get("review_pending"):
                push("review-pending", key, r, str(r.get("last_play_text") or "")[:80])
            if prev is not None:
                hs, as_, phs, pas = r.get("home_score"), r.get("away_score"), prev.get("home_score"), prev.get("away_score")
                if None not in (hs, as_, phs, pas):
                    if hs < phs or as_ < pas:
                        push("score-decrease", key, r, f"{pas}-{phs} -> {as_}-{hs}")
                    elif (hs, as_) != (phs, pas) and r.get("last_play_id") is not None and prev.get("last_play_id") is not None and str(r["last_play_id"]) == str(prev["last_play_id"]):
                        push("score-before-lastplay", key, r, f"{pas}-{phs} -> {as_}-{hs} @ play {r['last_play_id']}")
                if r.get("status") == "live" and prev.get("status") == "live" and r.get("period") == prev.get("period") and r.get("clock") is not None and prev.get("clock") is not None and r["clock"] > prev["clock"]:
                    push("clock-reversal", key, r, f"{prev['clock']} -> {r['clock']}")
            prev = r
    for e in episodes:
        e.pop("_idx", None)
    episodes.sort(key=lambda e: (e["event_key"], e["ts_start"], e["kind"]))
    return episodes


def format_study(summary: dict[str, Any], offsets: Iterable[int] = OFFSETS) -> str:
    offsets = tuple(offsets)
    lines = [f"{summary.get('n_events', 0)} events"]
    k_reg = summary.get("regression_offset") or regression_offset(offsets)
    for v, s in summary.get("venues", {}).items():
        mz, ur = s[f"mincer_zarnowitz_{k_reg}"], s[f"underreaction_{k_reg}"]
        lines.append(f"  {v}: n={s['n']}  MZ(+{k_reg}s) slope {mz['slope']} intercept {mz['intercept']}  underreaction slope {ur['slope']}")
        lines.append("    bucket                  n   sign  " + "  ".join(f"abs+{k:<4}" for k in offsets) + "  lag s")
        for key, c in s["table"].items():
            if key.count("|") == 2 and c["n"] < 5:
                continue  # the full cross is noisy below 5 events; the margins are always shown
            lines.append(f"    {key:<22} {c['n']:>4}  {c['sign_agree'] if c['sign_agree'] is not None else '-':>5}  " + "  ".join(f"{c[f'absorbed_{k}']:>8}" if c[f"absorbed_{k}"] is not None else f"{'-':>8}" for k in offsets) + f"  {c['first_print_lag_mean']}")
    return "\n".join(lines)

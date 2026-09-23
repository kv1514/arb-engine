"""Leakage-free L1 samples for momentum, recovery and independent-book lead-lag studies.

This module is deliberately offline and stdlib-only.  Every feature is computed from rows
whose observation time is at or before the decision time; labels use a narrow, forward
window and never carry a mark forward.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable, Optional

HORIZONS = (5, 15, 30, 60)


def _f(value: Any) -> Optional[float]:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def observation_time(row: dict[str, Any], tick_ts: Optional[float] = None) -> tuple[Optional[float], int]:
    """Return (time, approx_time). Old recordings fall back to their enclosing tick time."""
    obs = _f(row.get("obs_ts"))
    return (obs, 0) if obs is not None else (_f(tick_ts if tick_ts is not None else row.get("tick_ts") or row.get("ts")), 1)


def contract_key(row: dict[str, Any]) -> tuple[str, str]:
    """One economic observation per underlying book and contract identity."""
    book = str(row.get("book_id") or row.get("venue") or "")
    market = str(row.get("settlement_id") or row.get("contract_id") or row.get("venue_market_id") or row.get("outcome") or "")
    return book, market.split("#", 1)[0]


def _valid(row: dict[str, Any], now: float) -> bool:
    bid, ask = _f(row.get("bid")), _f(row.get("ask"))
    obs, _ = observation_time(row)
    if not row.get("refreshed") or not row.get("in_play", row.get("live", True)):
        return False
    if bid is None or ask is None or not 0 < bid <= ask < 1 or obs is None or obs > now:
        return False
    limit = 2.0 if row.get("source") == "fast" or row.get("fast_lane") else 6.0
    if now - obs > limit:
        return False
    quote_time = _f(row.get("quote_time"))
    return quote_time is None or 0 <= obs - quote_time <= 10.0


def _usable_history(row: dict[str, Any], t: float) -> bool:
    bid, ask = _f(row.get("bid")), _f(row.get("ask"))
    obs, _ = observation_time(row)
    quote_time = _f(row.get("quote_time"))
    return bool(row.get("refreshed") and row.get("in_play", row.get("live", True))
                and bid is not None and ask is not None and 0 < bid <= ask < 1
                and obs is not None and obs <= t
                and (quote_time is None or 0 <= obs - quote_time <= 10.0))


def _mid(row: dict[str, Any]) -> float:
    return (float(row["bid"]) + float(row["ask"])) / 2.0


def features_at(rows: Iterable[dict[str, Any]], t: float) -> dict[tuple[str, str], dict[str, Any]]:
    """Causal features at *t*. Future input is a caller error, not silently ignored."""
    rows = list(rows)
    for row in rows:
        obs, _ = observation_time(row)
        if obs is not None and obs > t:
            raise ValueError("features_at received an observation from the future")
    by: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if _usable_history(row, t):
            by[contract_key(row)].append(row)
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for key, history in by.items():
        history.sort(key=lambda r: observation_time(r)[0] or -math.inf)
        cur = history[-1]
        if not _valid(cur, t):
            continue
        now_mid = _mid(cur)
        anchor = next((r for r in reversed(history) if observation_time(r)[0] is not None and observation_time(r)[0] <= t - 30), None)
        dmid = now_mid - _mid(anchor) if anchor is not None else None
        dbid = float(cur["bid"]) - float(anchor["bid"]) if anchor is not None else None
        dask = float(cur["ask"]) - float(anchor["ask"]) if anchor is not None else None
        past = [_mid(r) for r in history if observation_time(r)[0] is not None and observation_time(r)[0] >= t - 60]
        trough = min(past) if past else now_mid
        peak = max(past) if past else now_mid
        out[key] = {"t": t, "book_id": key[0], "contract": key[1], "venue": cur.get("venue"),
                    "outcome": cur.get("outcome"), "side": cur.get("side", "yes"),
                    "bid": float(cur["bid"]), "ask": float(cur["ask"]), "mid": now_mid,
                    "spread": float(cur["ask"]) - float(cur["bid"]), "dmid_30": dmid,
                    "dbid_30": dbid, "dask_30": dask, "dip_60": now_mid - peak,
                    "recovery_60": now_mid - trough, "obs_ts": observation_time(cur)[0],
                    "quote_time": _f(cur.get("quote_time")), "row": cur}
    return out


def trigger_events(rows: Iterable[dict[str, Any]], cooldown_s: float = 60.0) -> list[dict[str, Any]]:
    """Confirmed 30-second moves, deduped once per book-contract per cooldown."""
    ordered = sorted(list(rows), key=lambda r: observation_time(r)[0] or -math.inf)
    history: list[dict[str, Any]] = []
    last: dict[tuple[str, str], float] = {}
    events = []
    for row in ordered:
        t, approx = observation_time(row)
        if t is None:
            continue
        history.append(row)
        try:
            features = features_at(history, t)
        except ValueError:  # impossible after sorting, retained for defensive callers
            continue
        key = contract_key(row)
        f = features.get(key)
        if not f or f["dmid_30"] is None or abs(f["dmid_30"]) < .05:
            continue
        if f["dbid_30"] * f["dmid_30"] <= 0 or f["dask_30"] * f["dmid_30"] <= 0:
            continue
        if abs(f["dbid_30"]) < .4 * abs(f["dmid_30"]) or abs(f["dask_30"]) < .4 * abs(f["dmid_30"]):
            continue
        if t - last.get(key, -math.inf) < cooldown_s:
            continue
        last[key] = t
        events.append({**f, "kind": "trigger", "approx_time": approx})
    return events


def unconditional_samples(rows: Iterable[dict[str, Any]], every_s: int = 5) -> list[dict[str, Any]]:
    ordered = sorted(list(rows), key=lambda r: observation_time(r)[0] or -math.inf)
    history: list[dict[str, Any]] = []
    last: dict[tuple[str, str], float] = {}
    out = []
    for row in ordered:
        t, approx = observation_time(row)
        if t is None:
            continue
        history.append(row)
        key = contract_key(row)
        if t - last.get(key, -math.inf) < every_s:
            continue
        f = features_at(history, t).get(key)
        if f:
            last[key] = t
            out.append({**f, "kind": "unconditional", "approx_time": approx})
    return out


def label_at(rows: Iterable[dict[str, Any]], sample: dict[str, Any], horizon: int) -> Optional[dict[str, Any]]:
    """First refreshed mark in [t+h, t+h+max(1,.2h)]."""
    lo, hi = sample["t"] + horizon, sample["t"] + horizon + max(1.0, .2 * horizon)
    key = (sample["book_id"], sample["contract"])
    candidates = []
    for row in rows:
        obs, approx = observation_time(row)
        if obs is not None and lo <= obs <= hi and contract_key(row) == key and _valid(row, obs):
            candidates.append((obs, approx, row))
    if not candidates:
        return None
    obs, approx, row = min(candidates, key=lambda x: x[0])
    return {"horizon": horizon, "label_ts": obs, "approx_time": approx,
            "future_mid": _mid(row), "dmid_h": _mid(row) - sample["mid"]}


def lead_lag_label(rows: Iterable[dict[str, Any]], leader: dict[str, Any], follower: dict[str, Any], horizon: int = 30) -> Optional[dict[str, Any]]:
    """H3 target: follower movement toward the independent leader's decision-time gap."""
    if leader["book_id"] == follower["book_id"]:
        return None
    label = label_at(rows, follower, horizon)
    if label is None:
        return None
    gap = leader["mid"] - follower["mid"]
    label["gap"] = gap
    label["convergence"] = label["dmid_h"] * (1 if gap > 0 else -1 if gap < 0 else 0)
    return label


decision_points = unconditional_samples

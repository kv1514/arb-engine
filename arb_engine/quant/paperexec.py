"""Conservative offline IOC and two-leg paper execution; never submits an order."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Optional

from ..fees.base import D


@dataclass
class PaperFill:
    requested: int
    filled: int
    entry_price: Optional[float]
    exit_price: Optional[float]
    entry_fee: Decimal = Decimal("0")
    exit_fee: Decimal = Decimal("0")
    pnl: Decimal = Decimal("0")
    missed: bool = False
    settled: int = 0


def _first(rows: Iterable[dict[str, Any]], earliest: float, side: str, latest: Optional[float] = None) -> Optional[dict[str, Any]]:
    eligible = [r for r in rows if float(r.get("obs_ts", r.get("ts", -1))) >= earliest and (latest is None or float(r.get("obs_ts", r.get("ts", -1))) <= latest) and r.get("refreshed", True)]
    return min(eligible, key=lambda r: float(r.get("obs_ts", r.get("ts")))) if eligible else None


def ioc(rows: Iterable[dict[str, Any]], decision_ts: float, order: int, fee_model: Any,
        latency_s: float = 1, horizon_s: float = 30, haircut: float = 1,
        settlement: Optional[float] = None) -> PaperFill:
    """Buy at the ask after latency; sell at the horizon bid. Unfilled IOC remainder dies."""
    rows = list(rows)
    entry = _first(rows, decision_ts + latency_s, "ask")
    if entry is None or entry.get("ask") is None:
        return PaperFill(order, 0, None, None, missed=True)
    size = max(0, int(float(entry.get("ask_size") or 0) * haircut))
    filled = min(int(order), size)
    if filled <= 0:
        return PaperFill(order, 0, None, None, missed=True)
    ep = float(entry["ask"])
    entry_fee = fee_model.fee(ep, filled, "taker")
    exit_row = _first(rows, decision_ts + latency_s + horizon_s, "bid")
    exit_price = None if exit_row is None else exit_row.get("bid")
    exit_count = min(filled, max(0, int(float(exit_row.get("bid_size") or 0) * haircut))) if exit_row and exit_price is not None else 0
    exit_fee = fee_model.fee(float(exit_price), exit_count, "taker") if exit_count else D(0)
    pnl = (D(exit_price) - D(ep)) * exit_count - entry_fee - exit_fee
    remaining = filled - exit_count
    settled = 0
    if remaining:
        later = _first(rows, decision_ts + latency_s + horizon_s + 1, "bid")
        if later is not None and later.get("bid") is not None:
            n = min(remaining, max(0, int(float(later.get("bid_size") or 0) * haircut)))
            fee = fee_model.fee(float(later["bid"]), n, "taker") if n else D(0)
            pnl += (D(later["bid"]) - D(ep)) * n - fee
            exit_fee += fee
            remaining -= n
        if remaining and settlement is not None:
            pnl += (D(settlement) - D(ep)) * remaining  # settlement carries no exit fee
            settled = remaining
            remaining = 0
    return PaperFill(order, filled, ep, _float(exit_price), entry_fee, exit_fee, pnl, False, settled)


def _float(value: Any) -> Optional[float]:
    return None if value is None else float(value)


def two_leg_arb(leg_a: dict[str, Any], leg_b: dict[str, Any], count: int,
                fee_a: Any, fee_b: Any, latency_s: float = 1, haircut: float = 1,
                tie: bool = False) -> dict[str, Any]:
    """IOC both legs; if only one fills, immediately unwind it at its displayed bid."""
    if tie and (leg_a.get("tie_payout") is None or leg_b.get("tie_payout") is None):
        return {"excluded": "unknown-tie"}
    def fill(leg: dict[str, Any], fee: Any) -> tuple[int, Decimal]:
        n = min(count, max(0, int(float(leg.get("ask_size") or 0) * haircut)))
        cost = D(leg["ask"]) * n + (fee.fee(leg["ask"], n, "taker") if n else D(0))
        return n, cost
    na, ca = fill(leg_a, fee_a); nb, cb = fill(leg_b, fee_b)
    if na and nb:
        n = min(na, nb)
        payout = D((leg_a.get("tie_payout") or 0) + (leg_b.get("tie_payout") or 0)) if tie else D(1)
        return {"filled": n, "leg_failure": na != nb, "pnl": payout * n - ca - cb}
    leg, n, cost, fee = (leg_a, na, ca, fee_a) if na else (leg_b, nb, cb, fee_b)
    if not n:
        return {"filled": 0, "missed": True, "pnl": D(0)}
    bid = leg.get("bid")
    if bid is None:
        return {"filled": n, "leg_failure": True, "unwound": 0, "pnl": -cost}
    unwind_fee = fee.fee(bid, n, "taker")
    return {"filled": n, "leg_failure": True, "unwound": n, "pnl": D(bid) * n - unwind_fee - cost}


simulate_ioc = ioc

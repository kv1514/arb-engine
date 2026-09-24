"""Offline paper execution for the microstructure experiment; never submits an order.

Every trade is judged against *recorded observations* of one contract (rows with ``obs_ts``,
``bid``/``ask``, displayed sizes and ``refreshed``), the way the live executor would meet
the book, and nothing is assumed that a polling recorder cannot see:

* **Entry** is an immediate-or-cancel buy with a limit (the ask seen at decision time). It
  meets the book at ``decision_ts + latency``: the first refreshed observation in
  ``[t+L, t+L+entry_tol]``. It fills only if that ask is at or below the limit, for at most
  ``floor(displayed ask size x haircut)``; the rest is cancelled. No observation in the
  window, an ask above the limit or no size is a *missed* fill, counted, never dropped.
* **Exit** sells to the bid at the first refreshed observation in the horizon window, at
  most ``floor(bid size x haircut)``; what is left rolls to later observations (up to
  ``max_rolls``), then to the settlement value when it is known (no exit fee at
  settlement). Contracts still held after that are **unresolved**: the trade is reported
  with ``pnl=None`` and excluded from P&L, never silently valued.
* **Fees** are the venue's ``FeeModel.fee(price, count, role)`` on each order at its real
  count (Kalshi rounds up per order; Rothera has a per-order floor), on entry *and* exit.
* **Two-leg arbitrage**: each leg meets its own book after its own latency (an automated
  Kalshi leg ~1 s, a person on Robinhood ~15 s). Matched sets pay $1 in the win case
  (``tie_payouts`` gives the tie case); the excess of the larger leg is unwound at its bid
  once the other leg is known to have come up short.

Displayed size is an upper bound on what is available (cancellations are invisible to a
poller), hence the haircut. Queue position does not arise: every order here is a taker.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence

from ..fees.base import D


def _t(row: dict[str, Any]) -> Optional[float]:
    for k in ("obs_ts", "ts"):
        v = row.get(k)
        if v is not None:
            try:
                f = float(v)
                if math.isfinite(f):
                    return f
            except (TypeError, ValueError):
                pass
    return None


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _refreshed(row: dict[str, Any]) -> bool:
    r = row.get("refreshed")
    return True if r is None else bool(r)   # legacy rows carry no flag (full ticks only)


def first_obs(rows: Sequence[dict[str, Any]], lo: float, hi: float) -> Optional[dict[str, Any]]:
    """The first refreshed observation with lo <= obs_ts <= hi (rows need not be sorted)."""
    best, best_t = None, None
    for r in rows:
        t = _t(r)
        if t is not None and lo <= t <= hi and _refreshed(r) and (best_t is None or t < best_t):
            best, best_t = r, t
    return best


def _dedupe_observations(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """One displayed-liquidity observation may be recorded twice by overlapping consumers.
    It may supply size once, keyed by contract identity and receipt time."""
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        t = _t(row)
        if t is None:
            continue
        key = (t, row.get("book_id"), row.get("venue_market_id"), row.get("side", "yes"))
        unique.setdefault(key, row)
    return sorted(unique.values(), key=lambda r: _t(r) or -math.inf)


def _size(row: dict[str, Any], key: str, haircut: float) -> int:
    s = _num(row.get(key))
    return max(0, int(math.floor(s * haircut + 1e-9))) if s is not None else 0


@dataclass
class PaperTrade:
    requested: int
    limit: float
    filled: int = 0
    entry_price: Optional[float] = None
    entry_fee: Decimal = Decimal("0")
    exits: list[tuple[float, float, int, Decimal]] = field(default_factory=list)   # (ts, bid, count, fee)
    settled: int = 0
    settle_value: Optional[float] = None
    unresolved: int = 0
    missed: bool = False
    reason: str = ""

    @property
    def cancelled(self) -> int:
        """IOC entry remainder; it never becomes later inventory."""
        return max(0, self.requested - self.filled)

    @property
    def partial(self) -> bool:
        return 0 < self.filled < self.requested

    @property
    def exit_fee(self) -> Decimal:
        return sum((f for _, _, _, f in self.exits), Decimal("0"))

    @property
    def pnl(self) -> Optional[Decimal]:
        """Net dollars for the whole order; None when contracts are unresolved or nothing filled."""
        if self.missed or self.filled == 0 or self.unresolved:
            return None
        proceeds = sum((D(p) * n for _, p, n, _ in self.exits), Decimal("0"))
        if self.settled:
            proceeds += D(self.settle_value) * self.settled
        return proceeds - D(self.entry_price) * self.filled - self.entry_fee - self.exit_fee

    @property
    def pnl_per_contract(self) -> Optional[float]:
        p = self.pnl
        return None if p is None else float(p / self.filled)


def ioc_entry(rows: Sequence[dict[str, Any]], decision_ts: float, limit: float, order: int, fee_model: Any,
              latency_s: float = 1.0, haircut: float = 1.0, entry_tol_s: float = 2.0) -> tuple[PaperTrade, Optional[dict[str, Any]]]:
    """The buy alone: (trade with filled / entry_price / entry_fee or ``missed``, the row it met)."""
    trade = PaperTrade(requested=int(order), limit=float(limit))
    t_in = decision_ts + latency_s
    entry = first_obs(rows, t_in, t_in + entry_tol_s)
    if entry is None:
        trade.missed, trade.reason = True, "no quote when the order arrives"
        return trade, None
    ask = _num(entry.get("ask"))
    if ask is None or ask > limit + 1e-9:
        trade.missed, trade.reason = True, "ask moved above the limit"
        return trade, entry
    filled = min(int(order), _size(entry, "ask_size", haircut))
    if filled <= 0:
        trade.missed, trade.reason = True, "no displayed size"
        return trade, entry
    trade.filled, trade.entry_price = filled, ask
    trade.entry_fee = D(fee_model.fee(ask, filled, "taker"))
    return trade, entry


def ioc_round_trip(rows: Iterable[dict[str, Any]], decision_ts: float, limit: float, order: int, fee_model: Any,
                   latency_s: float = 1.0, horizon_s: float = 30.0, haircut: float = 1.0,
                   settlement: Optional[float] = None, entry_tol_s: float = 2.0,
                   max_rolls: int = 3, roll_window_s: float = 60.0) -> PaperTrade:
    """Buy ``order`` contracts IOC at ``limit`` after ``latency_s``; sell to the bid after
    ``horizon_s`` (see the module docstring for every rule)."""
    rows = _dedupe_observations(rows)
    trade, entry = ioc_entry(rows, decision_ts, limit, order, fee_model, latency_s, haircut, entry_tol_s)
    if trade.missed:
        return trade
    held = trade.filled
    # The registered horizon is measured from the decision plus the assumed latency. A late
    # observation inside the arrival window must not silently move the label later.
    t_out = decision_ts + latency_s + horizon_s
    tol = max(1.0, 0.2 * horizon_s)
    mark = first_obs(rows, t_out, t_out + tol)
    candidates = [mark] if mark is not None else []
    later = sorted((r for r in rows if _refreshed(r) and (_t(r) or 0) > (t_out + tol if mark is None else _t(mark)) and (_t(r) or 0) <= t_out + roll_window_s), key=_t)
    candidates += later[:max_rolls]
    for r in candidates:
        if held <= 0:
            break
        bid = _num(r.get("bid"))
        n = min(held, _size(r, "bid_size", haircut)) if bid is not None and bid > 0 else 0
        if n <= 0:
            continue
        fee = D(fee_model.fee(bid, n, "taker"))
        trade.exits.append((_t(r), bid, n, fee))
        held -= n
    if held and settlement is not None:
        trade.settled, trade.settle_value, held = held, float(settlement), 0
    trade.unresolved = held
    if held:
        trade.reason = "contracts neither sold nor settled"
    return trade


@dataclass
class ArbResult:
    legs: tuple[PaperTrade, PaperTrade]
    matched: int = 0
    unwound: int = 0
    unwind_proceeds: Decimal = Decimal("0")
    unwind_fee: Decimal = Decimal("0")
    unresolved: int = 0
    excluded: str = ""
    tie_payouts: Optional[tuple[float, float]] = None
    settlement_compatible: Optional[bool] = None

    @property
    def guaranteed(self) -> bool:
        """A positive win-case P&L is a lock only with verified settlement and tie cases."""
        return self.settlement_compatible is True and self.tie_payouts is not None and self.pnl is not None and self.pnl > 0 and self.pnl_tie is not None and self.pnl_tie >= 0

    @property
    def leg_failure(self) -> bool:
        return self.legs[0].filled != self.legs[1].filled

    def _cost(self) -> Decimal:
        return sum((D(l.entry_price) * l.filled + l.entry_fee for l in self.legs if l.filled), Decimal("0"))

    @property
    def pnl(self) -> Optional[Decimal]:
        """Win-case net dollars; None when excluded or when excess contracts are unresolved."""
        if self.excluded or self.unresolved:
            return None
        return D(1) * self.matched + self.unwind_proceeds - self.unwind_fee - self._cost()

    @property
    def pnl_tie(self) -> Optional[Decimal]:
        if self.pnl is None or self.tie_payouts is None:
            return None
        return D(sum(self.tie_payouts)) * self.matched + self.unwind_proceeds - self.unwind_fee - self._cost()


def two_leg_arb(rows_a: Iterable[dict[str, Any]], rows_b: Iterable[dict[str, Any]], decision_ts: float,
                limit_a: float, limit_b: float, count: int, fee_a: Any, fee_b: Any,
                latency_a_s: float = 1.0, latency_b_s: float = 15.0, haircut: float = 1.0,
                tie_payouts: Optional[tuple[Optional[float], Optional[float]]] = None,
                unwind_latency_s: float = 1.0, entry_tol_s: float = 2.0, max_rolls: int = 3,
                unwind_window_s: float = 60.0, settlement_compatible: Optional[bool] = None) -> ArbResult:
    """Both legs IOC after their own latency. ``tie_payouts`` = (leg a, leg b) dollars per
    contract on a tie; an unknown value excludes the pair (ties settle differently by venue)."""
    rows_a, rows_b = _dedupe_observations(rows_a), _dedupe_observations(rows_b)
    books_a = {str(r.get("book_id")) for r in rows_a if r.get("book_id")}
    books_b = {str(r.get("book_id")) for r in rows_b if r.get("book_id")}
    if books_a & books_b:
        empty = PaperTrade(count, limit_a), PaperTrade(count, limit_b)
        return ArbResult(legs=empty, excluded="same-book")
    if settlement_compatible is False:
        empty = PaperTrade(count, limit_a), PaperTrade(count, limit_b)
        return ArbResult(legs=empty, excluded="settlement-mismatch")
    if tie_payouts is not None and any(v is None for v in tie_payouts):
        empty = PaperTrade(count, limit_a), PaperTrade(count, limit_b)
        return ArbResult(legs=empty, excluded="unknown-tie")
    a, obs_a = ioc_entry(rows_a, decision_ts, limit_a, count, fee_a, latency_a_s, haircut, entry_tol_s)
    b, obs_b = ioc_entry(rows_b, decision_ts, limit_b, count, fee_b, latency_b_s, haircut, entry_tol_s)
    res = ArbResult(legs=(a, b), matched=min(a.filled, b.filled), settlement_compatible=settlement_compatible,
                    tie_payouts=tuple(float(v) for v in tie_payouts) if tie_payouts is not None else None)
    excess = abs(a.filled - b.filled)
    if excess:
        # The larger leg is unhedged from the moment the other leg's result is known.
        known_a = _t(obs_a) if obs_a is not None else decision_ts + latency_a_s + entry_tol_s
        known_b = _t(obs_b) if obs_b is not None else decision_ts + latency_b_s + entry_tol_s
        big, rows, fee, known = (a, rows_a, fee_a, max(known_a, known_b)) if a.filled > b.filled else (b, rows_b, fee_b, max(known_a, known_b))
        t0 = known + unwind_latency_s
        later = sorted((r for r in rows if _refreshed(r) and (_t(r) or 0) >= t0
                        and (_t(r) or 0) <= t0 + unwind_window_s), key=_t)[: max_rolls + 1]
        for r in later:
            if not excess:
                break
            bid = _num(r.get("bid"))
            n = min(excess, _size(r, "bid_size", haircut)) if bid is not None and bid > 0 else 0
            if n <= 0:
                continue
            res.unwind_proceeds += D(bid) * n
            res.unwind_fee += D(fee.fee(bid, n, "taker"))
            res.unwound += n
            excess -= n
        res.unresolved = excess
    return res


def ioc_short_via_complement(complement_rows: Iterable[dict[str, Any]], decision_ts: float,
                             complement_limit: float, order: int, fee_model: Any, **kwargs: Any) -> PaperTrade:
    """Executable short exposure is a purchase of the complementary contract. The caller
    must supply that contract's own rows, limit, depth and fee model; no synthetic sale is
    inferred from the long book."""
    return ioc_round_trip(complement_rows, decision_ts, complement_limit, order, fee_model, **kwargs)


# Codex-era names.
ioc = ioc_round_trip
simulate_ioc = ioc_round_trip

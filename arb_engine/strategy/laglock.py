"""Turn LAG positions into locked sets: watch the other outcome after a LAG buy fills.

A LAG buys one outcome on the lagging venue; on its own that is a bet on convergence. Buy the
*other* outcome as well at a price where the two cost less than the $1 the pair pays, and
the bet becomes an arbitrage. The LAG's stale entry is what makes that likely: the game keeps
moving, and every swing gives the other side a chance to become cheap enough.

``LagLockBook`` tracks each filled LAG position (paper, demo or live) for ``watch_s`` seconds.
On every quote update it prices the other outcome on every executable venue at the position's
size, fees in (``FeeModel.fee`` at the real count), and locks when

    entry all-in + other outcome all-in <= 1 - target_margin      (the win case)

* paper positions lock on paper when the other outcome's ask shows enough size;
* demo / live positions whose cheapest lock is on Kalshi send an immediate-or-cancel buy
  through the LAG executor (``LagExecutor.buy_lock``) for the contracts not yet hedged; only
  fills lock, an unfilled IOC keeps watching, a partial fill keeps watching for the rest.
  The executor bounds every lock by the entry's verified fill (the order ledger) and a
  maximum number of attempts, so an unknown or partial lock result can never over-hedge;
  when it refuses (attempts used up, nothing left to hedge) the watch closes. A lock on a
  venue the engine cannot trade (Robinhood) is recorded as ``lockable`` for a person to act
  on. Lock orders are exempt from the LAG notional caps: they cut exposure, and a cap that
  blocked a hedge would leave the position naked.

The tie case is reported, not hidden: Kalshi YES + Kalshi YES pays $0.50 + $0.50, but a
Kalshi YES + a Rothera YES pays $0.50 on a tie (Rothera's YES pays nothing) and loses.
``require_tie_safe`` (default on) locks only pairs that pay >= $1 on a tie.

Every position lands in ``lag_locks``; ``summary`` gives the conversion rate (share of
positions that locked), time to lock and the locked margin - the numbers that decide whether
LAG deserves to be pushed again.
"""
from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from ..fees.base import D
from ..fees.registry import fee_model_for_quote

TIE = {"half": 0.5, "no_winner": 0.0}


@dataclass
class LockPosition:
    key: str
    event_key: str
    outcome: str
    lock_outcome: str
    venue: str
    contracts: int
    entry_price: float
    entry_all_in: float
    opened: float
    source: str                       # paper | demo | live
    entry_tie: Optional[float] = None  # what the entry contract pays on a tie
    status: str = "watching"          # watching | locked | lockable | expired | partial
    parent_id: Optional[str] = None   # demo/live: the entry's order-ledger intent (bounds the lock legs)
    locked_contracts: int = 0         # contracts of the lock outcome bought so far
    last_try: Optional[float] = None  # when a lock leg was last sent (demo/live retries are spaced)
    locked_at: Optional[float] = None
    lock_venue: Optional[str] = None
    lock_ask: Optional[float] = None
    lock_all_in: Optional[float] = None
    lock_margin: Optional[float] = None   # per contract, win case: 1 - entry all-in - lock all-in
    lock_tie_sum: Optional[float] = None
    order: Optional[dict] = None
    closed_at: Optional[float] = None

    @property
    def open(self) -> bool:
        return self.status == "watching"


class LagLockBook:
    def __init__(self, store: Any = None, watch_s: float = 600.0, target_margin: float = 0.0, executable: Optional[set[str]] = None,
                 executor: Any = None, alerter: Any = None, settings: Optional[dict[str, Any]] = None,
                 require_tie_safe: bool = True, fresh_s: float = 10.0, lock_retry_s: float = 10.0) -> None:
        self.store, self.watch_s, self.target_margin = store, float(watch_s), float(target_margin)
        self.executable = set(executable) if executable is not None else None
        self.executor, self.alerter, self.settings = executor, alerter, settings
        self.require_tie_safe, self.fresh_s = require_tie_safe, fresh_s
        # Seconds between two lock legs for one position: the fast lane observes every second,
        # and an unfilled IOC retried on every observation was a storm (457 lock orders for
        # one demo position on 2026-09-24). The executor also caps the number of attempts.
        self.lock_retry_s = float(lock_retry_s)
        self.positions: list[LockPosition] = []
        self._keys: set[str] = set()
        self._ensure_table()

    # ---- persistence ------------------------------------------------------------------
    def _ensure_table(self) -> None:
        conn = getattr(self.store, "conn", None)
        if conn is None:
            return
        with conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS lag_locks (
  key TEXT PRIMARY KEY, event_key TEXT, outcome TEXT, lock_outcome TEXT, venue TEXT, contracts INTEGER,
  entry_price REAL, entry_all_in REAL, opened REAL, source TEXT, status TEXT, locked_at REAL, lock_venue TEXT,
  lock_ask REAL, lock_all_in REAL, lock_margin REAL, lock_tie_sum REAL, closed_at REAL, extra_json TEXT)""")
            # A watch lives in memory; one a restart (or a game that stopped being observed)
            # left 'watching' past its window is expired at its window's end, whoever wrote it.
            conn.execute("UPDATE lag_locks SET status = 'expired', closed_at = opened + ? WHERE status = 'watching' AND opened < ?",
                         (self.watch_s, time.time() - self.watch_s))

    def _save(self, p: LockPosition) -> None:
        conn = getattr(self.store, "conn", None)
        if conn is None:
            return
        import json

        row = (p.key, p.event_key, p.outcome, p.lock_outcome, p.venue, p.contracts, p.entry_price, p.entry_all_in, p.opened, p.source,
               p.status, p.locked_at, p.lock_venue, p.lock_ask, p.lock_all_in, p.lock_margin, p.lock_tie_sum, p.closed_at,
               json.dumps({"order": p.order, "entry_tie": p.entry_tie, "parent_id": p.parent_id, "locked_contracts": p.locked_contracts}, default=str))
        lock = getattr(self.store, "_lock", None)
        sql = "INSERT OR REPLACE INTO lag_locks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        if lock is not None:
            with lock, conn:
                conn.execute(sql, row)
        else:
            with conn:
                conn.execute(sql, row)

    # ---- lifecycle --------------------------------------------------------------------
    def open(self, key: str, event_key: str, outcome: str, lock_outcome: str, venue: str, contracts: int, entry_price: float,
             entry_all_in: float, now: float, source: str, entry_tie: Optional[float] = None, parent_id: Optional[str] = None) -> Optional[LockPosition]:
        """Start watching a filled LAG position (once per key). ``parent_id`` is the entry's
        order-ledger intent for demo / live positions."""
        if key in self._keys or contracts <= 0:
            return None
        self._keys.add(key)
        p = LockPosition(key=key, event_key=event_key, outcome=outcome, lock_outcome=lock_outcome, venue=venue, contracts=int(contracts),
                         entry_price=float(entry_price), entry_all_in=float(entry_all_in), opened=float(now), source=source, entry_tie=entry_tie,
                         parent_id=parent_id)
        self.positions.append(p)
        self._save(p)
        return p

    def _cheapest(self, p: LockPosition, quotes_by_venue: dict[str, list[Any]], now: float) -> Optional[tuple]:
        """(venue, ask, all_in, quote, tie payout) of the cheapest fresh other-outcome contract
        that is an acceptable lock: with ``require_tie_safe``, the cheapest one whose pair pays
        >= $1 on a tie - not the cheapest overall (a cheaper Rothera YES that loses on a tie must
        not hide a Kalshi contract that locks)."""
        from .leadlag import _fresh
        from ..matching.settlement_rules import rule_for_quote

        sport = p.event_key.split(":", 1)[0].lower()
        mtype = "spread" if ":spread:" in p.event_key else ("total" if ":total:" in p.event_key else "moneyline")
        best = None
        for v, qs in quotes_by_venue.items():
            if self.executable is not None and v not in self.executable:
                continue
            for q in qs:
                if q.event_key != p.event_key or q.outcome != p.lock_outcome or q.ask is None or not 0 < q.ask < 1 or not _fresh(q, now, self.fresh_s):
                    continue
                if q.ask_size is not None and q.ask_size < p.contracts:
                    continue   # not enough shown to lock the whole position at this price
                try:
                    fm = fee_model_for_quote(q, self.settings)
                    all_in = float(D(q.ask) + fm.fee(q.ask, p.contracts, "taker") / p.contracts)
                except Exception:
                    continue
                try:
                    tv = TIE.get((rule_for_quote(q, sport, mtype) or {}).get("tie"))
                    if tv is not None and (q.meta or {}).get("side") == "no":
                        tv = 1.0 - tv
                except Exception:
                    tv = None
                if self.require_tie_safe and (p.entry_tie is None or tv is None or p.entry_tie + tv < 1.0 - 1e-9):
                    continue
                if best is None or all_in < best[2]:
                    best = (v, float(q.ask), all_in, q, tv)
        return best

    def _expire(self, p: LockPosition, now: float) -> str:
        p.status, p.closed_at = ("partial" if p.locked_contracts else "expired"), now
        self._save(p)
        return (f"LAG lock watch expired after {self.watch_s:.0f}s with {p.locked_contracts} of {p.contracts} hedged: "
                f"{p.contracts} x {p.outcome} on {p.venue} @ {p.entry_price:.2f}")

    def sweep(self, now: Optional[float] = None) -> list[str]:
        """Expire every watch past its window, whatever game it is on (``observe`` only sees
        the game it is called for, and a finished game is not observed again)."""
        now = time.time() if now is None else float(now)
        return [self._expire(p, now) for p in self.positions if p.open and now - p.opened > self.watch_s]

    def observe(self, event_key: str, quotes_by_venue: dict[str, list[Any]], now: Optional[float] = None) -> list[str]:
        now = time.time() if now is None else float(now)
        out: list[str] = []
        for p in self.positions:
            if p.event_key != event_key or not p.open:
                continue
            if now - p.opened > self.watch_s:
                out.append(self._expire(p, now))
                continue
            best = self._cheapest(p, quotes_by_venue, now)
            if best is None:
                continue
            v, ask, all_in, q, tv = best
            if p.entry_all_in + all_in > 1.0 - self.target_margin + 1e-12:
                continue
            tie_sum = (p.entry_tie + tv) if p.entry_tie is not None and tv is not None else None
            if self.require_tie_safe and (tie_sum is None or tie_sum < 1.0 - 1e-9):
                continue   # a pair that loses on a tie (or an unknown tie rule) is not a lock
            if p.source in ("demo", "live"):
                if v != "kalshi" or self.executor is None or not hasattr(self.executor, "buy_lock"):
                    if p.status != "lockable":
                        p.status, p.lock_venue, p.lock_ask, p.lock_all_in, p.lock_tie_sum = "lockable", v, ask, all_in, tie_sum
                        p.lock_margin = 1.0 - p.entry_all_in - all_in
                        self._save(p)
                        out.append(self._line(p, "LOCKABLE (needs a person on " + v + ")"))
                    continue
                if p.last_try is not None and now - p.last_try < self.lock_retry_s:
                    continue      # the last lock leg went out moments ago: space the retries
                p.last_try = now
                remaining = p.contracts - p.locked_contracts
                rec = self.executor.buy_lock(q, remaining, ask, p.event_key, now, parent_id=p.parent_id)
                p.order = rec
                filled = _num((rec or {}).get("fill_count")) if (rec or {}).get("status") == "SUBMITTED" else None
                if filled:
                    p.locked_contracts += int(filled)
                if p.locked_contracts < p.contracts:
                    if (rec or {}).get("status") == "skipped" and p.locked_contracts == 0 and "attempts" not in str(rec.get("reason")) and "hedged" not in str(rec.get("reason")):
                        self._save(p)     # e.g. no entry intent: nothing sent, keep watching
                        out.append(f"LAG lock not sent ({rec.get('reason')}); still watching {p.lock_outcome}")
                        continue
                    if (rec or {}).get("status") == "skipped":
                        # The executor refuses more lock legs (attempts used up / nothing left
                        # unhedged): the watch ends with what was hedged.
                        p.status, p.closed_at = ("partial" if p.locked_contracts else "expired"), now
                        self._save(p)
                        out.append(f"LAG lock watch closed with {p.locked_contracts} of {p.contracts} hedged: {rec.get('reason')}")
                        continue
                    self._save(p)
                    out.append(f"LAG lock order {(rec or {}).get('status')} (filled {filled or 0}; {p.locked_contracts} of {p.contracts} hedged); still watching {p.lock_outcome}")
                    continue
            self._lock(p, v, ask, all_in, tie_sum, now)
            out.append(self._line(p, "LOCKED"))
            if self.alerter is not None:
                try:
                    self.alerter.alert("LAG LOCKED", out[-1], event=p.event_key, lag_lock=asdict(p))
                except Exception:
                    pass
        return out

    def _lock(self, p: LockPosition, v: str, ask: float, all_in: float, tie_sum: Optional[float], now: float) -> None:
        p.status, p.locked_at, p.closed_at = "locked", now, now
        p.lock_venue, p.lock_ask, p.lock_all_in, p.lock_tie_sum = v, ask, all_in, tie_sum
        p.lock_margin = 1.0 - p.entry_all_in - all_in
        self._save(p)

    @staticmethod
    def _line(p: LockPosition, what: str) -> str:
        return (f"LAG {what}: {p.contracts} x {p.outcome} on {p.venue.upper()} (all-in {p.entry_all_in:.4f}) + {p.contracts} x {p.lock_outcome} "
                f"on {str(p.lock_venue).upper()} at the ask ${p.lock_ask:.2f} (all-in {p.lock_all_in:.4f}) = {p.entry_all_in + p.lock_all_in:.4f} "
                f"-> +${p.lock_margin * p.contracts:.2f} locked ({p.lock_margin * 100:+.2f}c/ct)"
                + (f"; tie pays ${p.lock_tie_sum:.2f}" if p.lock_tie_sum is not None else "") + f" [{p.source}]")

    def summary(self, event_key: Optional[str] = None) -> dict[str, Any]:
        ps = [p for p in self.positions if event_key is None or p.event_key == event_key]
        locked = [p for p in ps if p.status == "locked"]
        done = [p for p in ps if not p.open]
        out: dict[str, Any] = {"positions": len(ps), "locked": len(locked), "lockable": sum(1 for p in ps if p.status == "lockable"),
                               "partial": sum(1 for p in ps if p.status == "partial"),
                               "expired": sum(1 for p in ps if p.status == "expired"), "watching": sum(1 for p in ps if p.open),
                               "conversion": (len(locked) / len(done)) if done else None}
        if locked:
            waits = sorted(p.locked_at - p.opened for p in locked)
            out["median_seconds_to_lock"] = waits[len(waits) // 2]
            out["mean_lock_margin"] = sum(p.lock_margin for p in locked) / len(locked)
            out["locked_dollars"] = sum(p.lock_margin * p.contracts for p in locked)
        return out


def _num(x: Any) -> Optional[float]:
    try:
        f = float(x)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None

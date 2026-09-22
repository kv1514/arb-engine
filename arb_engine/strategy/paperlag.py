"""Paper fills for LAG signals, judged on the fast lane's next quotes.

A replayed LAG looks good (docs/MODEL.md: 120 W / 12 L selling to the bid a minute later)
only if the follower's ask is still there when the order arrives. This book answers that
with the data the fast lane already produces: every LAG opens a paper order at the
follower's ask for the suggested size; on each later step the order fills if the follower
still shows an ask ≤ the order price with size, expires after ``fill_window_s`` otherwise;
a filled order is marked to the follower's bid at +30 / +60 / +300 s and settled from the
final score (``Store.update_ladder`` settles the matching observation; this book reads the
same final ticks). Nothing is sent anywhere — it is a fill-probability and P&L experiment
whose rows live in ``lag_paper``.

What it deliberately does not model: queue position (a taker order at the ask trades
immediately if the size is there, so none), partial fills (the size shown is taken as
available in full), and Kalshi's order latency (~100 ms; the fast lane's 1 s step is the
coarser clock, so a fill measured here is at least as slow as a real one).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..models import OutcomeQuote

MARK_OFFSETS = (30, 60, 300)


@dataclass
class PaperOrder:
    key: str
    event_key: str
    follower: str
    outcome: str
    price: float          # the follower's ask when the LAG fired
    all_in: float         # price + entry fee per contract
    contracts: int
    opened: float
    leader: str
    edge: float
    filled_at: Optional[float] = None
    fill_price: Optional[float] = None
    expired_at: Optional[float] = None
    marks: dict[str, Optional[float]] = field(default_factory=dict)   # "bid_30" -> bid at +30 s
    settled: bool = False
    settle_value: Optional[float] = None

    @property
    def open(self) -> bool:
        return self.filled_at is None and self.expired_at is None


class LagPaperBook:
    """Open, fill, mark and settle paper LAG orders from the fast lane's quote stream."""

    def __init__(self, store: Any = None, fill_window_s: float = 10.0, alerter: Any = None) -> None:
        self.store = store
        self.fill_window_s = fill_window_s
        self.alerter = alerter
        self.orders: list[PaperOrder] = []
        self._ensure_table()

    def _ensure_table(self) -> None:
        conn = getattr(self.store, "conn", None)
        if conn is None:
            return
        with conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS lag_paper (
  key TEXT PRIMARY KEY, ts REAL, event_key TEXT, follower TEXT, outcome TEXT, leader TEXT, price REAL, all_in REAL,
  contracts INTEGER, edge REAL, filled_at REAL, fill_price REAL, expired_at REAL,
  bid_30 REAL, bid_60 REAL, bid_300 REAL, settled INTEGER DEFAULT 0, settle_value REAL, pnl_settle REAL, extra_json TEXT)""")

    def _save(self, o: PaperOrder) -> None:
        conn = getattr(self.store, "conn", None)
        if conn is None:
            return
        pnl = None
        if o.settled and o.settle_value is not None and o.fill_price is not None:
            pnl = round(o.settle_value - o.all_in, 4)
        row = (o.key, o.opened, o.event_key, o.follower, o.outcome, o.leader, o.price, o.all_in, o.contracts, o.edge, o.filled_at, o.fill_price, o.expired_at, o.marks.get("bid_30"), o.marks.get("bid_60"), o.marks.get("bid_300"), int(o.settled), o.settle_value, pnl, json.dumps({"marks": o.marks}))
        lock = getattr(self.store, "_lock", None)
        if lock is not None:
            with lock, conn:
                conn.execute("INSERT OR REPLACE INTO lag_paper VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        else:
            with conn:
                conn.execute("INSERT OR REPLACE INTO lag_paper VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)

    # ---- lifecycle -----------------------------------------------------------------------
    def open(self, sig: Any, now: float) -> Optional[PaperOrder]:
        """Open a paper order for a LagSignal (one per (event, follower, side) at a time)."""
        key = f"{sig.event_key}|{sig.follower}|{sig.outcome}|{int(now)}"
        if any(o.open and o.event_key == sig.event_key and o.follower == sig.follower and o.outcome == sig.outcome for o in self.orders):
            return None
        qty = int(sig.suggested_contracts or 0) or int(min(sig.depth or 10, 10))
        o = PaperOrder(key=key, event_key=sig.event_key, follower=sig.follower, outcome=sig.outcome, price=float(sig.follower_ask), all_in=float(sig.follower_all_in), contracts=qty, opened=now, leader=sig.leader, edge=float(sig.edge))
        self.orders.append(o)
        self._save(o)
        return o

    def observe(self, event_key: str, quotes_by_venue: dict[str, list[OutcomeQuote]], now: float) -> list[str]:
        """Feed one step's quotes: fill / expire open orders, mark filled ones. Returns log lines."""
        out: list[str] = []
        for o in self.orders:
            if o.event_key != event_key:
                continue
            q = next((x for x in quotes_by_venue.get(o.follower, []) if x.outcome == o.outcome and (x.meta or {}).get("side") != "no"), None)
            if o.open:
                if now - o.opened > self.fill_window_s:
                    o.expired_at = now
                    self._save(o)
                    out.append(f"paper LAG expired unfilled after {self.fill_window_s:.0f}s: {o.outcome} on {o.follower} @ {o.price:.2f}")
                    if self.alerter is not None:
                        self.alerter.info(out[-1], event=o.event_key, paper_lag=o.key)
                elif q is not None and q.ask is not None and q.ask <= o.price + 1e-9 and (q.ask_size is None or q.ask_size > 0):
                    o.filled_at, o.fill_price = now, q.ask
                    self._save(o)
                    out.append(f"paper LAG filled {o.contracts} x {o.outcome} on {o.follower} @ {q.ask:.2f} after {now - o.opened:.1f}s")
                    if self.alerter is not None:
                        self.alerter.info(out[-1], event=o.event_key, paper_lag=o.key)
            elif o.filled_at is not None and q is not None and q.bid is not None:
                for off in MARK_OFFSETS:
                    k = f"bid_{off}"
                    if k not in o.marks and now - o.filled_at >= off:
                        o.marks[k] = q.bid
                        self._save(o)
        return out

    def settle(self, event_key: str, winner: Optional[str], now: Optional[float] = None) -> int:
        """Settle every filled order of a final game (winner None = tie)."""
        n = 0
        for o in self.orders:
            if o.event_key == event_key and o.filled_at is not None and not o.settled:
                o.settled, o.settle_value = True, (0.5 if winner is None else (1.0 if o.outcome == winner else 0.0))
                self._save(o)
                n += 1
        return n

    def summary(self) -> dict[str, Any]:
        filled = [o for o in self.orders if o.filled_at is not None]
        expired = [o for o in self.orders if o.expired_at is not None]
        out: dict[str, Any] = {"orders": len(self.orders), "filled": len(filled), "expired": len(expired), "open": sum(1 for o in self.orders if o.open)}
        if filled:
            out["fill_latency_median_s"] = sorted(o.filled_at - o.opened for o in filled)[len(filled) // 2]
            for off in MARK_OFFSETS:
                xs = [o.marks[f"bid_{off}"] - o.all_in for o in filled if o.marks.get(f"bid_{off}") is not None]
                if xs:
                    out[f"pnl_bid_{off}"] = {"n": len(xs), "wins": sum(1 for x in xs if x > 0), "mean": round(sum(xs) / len(xs), 4)}
            st = [o.settle_value - o.all_in for o in filled if o.settled and o.settle_value is not None]
            if st:
                out["pnl_settle"] = {"n": len(st), "wins": sum(1 for x in st if x > 0), "mean": round(sum(st) / len(st), 4)}
        return out

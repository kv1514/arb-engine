"""Execute LAG signals on Kalshi: an immediate-or-cancel buy at the laggard's ask.

The lead-lag edge lives ~23 s (docs/MODEL.md, "The first live Sunday"); a person reading a
push, opening the market and typing an order is slower than that, so this is the path that
turns the signal into a fill. Modes, from safest up:

* ``off``    — nothing (the default; the paper book still records what would have happened).
* ``intent`` — every order the executor *would* send is appended to
               ``out/orders/lag_intents.jsonl`` (ticker, side, price, count, edge) — the
               dry run to read before turning anything on.
* ``demo``   — sends to Kalshi's demo exchange (``KALSHI_ENV=demo`` + the demo key).
* ``live``   — sends to production; also needs ``ARB_LIVE_TRADING=1`` (the executor's own gate).

Every order is a taker buy of the outcome's own market (YES on the ticker the signal names)
at the ask the signal saw, ``immediate_or_cancel`` so nothing rests if the book has moved,
sized by the signal (bankroll fraction ∧ displayed depth) and capped by ``max_contracts``,
``max_notional_per_game`` (open cost per game) and ``daily_notional`` (total sent today).
A signal whose follower is not Kalshi, whose Kalshi row is a NO leg of another ticker, or
whose edge is below ``min_edge`` is skipped and the reason journalled. Fills are read back
from the create-order response (``fill_count`` / ``remaining_count``) when the exchange
reports them, else left as ``unknown`` for the demo check script to reconcile.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

MODES = ("off", "intent", "demo", "live")


@dataclass
class LagExecutor:
    mode: str = "off"
    executor: Any = None                 # execution.kalshi.KalshiExecutor (demo/live)
    intents_path: str = "out/orders/lag_intents.jsonl"
    max_contracts: int = 50
    max_notional_per_game: float = 100.0
    daily_notional: float = 500.0
    min_edge: float = 0.02
    alerter: Any = None
    clock: Any = time.time
    sent_notional: float = 0.0
    per_game: dict[str, float] = field(default_factory=dict)
    day: Optional[str] = None
    orders: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if self.mode in ("demo", "live") and self.executor is None:
            raise ValueError("demo/live need a KalshiExecutor")
        if self.mode == "live" and os.environ.get("ARB_LIVE_TRADING") != "1":
            raise RuntimeError("live LAG execution requires ARB_LIVE_TRADING=1")

    # ---- helpers ------------------------------------------------------------------------
    def describe(self, rec: Optional[dict[str, Any]]) -> Optional[str]:
        """One line for the LAG push: what the auto-trader did with this signal."""
        if not rec or self.mode == "off":
            return None
        tag = f"AUTO ({self.mode})"
        st = rec.get("status")
        if st == "intent":
            return f"{tag}: would send IOC buy {rec.get('count')} x {rec.get('ticker')} @ {rec.get('price')}"
        if st == "skipped":
            return f"{tag}: skipped - {rec.get('reason')}"
        if st == "SUBMITTED":
            return (f"{tag}: sent IOC buy {rec.get('count')} x {rec.get('ticker')} @ {rec.get('price')} -> "
                    f"filled {rec.get('fill_count')}" + (f" (${rec['filled_notional']:.2f})" if isinstance(rec.get("filled_notional"), (int, float)) else ""))
        return f"{tag}: FAILED - {rec.get('reason') or st}"

    def _roll_day(self, now: float) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if day != self.day:
            self.day, self.sent_notional, self.per_game = day, 0.0, {}

    def _journal(self, rec: dict[str, Any]) -> None:
        """Append to the intents file; an order that errored (or that the executor did not
        submit) also raises an EXEC ERROR alert, which is pushed: a broken auto-trader in the
        middle of a game must not be a line in a file nobody is reading."""
        self.orders.append(rec)
        try:
            os.makedirs(os.path.dirname(self.intents_path) or ".", exist_ok=True)
            with open(self.intents_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass
        if self.alerter is not None and self.mode in ("demo", "live") and rec.get("status") not in ("SUBMITTED", "intent", "skipped", None):
            try:
                self.alerter.alert("EXEC ERROR", f"LAG auto-trade ({self.mode}) failed: {rec.get('ticker')} buy {rec.get('count')} @ {rec.get('price')} - {rec.get('reason') or rec.get('status')}", event=rec.get("event_key"))
            except Exception:
                pass
        if self.alerter is not None:
            try:
                self.alerter.info(f"lag-exec {rec.get('status')}: {rec.get('ticker')} {rec.get('side')} {rec.get('count')} @ {rec.get('price')}" + (f" — {rec['reason']}" if rec.get("reason") else ""), event=rec.get("event_key"), lag_exec=rec)
            except Exception:
                pass

    @staticmethod
    def kalshi_leg(sig: Any, quotes_by_venue: dict[str, list[Any]]) -> Optional[tuple[str, str, Optional[int]]]:
        """(ticker, side, exchange_index) for the signal's Kalshi row: the outcome's own YES
        market. A NO row (the other contract) is not traded here."""
        for q in quotes_by_venue.get("kalshi", []):
            if q.outcome != sig.outcome:
                continue
            meta = q.meta or {}
            if meta.get("side") == "no":
                continue
            ticker = meta.get("ticker") or q.venue_market_id.split("#")[0]
            if ticker:
                return ticker, "yes", meta.get("exchange_index")
        return None

    # ---- the one entry point ----------------------------------------------------------
    def on_signal(self, sig: Any, quotes_by_venue: dict[str, list[Any]], now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """Consider one LagSignal. Returns the journal record (or None when mode is off)."""
        if self.mode == "off":
            return None
        now = self.clock() if now is None else now
        self._roll_day(now)
        rec: dict[str, Any] = {"ts": now, "mode": self.mode, "event_key": sig.event_key, "follower": sig.follower, "outcome": sig.outcome, "leader": sig.leader, "edge": round(sig.edge, 4), "price": sig.follower_ask, "count": None, "ticker": None, "side": None, "status": None}
        if sig.follower != "kalshi":
            rec.update(status="skipped", reason="follower is not kalshi")
            self._journal(rec)
            return rec
        if sig.edge < self.min_edge:
            rec.update(status="skipped", reason=f"edge {sig.edge:.3f} < {self.min_edge}")
            self._journal(rec)
            return rec
        leg = self.kalshi_leg(sig, quotes_by_venue)
        if leg is None:
            rec.update(status="skipped", reason="no Kalshi YES row for this outcome")
            self._journal(rec)
            return rec
        ticker, side, exchange_index = leg
        rec.update(ticker=ticker, side=side)
        count = int(sig.suggested_contracts or 0) or int(min(sig.depth or 0, self.max_contracts))
        count = min(count, self.max_contracts)
        cost = count * float(sig.follower_ask)
        room_game = self.max_notional_per_game - self.per_game.get(sig.event_key, 0.0)
        room_day = self.daily_notional - self.sent_notional
        room = min(room_game, room_day)
        if room <= 0:
            rec.update(count=0, status="skipped", reason="notional cap reached")
            self._journal(rec)
            return rec
        if cost > room:
            count = int(room // float(sig.follower_ask))
            cost = count * float(sig.follower_ask)
        if count <= 0:
            rec.update(count=0, status="skipped", reason="size rounds to zero under the caps")
            self._journal(rec)
            return rec
        rec["count"] = count
        if self.mode == "intent":
            rec["status"] = "intent"
            self._journal(rec)
            return rec
        try:
            plan = self.executor.plan(ticker, "buy", side, count, float(sig.follower_ask), post_only=False, exchange_index=exchange_index, note=f"LAG {sig.leader}->{sig.follower} edge {sig.edge:+.3f}", time_in_force="immediate_or_cancel")
            res = self.executor.execute(plan, confirm=True)
        except Exception as e:
            rec.update(status="error", reason=repr(e))
            self._journal(rec)
            return rec
        rec["status"] = res.get("status")
        resp = res.get("response") or {}
        od = resp.get("order") or resp
        rec["order_id"] = od.get("order_id") or od.get("id")
        rec["fill_count"] = od.get("fill_count", "unknown")
        rec["remaining_count"] = od.get("remaining_count", "unknown")
        if rec["status"] == "SUBMITTED":
            # The caps bound exposure, and an immediate-or-cancel order that did not fill is
            # none: count what filled. A response without a fill count counts in full.
            filled = _num(rec["fill_count"])
            spent = filled * float(sig.follower_ask) if filled is not None else cost
            rec["filled_notional"] = round(spent, 4)
            self.sent_notional += spent
            self.per_game[sig.event_key] = self.per_game.get(sig.event_key, 0.0) + spent
        else:
            rec.setdefault("reason", f"executor returned {rec['status']!r}")
        self._journal(rec)
        return rec


def _num(x: Any) -> Optional[float]:
    """Kalshi's fixed-point strings ("12.00") and numbers -> float; anything else -> None."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

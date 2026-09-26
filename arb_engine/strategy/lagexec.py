"""Execute LAG signals on Kalshi: an immediate-or-cancel buy at the laggard's ask.

This is the gated path from a LAG observation to an immediate-or-cancel order. Historical
latency and P&L measurements are legacy results until re-run with the current strict
horizon, two-sided fee and fill accounting. Modes, from safest up:

* ``off``    — nothing (the default; the paper book still records what would have happened).
* ``intent`` — every order the executor *would* send is appended to
               ``out/orders/lag_intents.jsonl`` (ticker, side, price, count, edge) — the
               dry run to read before turning anything on. Nothing is reserved or sent.
* ``demo``   — sends to Kalshi's demo exchange (``KALSHI_ENV=demo`` + the demo key).
* ``live``   — sends to production; also needs ``ARB_LIVE_TRADING=1`` (the executor's own gate).

Every order is a taker buy of the outcome's own market (YES on the ticker the signal names)
at the ask the signal saw, ``immediate_or_cancel`` so nothing rests if the book has moved,
sized by the signal (bankroll fraction ∧ displayed depth) and capped by ``max_contracts``.
A signal whose follower is not Kalshi, whose Kalshi row is a NO leg of another ticker, whose
contract has an unverified settlement rule or whose edge is below ``min_edge`` is skipped
and the reason journalled.

Demo / live orders go through the environment's durable order ledger
(``execution/ledger.py``), shared by every process trading that environment:

* the intent and its worst-case cost (fees included) are written and reserved against the
  ``daily_notional`` and ``max_notional_per_game`` budgets *before* the request, in one
  cross-process transaction; a signal already sent once (same event, outcome, leader and
  signal time) is refused as a duplicate; the ledger's ``client_order_id`` goes on the order;
* a request whose outcome is unknown (timeout, 5xx, 409, a response without an order id, a
  crash) leaves the intent ``ambiguous`` at its worst case and blocks every new order until
  :meth:`LagExecutor.reconcile` finds it on the exchange by that ``client_order_id``;
* the budgets survive a restart (they are sums over the ledger), and a restart first
  reconciles whatever the previous process left open;
* each accepted order's actual fills and fees are read back from the exchange and replace
  the reserved worst case;
* if the ledger cannot be opened or written, nothing is sent (``blocked``).

Lock legs (``buy_lock``, strategy/laglock.py) are exempt from the budgets - they cut
exposure - but only for contracts the entry verifiably filled that are not already hedged or
unresolved, and at most ``max_lock_attempts`` times per entry, so a partial or unknown lock
fill can never over-hedge.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from ..execution.ledger import ACCEPTED, Budget, LedgerError, OrderLedger

MODES = ("off", "intent", "demo", "live")
BLOCK_ALERT_EVERY_S = 600.0


@dataclass
class LagExecutor:
    mode: str = "off"
    executor: Any = None                 # execution.kalshi.KalshiExecutor (demo/live)
    intents_path: str = "out/orders/lag_intents.jsonl"
    max_contracts: int = 50
    max_notional_per_game: float = 100.0  # dollars per game (all its markets), fees included
    daily_notional: float = 500.0         # dollars per local day, fees included
    min_edge: float = 0.02
    alerter: Any = None
    clock: Any = time.time
    ledger: Any = None                   # execution.ledger.OrderLedger; built from the client when None
    ledger_path: Optional[str] = None
    reconcile_every_s: float = 5.0
    max_lock_attempts: int = 3
    orders: list[dict[str, Any]] = field(default_factory=list)
    blocked_reason: Optional[str] = None  # the ledger is unusable: nothing is sent
    journal_errors: int = 0
    _last_reconcile: float = field(default=-math.inf, repr=False)
    _last_alert: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if self.mode in ("demo", "live") and self.executor is None:
            raise ValueError("demo/live need a KalshiExecutor")
        if self.mode == "live" and os.environ.get("ARB_LIVE_TRADING") != "1":
            raise RuntimeError("live LAG execution requires ARB_LIVE_TRADING=1")
        if self.mode in ("demo", "live"):
            want = "prod" if self.mode == "live" else "demo"
            env = str(getattr(self.executor.client, "env", "") or "").lower()
            if env != want:
                raise RuntimeError(f"--execute-lag {self.mode} needs a Kalshi {want} client; this one is {env or 'unset'}")
            if self.ledger is None:
                try:
                    self.ledger = OrderLedger.for_client(self.executor.client, path=self.ledger_path, clock=self.clock)
                except LedgerError as e:
                    self.blocked_reason = f"order ledger unusable: {e}"
            if self.ledger is not None:
                self.recover()

    # ---- budgets (read from the ledger: they survive restarts and span processes) --------
    @property
    def sent_notional(self) -> float:
        """Dollars committed today by LAG entries (fees included; open orders at worst case)."""
        if self.ledger is None:
            return 0.0
        return float(self.ledger.exposure(strategy="lag", day=self.ledger.day_of(self.clock())))

    def game_notional(self, event_key: str) -> float:
        if self.ledger is None:
            return 0.0
        return float(self.ledger.exposure(strategy="lag", game_key=_game(event_key)))

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
        if st == "blocked":
            return f"{tag}: BLOCKED - {rec.get('reason')}"
        if st == "SUBMITTED":
            return (f"{tag}: sent IOC buy {rec.get('count')} x {rec.get('ticker')} @ {rec.get('price')} -> "
                    f"filled {rec.get('fill_count')}" + (f" (${rec['filled_notional']:.2f})" if isinstance(rec.get("filled_notional"), (int, float)) else ""))
        if st == "UNKNOWN":
            return (f"{tag}: UNKNOWN - sent IOC buy {rec.get('count')} x {rec.get('ticker')} @ {rec.get('price')} but the outcome is unknown "
                    f"({rec.get('reason')}); new orders blocked until it is reconciled")
        return f"{tag}: FAILED - {rec.get('reason') or st}"

    def _journal(self, rec: dict[str, Any]) -> None:
        """Append to the intents file (the human-readable log; the ledger is the durable
        record). A demo/live order that errored, went unknown or was blocked also raises an
        EXEC ERROR alert, which is pushed: a broken auto-trader in the middle of a game must
        not be a line in a file nobody is reading. A failed journal write is counted and
        alerted too instead of disappearing."""
        # ``ts`` is the decision time the caller passed (the fast-lane thread and the full tick
        # decide on their own clocks, so ``ts`` can step backwards between lines); ``logged_ts``
        # is the wall clock at the write, which orders the file.
        rec.setdefault("logged_ts", round(time.time(), 3))
        self.orders.append(rec)
        try:
            os.makedirs(os.path.dirname(self.intents_path) or ".", exist_ok=True)
            with open(self.intents_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError as e:
            self.journal_errors += 1
            self._alert_once("journal", f"LAG journal {self.intents_path} not writable ({e!r}); {self.journal_errors} record(s) only in memory"
                             + ("" if self.ledger is None else " - the order ledger still has every order"), rec.get("event_key"))
        st = rec.get("status")
        if self.alerter is not None and self.mode in ("demo", "live") and st not in ("SUBMITTED", "intent", "skipped", None):
            text = (f"LAG auto-trade ({self.mode}) {'blocked' if st == 'blocked' else 'failed' if st != 'UNKNOWN' else 'outcome unknown'}: "
                    f"{rec.get('ticker')} buy {rec.get('count')} @ {rec.get('price')} - {rec.get('reason') or st}")
            if st == "blocked":
                self._alert_once(f"blocked:{str(rec.get('reason'))[:60]}", text, rec.get("event_key"))
            else:
                try:
                    self.alerter.alert("EXEC ERROR", text, event=rec.get("event_key"))
                except Exception:
                    pass
        if self.alerter is not None:
            try:
                self.alerter.info(f"lag-exec {st}: {rec.get('ticker')} {rec.get('side')} {rec.get('count')} @ {rec.get('price')}" + (f" — {rec['reason']}" if rec.get("reason") else ""), event=rec.get("event_key"), lag_exec=rec)
            except Exception:
                pass

    def _alert_once(self, key: str, text: str, event: Optional[str]) -> None:
        now = self.clock()
        if self.alerter is None or now - self._last_alert.get(key, -math.inf) < BLOCK_ALERT_EVERY_S:
            return
        self._last_alert[key] = now
        try:
            self.alerter.alert("EXEC ERROR", text, event=event)
        except Exception:
            pass

    @staticmethod
    def kalshi_leg(sig: Any, quotes_by_venue: dict[str, list[Any]]) -> Optional[tuple[str, str, Optional[int]]]:
        """(ticker, side, exchange_index) for the signal's Kalshi row: the outcome's own YES
        market. A NO row (the other contract) is not traded here."""
        q = LagExecutor._kalshi_quote(sig, quotes_by_venue)
        if q is None:
            return None
        meta = q.meta or {}
        return meta.get("ticker") or q.venue_market_id.split("#")[0], "yes", meta.get("exchange_index")

    @staticmethod
    def _kalshi_quote(sig: Any, quotes_by_venue: dict[str, list[Any]]) -> Any:
        for q in quotes_by_venue.get("kalshi", []):
            if q.outcome != sig.outcome:
                continue
            meta = q.meta or {}
            if meta.get("side") == "no":
                continue
            if meta.get("ticker") or q.venue_market_id.split("#")[0]:
                return q
        return None

    # ---- reconciliation -----------------------------------------------------------------
    def recover(self) -> list[dict]:
        """At start: reconcile whatever earlier processes left open, and say if that blocks."""
        res = self.reconcile(force=True)
        if self.ledger is not None:
            why = self.ledger.blocked(self.clock())
            self._journal_note({"kind": "recover", "reconciled": res, "blocked": why})
            if why:
                self._alert_once("recover", f"LAG executor ({self.mode}) starts BLOCKED: {why}", None)
        return res

    def reconcile(self, now: Optional[float] = None, force: bool = False) -> list[dict]:
        """Resolve open orders against the exchange (at most every ``reconcile_every_s``).
        Never raises: a failure leaves the orders open (and an unknown one blocking)."""
        if self.ledger is None or self.mode not in ("demo", "live"):
            return []
        now = self.clock() if now is None else now
        if not force and now - self._last_reconcile < self.reconcile_every_s:
            return []
        self._last_reconcile = now
        try:
            res = self.ledger.reconcile(self.executor.client, now)
        except Exception as e:  # noqa: BLE001
            res = [{"error": repr(e)[:300]}]
        changed = [r for r in res if r.get("before") != r.get("after") or r.get("error") or str(r.get("note", "")).startswith("error")]
        if changed:
            self._journal_note({"kind": "reconcile", "ts": now, "results": changed})
        return res

    def _journal_note(self, rec: dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(self.intents_path) or ".", exist_ok=True)
            with open(self.intents_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": self.clock(), "mode": self.mode, **rec}, default=str) + "\n")
        except OSError:
            self.journal_errors += 1

    # ---- sending (demo / live) ----------------------------------------------------------
    def _send(self, res: Any, ticker: str, side: str, price: float, exchange_index: Optional[int], note: str) -> dict[str, Any]:
        """Submit a reserved intent and record the answer. Returns the fields for the record."""
        req_ts = self.clock()
        try:
            plan = self.executor.plan(ticker, "buy", side, res.count, float(price), post_only=False, exchange_index=exchange_index,
                                      note=note, time_in_force="immediate_or_cancel", client_order_id=res.client_order_id)
        except Exception as e:  # never sent
            self.ledger.rejected(res.intent_id, f"plan refused: {e!r}")
            return {"status": "error", "state": "rejected", "reason": f"plan refused: {e!r}"}
        try:
            result = self.executor.execute(plan, confirm=True)
        except Exception as e:  # the request may have reached the exchange
            hint = getattr(e, "status", None)
            reason = f"{type(e).__name__}: {e}"[:300]
            try:
                self.ledger.ambiguous(res.intent_id, reason, req_ts=req_ts, hint=hint if isinstance(hint, int) else None)
            except LedgerError as le:
                self.blocked_reason = f"ledger write failed after an order was sent: {le}"
            return {"status": "UNKNOWN", "state": "ambiguous", "reason": reason, "req_ts": req_ts, "resp_ts": self.clock()}
        resp_ts = self.clock()
        st = result.get("status")
        if st != "SUBMITTED":   # a gate refused it: nothing was sent
            self.ledger.rejected(res.intent_id, f"executor returned {st!r}")
            return {"status": st, "state": "rejected", "reason": f"executor returned {st!r}"}
        resp = result.get("response") or {}
        try:
            state = self.ledger.accepted(res.intent_id, resp, now=resp_ts, req_ts=req_ts)
        except LedgerError as le:
            self.blocked_reason = f"ledger write failed after an order was sent: {le}"
            state = "unrecorded"
        od = resp.get("order") if isinstance(resp.get("order"), dict) else resp
        out: dict[str, Any] = {"state": state, "order_id": od.get("order_id") or od.get("id"),
                               "fill_count": od.get("fill_count", "unknown"), "remaining_count": od.get("remaining_count", "unknown"),
                               "average_fill_price": od.get("average_fill_price"), "average_fee_paid": od.get("average_fee_paid"),
                               "req_ts": req_ts, "resp_ts": resp_ts, "latency_ms": round((resp_ts - req_ts) * 1000, 1)}
        if state == ACCEPTED:
            out["status"] = "SUBMITTED"
        else:
            out.update(status="UNKNOWN", reason="create response without an order id" if state != "unrecorded" else self.blocked_reason)
        return out

    def buy_lock(self, quote: Any, count: int, price: float, event_key: str, now: Optional[float] = None,
                 parent_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        """The lock leg of a filled LAG (strategy/laglock.py): an immediate-or-cancel buy of
        the other outcome's Kalshi contract at ``price``. Exempt from the notional budgets -
        it cuts exposure, and a cap that blocked it would leave the position unhedged - but
        bounded by the entry's verified fill (``parent_id`` = the entry's intent), the lock
        contracts already bought or unresolved, and ``max_lock_attempts``."""
        if self.mode in ("off", "intent"):
            return {"status": self.mode, "reason": "lock legs are only sent in demo / live"}
        now = self.clock() if now is None else now
        meta = getattr(quote, "meta", None) or {}
        ticker = meta.get("ticker") or str(getattr(quote, "venue_market_id", "")).split("#")[0]
        side = meta.get("side") or "yes"
        rec: dict[str, Any] = {"ts": now, "mode": self.mode, "event_key": event_key, "kind": "lock", "ticker": ticker, "side": side,
                               "price": price, "count": int(count), "parent_id": parent_id, "status": None,
                               "quote_age_s": _age(quote, now)}
        if self.blocked_reason:
            rec.update(status="blocked", reason=self.blocked_reason)
            self._journal(rec)
            return rec
        if not parent_id:
            rec.update(status="skipped", reason="lock leg without its entry intent: the inventory cannot be verified")
            self._journal(rec)
            return rec
        try:
            res = self.ledger.reserve(strategy="lock", ticker=ticker, side=side, count=int(count), limit_price=price, event_key=event_key,
                                      game_key=_game(event_key), parent_id=parent_id, max_lock_attempts=self.max_lock_attempts, now=now,
                                      detail={"kind": "lock"})
        except LedgerError as e:
            rec.update(status="blocked", reason=f"order ledger: {e}")
            self._journal(rec)
            return rec
        if not res.ok:
            rec.update(status="skipped", reason=res.reason)
            self._journal(rec)
            return rec
        rec.update(count=res.count, intent_id=res.intent_id, client_order_id=res.client_order_id, max_cost=float(res.max_cost))
        rec.update(self._send(res, ticker, side, price, meta.get("exchange_index"), "LAG lock leg"))
        self._journal(rec)
        return rec

    # ---- the one entry point ----------------------------------------------------------
    def on_signal(self, sig: Any, quotes_by_venue: dict[str, list[Any]], now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """Consider one LagSignal. Returns the journal record (or None when mode is off)."""
        if self.mode == "off":
            return None
        now = self.clock() if now is None else now
        rec: dict[str, Any] = {"ts": now, "mode": self.mode, "event_key": sig.event_key, "follower": sig.follower, "outcome": sig.outcome, "leader": sig.leader,
                               "edge": round(sig.edge, 4), "price": sig.follower_ask, "count": None, "ticker": None, "side": None, "status": None,
                               "signal_ts": getattr(sig, "ts", None), "signal_age_s": _round(now - float(sig.ts)) if isinstance(getattr(sig, "ts", None), (int, float)) else None}
        settlement_flags = tuple(getattr(sig, "settlement_flags", ()) or ())
        if settlement_flags:
            rec.update(status="skipped", reason="the contract bought has an unverified settlement rule: " + ", ".join(settlement_flags))
            self._journal(rec)
            return rec
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
        rec.update(ticker=ticker, side=side, quote_age_s=_age(self._kalshi_quote(sig, quotes_by_venue), now))
        count = int(sig.suggested_contracts or 0) or int(min(sig.depth or 0, self.max_contracts))
        count = min(count, self.max_contracts)
        if count <= 0:
            rec.update(count=0, status="skipped", reason="size rounds to zero")
            self._journal(rec)
            return rec
        if self.mode == "intent":
            rec.update(count=count, status="intent")
            self._journal(rec)
            return rec
        # ---- demo / live: through the ledger --------------------------------------------
        self.reconcile(now)
        if self.blocked_reason:
            rec.update(count=count, status="blocked", reason=self.blocked_reason)
            self._journal(rec)
            return rec
        dedupe = f"lag:{sig.event_key}:{sig.outcome}:{sig.leader}:{float(sig.ts):.3f}" if isinstance(getattr(sig, "ts", None), (int, float)) else None
        try:
            res = self.ledger.reserve(strategy="lag", ticker=ticker, side=side, count=count, limit_price=sig.follower_ask, event_key=sig.event_key,
                                      game_key=_game(sig.event_key), dedupe_key=dedupe,
                                      budget=Budget(daily=Decimal(str(self.daily_notional)), per_game=Decimal(str(self.max_notional_per_game))),
                                      now=now, detail={"leader": sig.leader, "edge": round(sig.edge, 4), "signal_ts": getattr(sig, "ts", None),
                                                       "quote_age_s": rec.get("quote_age_s"), "leader_mid": getattr(sig, "leader_mid", None)})
        except LedgerError as e:
            rec.update(count=count, status="blocked", reason=f"order ledger: {e}")
            self._journal(rec)
            return rec
        if not res.ok:
            blocked = "unknown outcome" in res.reason
            rec.update(count=0 if not blocked else count, status="blocked" if blocked else "skipped",
                       reason=res.reason if blocked or "budget" not in res.reason else f"notional cap reached ({res.reason})")
            self._journal(rec)
            return rec
        rec.update(count=res.count, intent_id=res.intent_id, client_order_id=res.client_order_id, max_cost=float(res.max_cost))
        rec.update(self._send(res, ticker, side, sig.follower_ask, exchange_index, f"LAG {sig.leader}->{sig.follower} edge {sig.edge:+.3f}"))
        if rec.get("status") == "SUBMITTED":
            filled = _num(rec.get("fill_count"))
            avg = _num(rec.get("average_fill_price"))
            px = avg if avg is not None else float(sig.follower_ask)
            # A response without a fill count counts in full (the ledger reserves it so too).
            rec["filled_notional"] = round((filled if filled is not None else res.count) * px, 4)
        self._journal(rec)
        return rec


def _num(x: Any) -> Optional[float]:
    """Kalshi's fixed-point strings ("12.00") and numbers -> float; anything else -> None."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _round(x: float) -> float:
    return round(float(x), 3)


def _age(q: Any, now: float) -> Optional[float]:
    """Seconds since the quote was observed (its receipt time when recorded, else its ts)."""
    if q is None:
        return None
    meta = getattr(q, "meta", None) or {}
    t = _num(meta.get("obs_ts")) if meta.get("obs_ts") is not None else _num(getattr(q, "ts", None))
    return _round(now - t) if t is not None else None


def _game(event_key: str) -> str:
    from ..matching.normalize import game_event_key

    return game_event_key(str(event_key or ""))

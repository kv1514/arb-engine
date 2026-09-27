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
exposure - but only once they are proven hedges of their entry, and only of an entry whose
fill reconciliation has *verified* (a final order row and a complete fills listing agree,
never below an earlier answer): a create answer alone, or exchange answers that contradict
each other, size no lock leg (the lock book keeps watching until the entry is verified).
A contradicted order - fewer fills than an earlier answer showed, or row and fills apart -
keeps its whole worst case and raises an EXEC ERROR alert. ``buy_lock`` checks what
market data can show: the quote is a Kalshi quote of the entry's market, no older than
``lock_quote_max_age_s``, priced at or under its ask; the lock contract's settlement identity
(the outcome it pays on, its side, its tie payout, whether the market can tie) comes from
the settlement registry, or the lock is refused; and the exchange still holds the entry's
contracts (``GET /portfolio/positions?ticker=``; unreadable -> refused, fewer -> capped).
The ledger then proves the rest atomically (``execution/ledger.OrderLedger._lock_check``):
same market and Kalshi event, the *other* outcome, complementary payoffs, no related order
with an unknown outcome, and the remaining inventory after exits and earlier lock legs; at
most ``max_lock_attempts`` legs per entry. Every entry records its own settlement identity
for that proof.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from ..execution.ledger import (ACCEPTED, CONTRADICTED, HEDGEABLE, Budget, FeeMultipliers, LedgerError, OrderLedger, quote_fee_multiplier,
                                refusal_hint, settlement_identity)

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
    fee_multipliers: Any = None          # execution.ledger.FeeMultipliers on the trading client (demo/live)
    lock_quote_max_age_s: float = 10.0   # a lock leg is priced on a quote no older than this (laglock's fresh_s)
    verify_position: bool = True         # lock legs: the exchange must still show the entry's contracts
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
            if self.fee_multipliers is None:
                self.fee_multipliers = FeeMultipliers(self.executor.client, clock=self.clock)
            if self.ledger is None:
                try:
                    self.ledger = OrderLedger.for_client(self.executor.client, path=self.ledger_path, clock=self.clock,
                                                         lock_quote_max_age_s=self.lock_quote_max_age_s)
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
        changed = [r for r in res if r.get("before") != r.get("after") or r.get("error") or str(r.get("note", "")).startswith("error")
                   or r.get("fill_state") == CONTRADICTED]
        if changed:
            self._journal_note({"kind": "reconcile", "ts": now, "results": changed})
        for r in res:
            if r.get("fill_state") == CONTRADICTED:
                # The exchange's answers about an order disagree (fewer fills than it showed
                # before, or order row and fills apart): its whole worst case stays reserved,
                # and a person decides (`kalshi reconcile`, or `kalshi correct` for a real
                # exchange correction). Once per intent per BLOCK_ALERT_EVERY_S.
                self._alert_once(f"contradicted:{r.get('intent_id')}",
                                 f"LAG executor ({self.mode}): Kalshi's answers about {r.get('strategy')} {r.get('ticker')} disagree - "
                                 f"{str(r.get('note'))[:300]}. Check the exchange, then `kalshi reconcile` or `kalshi correct`.", None)
        return res

    def _journal_note(self, rec: dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(self.intents_path) or ".", exist_ok=True)
            with open(self.intents_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": self.clock(), "mode": self.mode, **rec}, default=str) + "\n")
        except OSError:
            self.journal_errors += 1

    def _fee_multiplier(self, ticker: str, quote: Any) -> tuple[Any, Optional[str]]:
        """The market's fee multiplier for the reservation: the larger of what the quote
        states and what the trading exchange reports; (None, why) when neither is known."""
        stated = quote_fee_multiplier(getattr(quote, "fee_params", None)) if quote is not None else None
        return self.fee_multipliers.resolve(ticker, stated)

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
            reason = f"{type(e).__name__}: {e}"[:300]
            try:
                self.ledger.ambiguous(res.intent_id, reason, req_ts=req_ts, hint=refusal_hint(e))
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

    # ---- lock legs: what the caller checks before the ledger's proof --------------------
    def _lock_quote_problem(self, quote: Any, ticker: str, price: float, event_key: str, now: float) -> Optional[str]:
        """Why this quote cannot price a lock leg of ``event_key``'s entry, or None."""
        if str(getattr(quote, "venue", "")).lower() != "kalshi":
            return f"not a Kalshi quote ({getattr(quote, 'venue', None)}): the lock is sent to Kalshi"
        if not ticker:
            return "the quote names no Kalshi ticker"
        if str(getattr(quote, "event_key", "") or "") != str(event_key or ""):
            return f"unrelated market: the quote is for {getattr(quote, 'event_key', None)}, the position for {event_key}"
        t = _quote_time(quote)
        if t is None:
            return "the quote has no time: its freshness cannot be shown"
        age = now - t
        if age > self.lock_quote_max_age_s:
            return f"stale quote: {age:.1f}s old (max {self.lock_quote_max_age_s:g}s)"
        if age < -2.0:
            return f"quote time {-age:.1f}s in the future"
        ask = _num(getattr(quote, "ask", None))
        p = _num(price)
        if ask is None or not 0 < ask < 1:
            return "the quote has no ask"
        if p is None or not 0 < p <= ask + 1e-9:
            return f"price {price!r} is not at or under the quote's ask {ask}"
        return None

    def _exchange_holding(self, ticker: str, side: str) -> tuple[Optional[Decimal], Optional[str]]:
        """(contracts of ``ticker``/``side`` the exchange shows this account holding, None) or
        (None, why unreadable). Kalshi nets a market's YES and NO into one signed position."""
        fn = getattr(self.executor.client, "positions", None)
        if not callable(fn):
            return None, "this client cannot read positions"
        try:
            res = fn(ticker=ticker)
        except Exception as e:  # noqa: BLE001
            status = getattr(e, "status", None)
            return None, f"GET /portfolio/positions failed ({type(e).__name__}{f' {status}' if status is not None else ''})"
        rows = (res or {}).get("market_positions") if isinstance(res, dict) else None
        if not isinstance(rows, list):
            return None, "GET /portfolio/positions answered without market_positions"
        pos = Decimal(0)
        for r in rows:
            if str(r.get("ticker") or r.get("market_ticker") or "") == ticker:
                v = r.get("position_fp") if r.get("position_fp") is not None else r.get("position")
                try:
                    pos = Decimal(str(v))
                except Exception:  # noqa: BLE001
                    return None, f"unreadable position {v!r}"
                break
        held = pos if str(side).lower() == "yes" else -pos
        return max(held, Decimal(0)), None

    def buy_lock(self, quote: Any, count: int, price: float, event_key: str, now: Optional[float] = None,
                 parent_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        """The lock leg of a filled LAG (strategy/laglock.py): an immediate-or-cancel buy of
        the other outcome's Kalshi contract at ``price``. Exempt from the notional budgets -
        it cuts exposure, and a cap that blocked it would leave the position unhedged - but
        only as a proven hedge of the entry ``parent_id``: the checks here (Kalshi quote of
        the entry's market, fresh, priced at or under its ask; the lock contract's settlement
        identity from the registry; the exchange still showing the entry's contracts) and
        the ledger's (same market, other outcome, complementary payoffs, no related unknown
        order, remaining inventory after exits and earlier lock legs, ``max_lock_attempts``)."""
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

        def skip(why: str) -> dict[str, Any]:
            rec.update(status="skipped", reason=f"lock leg: {why}")
            self._journal(rec)
            return rec

        why = self._lock_quote_problem(quote, ticker, price, event_key, now)
        if why:
            return skip(why)
        parent = self.ledger.get(parent_id)
        if parent is None:
            return skip("unknown entry intent")
        if str(parent.get("event_key") or "") != str(event_key):
            return skip(f"unrelated market: the entry is in {parent.get('event_key')}, the lock quote in {event_key}")
        # Only a verified fill is inventory (the ledger checks it again, atomically): not a
        # create answer alone, not answers that disagree. Not terminal: reconciliation may
        # verify it on the next read, and the lock book keeps watching.
        if parent.get("fill_state") == CONTRADICTED:
            return skip("the entry's fill evidence is contradicted: nothing is hedged on it until it is resolved")
        if parent.get("fill_state") not in HEDGEABLE:
            return skip("the entry's fill is not verified (its order row and a complete fills listing have not confirmed it yet)")
        sd, why = settlement_of(quote, event_key)
        if sd is None:
            return skip(f"unknown settlement identity ({why})")
        rec["settlement"] = sd
        if self.verify_position:
            held, why = self._exchange_holding(str(parent["ticker"]), str(parent["side"]))
            if held is None:
                return skip(f"the entry's contracts cannot be verified on the exchange ({why})")
            rec["exchange_holding"] = str(held)
            if held < 1:
                return skip(f"the exchange shows none of the entry's {parent['ticker']} {parent['side']} held")
            count = min(int(count), int(held))
        mult, why = self._fee_multiplier(ticker, quote)
        if mult is None:
            return skip(why)
        try:
            res = self.ledger.reserve(strategy="lock", ticker=ticker, side=side, count=int(count), limit_price=price, event_key=event_key,
                                      game_key=_game(event_key), parent_id=parent_id, max_lock_attempts=self.max_lock_attempts, now=now,
                                      fee_multiplier=mult, settlement=sd, quote_ts=_quote_time(quote), detail={"kind": "lock"})
        except LedgerError as e:
            rec.update(status="blocked", reason=f"order ledger: {e}")
            self._journal(rec)
            return rec
        if not res.ok:
            # Terminal when no later lock leg of this entry can pass either (the watch can end).
            rec.update(status="skipped", reason=res.reason, terminal=("attempts already" in res.reason or "nothing left to hedge" in res.reason))
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
        kq = self._kalshi_quote(sig, quotes_by_venue)
        mult, why = self._fee_multiplier(ticker, kq)
        if mult is None:
            rec.update(count=count, status="skipped", reason=why)
            self._journal(rec)
            return rec
        rec["fee_multiplier"] = str(mult)
        entry_settlement, _ = settlement_of(kq, sig.event_key)     # recorded: a lock leg must prove itself against it
        try:
            res = self.ledger.reserve(strategy="lag", ticker=ticker, side=side, count=count, limit_price=sig.follower_ask, event_key=sig.event_key,
                                      game_key=_game(sig.event_key), dedupe_key=dedupe,
                                      budget=Budget(daily=Decimal(str(self.daily_notional)), per_game=Decimal(str(self.max_notional_per_game))),
                                      fee_multiplier=mult, now=now, settlement=entry_settlement,
                                      detail={"leader": sig.leader, "edge": round(sig.edge, 4), "signal_ts": getattr(sig, "ts", None),
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


# Sports whose games cannot end tied (overtime / shootout / tiebreak decide them).
SPORTS_WITHOUT_TIES = frozenset({"ncaaf", "nba", "tennis", "mlb"})


def can_tie(event_key: str, rule: Optional[dict] = None) -> bool:
    """Whether a market can settle on a tie / push, which is when a pair's tie payouts matter:
    an integer spread or total line can push, a half-point one cannot; a moneyline can tie
    unless the sport cannot end level - or whenever the venue's rule states a tie clause.
    Unknown sports count as able to tie."""
    ek = str(event_key or "")
    if ":spread:" in ek or ":total:" in ek:
        tail = ek.rsplit(":", 1)[-1]
        num = tail.rsplit("-", 1)[-1] if ":spread:" in ek else tail
        try:
            v = float(num.lstrip("+"))
        except ValueError:
            return True
        return abs(v - round(v)) < 1e-9
    if rule and rule.get("tie"):
        return True
    return ek.split(":", 1)[0].lower() not in SPORTS_WITHOUT_TIES


def settlement_of(quote: Any, event_key: str) -> tuple[Optional[dict], Optional[str]]:
    """The settlement identity of the contract ``quote`` buys (execution/ledger.
    settlement_identity), from the settlement registry's *verified* rule for its venue and
    market; (None, why) when there is no verified rule or a tie payout the market needs is
    unknown. A NO row pays ``1 - tie`` on a tie."""
    if quote is None:
        return None, "no quote"
    ek = str(event_key or getattr(quote, "event_key", "") or "")
    sport = ek.split(":", 1)[0].lower()
    mtype = "spread" if ":spread:" in ek else ("total" if ":total:" in ek else "moneyline")
    try:
        from ..matching.settlement_rules import rule_for_quote

        rule = rule_for_quote(quote, sport, mtype)
    except Exception as e:  # noqa: BLE001
        return None, f"settlement rule lookup failed ({type(e).__name__})"
    if not rule:
        return None, f"no settlement rule for {getattr(quote, 'venue', None)} {sport} {mtype}"
    if str(rule.get("status") or "") != "verbatim":
        return None, f"the {getattr(quote, 'venue', None)} {sport} {mtype} rule is not verified ({rule.get('status')})"
    side = str((getattr(quote, "meta", None) or {}).get("side") or "yes").lower()
    tie = {"half": 0.5, "no_winner": 0.0}.get(rule.get("tie"))
    if tie is not None and side == "no":
        tie = 1.0 - tie
    tied = can_tie(ek, rule)
    if tied and tie is None:
        return None, f"the market can tie but the {sport} {mtype} rule states no tie payout"
    return settlement_identity(getattr(quote, "venue", ""), ek, getattr(quote, "outcome", ""), side, tie, tied), None


def _quote_time(q: Any) -> Optional[float]:
    """When the quote was observed: its receipt time when recorded, else its ts."""
    meta = getattr(q, "meta", None) or {}
    t = _num(meta.get("obs_ts")) if meta.get("obs_ts") is not None else _num(getattr(q, "ts", None))
    return t


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

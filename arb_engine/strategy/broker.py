"""Brokers for the maker runner: a paper simulator driven by live market data, and the
real Kalshi broker (demo or prod) behind the execution gates.

Both brokers share two safety pieces: :class:`SelfMatchGuard`, which refuses an order that
would trade against our own book (a hedge quoted from Kalshi's book via Robinhood, or a
resting own order on the other side of the same ticker), and :meth:`Broker.cancel_all`, the
one call a shutdown path needs to leave zero resting orders (batched on Kalshi)."""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..execution.kalshi import gtd_horizon_s
from ..venues.kalshi import KalshiClient, batch_cancel_reduced, build_order_payload, order_expiration, order_side_price

log = logging.getLogger(__name__)


@dataclass
class RestingOrder:
    order_id: str
    ticker: str
    side: str            # yes | no
    price: float         # price of that side, dollars
    count: float
    filled: float = 0.0
    status: str = "resting"   # resting | filled | canceled | rejected
    created: float = field(default_factory=time.time)
    watch_key: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def remaining(self) -> float:
        return max(0.0, self.count - self.filled)

    @property
    def notional(self) -> float:
        return self.price * self.remaining


class SelfMatchRefused(RuntimeError):
    """Raised by ``place`` when :class:`SelfMatchGuard` says the order would hit our own book."""


class SelfMatchGuard:
    """Why an order must not be sent, or ``None``.

    * ``hedge_book_id == "kalshi"``: the hedge quote is Robinhood re-selling Kalshi's own
      book (``OutcomeQuote.book_id``), so the "hedge" is the same liquidity we are resting
      against — filling and hedging would be one trade with ourselves plus two fees.
    * A resting own order on the same ticker and the other side crosses when the two buy
      prices sum to at least $1: our YES bid at ``p`` is a NO ask at ``1 - p``, so a NO bid
      at ``q >= 1 - p`` would lift it. Kalshi's ``self_trade_prevention_type`` would cancel
      one side after the fact; refusing up front keeps the book the maker believes it has.
    """

    def __init__(self, eps: float = 1e-9):
        self.eps = eps  # float slack so 0.40 + 0.60 counts as $1

    def crosses(self, side: str, price: float, other_side: str, other_price: float) -> bool:
        if side.lower() == other_side.lower():
            return False
        return float(price) + float(other_price) >= 1.0 - self.eps

    def check(self, ticker: str, side: str, price: float, *, hedge_book_id: Optional[str] = None, resting: Optional[Iterable[RestingOrder]] = None) -> Optional[str]:
        if (hedge_book_id or "").lower() == "kalshi":
            return "hedge leg is Kalshi's own book (book_id=kalshi): resting would trade against ourselves"
        for o in resting or ():
            if o.status != "resting" or o.ticker != ticker:
                continue
            if self.crosses(side, price, o.side, o.price):
                return f"would cross own resting {o.side} @ {o.price:.2f} on {ticker} (order {o.order_id})"
        return None


class Broker:
    name = "base"
    guard: SelfMatchGuard = SelfMatchGuard()

    def __init__(self) -> None:
        self.placed: list[RestingOrder] = []  # every order this broker placed

    @property
    def _placed(self) -> list[RestingOrder]:
        """``placed`` for subclasses (test fakes) that skip ``__init__``."""
        if "placed" not in self.__dict__:
            self.placed = []
        return self.placed

    def place(self, ticker: str, side: str, price: float, count: float, watch_key: str = "", exchange_index: Optional[int] = None, *, resting: Optional[Iterable[RestingOrder]] = None, hedge_book_id: Optional[str] = None, kickoff: Optional[float] = None) -> RestingOrder:  # pragma: no cover - interface
        raise NotImplementedError

    def cancel(self, order: RestingOrder) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def poll(self, orders: list[RestingOrder], market_state: dict[str, dict[str, float]]) -> list[tuple[RestingOrder, float, float]]:
        """Update orders in place; return (order, newly_filled_count, fill_price) for new fills."""
        raise NotImplementedError  # pragma: no cover

    def _refuse_self_match(self, ticker: str, side: str, price: float, resting: Optional[Iterable[RestingOrder]], hedge_book_id: Optional[str]) -> None:
        # Our own tracked orders are always consulted; the caller's ``resting`` (P11's
        # reconcile passes the runner's list) covers orders placed before a restart.
        seen = list(resting or ()) + [o for o in self._placed if o.status == "resting"]
        reason = self.guard.check(ticker, side, price, hedge_book_id=hedge_book_id, resting=seen)
        if reason:
            raise SelfMatchRefused(reason)

    def cancel_all(self, orders: Optional[Iterable[RestingOrder]] = None) -> list[RestingOrder]:
        """Cancel every resting order (the given ones plus everything this broker placed);
        returns the orders that were cancelled. Default: one ``cancel`` per order."""
        out: list[RestingOrder] = []
        for o in self._resting(orders):
            self.cancel(o)
            out.append(o)
        return out

    def _resting(self, orders: Optional[Iterable[RestingOrder]]) -> list[RestingOrder]:
        seen: dict[int, RestingOrder] = {}
        for o in list(orders or ()) + self._placed:
            if o.status == "resting":
                seen.setdefault(id(o), o)
        return list(seen.values())


class PaperBroker(Broker):
    """Simulated Kalshi: an order rests until the market's best ask for our side drops to our
    price or below (someone hit our bid), then fills in full at our price. Optimistic about
    queue position — a real fill needs sellers to reach us — so paper results are an upper
    bound on fill frequency, not on margin (prices are the real ones)."""

    name = "paper"

    def place(self, ticker: str, side: str, price: float, count: float, watch_key: str = "", exchange_index: Optional[int] = None, *, resting: Optional[Iterable[RestingOrder]] = None, hedge_book_id: Optional[str] = None, kickoff: Optional[float] = None) -> RestingOrder:
        self._refuse_self_match(ticker, side, price, resting, hedge_book_id)
        payload = build_order_payload(ticker, "buy", side, count, price, post_only=True, exchange_index=exchange_index, expiration_time=order_expiration(kickoff, gtd_horizon_s=gtd_horizon_s()), cancel_order_on_pause=True)
        order = RestingOrder(order_id="paper-" + uuid.uuid4().hex[:8], ticker=ticker, side=side, price=price, count=count, watch_key=watch_key, payload=payload)
        self._placed.append(order)
        return order

    def cancel(self, order: RestingOrder) -> None:
        if order.status == "resting":
            order.status = "canceled"

    def poll(self, orders: list[RestingOrder], market_state: dict[str, dict[str, float]]) -> list[tuple[RestingOrder, float, float]]:
        fills: list[tuple[RestingOrder, float, float]] = []
        for o in orders:
            if o.status != "resting":
                continue
            st = market_state.get(o.ticker) or {}
            ask = st.get(f"{o.side}_ask")
            if ask is not None and ask <= o.price + 1e-9:
                qty = o.remaining
                o.filled = o.count
                o.status = "filled"
                fills.append((o, qty, o.price))
        return fills


class KalshiBroker(Broker):
    """Real orders through the signed Kalshi API. ``client.env`` decides demo vs prod; prod
    additionally needs ``ARB_LIVE_TRADING=1`` and ``confirm=True`` (the CLI's --confirm)."""

    name = "kalshi"

    def __init__(self, client: Optional[KalshiClient] = None, confirm: bool = False):
        super().__init__()
        self.client = client or KalshiClient()
        self.confirm = confirm
        if not self.client.has_credentials:
            raise RuntimeError("KalshiBroker needs KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH")
        if self.client.env == "prod" and (os.environ.get("ARB_LIVE_TRADING") != "1" or not confirm):
            raise RuntimeError("prod trading requires ARB_LIVE_TRADING=1 and --confirm")
        if not confirm:
            raise RuntimeError("pass confirm=True (--confirm) to let the runner place demo/prod orders")

    def place(self, ticker: str, side: str, price: float, count: float, watch_key: str = "", exchange_index: Optional[int] = None, *, resting: Optional[Iterable[RestingOrder]] = None, hedge_book_id: Optional[str] = None, kickoff: Optional[float] = None) -> RestingOrder:
        self._refuse_self_match(ticker, side, price, resting, hedge_book_id)
        payload = build_order_payload(ticker, "buy", side, count, price, post_only=True, exchange_index=exchange_index, client_order_id=str(uuid.uuid4()), expiration_time=order_expiration(kickoff, gtd_horizon_s=gtd_horizon_s()), cancel_order_on_pause=True)
        res = self.client.create_order(payload)
        od = res.get("order") or res
        oid = str(od.get("order_id") or od.get("id") or payload["client_order_id"])
        status = str(od.get("status") or "resting")
        order = RestingOrder(order_id=oid, ticker=ticker, side=side, price=price, count=count, watch_key=watch_key, payload=payload)
        if status in ("canceled", "cancelled", "rejected"):
            order.status = "rejected"
        self._placed.append(order)
        return order

    def cancel(self, order: RestingOrder) -> None:
        if order.status != "resting":
            return
        try:
            self.client.cancel_order(order.order_id)
        finally:
            order.status = "canceled"

    def cancel_all(self, orders: Optional[Iterable[RestingOrder]] = None, sweep: bool = False) -> list[RestingOrder]:
        """One batched DELETE per :data:`BATCH_CANCEL_MAX` orders for everything resting;
        with ``sweep=True`` also cancels resting orders the exchange reports that this process
        does not know about (orphans from a killed run). Returns the orders now off the book.

        Only an exchange-confirmed cancel (``reduced_by > 0`` in the batched response, or a
        2xx single cancel) marks an order ``canceled``; anything else stays ``resting`` so the
        caller can see what is still on the book. If the batched call itself fails the orders
        are cancelled one by one, so a shutdown never leaves orders because one endpoint
        misbehaved. A failed sweep listing is never swallowed: the exchange-side
        ``cancel_all_orders`` (needs no listing) takes over, and if that fails too the error
        propagates after our own orders were cancelled — a sweep that silently returns ``[]``
        is exactly the orphan bug this method exists to prevent."""
        mine = self._resting(orders)
        known = {o.order_id for o in mine}
        sweep_error: Optional[BaseException] = None
        if sweep:
            try:
                for od in self.client.orders_v2(status="resting"):
                    oid = str(od.get("order_id") or od.get("id") or "")
                    if oid and oid not in known:
                        known.add(oid)
                        side, price = order_side_price(od)
                        orphan = RestingOrder(order_id=oid, ticker=str(od.get("ticker") or od.get("market_ticker") or ""), side=side, price=price or 0.0, count=_fp(od.get("remaining_count_fp") or od.get("initial_count_fp") or od.get("count_fp")) or 0.0, payload=dict(od))
                        mine.append(orphan)
                        self._placed.append(orphan)  # now tracked: a later cancel_all retries it and the guard sees it
            except Exception as e:  # noqa: BLE001 - reported below, never dropped
                sweep_error = e
                log.warning("kalshi sweep: listing resting orders failed (%r); falling back to DELETE /portfolio/events/orders", e)
        done = self._cancel_batched(mine) if mine else []
        if sweep_error is not None:
            try:
                self.client.cancel_all_orders()
            except Exception as e2:
                raise RuntimeError(f"kalshi sweep failed: listing ({sweep_error!r}) and cancel-all ({e2!r}) both errored; resting orders may remain on the book") from e2
        return done

    def _cancel_batched(self, mine: list[RestingOrder]) -> list[RestingOrder]:
        by_id = {o.order_id: o for o in mine}
        entries = [{"order_id": o.order_id, "exchange_index": o.payload.get("exchange_index"), "market_ticker": o.ticker or None} for o in mine]
        try:
            reduced = batch_cancel_reduced(self.client.cancel_orders_batched(entries))
        except Exception as e:  # noqa: BLE001 - per-order fallback keeps the shutdown going
            log.warning("kalshi batched cancel failed (%r); cancelling %d orders one by one", e, len(mine))
            reduced = {}
        confirmed = {oid for oid, n in reduced.items() if n > 0}
        done: list[RestingOrder] = []
        for oid, o in by_id.items():
            if oid not in confirmed:
                # reduced_by == 0 ("the cancel errored") or missing from the response: retry singly.
                try:
                    self.client.cancel_order(oid)
                except Exception:
                    continue  # stays "resting" so the caller can see what is still on the book
            o.status = "canceled"
            done.append(o)
        return done

    def poll(self, orders: list[RestingOrder], market_state: dict[str, dict[str, float]]) -> list[tuple[RestingOrder, float, float]]:
        fills: list[tuple[RestingOrder, float, float]] = []
        for o in orders:
            if o.status != "resting":
                continue
            try:
                od = self.client.order(o.order_id)
            except Exception:
                continue
            filled = _fp(od.get("fill_count_fp") or od.get("fill_count") or 0.0)
            if filled is None:
                remaining = _fp(od.get("remaining_count_fp") or od.get("remaining_count"))
                filled = (o.count - remaining) if remaining is not None else 0.0
            new = max(0.0, filled - o.filled)
            if new > 0:
                o.filled = filled
                fills.append((o, new, o.price))
            status = str(od.get("status") or "").lower()
            if status in ("executed", "filled") or o.remaining <= 1e-9:
                o.status = "filled"
            elif status in ("canceled", "cancelled", "expired"):
                o.status = "canceled"
        return fills


def _fp(x: Any) -> Optional[float]:
    try:
        return float(x) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None

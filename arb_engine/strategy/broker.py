"""Brokers for the maker runner: a paper simulator driven by live market data, and the
real Kalshi broker (demo or prod) behind the execution gates."""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..venues.kalshi import KalshiClient, build_order_payload


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


class Broker:
    name = "base"

    def place(self, ticker: str, side: str, price: float, count: float, watch_key: str = "", exchange_index: Optional[int] = None) -> RestingOrder:  # pragma: no cover - interface
        raise NotImplementedError

    def cancel(self, order: RestingOrder) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def poll(self, orders: list[RestingOrder], market_state: dict[str, dict[str, float]]) -> list[tuple[RestingOrder, float, float]]:
        """Update orders in place; return (order, newly_filled_count, fill_price) for new fills."""
        raise NotImplementedError  # pragma: no cover


class PaperBroker(Broker):
    """Simulated Kalshi: an order rests until the market's best ask for our side drops to our
    price or below (someone hit our bid), then fills in full at our price. Optimistic about
    queue position — a real fill needs sellers to reach us — so paper results are an upper
    bound on fill frequency, not on margin (prices are the real ones)."""

    name = "paper"

    def place(self, ticker: str, side: str, price: float, count: float, watch_key: str = "", exchange_index: Optional[int] = None) -> RestingOrder:
        return RestingOrder(order_id="paper-" + uuid.uuid4().hex[:8], ticker=ticker, side=side, price=price, count=count, watch_key=watch_key, payload=build_order_payload(ticker, "buy", side, count, price, post_only=True, exchange_index=exchange_index))

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
        self.client = client or KalshiClient()
        self.confirm = confirm
        if not self.client.has_credentials:
            raise RuntimeError("KalshiBroker needs KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH")
        if self.client.env == "prod" and (os.environ.get("ARB_LIVE_TRADING") != "1" or not confirm):
            raise RuntimeError("prod trading requires ARB_LIVE_TRADING=1 and --confirm")
        if not confirm:
            raise RuntimeError("pass confirm=True (--confirm) to let the runner place demo/prod orders")

    def place(self, ticker: str, side: str, price: float, count: float, watch_key: str = "", exchange_index: Optional[int] = None) -> RestingOrder:
        payload = build_order_payload(ticker, "buy", side, count, price, post_only=True, exchange_index=exchange_index, client_order_id=str(uuid.uuid4()))
        res = self.client.create_order(payload)
        od = res.get("order") or res
        oid = str(od.get("order_id") or od.get("id") or payload["client_order_id"])
        status = str(od.get("status") or "resting")
        order = RestingOrder(order_id=oid, ticker=ticker, side=side, price=price, count=count, watch_key=watch_key, payload=payload)
        if status in ("canceled", "cancelled", "rejected"):
            order.status = "rejected"
        return order

    def cancel(self, order: RestingOrder) -> None:
        if order.status != "resting":
            return
        try:
            self.client.cancel_order(order.order_id)
        finally:
            order.status = "canceled"

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

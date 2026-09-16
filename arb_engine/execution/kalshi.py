from __future__ import annotations

import os
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from ..venues.kalshi import KalshiClient, build_order_payload


@dataclass
class OrderPlan:
    venue: str
    ticker: str
    action: str  # buy | sell
    side: str    # yes | no
    count: float
    price: float
    post_only: bool = False
    time_in_force: str = "good_till_canceled"
    exchange_index: Optional[int] = None
    client_order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    note: str = ""

    def payload(self) -> dict[str, Any]:
        return build_order_payload(self.ticker, self.action, self.side, self.count, self.price, time_in_force=self.time_in_force, post_only=self.post_only, client_order_id=self.client_order_id, exchange_index=self.exchange_index)


class KalshiExecutor:
    """Three safety gates before anything reaches the exchange:

    1. ``confirm=True`` must be passed (the CLI flag ``--confirm``).
    2. The client targets the DEMO environment unless ``KALSHI_ENV=prod``.
    3. On prod, ``ARB_LIVE_TRADING=1`` must also be set.
    """

    def __init__(self, client: Optional[KalshiClient] = None):
        self.client = client or KalshiClient()

    def plan(self, ticker: str, action: str, side: str, count: float, price: float, post_only: bool = False, exchange_index: Optional[int] = None, note: str = "") -> OrderPlan:
        if not (0 < price < 1):
            raise ValueError("price must be in (0, 1) dollars")
        if count <= 0:
            raise ValueError("count must be positive")
        return OrderPlan(venue="kalshi", ticker=ticker, action=action, side=side, count=count, price=round(price, 4), post_only=post_only, exchange_index=exchange_index, note=note)

    def execute(self, plan: OrderPlan, confirm: bool = False) -> dict[str, Any]:
        payload = plan.payload()
        preview = {"env": self.client.env, "base_url": self.client.base_url, "plan": asdict(plan), "payload": payload}
        if not confirm:
            preview["status"] = "DRY_RUN (pass confirm=True / --confirm to submit)"
            return preview
        if self.client.env == "prod" and os.environ.get("ARB_LIVE_TRADING") != "1":
            preview["status"] = "BLOCKED: KALSHI_ENV=prod requires ARB_LIVE_TRADING=1"
            return preview
        preview["response"] = self.client.create_order(payload)
        preview["status"] = "SUBMITTED"
        return preview

    def execute_legs(self, plans: list[OrderPlan], confirm: bool = False) -> list[dict[str, Any]]:
        """Submit legs sequentially; stop at the first failure so you are never left with
        more unhedged legs than necessary. Cross-venue arbs are still exposed to leg risk on
        the venues we cannot route to."""
        out: list[dict[str, Any]] = []
        for plan in plans:
            res = self.execute(plan, confirm=confirm)
            out.append(res)
            if res.get("status") not in ("SUBMITTED", "DRY_RUN (pass confirm=True / --confirm to submit)"):
                break
        return out

"""Kalshi order plans and the gated executor.

Every order the engine can send is built here as an :class:`OrderPlan` first, so the dry-run
preview shows the exact V2 payload (including the expiry) that ``confirm=True`` would submit.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from ..venues.kalshi import IOC_TIFS, KalshiClient, build_order_payload, order_expiration

# How long a resting (GTC) order may live without the process that hedges it re-affirming it.
# Kalshi's ``expiration_time`` is the exchange-side backstop for a killed maker: the order
# dies on its own even when the shutdown cancel never ran. Kickoff caps it further.
KALSHI_GTD_HORIZON_S_DEFAULT = 3600.0
try:  # settings registry from P01; keep importable without it
    from ..config import declare_setting as _declare_setting  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - depends on which plan items are merged
    _declare_setting = None
if _declare_setting is not None:  # pragma: no cover
    try:
        _declare_setting("kalshi_gtd_horizon_s", env="KALSHI_GTD_HORIZON_S", default=KALSHI_GTD_HORIZON_S_DEFAULT, cast=float, doc="Seconds a resting Kalshi order may live before the exchange expires it (capped at kickoff).")
    except Exception:
        pass


def gtd_horizon_s() -> float:
    """``KALSHI_GTD_HORIZON_S`` (seconds); ``0`` disables the horizon (kickoff cap still applies)."""
    try:
        return float(os.environ.get("KALSHI_GTD_HORIZON_S", KALSHI_GTD_HORIZON_S_DEFAULT))
    except ValueError:
        return KALSHI_GTD_HORIZON_S_DEFAULT


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
    kickoff: Optional[float] = None            # epoch seconds; the order must not outlive the pre-game book
    expiration_time: Optional[int] = None      # Unix seconds (V2 int64); computed from kickoff / horizon when None
    cancel_order_on_pause: bool = True
    order_group_id: Optional[str] = None

    @property
    def is_resting(self) -> bool:
        return self.time_in_force.lower() not in IOC_TIFS

    def expiry(self, now: Optional[float] = None) -> Optional[int]:
        """The expiry the payload will carry: explicit ``expiration_time``, else
        ``min(kickoff, now + KALSHI_GTD_HORIZON_S)``; ``None`` for IOC/FOK."""
        if not self.is_resting:
            return None
        if self.expiration_time:
            return int(self.expiration_time)
        return order_expiration(self.kickoff, now=now, gtd_horizon_s=gtd_horizon_s())

    def payload(self, now: Optional[float] = None) -> dict[str, Any]:
        return build_order_payload(self.ticker, self.action, self.side, self.count, self.price, time_in_force=self.time_in_force, post_only=self.post_only, client_order_id=self.client_order_id, exchange_index=self.exchange_index, expiration_time=self.expiry(now), cancel_order_on_pause=self.cancel_order_on_pause if self.is_resting else None, order_group_id=self.order_group_id)


class KalshiExecutor:
    """Three safety gates before anything reaches the exchange:

    1. ``confirm=True`` must be passed (the CLI flag ``--confirm``).
    2. The client targets the DEMO environment unless ``KALSHI_ENV=prod``.
    3. On prod, ``ARB_LIVE_TRADING=1`` must also be set.
    """

    def __init__(self, client: Optional[KalshiClient] = None):
        self.client = client or KalshiClient()

    def plan(self, ticker: str, action: str, side: str, count: float, price: float, post_only: bool = False, exchange_index: Optional[int] = None, note: str = "", *, time_in_force: str = "good_till_canceled", kickoff: Optional[float] = None, expiration_time: Optional[int] = None, cancel_order_on_pause: bool = True, order_group_id: Optional[str] = None) -> OrderPlan:
        if not (0 < price < 1):
            raise ValueError("price must be in (0, 1) dollars")
        if count <= 0:
            raise ValueError("count must be positive")
        return OrderPlan(venue="kalshi", ticker=ticker, action=action, side=side, count=count, price=round(price, 4), post_only=post_only, time_in_force=time_in_force, exchange_index=exchange_index, note=note, kickoff=kickoff, expiration_time=expiration_time, cancel_order_on_pause=cancel_order_on_pause, order_group_id=order_group_id)

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

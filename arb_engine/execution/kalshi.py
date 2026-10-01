"""Kalshi order plans and the gated executor.

Every order the engine can send is built here as an :class:`OrderPlan` first, so the dry-run
preview shows the exact V2 payload (including the expiry) that ``confirm=True`` would submit.
"""

from __future__ import annotations

import math
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from ..venues.kalshi import IOC_TIFS, KalshiClient, build_order_payload, order_expiration
from .ledger import env_host_problem

DRY_RUN = "DRY_RUN (pass confirm=True / --confirm to submit)"

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
    """Four safety gates before anything reaches the exchange:

    1. ``confirm=True`` must be passed (the CLI flag ``--confirm``).
    2. The client targets the DEMO environment unless ``KALSHI_ENV=prod``.
    3. The host is a known Kalshi host *of that environment* (``KALSHI_BASE_URL`` can move
       the host without changing ``KALSHI_ENV``; see ``ledger.env_host_problem``).
    4. On prod, ``ARB_LIVE_TRADING=1`` must also be set.
    """

    def __init__(self, client: Optional[KalshiClient] = None, *, ledger_path=None,
                 clock=time.time, transport=None):
        self.client = client or KalshiClient()
        self.ledger_path, self.clock, self._once_transport = ledger_path, clock, transport

    def _mutation_preview(self, operation: str, **fields: Any) -> dict[str, Any]:
        return {"env": self.client.env, "base_url": self.client.base_url, "operation": operation, **fields}

    def gate_problem(self) -> Optional[str]:
        """Why a *confirmed* mutation would be blocked (gates 2-4), or None."""
        problem = env_host_problem(self.client.env, self.client.base_url)
        if problem:
            return problem
        if self.client.env == "prod" and os.environ.get("ARB_LIVE_TRADING") != "1":
            return "KALSHI_ENV=prod requires ARB_LIVE_TRADING=1"
        return None

    def _mutation_allowed(self, preview: dict[str, Any], confirm: bool) -> bool:
        if confirm is not True:
            preview["status"] = DRY_RUN
            return False
        problem = self.gate_problem()
        if problem:
            preview["status"] = f"BLOCKED: {problem}"
            return False
        return True

    def plan(self, ticker: str, action: str, side: str, count: float, price: float, post_only: bool = False, exchange_index: Optional[int] = None, note: str = "", *, time_in_force: str = "good_till_canceled", kickoff: Optional[float] = None, expiration_time: Optional[int] = None, cancel_order_on_pause: bool = True, order_group_id: Optional[str] = None, client_order_id: Optional[str] = None) -> OrderPlan:
        if not str(ticker or "").strip():
            raise ValueError("ticker is required")
        if action not in ("buy", "sell"):
            raise ValueError("action must be buy or sell")
        if side not in ("yes", "no"):
            raise ValueError("side must be yes or no")
        try:
            price, count = float(price), float(count)
        except (TypeError, ValueError):
            raise ValueError("price and count must be numbers") from None
        # NaN fails every comparison and inf passes "> 0": both must be refused explicitly,
        # or a NaN count slips past a notional cap (NaN > cap is False).
        if not math.isfinite(price) or not (0 < price < 1):
            raise ValueError("price must be a finite number in (0, 1) dollars")
        if not math.isfinite(count) or count <= 0:
            raise ValueError("count must be a finite positive number")
        if round(count, 2) != count:
            raise ValueError("count has at most 2 decimals (Kalshi fixed-point)")
        if exchange_index is not None and (isinstance(exchange_index, bool) or int(exchange_index) != exchange_index or exchange_index < 0):
            raise ValueError("exchange_index must be a non-negative integer")
        tif = time_in_force.lower()
        if tif not in {"good_till_canceled", "immediate_or_cancel", "fill_or_kill", "ioc", "fok"}:
            raise ValueError("unsupported time_in_force")
        if post_only and tif in IOC_TIFS:
            raise ValueError("post_only cannot be combined with IOC/FOK")
        plan = OrderPlan(venue="kalshi", ticker=ticker, action=action, side=side, count=count, price=round(price, 4), post_only=post_only, time_in_force=time_in_force, exchange_index=exchange_index, note=note, kickoff=kickoff, expiration_time=expiration_time, cancel_order_on_pause=cancel_order_on_pause, order_group_id=order_group_id)
        if client_order_id:
            plan.client_order_id = str(client_order_id)   # the ledger's id: reconciliation finds the order by it
        return plan

    def execute(self, plan: OrderPlan, confirm: bool = False, *, one_send=False,
                not_after=None) -> dict[str, Any]:
        payload = plan.payload()
        preview = self._mutation_preview("create_order", plan=asdict(plan), payload=payload)
        client_at_decision = (self.client.env, self.client.base_url, self.client.api_key,
                              getattr(self.client, "private_key_path", None)) if one_send else None
        send_path = self.ledger_path
        if one_send and send_path is None:
            from .ledger import default_path
            send_path = default_path(client_at_decision[0])
        if not self._mutation_allowed(preview, confirm):
            return preview
        if one_send:
            now = self.clock()
            if (isinstance(not_after, bool) or not isinstance(not_after, (int, float)) or
                    not math.isfinite(not_after) or isinstance(now, bool) or
                    not isinstance(now, (int, float)) or not math.isfinite(now) or
                    now < 0 or now >= not_after or plan.time_in_force.lower() not in IOC_TIFS):
                preview["status"] = "BLOCKED: single-attempt dispatch requires an unexpired finite IOC deadline"
                return preview
        if self.client.env == "prod" or one_send:
            # A direct call (or the built-in CLI if its ledger plugin failed to
            # load) must not bypass the shared cash ceilings. Claim one send
            # atomically; the normal caller still records its final answer.
            problem = self._production_reservation_problem(plan, path=send_path)
            if problem:
                preview["status"] = f"BLOCKED: {problem}"
                return preview
        if one_send:
            from .kalshi_once import KalshiOnceError, _create_once
            try:
                # Authenticated account reads may block or invoke client callbacks.
                # The claimed reservation must still belong to the same exact
                # client and immutable plan that produced this payload.
                if (client_at_decision != (self.client.env, self.client.base_url, self.client.api_key,
                                          getattr(self.client, "private_key_path", None)) or
                        preview["plan"] != asdict(plan)):
                    raise KalshiOnceError("client or order terms changed during reservation; reconcile the claimed intent")
                preview["response"] = _create_once(self.client, payload, confirm=confirm,
                                                   not_after=not_after, clock=self.clock,
                                                   transport=self._once_transport)
                state = self._once_answer(payload["client_order_id"], client_at_decision, send_path, preview["response"])
                preview["ledger_state"] = state
                if state == "ambiguous":
                    preview["status"] = "UNKNOWN: exchange acceptance not established; reconcile, never resend"
                    return preview
            except Exception:
                # A crash before this write still leaves the permanent send claim
                # and full pending hold. No failure authorizes another attempt.
                self._once_answer(payload["client_order_id"], client_at_decision, send_path, None)
                raise KalshiOnceError("single-attempt submission not verified; reconcile the claimed intent") from None
        else:
            preview["response"] = self.client.create_order(payload)
        preview["status"] = "SUBMITTED"
        return preview

    def _once_answer(self, client_order_id, original, path, response):
        from .ledger import OrderLedger
        led = None
        try:
            # Use the sending ledger, even if this client's environment/key was
            # changed by a callback. Opening this file makes no account read.
            led = OrderLedger(path, original[0],
                              original[1], clock=self.clock)
            row = led.conn.execute("SELECT intent_id FROM intents WHERE client_order_id=?",
                                   (client_order_id,)).fetchone()
            if row is None:
                raise RuntimeError("claimed intent missing")
            if response is None:
                led.ambiguous(row["intent_id"], "single-attempt send/preparation failed; never resend")
                return "ambiguous"
            else:
                return led.accepted(row["intent_id"], response)
        except Exception:
            # Keep the original claim/hold. A subsequent reconciliation may find
            # the order by its durable client id; never invent a refusal here.
            if response is not None:
                raise
        finally:
            if led is not None:
                led.close()

    def _production_reservation_problem(self, plan: OrderPlan, *, path=None) -> Optional[str]:
        from decimal import Decimal
        from .ledger import OrderLedger
        from .shared_limits import LEG_CAP, TOTAL_CAP, exposure

        led = None
        try:
            led = OrderLedger.for_client(self.client, path=path or self.ledger_path, clock=self.clock)
            with led._tx() as connection:
                row = connection.execute("SELECT * FROM intents WHERE client_order_id=?", (plan.client_order_id,)).fetchone()
                if (row is None or row["state"] != "pending" or row["ticker"] != plan.ticker or
                        row["key_fp"] != led.identity.key_fp or row["account_fp"] != led.identity.account_fp or
                        row["action"] != plan.action or row["side"] != plan.side or row["tif"] != plan.time_in_force or
                        Decimal(str(row["count"])) != Decimal(str(plan.count)) or
                        Decimal(row["limit_price"]) != Decimal(str(plan.price))):
                    return "production order needs its matching pending reservation in the shared ledger"
                cost = Decimal(row["max_cost"])
                if not cost.is_finite() or cost <= 0 or cost > LEG_CAP or exposure(connection) > TOTAL_CAP:
                    return "shared production cash ceiling exceeded"
                have = connection.execute("SELECT 1 FROM sqlite_master WHERE name='pm_us_intents'").fetchone()
                if have and connection.execute("SELECT 1 FROM pm_us_intents WHERE state NOT IN ('done','missed') LIMIT 1").fetchone():
                    return "Polymarket US order unresolved; new sends blocked"
                connection.execute("CREATE TABLE IF NOT EXISTS production_sends (client_order_id TEXT PRIMARY KEY)")
                if connection.execute("SELECT 1 FROM production_sends WHERE client_order_id=?", (plan.client_order_id,)).fetchone():
                    return "production intent already attempted; reconcile, never resend"
                connection.execute("INSERT INTO production_sends VALUES (?)", (plan.client_order_id,))
                led._event(connection, led.clock(), row["intent_id"], "production-send-claimed")
        except Exception:
            return "production ledger unavailable or account identity unverified"
        finally:
            if led is not None:
                led.close()
        return None

    def cancel(self, order_id: str, confirm: bool = False, *, market_ticker: Optional[str] = None,
               exchange_index: Optional[int] = None, subaccount: Optional[int] = None) -> dict[str, Any]:
        """Cancel one order through the same gates as submission."""
        if not str(order_id or "").strip():
            raise ValueError("order_id is required")
        preview = self._mutation_preview("cancel_order", order_id=str(order_id), market_ticker=market_ticker,
                                         exchange_index=exchange_index, subaccount=subaccount)
        if not self._mutation_allowed(preview, confirm):
            return preview
        preview["response"] = self.client.cancel_order(str(order_id), market_ticker=market_ticker,
                                                        exchange_index=exchange_index, subaccount=subaccount)
        preview["status"] = "CANCELLED"
        return preview

    def cancel_all(self, confirm: bool = False, subaccount: Optional[int] = None) -> dict[str, Any]:
        """Emergency sweep of resting orders, guarded like every other mutation."""
        preview = self._mutation_preview("cancel_all_orders", subaccount=subaccount)
        if not self._mutation_allowed(preview, confirm):
            return preview
        self.client.cancel_all_orders(subaccount=subaccount)
        preview["status"] = "CANCELLED_ALL"
        return preview

    def execute_legs(self, plans: list[OrderPlan], confirm: bool = False) -> list[dict[str, Any]]:
        """Submit legs sequentially; stop at the first failure so you are never left with
        more unhedged legs than necessary. Cross-venue arbs are still exposed to leg risk on
        the venues we cannot route to."""
        out: list[dict[str, Any]] = []
        for plan in plans:
            res = self.execute(plan, confirm=confirm)
            out.append(res)
            if res.get("status") not in ("SUBMITTED", DRY_RUN):
                break
        return out

"""Pure US paired-order/evidence helpers; this module never sends an order.

Official US Orders/Portfolio overview and Get User Positions, 2026-09-30:
prices are ALWAYS long/YES prices; netPositionDecimal is signed. Pagination is
complete only at explicit eof=true. Deprecated rounded quantities are not proof.
These helpers do not confer pair ownership, permission or reduce-only semantics.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..fees.polymarket import PolymarketUSFees
from ..venues.polymarket_us_trading import API
from .polymarket_us_ioc import MissingEvidence, TERMINAL, USOrderLedger, USOrderPlan, decimal, timestamp


@dataclass(frozen=True)
class USPairOrder(USOrderPlan):
    action: str = "buy"

    def validate(self):
        n, p = super().validate()
        if self.action not in ("buy", "sell"):
            raise ValueError("US pair action must be buy or sell")
        return n, p

    def payload(self):
        result = super().payload()
        result["intent"] = "ORDER_INTENT_" + self.action.upper() + ("_LONG" if self.side == "yes" else "_SHORT")
        result["manualOrderIndicator"] = "MANUAL_ORDER_INDICATOR_AUTOMATIC"
        return result

    def fee_bound(self, now):
        n, p = self.validate()
        # A better sale may move towards .5 just as a better purchase can.
        peak = min(p, Decimal(".5")) if self.action == "buy" else max(p, Decimal(".5"))
        day = datetime.fromtimestamp(timestamp(now), ZoneInfo("America/New_York")).date()
        return PolymarketUSFees.for_date(day).fee(peak, n, "taker")

    def worst_cost(self, now):
        n, p = self.validate()
        # Never use anticipated sale proceeds as cash available to a new order.
        return (n*p if self.action == "buy" else Decimal(0)) + self.fee_bound(now)


@dataclass(frozen=True)
class USOrderEvidence:
    quantity: Decimal
    remaining: Decimal
    cash: Decimal | None
    fees: Decimal | None
    final: bool
    verified: bool


def order_evidence(plan, raw, expected_id, *, final_read=True):
    """Strict exact-order evidence; synchronous/create snapshots are NOT final.

    Missing money remains None. Contradictory identity/terms/limits raise. The
    parent ledger must ALSO enforce monotonic counts/money and sticky conflicts.
    """
    n, limit = plan.validate()
    if not isinstance(expected_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", expected_id):
        raise ValueError("exchange order ID required")
    payload = plan.payload()
    if not isinstance(raw, dict):
        raise MissingEvidence("missing US order")
    if (raw.get("id") != expected_id or any(raw.get(k) != payload[k] for k in ("marketSlug", "intent", "type", "tif")) or
            decimal(raw.get("quantity")) != n or USOrderLedger._money(raw.get("price")) != USOrderLedger._money(payload["price"])):
        raise ValueError("US order identity or immutable terms changed")
    # These optional fields override intent at the API. Never ignore a conflict.
    for field, expected in (("outcomeSide", "OUTCOME_SIDE_" + plan.side.upper()),
                            ("action", "ORDER_ACTION_" + plan.action.upper())):
        if field in raw and raw[field] != expected:
            raise ValueError("US order side/action conflict")
    if raw.get("cumQuantity") is None or raw.get("leavesQuantity") is None:
        raise MissingEvidence("missing US cumulative/remaining quantity")
    qty, left = decimal(raw["cumQuantity"]), decimal(raw["leavesQuantity"])
    if min(qty, left) < 0 or qty+left > n or (raw.get("state") == "ORDER_STATE_FILLED" and qty != n):
        raise ValueError("impossible US quantities")
    cash = Decimal(0) if not qty else None
    if qty and raw.get("avgPx") is not None:
        price = USOrderLedger._money(raw["avgPx"])
        if not Decimal(".01") <= price <= Decimal(".99"):
            raise ValueError("invalid US average long price")
        side_price = price if plan.side == "yes" else 1-price
        if (plan.action == "buy" and side_price > limit) or (plan.action == "sell" and side_price < limit):
            raise ValueError("US fill violated decision limit")
        cash = qty*side_price
    fees = None
    if raw.get("commissionNotionalTotalCollected") is not None:
        fees = USOrderLedger._money(raw["commissionNotionalTotalCollected"])
        if fees < 0 or (not qty and fees != 0):
            raise ValueError("invalid US fee evidence")
    final = final_read is True and raw.get("state") in TERMINAL and left == 0
    return USOrderEvidence(qty, left, cash, fees, final, final and cash is not None and fees is not None)


@dataclass(frozen=True)
class InventoryEvidence:
    key_fp: str
    market_slug: str
    side: str
    net: Decimal
    available: Decimal
    requested_at: float
    observed_at: float
    deadline: float


def read_inventory(client, slug, side, *, clock=time.time, max_pages=20):
    """Read a complete scoped positions map; late/missing evidence raises.

    Quantity availability for a short may be signed or an unsigned magnitude:
    signed net position establishes direction; availability cannot exceed it.
    An empty complete map proves zero, never a missing/failed/truncated map.
    Caller must reserve only THIS pair's independently verified excess, not all
    account inventory. No account-wide reduce-only endpoint is assumed here.
    """
    USOrderPlan(slug, side, 1, Decimal(".5")).validate()
    if (client.base_url != API or not isinstance(client.fingerprint, str) or
            not re.fullmatch(r"[a-f0-9]{64}", client.fingerprint)):
        raise ValueError("US inventory account/host unverified")
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 100:
        raise ValueError("invalid inventory page bound")
    key = client.fingerprint
    requested = timestamp(clock())
    last, cursor, seen, position = requested, None, set(), None
    for _ in range(max_pages):
        page = client.positions_page(slug, cursor=cursor)
        observed = timestamp(clock())
        if observed < last or observed >= requested+6 or client.base_url != API or client.fingerprint != key:
            raise ValueError("inventory expired, clock regressed or account changed")
        last = observed
        if not isinstance(page, dict) or not isinstance(page.get("positions"), dict) or type(page.get("eof")) is not bool:
            raise MissingEvidence("positions map and explicit EOF required")
        rows = page["positions"]
        if set(rows) - {slug}:
            raise ValueError("positions response not scoped to requested market")
        if slug in rows:
            row = rows[slug]
            if not isinstance(row, dict) or (position is not None and position != row):
                raise ValueError("conflicting duplicate position")
            position = dict(row)
        if page["eof"]:
            break
        cursor = page.get("nextCursor")
        if not isinstance(cursor, str) or not cursor or len(cursor) > 4096 or cursor in seen:
            raise MissingEvidence("unfinished or cycling positions pagination")
        seen.add(cursor)
    else:
        raise MissingEvidence("positions pagination limit reached")
    if position is None:
        net = available = Decimal(0)
    else:
        metadata = position.get("marketMetadata")
        if metadata is not None and (not isinstance(metadata, dict) or metadata.get("slug") != slug):
            raise ValueError("position market metadata mismatch")
        if position.get("expired") is not False:
            raise MissingEvidence("position expiry unknown or expired")
        if position.get("netPositionDecimal") is None or position.get("qtyAvailableDecimal") is None:
            raise MissingEvidence("unrounded net/available quantities required")
        net, available = decimal(position["netPositionDecimal"]), decimal(position["qtyAvailableDecimal"])
        if (side == "yes" and min(net, available) < 0) or (side == "no" and net > 0):
            raise ValueError("position belongs to opposite side")
        available = abs(available)
        if available > abs(net):
            raise ValueError("available position exceeds net inventory")
    return InventoryEvidence(key, slug, side, net, available, requested, observed, requested+6)

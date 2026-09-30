"""Ledgered US NFL IOC purchases; never an automatic two-leg arb runner.

Unknown sends are not retried. Final US REST cumulative quantities/long average
price/commission are authoritative accounting evidence; absent fees are unknown.
Known inventory remains charged. No documented client idempotency key or
order-scoped fills listing exists, so a lost exchange ID requires investigation.
"""
from __future__ import annotations

import math
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from ..fees.polymarket import PolymarketUSFees
from ..venues.polymarket_us_trading import API, gate_problem
from .ledger import LedgerError, OrderLedger, default_path
from .shared_limits import LEG_CAP, TOTAL_CAP, exposure, problem

ZERO = Decimal("0")
TERMINAL = {"ORDER_STATE_FILLED", "ORDER_STATE_CANCELED", "ORDER_STATE_REJECTED", "ORDER_STATE_EXPIRED"}


class MissingEvidence(ValueError):
    """Missing is not zero or proof of a contradiction."""


def decimal(value):
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError()
        return result
    except (ValueError, InvalidOperation):
        raise ValueError("missing or nonfinite numeric evidence") from None


def timestamp(value):
    if isinstance(value, bool):
        raise ValueError("invalid timestamp")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid timestamp")
    return value


def fee_bound(limit, count, now):
    # Better fills can have higher fees than the limit. Max over [0, limit]
    # peaks at min(limit, .5); the US cumulative fee cap bounds split fills.
    date = datetime.fromtimestamp(timestamp(now), ZoneInfo("America/New_York")).date()
    return PolymarketUSFees.for_date(date).fee(min(decimal(limit), Decimal("0.5")), decimal(count), "taker")


@dataclass(frozen=True)
class USOrderPlan:
    market_slug: str
    side: str
    count: int
    limit_price: Decimal
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def validate(self):
        if not isinstance(self.market_slug, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", self.market_slug):
            raise ValueError("invalid US market slug")
        if self.side not in ("yes", "no"):
            raise ValueError("side must be yes or no")
        n, p = decimal(self.count), decimal(self.limit_price)
        if n <= 0 or n != n.to_integral_value() or n > 10000:
            raise ValueError("quantity must be 1..10000 whole contracts")
        if not Decimal("0.01") <= p <= Decimal("0.99"):
            raise ValueError("limit must be .01..99 dollars")
        if not isinstance(self.request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.request_id):
            raise ValueError("invalid local request id")
        return n, p

    def payload(self):
        n, p = self.validate()
        return {"marketSlug": self.market_slug,
                "intent": "ORDER_INTENT_BUY_LONG" if self.side == "yes" else "ORDER_INTENT_BUY_SHORT",
                "type": "ORDER_TYPE_LIMIT", "price": {"value": format(p if self.side == "yes" else 1-p, "f"), "currency": "USD"},
                "quantity": int(n), "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", "participateDontInitiate": False,
                "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_MANUAL", "synchronousExecution": True, "maxBlockTime": "5"}

    def worst_cost(self, now):
        n, p = self.validate()
        return n*p + fee_bound(p, n, now)


class USOrderLedger:
    """Separate US tables, but the production Kalshi database and transaction lock."""

    def __init__(self, path=None, *, clock=time.time):
        self.clock = clock
        self.store = OrderLedger(path or default_path("prod"), "prod", "https://external-api.kalshi.com/trade-api/v2", clock=clock)
        with self.store._tx() as c:
            c.execute("CREATE TABLE IF NOT EXISTS pm_us_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            c.execute("""CREATE TABLE IF NOT EXISTS pm_us_intents (
                intent_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, key_fp TEXT NOT NULL,
                slug TEXT NOT NULL, side TEXT NOT NULL, count TEXT NOT NULL, limit_price TEXT NOT NULL,
                created_ts REAL NOT NULL, state TEXT NOT NULL, order_id TEXT UNIQUE,
                max_cost TEXT NOT NULL, charge TEXT NOT NULL, fill_seen TEXT NOT NULL DEFAULT '0',
                fill_cost TEXT, fees TEXT, cost_seen TEXT NOT NULL DEFAULT '0', fee_seen TEXT NOT NULL DEFAULT '0',
                send_started INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '')""")
            columns = {r[1] for r in c.execute("PRAGMA table_info(pm_us_intents)")}
            for name in ("cost_seen", "fee_seen"):
                if name not in columns:
                    c.execute(f"ALTER TABLE pm_us_intents ADD COLUMN {name} TEXT NOT NULL DEFAULT '0'")
            if "send_started" not in columns:
                c.execute("ALTER TABLE pm_us_intents ADD COLUMN send_started INTEGER NOT NULL DEFAULT 0")
            c.execute("""CREATE TABLE IF NOT EXISTS pm_us_events (
                id INTEGER PRIMARY KEY, intent_id TEXT, ts REAL NOT NULL, kind TEXT NOT NULL, reason TEXT NOT NULL)""")

    def close(self):
        self.store.close()

    def _event(self, c, iid, kind, reason=""):
        c.execute("INSERT INTO pm_us_events(intent_id,ts,kind,reason) VALUES (?,?,?,?)",
                  (iid, timestamp(self.clock()), kind, reason))

    @staticmethod
    def _bind(c, fingerprint):
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise LedgerError("invalid US key fingerprint")
        existing = c.execute("SELECT value FROM pm_us_meta WHERE key='key_fp'").fetchone()
        if existing is not None and existing["value"] != fingerprint:
            raise LedgerError("US ledger belongs to another API key; rotation requires operator reconciliation")
        if existing is None:
            c.execute("INSERT INTO pm_us_meta VALUES ('key_fp',?)", (fingerprint,))

    def reserve(self, plan, fingerprint):
        now = timestamp(self.clock())
        cost = plan.worst_cost(now)
        with self.store._tx() as c:
            self._bind(c, fingerprint)
            if c.execute("SELECT 1 FROM pm_us_intents WHERE request_id=?", (plan.request_id,)).fetchone():
                raise LedgerError("duplicate local request; never resubmit an existing intent")
            if c.execute("SELECT 1 FROM intents WHERE state IN ('pending','ambiguous','accepted') OR fill_state='contradicted' LIMIT 1").fetchone():
                raise LedgerError("Kalshi order unresolved; reconcile before new US exposure")
            why = problem(c, cost)
            if why:
                raise LedgerError(why)
            iid = uuid.uuid4().hex
            c.execute("""INSERT INTO pm_us_intents(intent_id,request_id,key_fp,slug,side,count,limit_price,created_ts,state,max_cost,charge)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                      (iid, plan.request_id, fingerprint, plan.market_slug, plan.side, str(plan.count), str(plan.limit_price), now,
                       "pending", str(cost), str(cost)))
            self._event(c, iid, "reserved")
        return iid

    def get(self, iid):
        with self.store._lock:
            row = self.store.conn.execute("SELECT * FROM pm_us_intents WHERE intent_id=?", (iid,)).fetchone()
            if row is None:
                raise LedgerError("unknown US intent")
            return dict(row)

    def status(self):
        with self.store._lock:
            total = exposure(self.store.conn)
            rows = [dict(r) for r in self.store.conn.execute("SELECT * FROM pm_us_intents ORDER BY created_ts,intent_id")]
        return {"shared_exposure": str(total), "leg_cap": str(LEG_CAP), "total_cap": str(TOTAL_CAP), "intents": rows,
                "inventory_release": "none: filled cash remains charged until explicit future settlement/exit support"}

    def unknown(self, iid, reason):
        with self.store._tx() as c:
            row = c.execute("SELECT * FROM pm_us_intents WHERE intent_id=?", (iid,)).fetchone()
            if row is not None and row["state"] not in ("done", "missed", "contradicted"):
                c.execute("UPDATE pm_us_intents SET state='unknown', reason=? WHERE intent_id=?", (reason, iid))
            self._event(c, iid, "unknown", reason)

    def claim_send(self, iid, fingerprint):
        with self.store._tx() as c:
            row = c.execute("SELECT * FROM pm_us_intents WHERE intent_id=?", (iid,)).fetchone()
            if (row is None or row["key_fp"] != fingerprint or row["state"] != "pending" or row["send_started"] or
                    decimal(row["max_cost"]) > LEG_CAP or exposure(c) > TOTAL_CAP):
                raise LedgerError("send is not a fresh account-bound reservation within the shared cap")
            c.execute("UPDATE pm_us_intents SET send_started=1 WHERE intent_id=?", (iid,))
            self._event(c, iid, "send-claimed")

    def _abandon_unsent(self, iid):
        # Only a local pre-send refusal. Never a public release of an unknown or
        # attempted order, including a crash just after claim_send.
        with self.store._tx() as c:
            changed = c.execute("UPDATE pm_us_intents SET state='missed',charge='0',reason='book expired before send' "
                                "WHERE intent_id=? AND state='pending' AND send_started=0 AND order_id IS NULL AND fill_seen='0'", (iid,)).rowcount
            if changed != 1:
                raise LedgerError("cannot abandon an attempted or observed order")
            self._event(c, iid, "never-sent", "book expired before send")

    def accepted(self, iid, response):
        oid = response.get("id") if isinstance(response, dict) else None
        if not isinstance(oid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", oid):
            self.unknown(iid, "create answer missing exchange ID; operator investigation required")
            return
        with self.store._tx() as c:
            row = c.execute("SELECT * FROM pm_us_intents WHERE intent_id=?", (iid,)).fetchone()
            if row is None or row["state"] != "pending":
                return  # compare-and-set; late answers never undo reconciliation
            c.execute("UPDATE pm_us_intents SET state='open',order_id=? WHERE intent_id=?", (oid, iid))
            self._event(c, iid, "accepted")
        executions = response.get("executions", [])
        if not isinstance(executions, list):
            self.unknown(iid, "malformed synchronous execution evidence")
            return
        # Execution snapshots are a batch, not necessarily chronological. Validate
        # every row's scope, then keep its maximum cumulative count atomically.
        candidates = []
        for execution in executions:
            if not isinstance(execution, dict) or not isinstance(execution.get("order"), dict):
                self.unknown(iid, "malformed synchronous execution evidence")
                return
            order = execution["order"]
            try:
                qty, _ = self._scope(self.get(iid), order)
            except (ValueError, TypeError):
                self.observe(iid, order, final_read=False)
                return
            candidates.append(order)
        # Scope/count sorting avoids treating name/array order as receive order.
        # Keep all monetary lower bounds, not just the final count snapshot.
        def sort_key(order):
            qty = decimal(order["cumQuantity"])
            try:
                px = self._money(order.get("avgPx"))
                cost = qty * (px if self.get(iid)["side"] == "yes" else 1-px)
            except ValueError:
                cost = ZERO
            try:
                fees = self._money(order.get("commissionNotionalTotalCollected"))
            except ValueError:
                fees = ZERO
            return qty, cost, fees
        for candidate in sorted(candidates, key=sort_key):
            self.observe(iid, candidate, final_read=False)

    def observe(self, iid, order, *, final_read=True):
        with self.store._tx() as c:
            row = c.execute("SELECT * FROM pm_us_intents WHERE intent_id=?", (iid,)).fetchone()
            if row is None:
                raise LedgerError("unknown US intent")
            try:
                expected = "ORDER_INTENT_BUY_LONG" if row["side"] == "yes" else "ORDER_INTENT_BUY_SHORT"
                if (isinstance(order, dict) and order.get("id") == row["order_id"] and order.get("marketSlug") == row["slug"] and
                        order.get("intent") == expected and order.get("cumQuantity") is not None):
                    # The count remains evidence even if some other field is
                    # missing. A later zero row must not erase this lower bound.
                    lower_bound = decimal(order["cumQuantity"])
                    if not decimal(row["fill_seen"]) <= lower_bound <= decimal(row["count"]):
                        raise ValueError("invalid or regressing cumulative fill evidence")
                    c.execute("UPDATE pm_us_intents SET fill_seen=? WHERE intent_id=?", (str(lower_bound), iid))
                qty, left = self._scope(dict(row), order)
                if qty < decimal(row["fill_seen"]):
                    raise ValueError("cumulative fill count regressed")
                if row["state"] in ("done", "missed") and (qty != decimal(row["fill_seen"]) or
                        order.get("state") not in TERMINAL or left != 0):
                    raise ValueError("final order changed after reconciliation")
                c.execute("UPDATE pm_us_intents SET fill_seen=? WHERE intent_id=?", (str(qty), iid))
                if row["state"] == "contradicted":
                    return False  # sticky, even if a later row is convenient
                # Every cumulative money field is evidence, including the create
                # answer. A final read may not silently book less than it showed.
                seen_cost, seen_fee = decimal(row["cost_seen"]), decimal(row["fee_seen"])
                if order.get("commissionNotionalTotalCollected") is not None:
                    seen_fee = self._money(order["commissionNotionalTotalCollected"])
                    if seen_fee < decimal(row["fee_seen"]) or seen_fee < 0:
                        raise ValueError("cumulative commission regressed")
                if qty and order.get("avgPx") is not None:
                    px = self._money(order["avgPx"])
                    if not Decimal("0.01") <= px <= Decimal("0.99"):
                        raise ValueError("invalid average long price")
                    seen_cost = qty * (px if row["side"] == "yes" else 1-px)
                    if seen_cost < decimal(row["cost_seen"]):
                        raise ValueError("cumulative cost regressed")
                c.execute("UPDATE pm_us_intents SET cost_seen=?,fee_seen=? WHERE intent_id=?", (str(seen_cost), str(seen_fee), iid))
                if seen_cost + seen_fee > decimal(row["max_cost"]):
                    c.execute("UPDATE pm_us_intents SET charge=? WHERE intent_id=?", (str(seen_cost+seen_fee), iid))
                    raise ValueError("reported money exceeded reservation")
                if seen_cost > qty * decimal(row["limit_price"]):
                    raise ValueError("fill exceeded decision limit")
                if order.get("state") == "ORDER_STATE_FILLED" and qty != decimal(row["count"]):
                    raise ValueError("FILLED order has a partial count")
                if not final_read or order.get("state") not in TERMINAL or left != 0:
                    self._event(c, iid, "observation", "nonfinal/create evidence; whole reservation retained")
                    return False
                fees = self._money(order.get("commissionNotionalTotalCollected"))
                if fees < 0:
                    raise ValueError("negative commission cannot finance new exposure")
                price = self._money(order.get("avgPx")) if qty else ZERO
                if qty and not Decimal("0.01") <= price <= Decimal("0.99"):
                    raise ValueError("invalid average long price")
                side_price = price if row["side"] == "yes" or not qty else 1-price
                cost, charge = qty*side_price, qty*side_price+fees
                if charge > decimal(row["max_cost"]):
                    c.execute("UPDATE pm_us_intents SET charge=? WHERE intent_id=?", (str(charge), iid))
                if qty and side_price > decimal(row["limit_price"]):
                    raise ValueError("fill exceeded decision limit")
                if charge > decimal(row["max_cost"]) or (not qty and charge):
                    raise ValueError("reported cost exceeded reservation")
                if row["state"] in ("done", "missed") and charge < decimal(row["charge"]):
                    raise ValueError("final cumulative cost/fees regressed")
                c.execute("UPDATE pm_us_intents SET state=?,charge=?,fill_cost=?,fees=?,reason='' WHERE intent_id=?",
                          ("done" if qty else "missed", str(charge), str(cost), str(fees), iid))
                self._event(c, iid, "final", "inventory retained" if qty else "zero-fill confirmed")
                return True
            except (ValueError, TypeError, AttributeError, InvalidOperation) as error:
                incomplete = isinstance(error, MissingEvidence) and row["state"] not in ("done", "missed", "contradicted")
                state = "open" if incomplete else "contradicted"
                current = c.execute("SELECT charge FROM pm_us_intents WHERE intent_id=?", (iid,)).fetchone()
                charge = max(decimal(row["max_cost"]), decimal(current["charge"]))
                c.execute("UPDATE pm_us_intents SET state=?,charge=?,reason=? WHERE intent_id=?",
                          (state, str(charge), "incomplete or contradictory order evidence; full reservation retained", iid))
                self._event(c, iid, state)
                return False

    @staticmethod
    def _money(value):
        if value is None:
            raise MissingEvidence("missing money field")
        if not isinstance(value, dict) or value.get("currency") != "USD":
            raise ValueError("wrong monetary currency")
        if value.get("value") is None:
            raise MissingEvidence("missing money value")
        return decimal(value["value"])

    @staticmethod
    def _scope(row, order):
        if not isinstance(order, dict):
            raise MissingEvidence("missing order")
        intent = "ORDER_INTENT_BUY_LONG" if row["side"] == "yes" else "ORDER_INTENT_BUY_SHORT"
        if order.get("id") != row["order_id"] or order.get("marketSlug") != row["slug"] or order.get("intent") != intent:
            raise ValueError("wrong order identity")
        if order.get("type") != "ORDER_TYPE_LIMIT" or order.get("tif") != "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL":
            raise ValueError("order type changed")
        price = USOrderLedger._money(order.get("price"))
        limit = decimal(row["limit_price"]) if row["side"] == "yes" else 1-decimal(row["limit_price"])
        if price != limit or decimal(order.get("quantity")) != decimal(row["count"]):
            raise ValueError("order terms changed")
        if order.get("cumQuantity") is None or order.get("leavesQuantity") is None:
            raise MissingEvidence("missing cumulative or remaining quantity")
        qty, left = decimal(order["cumQuantity"]), decimal(order["leavesQuantity"])
        if min(qty, left) < 0 or qty+left > decimal(row["count"]):
            raise ValueError("impossible fill/remaining quantities")
        return qty, left


def current_quote(plan):
    from ..venues.polymarket_us import PolymarketUSAdapter
    adapter = PolymarketUSAdapter(with_books=True)
    snapshot = adapter.fetch("nfl")
    matches = [q for q in snapshot.quotes if q.meta.get("market_slug") == plan.market_slug and q.meta.get("side") == plan.side]
    if len(matches) != 1:
        raise ValueError("market must uniquely match active US NFL full-game moneyline")
    q = matches[0]
    adapter.attach_books_for([q])
    return q, snapshot.events[q.event_key]


class PolymarketUSExecutor:
    def __init__(self, client=None, *, ledger=None, clock=time.time, quote_provider=None):
        self.client, self.ledger, self.clock = client, ledger, clock
        self.quote_provider = quote_provider or current_quote

    def _check_gate(self):
        if self.client is None or self.client.base_url != API:
            raise LedgerError("US authenticated client on exact production host required")
        if gate_problem():
            raise LedgerError(gate_problem())

    def _preflight(self, plan):
        self._check_gate()
        q, info = self.quote_provider(plan)
        now = timestamp(self.clock())
        observed, requested = timestamp(q.meta.get("obs_ts")), timestamp(q.meta.get("req_ts"))
        if (q.venue != "polymarket_us" or q.meta.get("market_slug") != plan.market_slug or q.meta.get("side") != plan.side or
                q.book_id != "polymarket_us" or q.meta.get("refreshed") is not True or q.meta.get("approx_time") is not False or
                not requested <= observed <= now or now-observed > 6):
            raise ValueError("invalid US receipt provenance")
        quote_time = getattr(q, "quote_time", q.meta.get("quote_time"))
        if quote_time is not None and not 0 <= observed-timestamp(quote_time) <= 10:
            raise ValueError("stale exchange book")
        if info.sport != "nfl" or info.market_type != "moneyline" or info.in_play is True or info.start_time is None or info.start_time.timestamp() <= now:
            raise ValueError("only pre-game full-game NFL moneylines supported")
        n, p = plan.validate()
        tick, minimum = decimal(q.meta.get("tick_size")), decimal(q.meta.get("min_order_size"))
        if tick <= 0 or minimum <= 0 or (p if plan.side == "yes" else 1-p) % tick or n % minimum:
            raise ValueError("limit/quantity off exchange grid")
        ask, bid, depth = decimal(q.ask), decimal(q.bid), decimal(q.ask_size)
        if not 0 < bid <= ask < 1 or ask > p or depth <= 0:
            raise ValueError("no executable two-sided book at decision limit")
        balances = self.client.balances().get("balances")
        usd = [r for r in balances if isinstance(r, dict) and r.get("currency") == "USD"] if isinstance(balances, list) else []
        now = timestamp(self.clock())
        if len(usd) != 1 or decimal(usd[0].get("buyingPower")) < plan.worst_cost(now):
            raise ValueError("unknown/insufficient USD buying power")
        if not observed <= now <= observed+6 or info.start_time.timestamp() <= now:
            raise ValueError("book expired during account preflight")
        return min(observed+6, info.start_time.timestamp())

    def execute(self, plan, *, confirm=False):
        payload, cost = plan.payload(), plan.worst_cost(timestamp(self.clock()))
        preview = {"venue": "polymarket_us", "payload": payload, "request_id": plan.request_id,
                   "max_cost_fees_included": str(cost), "leg_cap": str(LEG_CAP), "total_cap": str(TOTAL_CAP)}
        if cost > LEG_CAP:
            return {**preview, "status": "BLOCKED", "reason": "$25 per-leg cap exceeded including fees"}
        if confirm is not True:
            return {**preview, "status": "DRY_RUN", "note": "no account read, ledger write or order sent"}
        try:
            deadline = self._preflight(plan)
            if self.ledger is None:
                self.ledger = USOrderLedger(clock=self.clock)
            iid = self.ledger.reserve(plan, self.client.fingerprint)
        except (LedgerError, ValueError, TypeError, RuntimeError) as error:
            # Never echo arbitrary exceptions from an injected transport/provider.
            return {**preview, "status": "BLOCKED", "reason": "gate, book, balance or durable reservation refused; nothing sent"}
        try:
            self._check_gate()
            if timestamp(self.clock()) >= deadline:
                self.ledger._abandon_unsent(iid)
                return {**preview, "status": "BLOCKED", "intent_id": iid, "reason": "book expired while reserving; no order sent and cash released"}
            self.ledger.claim_send(iid, self.client.fingerprint)
            # BEGIN IMMEDIATE may itself wait for another process. Never let a
            # successful send claim make an expired decision executable.
            if timestamp(self.clock()) >= deadline:
                raise LedgerError("book expired while claiming send; reservation retained")
            response = self.client._create(payload, confirm=True)
            self.ledger.accepted(iid, response)
            return self.recover(iid, confirm=True)
        except Exception:
            self.ledger.unknown(iid, "submission/recording failed; never retry this intent")
            return {**preview, "status": "UNKNOWN", "intent_id": iid, "reason": "full reservation retained; reconcile before new exposure"}

    def recover(self, iid, *, confirm=False):
        if self.ledger is None or self.client is None:
            raise LedgerError("client and ledger required for recovery")
        row = self.ledger.get(iid)
        if row["key_fp"] != self.client.fingerprint or self.client.base_url != API:
            raise LedgerError("recovery client does not own this intent")
        if not row["order_id"]:
            return {"status": "UNKNOWN", "intent_id": iid, "reason": "no exchange ID: no automatic resend, release or activity matching"}
        try:
            order = self.client.order(row["order_id"]).get("order")
            final = self.ledger.observe(iid, order)
            current = self.ledger.get(iid)
            if not final and current["state"] != "contradicted" and isinstance(order, dict):
                _, left = self.ledger._scope(current, order)
                if left > 0 and confirm is True:
                    self._check_gate()
                    self.client._cancel(row["order_id"], row["slug"], confirm=True)
                    self.ledger.observe(iid, self.client.order(row["order_id"]).get("order"))
        except Exception:
            self.ledger.unknown(iid, "recovery read/cancel failed; full reservation retained")
        current = self.ledger.get(iid)
        return {"status": current["state"].upper(), "intent_id": iid, "order_id": current["order_id"],
                "filled": current["fill_seen"], "charge": current["charge"], "reason": current["reason"],
                "remainder": "cancelled only when final exchange row confirms zero leaves", "inventory": "held, not automatically hedged"}

"""KalshiClient hosts and fallback, V2 order payloads, batched cancel, the documented read
paths, the self-match guard and ``cancel_all`` on both brokers.

Fixture shapes under ``tests/fixtures/kalshi_orders/`` are **assumed** from the OpenAPI
schemas on docs.kalshi.com (see its README); ``scripts/kalshi_demo_check.py --record``
replaces them with verified responses.

The fake HTTP layer here is a *routed* one (``METHOD path-fragment`` -> payload) on purpose:
P01's ``tests.helpers.SequencedFakeHttp`` is a per-call script with a different contract, so
this module never imports it under that name.
"""

from __future__ import annotations

import json
import os
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest import mock

from arb_engine.execution.kalshi import KalshiExecutor, OrderPlan
from arb_engine.strategy.broker import SweepIncomplete, KalshiBroker, PaperBroker, RestingOrder, SelfMatchGuard, SelfMatchRefused
from arb_engine.venues import kalshi as kmod
from arb_engine.venues.http import HttpError
from arb_engine.venues.kalshi import BATCH_CANCEL_MAX, ENV_REST_BASE, LEGACY_REST_BASE, KalshiClient, batch_cancel_reduced, build_order_payload, order_expiration, order_side_price
from tests.helpers import load


class _RoutedFakeHttp:
    """Routes ``METHOD substring`` -> a payload, a callable, an exception to raise, or a list
    consumed one element per call (the last element repeats). Records every call as
    ``(method, url, json_body)``. Deliberately not ``tests.helpers.SequencedFakeHttp``."""

    transport = "fake"

    def __init__(self, routes: dict[str, object]):
        self.routes = dict(routes)
        self.calls: list[tuple[str, str, object]] = []

    def _match(self, method: str, url: str, body: object) -> object:
        self.calls.append((method, url, body))
        for needle, payload in self.routes.items():
            m, _, frag = needle.partition(" ")
            if frag and m == method and frag in url:
                if isinstance(payload, list):
                    payload = payload.pop(0) if len(payload) > 1 else payload[0]
                if isinstance(payload, BaseException):
                    raise payload
                if callable(payload):
                    import inspect

                    return payload(body) if inspect.signature(payload).parameters else payload()
                return payload
        raise AssertionError(f"unexpected {method} {url}")

    def request(self, method: str, url: str, params=None, json_body=None, headers=None, raw=False):
        if params:
            url = url + "?" + "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        return self._match(method.upper(), url, json_body)

    def get(self, url, params=None, headers=None, raw=False):
        return self.request("GET", url, params=params, headers=headers, raw=raw)

    def post(self, url, json_body=None, headers=None):
        return self.request("POST", url, json_body=json_body, headers=headers)

    def delete(self, url, headers=None):
        return self.request("DELETE", url, headers=headers)


def _client(routes: dict[str, object], env: str = "demo", **kw) -> KalshiClient:
    c = KalshiClient(env=env, api_key="k", private_key_path="/x.pem", http=_RoutedFakeHttp(routes), **kw)
    c._auth_headers = lambda m, p: {"KALSHI-ACCESS-KEY": "k"}  # signing is covered by test_kalshi_auth
    return c


def _fx(name: str) -> dict:
    return load(f"kalshi_orders/{name}.json")


def _ledger():
    """A throw-away order ledger (KalshiBroker records every order in one)."""
    import tempfile

    from arb_engine.execution.ledger import OrderLedger

    return OrderLedger(os.path.join(tempfile.mkdtemp(prefix="arb_test_"), "ledger.sqlite3"), "demo", "https://external-api.demo.kalshi.co/trade-api/v2")


class _OneX:
    """Fee multipliers: every series 1x (these tests are about order plumbing, not fees)."""

    def resolve(self, ticker, stated=None):
        from decimal import Decimal

        return Decimal(1), None


def _kb(client, **kw):
    return KalshiBroker(client, confirm=True, ledger=_ledger(), fee_multipliers=_OneX(), **kw)


class HostTests(unittest.TestCase):
    def test_default_hosts_are_external_api_with_legacy_fallback(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KALSHI_BASE_URL", None)
            prod, demo = KalshiClient(env="prod"), KalshiClient(env="demo")
        self.assertEqual(prod.base_url, "https://external-api.kalshi.com/trade-api/v2")
        self.assertEqual(demo.base_url, "https://external-api.demo.kalshi.co/trade-api/v2")
        self.assertEqual(prod.legacy_base_url, "https://api.elections.kalshi.com/trade-api/v2")
        self.assertEqual(demo.legacy_base_url, "https://demo-api.kalshi.co/trade-api/v2")
        self.assertEqual(set(ENV_REST_BASE), set(LEGACY_REST_BASE))

    def test_explicit_base_url_disables_fallback(self):
        with mock.patch.dict(os.environ, {"KALSHI_BASE_URL": "https://proxy.local/trade-api/v2/"}):
            c = KalshiClient(env="prod")
        self.assertEqual(c.base_url, "https://proxy.local/trade-api/v2")
        self.assertIsNone(c.legacy_base_url)
        c2 = KalshiClient(env="prod", base_url="https://other.local/trade-api/v2")
        self.assertIsNone(c2.legacy_base_url)

    def test_connection_error_falls_back_to_legacy_host_once(self):
        c = _client({"GET external-api.demo.kalshi.co/trade-api/v2/exchange/status": urllib.error.URLError("dns"), "GET demo-api.kalshi.co/trade-api/v2/exchange/status": {"exchange_active": True}})
        self.assertEqual(c.exchange_status(), {"exchange_active": True})
        self.assertTrue(c.fell_back)
        self.assertEqual(c.base_url, LEGACY_REST_BASE["demo"])
        self.assertIsNone(c.legacy_base_url)
        # The session stays on the legacy host: no second probe of the new one.
        c.exchange_status()
        hosts = [u.split("/")[2] for _, u, _ in c.http.calls]
        self.assertEqual(hosts, ["external-api.demo.kalshi.co", "demo-api.kalshi.co", "demo-api.kalshi.co"])

    def test_http_status_never_changes_hosts(self):
        c = _client({"GET external-api.demo.kalshi.co/trade-api/v2/exchange/status": HttpError(503, "u", "paused")})
        with self.assertRaises(HttpError):
            c.exchange_status()
        self.assertFalse(c.fell_back)
        self.assertEqual(len(c.http.calls), 1)

    def test_curl_transport_failure_counts_as_connection_error(self):
        self.assertTrue(KalshiClient._is_connection_error(HttpError(0, "u", "curl: (6) could not resolve host")))
        self.assertFalse(KalshiClient._is_connection_error(HttpError(401, "u", "")))
        self.assertTrue(KalshiClient._is_connection_error(TimeoutError()))

    def test_rate_limit_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KALSHI_RATE_LIMIT", None)
            c = KalshiClient(env="prod")
        self.assertEqual(c.http.limiter.rate, 15.0)


class OrderPayloadTests(unittest.TestCase):
    def test_expiration_and_pause_flags_on_resting_orders(self):
        """``CreateOrderV2Request.expiration_time`` is ``integer/int64`` Unix seconds; a string
        fails the gateway's validation, so the payload must carry a plain int."""
        p = build_order_payload("T", "buy", "yes", 10, 0.52, expiration_time=1_790_000_000, cancel_order_on_pause=True, order_group_id="g1")
        self.assertEqual((p["side"], p["price"], p["expiration_time"], p["cancel_order_on_pause"], p["order_group_id"]), ("bid", "0.5200", 1_790_000_000, True, "g1"))
        self.assertIs(type(p["expiration_time"]), int)
        self.assertEqual(json.loads(json.dumps(p))["expiration_time"], 1_790_000_000)
        # floats and tz-aware datetimes are normalised to the same int
        self.assertEqual(build_order_payload("T", "buy", "yes", 1, 0.5, expiration_time=1_790_000_000.7)["expiration_time"], 1_790_000_000)
        self.assertEqual(build_order_payload("T", "buy", "yes", 1, 0.5, expiration_time=datetime(2026, 9, 20, 17, 0, tzinfo=timezone.utc))["expiration_time"], 1_789_923_600)

    def test_ioc_omits_expiration(self):
        for tif in ("immediate_or_cancel", "fill_or_kill"):
            p = build_order_payload("T", "buy", "no", 1, 0.30, time_in_force=tif, expiration_time=1_790_000_000)
            self.assertNotIn("expiration_time", p)
            self.assertEqual(p["time_in_force"], tif)

    def test_legacy_callers_unchanged(self):
        """Every new kwarg is optional: the P09-owned payload tests see the same dict."""
        self.assertEqual(set(build_order_payload("T", "sell", "no", 2.5, 0.30, post_only=True, exchange_index=3)), {"ticker", "side", "count", "price", "time_in_force", "self_trade_prevention_type", "post_only", "exchange_index"})

    def test_order_expiration_is_min_of_kickoff_and_horizon(self):
        now = 1_800_000_000.0
        kick = datetime.fromtimestamp(now + 7200, tz=timezone.utc)
        self.assertEqual(order_expiration(kick, now=now, gtd_horizon_s=3600), 1_800_003_600)  # horizon binds
        self.assertEqual(order_expiration(now + 600, now=now, gtd_horizon_s=3600), 1_800_000_600)  # kickoff binds
        self.assertEqual(order_expiration(None, now=now, gtd_horizon_s=60), 1_800_000_060)
        self.assertIs(type(order_expiration(None, now=now, gtd_horizon_s=60)), int)
        self.assertIsNone(order_expiration(None, now=now, gtd_horizon_s=0))
        self.assertIsNone(order_expiration(None, now=now, gtd_horizon_s=None))
        # A kickoff in the past never produces an already-expired stamp.
        self.assertEqual(order_expiration(now - 100, now=now, gtd_horizon_s=None), 1_800_000_001)

    def test_order_plan_expiry_uses_horizon_setting(self):
        with mock.patch.dict(os.environ, {"KALSHI_GTD_HORIZON_S": "120"}):
            plan = OrderPlan(venue="kalshi", ticker="T", action="buy", side="yes", count=1, price=0.5, kickoff=1_800_000_000.0 + 3600)
            self.assertEqual(plan.payload(now=1_800_000_000.0)["expiration_time"], 1_800_000_120)
            ioc = OrderPlan(venue="kalshi", ticker="T", action="buy", side="yes", count=1, price=0.5, time_in_force="immediate_or_cancel")
            p = ioc.payload(now=1_800_000_000.0)
            self.assertNotIn("expiration_time", p)
            self.assertNotIn("cancel_order_on_pause", p)
        ex = KalshiExecutor(KalshiClient(env="demo"))
        plan = ex.plan("T", "buy", "yes", 1, 0.5, expiration_time=1_789_923_600, kickoff=1.0)
        self.assertEqual(plan.payload()["expiration_time"], 1_789_923_600)  # explicit wins


class ClientOrderPathTests(unittest.TestCase):
    def test_batched_cancel_sends_documented_body_in_chunks(self):
        """``BatchCancelOrdersV2Request`` is ``{"orders": [{"order_id", exchange_index?,
        market_ticker?, subaccount?}]}`` — not a bare id list."""
        c = _client({"DELETE /portfolio/events/orders/batched": _fx("cancel_batched")})
        ids = [f"id-{i}" for i in range(BATCH_CANCEL_MAX + 3)]
        res = c.cancel_orders_batched(ids)
        self.assertEqual(len(res), 2)
        self.assertEqual([m for m, _, _ in c.http.calls], ["DELETE", "DELETE"])
        self.assertEqual(c.http.calls[0][2], {"orders": [{"order_id": i} for i in ids[:BATCH_CANCEL_MAX]]})
        self.assertEqual(c.http.calls[1][2], {"orders": [{"order_id": i} for i in ids[BATCH_CANCEL_MAX:]]})
        self.assertTrue(c.http.calls[0][1].endswith("/trade-api/v2/portfolio/events/orders/batched"))
        self.assertEqual(c.cancel_orders_batched([]), [])
        # dict entries keep the routing fields and drop unknown/empty ones
        c.cancel_orders_batched([{"order_id": "a", "exchange_index": 0, "market_ticker": "T", "subaccount": None, "status": "resting"}, {"id": "b", "market_ticker": ""}, {"status": "resting"}])
        self.assertEqual(c.http.calls[-1][2], {"orders": [{"order_id": "a", "exchange_index": 0, "market_ticker": "T"}, {"order_id": "b"}]})

    def test_batch_cancel_reduced_marks_zero_as_errored(self):
        res = _fx("cancel_batched")
        res["orders"].append({"order_id": "z", "client_order_id": None, "reduced_by": "0.00", "ts_ms": None})
        reduced = batch_cancel_reduced([res, {"orders": [{"order_id": "y", "reduced_by": "2.50"}]}, {}])
        ids = [o["order_id"] for o in _fx("cancel_batched")["orders"]]   # recorded on demo 2026-09-22
        self.assertEqual(reduced, {ids[0]: 1.0, ids[1]: 1.0, "z": 0.0, "y": 2.5})

    def test_read_paths_are_portfolio_orders_and_fills(self):
        """Reads never moved to /portfolio/events/: the documented paths are GET
        /portfolio/orders, /portfolio/orders/{id} and /portfolio/fills (a 404 from the wrong
        family is what made the sweep return [] as if it had succeeded)."""
        page1 = dict(_fx("orders_v2"), cursor="c2")
        page2 = {"orders": [{"order_id": "x3", "status": "resting"}], "cursor": ""}
        c = _client({"GET /portfolio/orders?status=resting&cursor=c2": page2, "GET /portfolio/orders?status=resting": page1, "GET /portfolio/fills": _fx("fills_v2"), "GET /portfolio/orders/0b3c7a2e-demo-4c1f-9a11-000000000001": _fx("order")})
        orders = c.orders_v2(status="resting")
        self.assertEqual([o["order_id"] for o in orders][-1], "x3")
        self.assertEqual(len(orders), 3)
        fills = c.fills_v2()
        self.assertEqual(fills[0]["order_id"], _fx("fills_v2")["fills"][0]["order_id"])
        od = c.order_v2("0b3c7a2e-demo-4c1f-9a11-000000000001")
        self.assertEqual(od["outcome_side"], "yes")
        self.assertIs(KalshiClient.order_v2, KalshiClient.order)
        self.assertTrue(all("/portfolio/events/" not in u for _, u, _ in c.http.calls))

    def test_order_side_price_uses_canonical_fields(self):
        yes_row, no_row = _fx("orders_v2")["orders"]
        self.assertEqual(order_side_price(yes_row), ("yes", 0.01))
        self.assertEqual(order_side_price(no_row), ("no", 0.99))   # NO order: no_price_dollars, not the YES price
        self.assertEqual(order_side_price({"book_side": "ask", "yes_price_dollars": "0.3000"}), ("no", 0.7))
        self.assertEqual(order_side_price({"side": "no", "no_price": 40}), ("no", 0.4))  # legacy cents
        self.assertEqual(order_side_price({"outcome_side": "yes"}), ("yes", None))

    def test_create_and_cancel_single(self):
        """V2 create returns a flat ``{order_id, client_order_id, fill_count, remaining_count,
        ts_ms}``; V2 cancel returns ``{order_id, reduced_by, ts_ms}`` (recorded on demo: the
        cancel does not echo ``client_order_id``)."""
        c = _client({"POST /portfolio/events/orders": _fx("create_order"), f"DELETE /portfolio/events/orders/{_fx('create_order')['order_id']}": _fx("cancel_order")})
        res = c.create_order(build_order_payload("KXNFLGAME-26SEP20PHITEN-PHI", "buy", "yes", 1, 0.01, post_only=True))
        self.assertNotIn("order", res)
        self.assertEqual(res["remaining_count"], "1.00")
        self.assertEqual(c.cancel_order(res["order_id"])["reduced_by"], "1.00")

    def test_cancel_single_routes_to_the_correct_exchange_shard(self):
        oid = _fx("create_order")["order_id"]
        path = f"DELETE /portfolio/events/orders/{oid}?market_ticker=KXNBA-X&exchange_index=-1&subaccount=2"
        c = _client({path: _fx("cancel_order")})
        self.assertEqual(c.cancel_order(oid, market_ticker="KXNBA-X", exchange_index=-1, subaccount=2)["reduced_by"], "1.00")
        self.assertTrue(c.http.calls[-1][1].endswith("?market_ticker=KXNBA-X&exchange_index=-1&subaccount=2"))

    def test_cancel_all_orders_is_a_bodyless_delete(self):
        c = _client({"DELETE /portfolio/events/orders": {}})
        self.assertIsNone(c.cancel_all_orders())
        self.assertIsNone(c.cancel_all_orders(subaccount=0))
        self.assertTrue(c.http.calls[0][1].endswith("/trade-api/v2/portfolio/events/orders"))
        self.assertTrue(c.http.calls[1][1].endswith("/portfolio/events/orders?subaccount=0"))
        self.assertEqual([b for _, _, b in c.http.calls], [None, None])


class SelfMatchGuardTests(unittest.TestCase):
    def test_blocks_kalshi_book_hedge(self):
        self.assertIn("book_id=kalshi", SelfMatchGuard().check("T", "yes", 0.5, hedge_book_id="kalshi") or "")
        self.assertIsNone(SelfMatchGuard().check("T", "yes", 0.5, hedge_book_id="rothera"))
        self.assertIsNone(SelfMatchGuard().check("T", "yes", 0.5, hedge_book_id=None))

    def test_blocks_crossing_own_resting_order(self):
        own = RestingOrder("o1", "T", "no", 0.60, 10)
        g = SelfMatchGuard()
        self.assertIsNotNone(g.check("T", "yes", 0.40, resting=[own]))   # 0.40 + 0.60 = 1.00 -> would lift our NO
        self.assertIsNotNone(g.check("T", "yes", 0.45, resting=[own]))
        self.assertIsNone(g.check("T", "yes", 0.39, resting=[own]))      # rests below our own NO ask
        self.assertIsNone(g.check("T", "no", 0.99, resting=[own]))       # same side never self-matches
        self.assertIsNone(g.check("T2", "yes", 0.99, resting=[own]))     # other ticker
        own.status = "canceled"
        self.assertIsNone(g.check("T", "yes", 0.99, resting=[own]))      # only resting orders count

    def test_brokers_consult_guard(self):
        pb = PaperBroker()
        with self.assertRaises(SelfMatchRefused):
            pb.place("T", "yes", 0.5, 10, hedge_book_id="kalshi")
        first = pb.place("T", "no", 0.60, 10)
        with self.assertRaises(SelfMatchRefused):
            pb.place("T", "yes", 0.41, 10)                    # tracked by the broker itself
        ok = pb.place("T", "yes", 0.39, 10, resting=[first])
        self.assertEqual(ok.status, "resting")
        kb = _kb(_client({"POST /portfolio/events/orders": _fx("create_order")}))
        with self.assertRaises(SelfMatchRefused):
            kb.place("T", "yes", 0.41, 10, resting=[first], hedge_book_id="rothera")
        self.assertEqual(kb.client.http.calls, [])  # refused before any request
        placed = kb.place("T", "yes", 0.39, 10, resting=[first], hedge_book_id="rothera")
        self.assertEqual((placed.order_id, placed.status), (_fx("create_order")["order_id"], "resting"))  # flat V2 create response
        self.assertIs(type(placed.payload["expiration_time"]), int)


class CancelAllTests(unittest.TestCase):
    def test_paper_cancel_all_empties_resting(self):
        pb = PaperBroker()
        a, b = pb.place("T1", "yes", 0.3, 5), pb.place("T2", "no", 0.7, 5)
        b.status = "filled"
        stray = RestingOrder("s", "T3", "yes", 0.2, 1)
        done = pb.cancel_all([stray])
        self.assertEqual({o.order_id for o in done}, {a.order_id, "s"})
        self.assertEqual([o.status for o in (a, b, stray)], ["canceled", "filled", "canceled"])
        self.assertEqual([o for o in pb.placed if o.status == "resting"], [])

    def test_single_cancel_failure_stays_visible_as_resting(self):
        c = _client({"DELETE /portfolio/events/orders/o1": HttpError(500, "u", "boom")})
        kb = _kb(c)
        order = RestingOrder("o1", "KXNBA-X", "yes", .4, 1, payload={"exchange_index": 3})
        with self.assertRaises(HttpError):
            kb.cancel(order)
        self.assertEqual(order.status, "resting")

    def test_single_cancel_success_keeps_shard_routing(self):
        c = _client({"DELETE /portfolio/events/orders/o1": _fx("cancel_order")})
        kb = _kb(c)
        order = RestingOrder("o1", "KXNBA-X", "yes", .4, 1, payload={"exchange_index": 3})
        kb.cancel(order)
        self.assertEqual(order.status, "canceled")
        self.assertTrue(c.http.calls[-1][1].endswith("/o1?market_ticker=KXNBA-X&exchange_index=3"))

    def test_kalshi_cancel_all_is_one_batched_delete(self):
        c = _client({"POST /portfolio/events/orders": [{"order_id": "a1", "remaining_count": "5.00"}, {"order_id": "a2", "remaining_count": "5.00"}, {"order_id": "a3", "remaining_count": "5.00"}], "DELETE /portfolio/events/orders/batched": {"orders": [{"order_id": "a1", "reduced_by": "5.00"}, {"order_id": "a2", "reduced_by": "5.00"}, {"order_id": "a3", "reduced_by": "5.00"}]}})
        kb = _kb(c)
        o1, o2 = kb.place("T", "yes", 0.3, 5, exchange_index=0), kb.place("T", "yes", 0.2, 5)
        with mock.patch.dict(os.environ, {"KALSHI_GTD_HORIZON_S": "900"}):
            o3 = kb.place("T2", "no", 0.9, 5, kickoff=1e12)
            self.assertIn("expiration_time", o3.payload)
        done = kb.cancel_all()
        self.assertEqual(len(done), 3)
        deletes = [(u, b) for m, u, b in c.http.calls if m == "DELETE"]
        self.assertEqual(len(deletes), 1)
        # documented body: one entry per order with the shard when known, else the ticker for auto-routing
        self.assertEqual(deletes[0][1], {"orders": [{"order_id": "a1", "exchange_index": 0, "market_ticker": "T"}, {"order_id": "a2", "market_ticker": "T"}, {"order_id": "a3", "market_ticker": "T2"}]})
        self.assertTrue(all(o.status == "canceled" for o in (o1, o2, o3)))
        self.assertEqual(kb.cancel_all(), [])

    def test_kalshi_cancel_all_retries_reduced_by_zero_singly(self):
        """``reduced_by == 0`` means "the cancel errored": that order is retried on its own and
        stays ``resting`` if the retry fails, instead of being reported as cancelled."""
        c = _client({"POST /portfolio/events/orders": [{"order_id": "a1"}, {"order_id": "a2"}, {"order_id": "a3"}], "DELETE /portfolio/events/orders/batched": {"orders": [{"order_id": "a1", "reduced_by": "5.00"}, {"order_id": "a2", "reduced_by": "0.00"}]}, "DELETE /portfolio/events/orders/a2": HttpError(500, "u", "boom"), "DELETE /portfolio/events/orders/a3": _fx("cancel_order")})
        kb = _kb(c)
        o1, o2, o3 = (kb.place("T", "yes", p, 5) for p in (0.3, 0.2, 0.1))
        done = kb.cancel_all()
        self.assertEqual({o.order_id for o in done}, {"a1", "a3"})  # a3 was missing from the batch response -> single cancel OK
        self.assertEqual((o1.status, o2.status, o3.status), ("canceled", "resting", "canceled"))
        self.assertEqual([u.rsplit("/", 1)[1].split("?", 1)[0] for m, u, _ in c.http.calls if m == "DELETE"], ["batched", "a2", "a3"])

    def test_kalshi_cancel_all_sweep_and_fallback(self):
        c = _client({"GET /portfolio/orders?status=resting": _fx("orders_v2"), "DELETE /portfolio/events/orders/batched": HttpError(500, "u", "boom"), "DELETE /portfolio/events/orders/0b3c7a2e-demo-4c1f-9a11-000000000001": _fx("cancel_order"), "DELETE /portfolio/events/orders/0b3c7a2e-demo-4c1f-9a11-000000000002": HttpError(404, "u", "gone")})
        kb = _kb(c)
        with self.assertLogs("arb_engine.strategy.broker", level="WARNING"), self.assertRaises(SweepIncomplete) as cm:
            kb.cancel_all(sweep=True)
        # batched failed -> per-order fallback; the 404 one is reported as possibly still resting
        done = cm.exception.cancelled
        self.assertEqual([o.order_id[-1] for o in done], ["1"])
        self.assertEqual(cm.exception.report.unresolved, ["0b3c7a2e-demo-4c1f-9a11-000000000002"])
        self.assertFalse(cm.exception.report.listing_truncated)
        self.assertIsNone(cm.exception.report.fallback)                  # a complete listing needs no exchange-side sweep
        self.assertEqual([m for m, _, _ in c.http.calls], ["GET", "DELETE", "DELETE", "DELETE"])
        self.assertTrue(c.http.calls[0][1].endswith("/trade-api/v2/portfolio/orders?status=resting"))
        # swept orders carry the documented direction/price: a NO order's price is no_price_dollars
        swept = {o.order_id[-1]: o for o in kb.placed}  # orphans become tracked orders
        self.assertEqual((swept["1"].side, swept["1"].price, swept["1"].count), ("yes", 0.01, 1.0))
        self.assertEqual((swept["2"].side, swept["2"].price, swept["2"].ticker), ("no", 0.99, "KXNFLGAME-26SEP20PHITEN-TEN"))
        self.assertEqual(swept["2"].status, "resting")

    def test_kalshi_sweep_listing_failure_is_not_swallowed(self):
        """After a kill -9 the sweep is the only thing between the orphans and the book. A 404
        on the listing must not turn into ``[]``: the exchange-side cancel-all runs instead,
        and if that fails too the error propagates (after our own orders were cancelled)."""
        own_create = {"POST /portfolio/events/orders": {"order_id": "a1"}}
        batched_ok = {"DELETE /portfolio/events/orders/batched": {"orders": [{"order_id": "a1", "reduced_by": "5.00"}]}}
        c = _client({**own_create, "GET /portfolio/orders?status=resting": HttpError(404, "u", "not found"), **batched_ok, "DELETE /portfolio/events/orders": {}})
        kb = _kb(c, sleep=lambda s: None)
        o1 = kb.place("T", "yes", 0.3, 5)
        with self.assertLogs("arb_engine.strategy.broker", level="WARNING") as logs, self.assertRaises(SweepIncomplete) as cm:
            kb.cancel_all(sweep=True)
        # The exchange-side cancel-all ran, but no listing could confirm the book is empty:
        # that is not a complete shutdown, and it says so.
        self.assertEqual([o.order_id for o in cm.exception.cancelled], ["a1"])
        self.assertEqual((cm.exception.report.fallback, cm.exception.report.verified), ("cancel_all_orders", False))
        self.assertEqual(o1.status, "canceled")
        self.assertIn("listing resting orders failed", logs.output[0])
        self.assertEqual([(m, u.rsplit("/trade-api/v2", 1)[1]) for m, u, _ in c.http.calls][1:], [("GET", "/portfolio/orders?status=resting"), ("DELETE", "/portfolio/events/orders/batched"), ("DELETE", "/portfolio/events/orders"), ("GET", "/portfolio/orders?status=resting")])
        # When the listing works again after the exchange-side sweep and shows nothing, it is complete.
        c3 = _client({**own_create, "GET /portfolio/orders?status=resting": [HttpError(404, "u", "not found"), {"orders": [], "cursor": ""}], **batched_ok, "DELETE /portfolio/events/orders": {}})
        kb3 = _kb(c3, sleep=lambda s: None)
        kb3.place("T", "yes", 0.3, 5)
        with self.assertLogs("arb_engine.strategy.broker", level="WARNING"):
            self.assertEqual([o.order_id for o in kb3.cancel_all(sweep=True)], ["a1"])
        self.assertTrue(kb3.last_sweep.complete)
        self.assertEqual((kb3.last_sweep.fallback, kb3.last_sweep.verified), ("cancel_all_orders", True))
        c2 = _client({**own_create, "GET /portfolio/orders?status=resting": HttpError(404, "u", "not found"), **batched_ok, "DELETE /portfolio/events/orders": HttpError(500, "u", "down")})
        kb2 = _kb(c2)
        o = kb2.place("T", "yes", 0.3, 5)
        with self.assertLogs("arb_engine.strategy.broker", level="WARNING"), self.assertRaises(RuntimeError) as cm:
            kb2.cancel_all(sweep=True)
        self.assertIn("resting orders may remain", str(cm.exception))
        self.assertEqual(o.status, "canceled")  # our own order was still cancelled before raising


class TruncatedSweepTests(unittest.TestCase):
    """A listing the client stopped paging (a cursor still pending after max_pages) is not the
    book: the sweep runs the exchange-side cancel-all, verifies, and reports what may rest."""

    ROW = None

    def _routes(self, verify):
        row = _fx("orders_v2")["orders"][0]
        return {"GET /portfolio/orders?status=resting&cursor=more": {"orders": [row], "cursor": "more"},
                "GET /portfolio/orders?status=resting": [{"orders": [row], "cursor": "more"}] * 1 + verify,
                "DELETE /portfolio/events/orders/batched": {"orders": [{"order_id": row["order_id"], "reduced_by": "1.00"}]},
                "DELETE /portfolio/events/orders": {}}

    def _broker(self, routes, **kw):
        return _kb(_client(routes), sleep=lambda s: None, **kw)

    def test_a_truncated_listing_runs_the_fallback_and_verifies(self):
        kb = self._broker(self._routes([{"orders": [], "cursor": ""}]))
        with self.assertLogs("arb_engine.strategy.broker", level="WARNING") as logs:
            done = kb.cancel_all(sweep=True)
        self.assertEqual(len(done), 1)
        self.assertIn("truncated", logs.output[0])
        rep = kb.last_sweep
        self.assertTrue(rep.listing_truncated)
        self.assertEqual((rep.fallback, rep.verified, rep.unresolved, rep.complete), ("cancel_all_orders", True, [], True))
        self.assertTrue(any(m == "DELETE" and u.endswith("/portfolio/events/orders") for m, u, _ in kb.client.http.calls))

    def test_orders_still_listed_after_the_fallback_are_reported(self):
        straggler = dict(_fx("orders_v2")["orders"][1], order_id="late-1")
        kb = self._broker(self._routes([{"orders": [straggler], "cursor": ""}]), settle_s=0.0)
        with self.assertLogs("arb_engine.strategy.broker", level="WARNING"), self.assertRaises(SweepIncomplete) as cm:
            kb.cancel_all(sweep=True)
        self.assertEqual(cm.exception.report.unresolved, ["late-1"])
        self.assertIn("late-1", str(cm.exception))
        self.assertFalse(kb.last_sweep.complete)

    def test_a_verification_listing_that_is_truncated_again_verifies_nothing(self):
        kb = self._broker(self._routes([{"orders": [], "cursor": "more"}]))
        with self.assertLogs("arb_engine.strategy.broker", level="WARNING"), self.assertRaises(SweepIncomplete) as cm:
            kb.cancel_all(sweep=True)
        self.assertEqual((cm.exception.report.fallback, cm.exception.report.verified), ("cancel_all_orders", False))
        self.assertIn("could not be verified", str(cm.exception))

    def test_a_client_without_paged_reports_truncation_through_last_truncated(self):
        row = _fx("orders_v2")["orders"][0]

        class Legacy:
            env, base_url, has_credentials = "demo", "https://external-api.demo.kalshi.co/trade-api/v2", True

            def __init__(self):
                self.listings, self.cancel_all_calls = 0, 0
                self.last_truncated = False

            def orders_v2(self, **params):
                self.listings += 1
                self.last_truncated = self.listings == 1           # the first listing stopped early
                return [row] if self.listings == 1 else []

            def cancel_orders_batched(self, entries):
                return [{"orders": [{"order_id": e["order_id"], "reduced_by": "1.00"} for e in entries]}]

            def cancel_all_orders(self, subaccount=None):
                self.cancel_all_calls += 1
        c = Legacy()
        kb = _kb(c, sleep=lambda s: None)
        with self.assertLogs("arb_engine.strategy.broker", level="WARNING"):
            kb.cancel_all(sweep=True)
        self.assertEqual((c.cancel_all_calls, kb.last_sweep.complete), (1, True))

    def test_a_failing_fallback_still_raises_after_our_own_orders_are_cancelled(self):
        routes = self._routes([])
        routes["DELETE /portfolio/events/orders"] = HttpError(500, "u", "down")
        kb = self._broker(routes)
        with self.assertLogs("arb_engine.strategy.broker", level="WARNING"), self.assertRaises(RuntimeError) as cm:
            kb.cancel_all(sweep=True)
        self.assertIn("truncated", str(cm.exception))
        self.assertIn("resting orders may remain", str(cm.exception))


class MakerShutdownTests(unittest.TestCase):
    def test_cancel_all_leaves_zero_resting_after_a_maker_step(self):
        """The shutdown contract the maker runner (P11) relies on: after ``cancel_all`` no
        order the runner placed is still resting, and a fresh broker after a kill -9 can
        clear the previous process's list the same way."""
        from tests.test_maker import _runner

        r = _runner()
        r.step()
        self.assertTrue([o for o in r.orders if o.status == "resting"])
        self.assertTrue(all("expiration_time" in o.payload for o in r.orders))  # exchange-side backstop
        done = r.broker.cancel_all(r.orders)
        self.assertEqual(len(done), len(r.orders))
        self.assertEqual([o for o in r.orders if o.status == "resting"], [])
        r2 = _runner()
        r2.step()
        fresh = PaperBroker()  # a restarted process only has the journal's orders
        fresh.cancel_all(r2.orders)
        self.assertEqual([o for o in r2.orders if o.status == "resting"], [])


class TennisSettlementTests(unittest.TestCase):
    def test_registry_or_fallback(self):
        try:
            from arb_engine.matching import settlement_rules  # type: ignore[attr-defined]  # noqa: F401
            self.assertEqual(kmod.TENNIS_SETTLEMENT_SOURCE, "registry")
        except ImportError:
            self.assertEqual(kmod.TENNIS_SETTLEMENT_SOURCE, "fallback")
        self.assertEqual(kmod.TENNIS_SETTLEMENT["retirement"], "advancer")
        self.assertEqual(kmod.TENNIS_SETTLEMENT["walkover"], "fair_price")
        from arb_engine.venues.robinhood import KALSHI_TENNIS_SETTLEMENT
        self.assertEqual(KALSHI_TENNIS_SETTLEMENT, kmod.TENNIS_SETTLEMENT)

    def test_empty_registry_falls_back_to_literal(self):
        """P08's registry exports ``{}`` when its JSON is missing or corrupt and documents that
        as "use the adapters' literals"; an empty Kalshi entry must not become an empty
        settlement dict on every tennis EventInfo (that would mute the scanner's flags)."""
        import sys
        import types

        def with_registry(export):
            stub = types.ModuleType("arb_engine.matching.settlement_rules")
            stub.TENNIS_SETTLEMENT = export
            with mock.patch.dict(sys.modules, {"arb_engine.matching.settlement_rules": stub}):
                return kmod._registry_tennis_settlement()

        for empty in ({}, {"kalshi": {}}, {"polymarket": {"walkover": "50-50"}}, {"kalshi": None}, None):
            rules, src = with_registry(empty)
            self.assertEqual((rules, src), (kmod._TENNIS_SETTLEMENT_FALLBACK, "fallback"), repr(empty))
        rules, src = with_registry({"kalshi": {"retirement": "advancer", "walkover": "fair_price"}, "polymarket": {"walkover": "50-50"}})
        self.assertEqual((rules, src), ({"retirement": "advancer", "walkover": "fair_price"}, "registry"))
        rules, src = with_registry({"retirement": "advancer"})  # flat export
        self.assertEqual((rules, src), ({"retirement": "advancer"}, "registry"))
        with mock.patch.dict(sys.modules, {"arb_engine.matching.settlement_rules": None}):  # import fails
            self.assertEqual(kmod._registry_tennis_settlement()[1], "fallback")


class DemoCheckOfflineTests(unittest.TestCase):
    """``scripts/kalshi_demo_check.py`` driven end to end against the fixtures: the script,
    the client and the documented shapes agree, and ``--record`` writes every fixture in the
    envelope the offline tests unwrap (``order.json`` as ``{"order": ...}``)."""

    def _script(self):
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "scripts" / "kalshi_demo_check.py"
        spec = importlib.util.spec_from_file_location("kalshi_demo_check", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _routes(self, order_row: dict) -> dict:
        oid = "0b3c7a2e-demo-4c1f-9a11-000000000001"
        answers = [dict(_fx("create_order"), order_id=oid), {"order_id": "b2", "remaining_count": "1.00"}, {"order_id": "b3", "remaining_count": "1.00"},
                   {"order_id": "i4", "fill_count": "0.00", "remaining_count": "0.00"},    # the IOC: nothing at $0.01, nothing rests
                   {"order_id": "i5", "fill_count": "0.00", "remaining_count": "0.00"},    # the ledger step's IOC, answer "lost"
                   {"order_id": "e6", "fill_count": "0.00", "remaining_count": "0.00"}]    # the LAG executor's own IOC
        posted: list = []

        def create(body):
            posted.append(body)
            return dict(answers[min(len(posted), len(answers)) - 1], client_order_id=body.get("client_order_id"))

        def by_ticker(_body):
            # GET /portfolio/orders?ticker=&min_ts= (the ledger's reconciliation): every order
            # posted so far, as the exchange lists them, with the client_order_id they carried.
            rows = [{"order_id": answers[min(i, len(answers) - 1)]["order_id"], "client_order_id": b.get("client_order_id"), "ticker": b["ticker"],
                     "status": "canceled", "fill_count_fp": "0.00", "remaining_count_fp": "0.00", "taker_fill_cost_dollars": "0.000000",
                     "maker_fill_cost_dollars": "0.000000", "taker_fees_dollars": "0.000000", "maker_fees_dollars": "0.000000"}
                    for i, b in enumerate(posted)]
            return {"orders": rows, "cursor": ""}
        return {
            "GET /exchange/status": {"exchange_active": True, "trading_active": True},
            "GET /portfolio/balance": _fx("balance"),
            "GET /markets?series_ticker=KXNFLGAME": {"markets": [{"ticker": "KXNFLGAME-26SEP20PHITEN-PHI"}], "cursor": ""},
            "GET /portfolio/orders?ticker=": by_ticker,
            "GET /communications/id": {"communications_id": "offline-demo-account"},
            "GET /series/": {"series": {"ticker": "KXNFLGAME", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}},
            "GET /portfolio/orders/e6": {"order": {"order_id": "e6", "status": "canceled", "fill_count_fp": "0.00", "remaining_count_fp": "0.00",
                                                   "taker_fill_cost_dollars": "0.000000", "maker_fill_cost_dollars": "0.000000",
                                                   "taker_fees_dollars": "0.000000", "maker_fees_dollars": "0.000000"}},
            "POST /portfolio/events/orders": create,
            f"GET /portfolio/orders/{oid}": {"order": order_row},
            "GET /portfolio/orders?status=resting": [{"orders": [order_row], "cursor": ""}, {"orders": [], "cursor": ""}],
            f"DELETE /portfolio/events/orders/{oid}": dict(_fx("cancel_order"), order_id=oid),
            "DELETE /portfolio/events/orders/batched": {"orders": [{"order_id": "b2", "reduced_by": "1.00"}, {"order_id": "b3", "reduced_by": "1.00"}]},
            "GET /portfolio/fills": _fx("fills_v2"),
        }

    def _run(self, routes: dict, argv: list[str], mod=None) -> tuple[int, str]:
        import contextlib
        import io
        import tempfile
        from pathlib import Path

        mod = mod or self._script()
        client = _client(routes)
        out = io.StringIO()
        class FastLedger(mod.OrderLedger):                     # no read-lag wait against a fake exchange
            def __init__(self, *a, **kw):
                kw.setdefault("settle_s", 0.0)
                super().__init__(*a, **kw)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(mod, "KalshiClient", lambda: client), mock.patch.object(mod, "FIXTURES", Path(tmp)), mock.patch.object(mod, "load_dotenv", lambda: None), \
                mock.patch.object(mod, "OrderLedger", FastLedger), contextlib.redirect_stdout(out):
            rc = mod.main(argv)
            written = {p.name: json.loads(p.read_text()) for p in Path(tmp).glob("*.json")}
        return rc, out.getvalue(), written

    def test_happy_path_records_enveloped_fixtures(self):
        rc, text, written = self._run(self._routes(_fx("order")["order"]), ["--record"])
        self.assertEqual(rc, 0, text)
        self.assertIn("ALL PASS", text)
        self.assertNotIn("FAIL", text)
        self.assertEqual(set(written), {"balance.json", "create_order.json", "create_order_ioc.json", "order.json", "orders_v2.json", "cancel_order.json", "cancel_batched.json", "fills_v2.json"})
        self.assertIn("PASS POST immediate_or_cancel buy $0.01 x1", text)
        self.assertIn("PASS IOC payload (the LAG executor's) has no expiration_time and no post_only", text)
        # The ledger step: the "lost" answer blocks, and the listing by client_order_id resolves it.
        self.assertIn("PASS the ledger is bound to this key's account", text)
        self.assertNotIn("offline-demo-account", text)                     # a fingerprint is printed, never the id
        self.assertIn("PASS an order with an unknown outcome blocks new exposure", text)
        self.assertIn("PASS reconciliation found it by client_order_id in the orders listing: state=done order_id=i5", text)
        self.assertIn("PASS and new exposure is unblocked", text)
        self.assertIn("PASS LAG executor sends IOC 1 x KXNFLGAME-26SEP20PHITEN-PHI @ 0.01 through the ledger", text)
        self.assertIn("PASS the executor's order is reconciled from the exchange", text)
        self.assertIn("order", written["order.json"])  # the client unwraps; --record re-wraps
        self.assertEqual(written["order.json"]["order"]["outcome_side"], "yes")
        self.assertTrue(written["order.json"]["_fixture"].startswith("recorded, "))
        self.assertNotIn("user_id", written["order.json"]["order"])
        self.assertEqual(written["cancel_batched.json"]["orders"][0]["reduced_by"], "1.00")
        self.assertEqual(written["orders_v2.json"]["cursor"], "")

    def test_read_side_lag_is_waited_out_not_failed(self):
        """What demo did on 2026-09-22: the read-back 404s right after the create, the resting
        listing shows the new order only on a later read, and after the cancels the listing
        still shows it once. Each read now waits for the write to show (READ_SETTLE_S)."""
        row = _fx("order")["order"]
        routes = self._routes(row)
        oid = row["order_id"]
        routes[f"GET /portfolio/orders/{oid}"] = [HttpError(404, "u", '{"error":{"code":"not_found"}}'), {"order": row}]
        routes["GET /portfolio/orders?status=resting"] = [
            {"orders": [], "cursor": ""},       # not visible yet
            {"orders": [row], "cursor": ""},    # visible
            {"orders": [row], "cursor": ""},    # end: the cancel not visible yet
            {"orders": [], "cursor": ""},       # end: cleared
        ]
        mod = self._script()
        with mock.patch.object(mod.time, "sleep", lambda s: None):
            rc, text, _ = self._run(routes, [], mod=mod)
        self.assertEqual(rc, 0, text)
        self.assertNotIn("FAIL", text)
        self.assertIn("resting orders: 0 total, 0 from this run  (PASS)", text)

    def test_a_404_that_never_clears_still_fails(self):
        row = _fx("order")["order"]
        routes = self._routes(row)
        routes[f"GET /portfolio/orders/{row['order_id']}"] = HttpError(404, "u", "not found")
        mod = self._script()
        with mock.patch.object(mod, "READ_SETTLE_S", 0.0):
            rc, text, _ = self._run(routes, [], mod=mod)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL GET /portfolio/orders/{id}", text)

    def test_null_expiration_and_zero_reduced_by_fail(self):
        """A 200 is not a PASS: the read-back order must echo the expiry and every cancel must
        report ``reduced_by > 0``."""
        row = dict(_fx("order")["order"], expiration_time=None)
        routes = self._routes(row)
        routes["DELETE /portfolio/events/orders/batched"] = {"orders": [{"order_id": "b2", "reduced_by": "1.00"}, {"order_id": "b3", "reduced_by": "0.00"}]}
        rc, text, written = self._run(routes, [])
        self.assertEqual(rc, 1)
        self.assertIn("FAIL read-back order echoes a non-null expiration_time", text)
        self.assertIn("FAIL batched cancel reduced_by > 0 for every id: errored/absent: ['b3']", text)
        self.assertEqual(written, {})

    def test_sweep_cancels_by_documented_body_and_fails_on_listing_error(self):
        row = _fx("order")["order"]
        routes = {"GET /portfolio/orders?status=resting": [{"orders": [row], "cursor": ""}, {"orders": [], "cursor": ""}], "DELETE /portfolio/events/orders/batched": {"orders": [{"order_id": row["order_id"], "reduced_by": "1.00"}]}}
        rc, text, _ = self._run(routes, ["--sweep"])
        self.assertEqual(rc, 0, text)
        self.assertIn("resting orders after sweep: 0  (PASS)", text)
        rc, text, _ = self._run({"GET /portfolio/orders?status=resting": HttpError(404, "u", "nf")}, ["--sweep"])
        self.assertEqual(rc, 1)
        self.assertIn("listing failed", text)


class RecordedFillTests(unittest.TestCase):
    """A real demo fill (scripts/kalshi_demo_check.py --fill --confirm-demo --record, 2026-09-26):
    the create answer, the order row and the fill row the order ledger reconciles from."""

    def test_the_create_answer_reports_average_price_and_fee_per_contract(self):
        fx = _fx("create_order_fill")
        self.assertTrue(fx["_fixture"].startswith("recorded, 2026-09-26 demo"))
        self.assertEqual((fx["fill_count"], fx["average_fill_price"], fx["average_fee_paid"]), ("1.00", "0.5600", "0.0173"))

    def test_the_order_row_and_the_fill_agree_and_the_fee_is_rounded_to_the_centicent(self):
        from decimal import Decimal

        from arb_engine.fees.kalshi import KalshiFees

        od, fill = _fx("order_filled")["order"], _fx("fills_v2")["fills"][0]
        self.assertEqual((od["status"], od["fill_count_fp"], od["taker_fill_cost_dollars"], od["taker_fees_dollars"]), ("executed", "1.00", "0.560000", "0.017300"))
        self.assertEqual((fill["order_id"], fill["count_fp"], fill["fee_cost"], fill["is_taker"]), (od["order_id"], "1.00", "0.017300", True))
        # 0.07 x 0.56 x 0.44 = 0.017248: Kalshi (demo) charged it rounded up to $0.0001, not the cent.
        self.assertEqual(KalshiFees(rounding="centicent").fee("0.56", 1, "taker"), Decimal(od["taker_fees_dollars"]).normalize())
        self.assertEqual(KalshiFees().fee("0.56", 1, "taker"), Decimal("0.02"))       # the conservative default

    def test_the_ledger_books_the_recorded_fill_at_what_was_paid(self):
        import tempfile
        from decimal import Decimal

        from arb_engine.execution.ledger import OrderLedger

        od = _fx("order_filled")["order"]

        class Exchange:
            env, base_url = "demo", "https://external-api.demo.kalshi.co/trade-api/v2"

            def order(self, oid):
                return od

            def fills_v2(self, **params):
                return _fx("fills_v2")["fills"]
        clock = [1000.0]
        led = OrderLedger(os.path.join(tempfile.mkdtemp(prefix="arb_test_"), "l.sqlite3"), "demo", Exchange.base_url, clock=lambda: clock[0])
        res = led.reserve(strategy="lag", ticker=od["ticker"], side="yes", count=1, limit_price="0.56", fee_multiplier=1)
        led.accepted(res.intent_id, _fx("create_order_fill"))
        clock[0] += 3
        led.reconcile(Exchange())
        row = led.get(res.intent_id)
        self.assertEqual((row["state"], Decimal(row["fill_cost"]), Decimal(row["fees"])), ("done", Decimal("0.56"), Decimal("0.0173")))
        self.assertEqual(led.exposure(), Decimal("0.5773"))
        led.close()


class FixtureShapeTests(unittest.TestCase):
    def test_fixtures_are_marked_assumed_until_recorded(self):
        for name in ("balance", "create_order", "order", "orders_v2", "fills_v2", "cancel_order", "cancel_batched"):
            fx = _fx(name)
            self.assertIn("_fixture", fx, name)
            self.assertIn(fx["_fixture"].split(" ")[0].rstrip(","), ("assumed", "recorded"), name)
        # The documented shapes: flat create/cancel responses, enveloped single order, rows
        # with outcome_side/book_side + *_price_dollars.
        self.assertEqual(set(_fx("create_order")) - {"_fixture"}, {"order_id", "client_order_id", "fill_count", "remaining_count", "ts_ms"})
        # Recorded on demo 2026-09-22: cancels do not echo client_order_id.
        self.assertEqual(set(_fx("cancel_order")) - {"_fixture"}, {"order_id", "reduced_by", "ts_ms"})
        self.assertEqual(set(_fx("cancel_batched")["orders"][0]), {"order_id", "reduced_by", "ts_ms"})
        od = _fx("order")["order"]
        self.assertLessEqual({"order_id", "ticker", "outcome_side", "book_side", "status", "yes_price_dollars", "no_price_dollars", "remaining_count_fp", "expiration_time"}, set(od))
        self.assertLessEqual({"fill_id", "order_id", "ticker", "outcome_side", "book_side", "count_fp", "yes_price_dollars", "is_taker"}, set(_fx("fills_v2")["fills"][0]))


if __name__ == "__main__":
    unittest.main()

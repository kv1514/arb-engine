#!/usr/bin/env python3
"""By-hand check of the signed Kalshi path against the DEMO exchange (network, needs a key).

    KALSHI_ENV=demo KALSHI_API_KEY=... KALSHI_PRIVATE_KEY_PATH=... python scripts/kalshi_demo_check.py [--record]

Steps (each prints PASS/FAIL and the HTTP status; the script refuses to run on prod):

1. GET /exchange/status and GET /portfolio/balance            -> the PSS salt / host change works
2. POST a post-only bid at $0.01 for 1 contract (never fills)  -> V2 payload accepted incl. expiration_time (int seconds)
3. GET /portfolio/orders/{id} and ?status=resting              -> read paths / field names; the echoed
                                                                  expiration_time must be non-null (else the
                                                                  exchange-side backstop does not exist)
4. DELETE it, then place two more and DELETE .../batched       -> single and batched cancel; each must report
                                                                  reduced_by > 0 (a 200 with "0.00" is an errored cancel)
5. POST an immediate_or_cancel buy at $0.01 x1                -> the order shape --execute-lag sends (no
                                                                  expiry, no post_only) is accepted and rests nothing
   GET /portfolio/fills (page 1)                               -> fills read path
6. GET resting orders: must be zero at the end                 -> nothing orphaned

``--sweep`` only lists and batch-cancels every resting order (run it after a ``kill -9`` of
``python -m arb_engine maker --mode kalshi`` to prove the orphan path; with
``KALSHI_GTD_HORIZON_S`` the exchange expires them by itself even without this).
``--record`` writes trimmed responses into ``tests/fixtures/kalshi_orders/`` replacing the
"assumed" fixtures with real shapes (secrets and user ids are stripped). Nothing here is
run by the unit tests.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb_engine.config import load_dotenv  # noqa: E402
from arb_engine.execution.kalshi import KalshiExecutor  # noqa: E402
from arb_engine.venues.http import HttpError  # noqa: E402
from arb_engine.venues.kalshi import KalshiClient, batch_cancel_reduced  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "kalshi_orders"
STRIP = {"user_id", "member_id", "api_key", "token"}


def _trim(obj: Any, depth: int = 0) -> Any:
    """Drop identifying fields and cap lists at two elements so fixtures stay small."""
    if isinstance(obj, dict):
        return {k: _trim(v, depth + 1) for k, v in obj.items() if k not in STRIP}
    if isinstance(obj, list):
        return [_trim(x, depth + 1) for x in obj[:2]]
    return obj


def _as_response(fixture: str, res: Any) -> dict:
    """The client unwraps some responses (``order`` -> the bare ``Order`` row, ``orders_v2`` ->
    list, ``cancel_orders_batched`` -> one dict per chunk); re-wrap them in the shape the
    fixture file documents so the offline tests' ``.get("order")`` unwrapping still holds."""
    if fixture == "order":
        return {"order": res}
    if isinstance(res, dict):
        return res
    if fixture in ("orders_v2", "fills_v2"):
        return {fixture.split("_")[0]: list(res or []), "cursor": ""}
    if fixture == "cancel_batched":
        return (res or [{}])[0]
    return {"items": res}


def _reduced_by(res: Any) -> dict[str, float]:
    """``order_id -> reduced_by`` for a single-cancel dict or the batched chunk list."""
    if isinstance(res, dict):
        return {str(res.get("order_id") or ""): float(res.get("reduced_by") or 0)}
    return batch_cancel_reduced(res or [])


# Kalshi's read side trails its write side (measured on demo 2026-09-22): GET
# /portfolio/orders/{id} answered 404 ~150 ms after the create returned the id, the resting
# listing did not show the order yet, and after a successful cancel (reduced_by 1.0) the
# listing still showed it resting. Seconds later every read was right. So a read that checks
# a write polls until it agrees, for up to READ_SETTLE_S, before it counts as a FAIL.
READ_SETTLE_S = 10.0


def settle(fn, ok, timeout: Optional[float] = None, every: float = 0.5):
    """Call ``fn`` until ``ok(result)`` or ``timeout`` (default READ_SETTLE_S); a 404 counts as
    "not visible yet". Returns (result, seconds waited); the last result (or the last
    HttpError) on timeout."""
    timeout = READ_SETTLE_S if timeout is None else timeout
    t0 = time.time()
    while True:
        try:
            res, err = fn(), None
        except HttpError as e:
            if e.status != 404:
                raise
            res, err = None, e
        if err is None and ok(res):
            return res, time.time() - t0
        if time.time() - t0 >= timeout:
            if err is not None:
                raise err
            return res, time.time() - t0
        time.sleep(every)


class Check:
    def __init__(self, client: KalshiClient, record: bool):
        self.client = client
        self.record = record
        self.failures = 0
        self.recorded: dict[str, Any] = {}

    def step(self, name: str, fn, fixture: Optional[str] = None) -> Any:
        t0 = time.time()
        try:
            res = fn()
        except HttpError as e:
            self.failures += 1
            print(f"FAIL {name}: HTTP {e.status} {e.body[:200]}")
            return None
        except Exception as e:  # noqa: BLE001 - report and continue
            self.failures += 1
            print(f"FAIL {name}: {e!r}")
            return None
        print(f"PASS {name} ({(time.time() - t0) * 1000:.0f} ms)")
        if fixture and self.record:
            self.recorded[fixture] = dict({"_fixture": f"recorded, {time.strftime('%Y-%m-%d')} demo {self.client.base_url.split('/')[2]}"}, **_trim(_as_response(fixture, res)))
        return res

    def expect(self, name: str, ok: bool, detail: str = "") -> bool:
        """A PASS/FAIL line for a condition on a response (a 200 alone proves nothing about
        the field the engine relies on)."""
        if not ok:
            self.failures += 1
        print(f"{'PASS' if ok else 'FAIL'} {name}{(': ' + detail) if detail else ''}")
        return ok

    def expect_cancelled(self, name: str, res: Any, ids: list[str]) -> None:
        reduced = _reduced_by(res)
        zero = [i for i in ids if reduced.get(i, 0.0) <= 0]
        self.expect(name + " reduced_by > 0 for every id", not zero, f"errored/absent: {zero}" if zero else f"{ {i: reduced.get(i) for i in ids} }")

    def write_fixtures(self) -> None:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        for name, payload in self.recorded.items():
            (FIXTURES / f"{name}.json").write_text(json.dumps(payload, indent=1, sort_keys=False) + "\n", encoding="utf-8")
            print(f"wrote {FIXTURES / (name + '.json')}")


def pick_ticker(client: KalshiClient, series: str) -> str:
    ms = client.markets(series, status="open", limit=5, max_pages=1)
    if not ms:
        raise RuntimeError(f"no open markets in {series} on demo; pass --ticker")
    return ms[0]["ticker"]


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", help="market to rest the $0.01 test bids on (default: first open KXNFLGAME market)")
    ap.add_argument("--series", default="KXNFLGAME")
    ap.add_argument("--record", action="store_true", help="overwrite tests/fixtures/kalshi_orders/*.json with trimmed real responses")
    ap.add_argument("--sweep", action="store_true", help="only list and batch-cancel every resting order, then exit")
    args = ap.parse_args(argv)

    client = KalshiClient()
    if client.env != "demo":
        print(f"refusing: KALSHI_ENV={client.env!r}; this script places real (tiny) orders and only runs on demo")
        return 2
    if not client.has_credentials:
        print("refusing: set KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH (demo key)")
        return 2
    print(f"env={client.env} base={client.base_url} legacy={client.legacy_base_url}")
    chk = Check(client, record=args.record)

    if args.sweep:
        resting = chk.step("GET /portfolio/orders?status=resting", lambda: client.orders_v2(status="resting"), "orders_v2")
        if resting is None:
            print("listing failed: a sweep that cannot list is the orphan bug; not pretending it succeeded")
            return 1
        print(f"resting orders before sweep: {len(resting)}")
        ids = [str(o.get("order_id") or o.get("id")) for o in resting]
        if ids:
            entries = [{"order_id": i, "exchange_index": o.get("exchange_index"), "market_ticker": o.get("ticker")} for i, o in zip(ids, resting)]
            res = chk.step(f"DELETE .../orders/batched x{len(ids)}", lambda: client.cancel_orders_batched(entries), "cancel_batched")
            if res is not None:
                chk.expect_cancelled("batched sweep", res, ids)
        after = chk.step("GET resting after sweep", lambda: client.orders_v2(status="resting")) or []
        print(f"resting orders after sweep: {len(after)}  ({'PASS' if not after else 'FAIL'})")
        if args.record:
            chk.write_fixtures()
        return 0 if not after and not chk.failures else 1

    chk.step("GET /exchange/status", client.exchange_status)
    bal = chk.step("GET /portfolio/balance", client.balance, "balance")
    if bal is None:
        print("balance failed: the signed path is broken (salt/host/key); stopping before any order")
        return 1
    print(f"  balance: {bal.get('balance_dollars') or bal.get('balance')}")
    if client.fell_back:
        print("  NOTE: fell back to the legacy host; external-api.demo.kalshi.co was unreachable")

    ticker = args.ticker or chk.step(f"pick open market in {args.series}", lambda: pick_ticker(client, args.series))
    if not ticker:
        return 1
    ex = KalshiExecutor(client)
    plan = ex.plan(ticker, "buy", "yes", 1, 0.01, post_only=True, exchange_index=0, kickoff=time.time() + 3600, note="demo check")
    payload = plan.payload()
    print(f"  payload: {json.dumps(payload)}")
    chk.expect("payload.expiration_time is an int (V2 int64 Unix seconds)", isinstance(payload.get("expiration_time"), int), repr(payload.get("expiration_time")))
    created = chk.step("POST /portfolio/events/orders (post-only bid $0.01 x1)", lambda: client.create_order(payload), "create_order")
    oid = str(((created or {}).get("order") or created or {}).get("order_id") or "")
    if not oid:
        print("no order_id in the create response; check the fixture shape")
        return 1
    print(f"  order_id={oid}  remaining_count={(created or {}).get('remaining_count')}")
    od = chk.step("GET /portfolio/orders/{id} (KalshiBroker.poll reads this; waits for the read side)", lambda: settle(lambda: client.order(oid), lambda r: bool(r))[0], "order")
    if od is not None:
        # The exchange-side backstop only exists if the order actually carries the expiry.
        chk.expect("read-back order echoes a non-null expiration_time", bool(od.get("expiration_time")), f"expiration_time={od.get('expiration_time')!r} status={od.get('status')!r}")
        chk.expect("read-back order carries outcome_side/book_side + yes_price_dollars", od.get("outcome_side") in ("yes", "no") and od.get("book_side") in ("bid", "ask") and bool(od.get("yes_price_dollars")), f"outcome_side={od.get('outcome_side')!r} book_side={od.get('book_side')!r} yes_price_dollars={od.get('yes_price_dollars')!r}")
    listed = chk.step("GET /portfolio/orders?status=resting (waits for the new order)", lambda: settle(lambda: client.orders_v2(status="resting"), lambda r: any(str(o.get("order_id")) == oid for o in r or []))[0], "orders_v2")
    if listed is not None:
        chk.expect("the new order is in the resting listing", any(str(o.get("order_id")) == oid for o in listed), f"{len(listed)} resting")
    res = chk.step("DELETE /portfolio/events/orders/{id}", lambda: client.cancel_order(oid), "cancel_order")
    if res is not None:
        chk.expect_cancelled("single cancel", res, [oid])

    more: list[str] = []
    for i in range(2):
        p = ex.plan(ticker, "buy", "yes", 1, 0.01 + 0.01 * i, post_only=True, exchange_index=0, kickoff=time.time() + 3600).payload()
        r = chk.step(f"POST order {i + 2} for the batch", lambda p=p: client.create_order(p))
        o = str(((r or {}).get("order") or r or {}).get("order_id") or "")
        if o:
            more.append(o)
    if more:
        entries = [{"order_id": o, "exchange_index": 0, "market_ticker": ticker} for o in more]
        res = chk.step(f"DELETE /portfolio/events/orders/batched x{len(more)}", lambda: client.cancel_orders_batched(entries), "cancel_batched")
        if res is not None:
            chk.expect_cancelled("batched cancel", res, more)
    # The LAG executor's order is a different shape from the resting ones above: immediate-or-
    # cancel, no post_only, no expiration_time (the gateway rejects an expiry on IOC). At $0.01
    # nothing is offered, so it must be accepted and leave nothing resting.
    ioc = ex.plan(ticker, "buy", "yes", 1, 0.01, post_only=False, exchange_index=0, time_in_force="immediate_or_cancel", note="demo check IOC").payload()
    chk.expect("IOC payload (the LAG executor's) has no expiration_time and no post_only", "expiration_time" not in ioc and not ioc.get("post_only") and ioc.get("time_in_force") == "immediate_or_cancel", json.dumps(ioc))
    r = chk.step("POST immediate_or_cancel buy $0.01 x1 (what --execute-lag sends)", lambda: client.create_order(ioc), "create_order_ioc")
    if r is not None:
        od = r.get("order") or r
        io = str(od.get("order_id") or "")
        print(f"  order_id={io}  fill_count={od.get('fill_count')}  remaining_count={od.get('remaining_count')}")
        if io:
            more.append(io)   # the final check proves it did not rest
    chk.step("GET /portfolio/fills", lambda: client.fills_v2(limit=5), "fills_v2")
    ids = {oid, *more}
    resting = chk.step("GET resting orders at the end (waits for the cancels to show)", lambda: settle(lambda: client.orders_v2(status="resting"), lambda r: not [o for o in r or [] if str(o.get("order_id") or o.get("id")) in ids])[0]) or []
    ours = [o for o in resting if str(o.get("order_id") or o.get("id")) in {oid, *more}]
    print(f"  resting orders: {len(resting)} total, {len(ours)} from this run  ({'PASS' if not ours else 'FAIL'})")
    if ours:
        chk.failures += 1
    if args.record:
        chk.write_fixtures()
    print(f"{'ALL PASS' if not chk.failures else str(chk.failures) + ' FAILED'}")
    return 0 if not chk.failures else 1


if __name__ == "__main__":
    sys.exit(main())

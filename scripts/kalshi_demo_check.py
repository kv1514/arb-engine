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
6. The order ledger against the real exchange (execution/ledger.py): an IOC $0.01 x1 is
   sent with the ledger's client_order_id and its answer is thrown away on purpose - the
   ledger must block new exposure, then find the order in GET /portfolio/orders?ticker=
   &min_ts= by that client_order_id and resolve it                -> unknown-outcome recovery works
   6b. the LAG executor itself (strategy/lagexec.py): a synthetic signal -> ledger -> IOC ->
   reconciled; with --fill it buys 1 contract, sends the lock leg on the other outcome through
   buy_lock (asked 5, bounded to the 1 held; a second lock refused) and sells both back
   6c. the maker's broker (strategy/broker.KalshiBroker): a post-only $0.01 bid recorded in
   the ledger before it is sent, booked by the broker's poll, cancelled, reconciled to done
7. (--fill --confirm-demo) buy 1 contract IOC at a demo market's ask through the ledger,
   reconcile it (fill count, fill cost, fees from the order row; /portfolio/fills?order_id=
   cross-check) and compare the fee charged with the engine's Kalshi fee model (cent vs
   centicent rounding: one contract tells them apart), then sell it back IOC at the bid
                                                                 -> actual fill and fee accounting
8. GET resting orders: must be zero at the end                 -> nothing orphaned

``--sweep`` only lists and batch-cancels every resting order (run it after a ``kill -9`` of
``python -m arb_engine maker --mode kalshi`` to prove the orphan path; with
``KALSHI_GTD_HORIZON_S`` the exchange expires them by itself even without this).
``--record`` writes trimmed responses into ``tests/fixtures/kalshi_orders/`` replacing the
"assumed" fixtures with real shapes (secrets and user ids are stripped). Nothing here is
run by the unit tests. The ledger steps use their own temporary ledger file (``--ledger``
to keep it), never the engine's ``out/orders`` ledger. Demo orders only: step 7 spends at
most ``--max-price`` of play money per contract and only with ``--confirm-demo``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb_engine.config import load_dotenv  # noqa: E402
from arb_engine.execution.kalshi import KalshiExecutor  # noqa: E402
from arb_engine.execution.ledger import DONE, FeeMultipliers, OrderLedger, fee_bound  # noqa: E402
from arb_engine.fees.kalshi import KalshiFees  # noqa: E402
from arb_engine.venues.http import HttpError  # noqa: E402
from arb_engine.venues.kalshi import KalshiClient, batch_cancel_reduced, parse_orderbook  # noqa: E402

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


def reconcile_until(led: OrderLedger, client: KalshiClient, intent_id: str, timeout: float = READ_SETTLE_S + 20.0) -> dict:
    """Reconcile until the intent is resolved (done / rejected) or ``timeout``; the row."""
    t0 = time.time()
    while True:
        led.reconcile(client)
        row = led.get(intent_id) or {}
        if row.get("state") in (DONE, "rejected") or time.time() - t0 >= timeout:
            return row
        time.sleep(1.0)


def ledger_lost_answer(chk: "Check", client: KalshiClient, ex: KalshiExecutor, led: OrderLedger, ticker: str) -> Optional[str]:
    """Step 6: an order whose answer is discarded must be found by its client_order_id."""
    mult, why = FeeMultipliers(client).resolve(ticker)
    if not chk.expect("the market's fee multiplier is known (GET /series/{series})", mult is not None, why or f"multiplier {mult}"):
        return None
    res = led.reserve(strategy="democheck", ticker=ticker, side="yes", count=1, limit_price="0.01", fee_multiplier=mult, detail={"step": "lost answer"})
    if not chk.expect("ledger reserves the intent before the request", res.ok, res.reason):
        return None
    plan = ex.plan(ticker, "buy", "yes", 1, 0.01, exchange_index=0, time_in_force="immediate_or_cancel", note="demo check lost answer",
                   client_order_id=res.client_order_id)
    t_req = time.time()
    r = chk.step("POST IOC $0.01 x1 whose answer the ledger then treats as lost", lambda: ex.execute(plan, confirm=True))
    if r is None:
        led.rejected(res.intent_id, "demo check: the request itself failed")
        return None
    od = (r.get("response") or {})
    oid = str((od.get("order") or od).get("order_id") or "")
    led.ambiguous(res.intent_id, "demo check: answer discarded on purpose", req_ts=t_req)
    chk.expect("an order with an unknown outcome blocks new exposure", led.blocked() is not None, led.blocked() or "not blocked")
    row = reconcile_until(led, client, res.intent_id)
    chk.expect("reconciliation found it by client_order_id in the orders listing", row.get("state") == DONE and row.get("order_id") == oid,
               f"state={row.get('state')} order_id={row.get('order_id')} (sent {oid}) fill={row.get('fill_count')}")
    chk.expect("and new exposure is unblocked", led.blocked() is None, led.blocked() or "")
    return oid or None


def pick_fillable(client: KalshiClient, series_list: list[str], max_price: float) -> Optional[tuple[str, dict, float, float, Optional[float]]]:
    """(ticker, series, ask, ask size, bid) of the first open demo market whose YES ask is at
    most ``max_price`` with at least one contract shown."""
    for series in series_list:
        try:
            ser = client.series(series)
            markets = client.markets(series, status="open", limit=100, max_pages=1)
        except Exception:  # noqa: BLE001 - a series missing on demo is not a failure
            continue
        for m in markets:
            try:
                yes_book, _ = parse_orderbook(client.orderbook(m["ticker"], depth=3))
            except Exception:  # noqa: BLE001
                continue
            if yes_book.asks and 0.02 <= yes_book.asks[0].price <= max_price and yes_book.asks[0].size >= 1:
                bid = yes_book.bids[0].price if yes_book.bids else None
                return m["ticker"], ser, float(yes_book.asks[0].price), float(yes_book.asks[0].size), bid
    return None


def ledger_fill(chk: "Check", client: KalshiClient, ex: KalshiExecutor, led: OrderLedger, series_list: list[str], max_price: float) -> list[str]:
    """Step 7: one real (play-money) fill through the ledger, its fees against the fee model,
    then flat again. Returns the order ids it created."""
    pick = pick_fillable(client, series_list, max_price)
    if pick is None:
        print(f"SKIP fill: no open demo market in {', '.join(series_list)} shows a YES ask <= ${max_price:.2f}")
        return []
    ticker, ser, ask, size, bid = pick
    print(f"  fill market: {ticker} ask {ask} x{size:g} bid {bid}  fee_type={ser.get('fee_type')} fee_multiplier={ser.get('fee_multiplier')}")
    mult, why = FeeMultipliers(client).resolve(ticker, ser.get("fee_multiplier"))
    if not chk.expect("the market's fee multiplier is known", mult is not None, why or f"multiplier {mult}"):
        return []
    res = led.reserve(strategy="democheck", ticker=ticker, side="yes", count=1, limit_price=str(ask), fee_multiplier=mult, detail={"step": "fill"})
    if not chk.expect("ledger reserves the fill", res.ok, res.reason):
        return []
    plan = ex.plan(ticker, "buy", "yes", 1, ask, time_in_force="immediate_or_cancel", note="demo check fill", client_order_id=res.client_order_id)
    t_req = time.time()
    r = chk.step(f"POST IOC buy 1 x {ticker} @ {ask} (fills)", lambda: ex.execute(plan, confirm=True))
    if r is None:
        led.ambiguous(res.intent_id, "demo check: create failed", req_ts=t_req)
        return []
    resp = r.get("response") or {}
    if chk.record:
        chk.recorded["create_order_fill"] = dict({"_fixture": f"recorded, {time.strftime('%Y-%m-%d')} demo {client.base_url.split('/')[2]}"}, **_trim(resp))
    led.accepted(res.intent_id, resp, req_ts=t_req)
    od = resp.get("order") or resp
    print(f"  create answer: fill_count={od.get('fill_count')} average_fill_price={od.get('average_fill_price')} average_fee_paid={od.get('average_fee_paid')}")
    ids = [str(od.get("order_id") or "")]
    row = reconcile_until(led, client, res.intent_id)
    filled = float(row.get("fill_count") or 0)
    chk.expect("reconciled from the order row", row.get("state") == DONE, f"state={row.get('state')} checks={row.get('checks')}")
    if filled >= 1:
        paid = float(row["fill_cost"])
        fee = row["fees"]
        cent = KalshiFees.from_series(ser).fee(paid, 1, "taker")
        centi = KalshiFees.from_series(ser, rounding="centicent").fee(paid, 1, "taker")
        which = "cent" if str(cent) == str(fee) or float(cent) == float(fee) else ("centicent" if float(centi) == float(fee) else "neither")
        print(f"  paid ${paid} for 1 contract, fee ${fee}; fee model: cent ${cent} / centicent ${centi} -> matches {which}")
        chk.expect("fee charged matches the engine's Kalshi fee model (cent or centicent rounding)", which != "neither", f"charged {fee}, model cent {cent} centicent {centi}")
        chk.expect("fee charged is within the ledger's reserved fee bound", float(fee) <= float(fee_bound(ask, 1, mult)), f"{fee} <= {fee_bound(ask, 1, mult)}")
        detail = [e for e in led.events(res.intent_id) if e["kind"] == "done"]
        print(f"  ledger done event: {detail[-1]['detail'] if detail else None}")
        if chk.record:
            try:
                chk.recorded["order_filled"] = dict({"_fixture": f"recorded, {time.strftime('%Y-%m-%d')} demo"}, **_trim({"order": client.order(ids[0])}))
                chk.recorded["fills_v2"] = dict({"_fixture": f"recorded, {time.strftime('%Y-%m-%d')} demo"}, **_trim({"fills": client.fills_v2(order_id=ids[0]), "cursor": ""}))
            except Exception as e:  # noqa: BLE001
                print(f"  (could not record the filled order / fills: {e!r})")
        # Flat again: sell the contract back at the bid (play money either way).
        try:
            yes_book, _ = parse_orderbook(client.orderbook(ticker, depth=3))
            bid = yes_book.bids[0].price if yes_book.bids else None
        except Exception:  # noqa: BLE001
            bid = None
        if bid:
            res2 = led.reserve(strategy="democheck", ticker=ticker, side="yes", action="sell", count=1, limit_price=str(bid),
                               max_cost_per_contract=Decimal(1) - Decimal(str(bid)) + fee_bound(1 - Decimal(str(bid)), 1, mult),
                               fee_multiplier=mult, detail={"step": "flatten"})
            plan2 = ex.plan(ticker, "sell", "yes", 1, bid, time_in_force="immediate_or_cancel", note="demo check flatten", client_order_id=res2.client_order_id)
            r2 = chk.step(f"POST IOC sell 1 x {ticker} @ {bid} (flatten)", lambda: ex.execute(plan2, confirm=True))
            if r2 is not None:
                led.accepted(res2.intent_id, r2.get("response") or {})
                od2 = (r2.get("response") or {}).get("order") or r2.get("response") or {}
                ids.append(str(od2.get("order_id") or ""))
                row2 = reconcile_until(led, client, res2.intent_id)
                print(f"  flatten: state={row2.get('state')} filled={row2.get('fill_count')} proceeds={row2.get('fill_cost')} fees={row2.get('fees')}")
        else:
            print("  no demo bid to sell into: the contract stays in the demo account")
    return [i for i in ids if i]


def engine_path(chk: "Check", client: KalshiClient, led_path: str, series_list: list[str], fill: bool, max_price: float) -> list[str]:
    """Step 6b: the LAG executor's own path (strategy/lagexec.py) on demo - a synthetic signal
    goes through the ledger to an IOC and is reconciled; with ``fill`` it buys 1 contract, then
    sends the lock leg on the other outcome through ``buy_lock`` (bounded by the entry's fill:
    a second lock is refused), and sells both back. Returns the order ids."""
    import tempfile

    from arb_engine.models import OutcomeQuote
    from arb_engine.strategy.lagexec import LagExecutor
    from arb_engine.strategy.leadlag import LagSignal

    pick = pick_fillable(client, series_list, max_price) if fill else None
    if fill and pick is None:
        print("SKIP engine fill: no demo market with an ask to fill")
        return []
    if pick is not None:
        ticker, _, price, _, _ = pick
    else:
        ticker, price = pick_ticker(client, series_list[0]), 0.01
    ex = LagExecutor(mode="demo", executor=KalshiExecutor(client), intents_path=os.path.join(tempfile.mkdtemp(prefix="kalshi_demo_check_"), "lag.jsonl"),
                     ledger=OrderLedger.for_client(client, path=led_path), max_contracts=1, max_notional_per_game=5.0, daily_notional=5.0)
    # A real market identity: the sport of the series, the two outcomes named by the event's
    # market suffixes. A lock leg is only ever a proven hedge of its entry (same market, the
    # other outcome, complementary payoffs from the settlement registry).
    sport = SERIES_SPORT.get(ticker.split("-", 1)[0].upper(), "nfl")
    a, other = ticker.rsplit("-", 1)[-1], None
    if fill:
        ev = client.market(ticker).get("event_ticker")
        others = [m for m in client.get("/markets", {"event_ticker": ev}).get("markets", []) if m.get("ticker") != ticker] if ev else []
        other = others[0]["ticker"] if others else None
    b = other.rsplit("-", 1)[-1] if other else "OTHER"
    key = f"{sport}:{a}|{b}:{time.strftime('%Y-%m-%d')}"
    sig = LagSignal(event_key=key, title="demo check", leader="robinhood", follower="kalshi", outcome=a, label=ticker, lead_move=0.05, follower_move=0.0,
                    leader_mid=min(0.99, price + 0.05), follower_ask=price, follower_all_in=price, edge=0.05, depth=1, suggested_contracts=1, lag_s=0.0, ts=time.time())
    quotes = {"kalshi": [OutcomeQuote("kalshi", ticker, key, a, ask=price, meta={"ticker": ticker, "side": "yes"}, ts=time.time())]}
    rec = ex.on_signal(sig, quotes)
    chk.expect(f"LAG executor sends IOC 1 x {ticker} @ {price} through the ledger", rec.get("status") == "SUBMITTED" and bool(rec.get("intent_id")),
               f"status={rec.get('status')} state={rec.get('state')} reason={rec.get('reason')} latency_ms={rec.get('latency_ms')}")
    if not rec.get("intent_id"):
        return []
    ids = [str(rec.get("order_id") or "")]
    row = reconcile_until(ex.ledger, client, rec["intent_id"])
    chk.expect("the executor's order is reconciled from the exchange", row.get("state") == DONE, f"fill={row.get('fill_count')} cost={row.get('fill_cost')} fees={row.get('fees')}")
    if not fill or float(row.get("fill_count") or 0) < 1:
        return [i for i in ids if i]
    # The lock leg: the other outcome of the same event, through buy_lock (a proven hedge only).
    if not other:
        print("  no other outcome in this event: lock leg not exercised")
        return [i for i in ids if i]
    if not (ex.ledger.get(rec["intent_id"]) or {}).get("settlement"):
        print(f"  SKIP lock leg: no verified {sport} moneyline settlement rule, so the entry has no settlement identity to hedge against")
        return [i for i in ids if i]
    # The exchange must show the entry's contract before a lock leg may hedge it (reads trail writes).
    held, _ = settle(lambda: ex._exchange_holding(ticker, "yes")[0], lambda h: h is not None and h >= 1)
    chk.expect("the exchange shows the entry's contract (GET /portfolio/positions)", held is not None and held >= 1, f"holding {held}")
    yes_book, _ = parse_orderbook(client.orderbook(other, depth=3))
    if not yes_book.asks:
        print(f"  {other} shows no ask: lock leg not exercised")
        return [i for i in ids if i]
    lock_px = float(yes_book.asks[0].price)
    q = OutcomeQuote("kalshi", other, key, b, ask=lock_px, meta={"ticker": other, "side": "yes"}, ts=time.time())
    lock = ex.buy_lock(q, 5, lock_px, key, parent_id=rec["intent_id"])
    chk.expect("buy_lock asks only for the entry's verified fill (5 requested, 1 held)", lock.get("count") == 1 and lock.get("status") == "SUBMITTED",
               f"count={lock.get('count')} status={lock.get('status')} reason={lock.get('reason')}")
    if lock.get("order_id"):
        ids.append(str(lock["order_id"]))
    lrow = reconcile_until(ex.ledger, client, lock["intent_id"]) if lock.get("intent_id") else {}
    if float(lrow.get("fill_count") or 0) >= 1:
        again = ex.buy_lock(q, 1, lock_px, key, parent_id=rec["intent_id"])
        chk.expect("a second lock leg is refused: nothing left unhedged", again.get("status") == "skipped" and "hedged" in str(again.get("reason")), str(again.get("reason")))
    # Flat again (play money either way).
    for t, n in ((ticker, float(row.get("fill_count") or 0)), (other, float(lrow.get("fill_count") or 0))):
        if n < 1:
            continue
        yb, _ = parse_orderbook(client.orderbook(t, depth=3))
        if not yb.bids:
            print(f"  no demo bid for {t}: 1 contract stays in the demo account")
            continue
        bid = float(yb.bids[0].price)
        t_mult, why = FeeMultipliers(client).resolve(t)
        if t_mult is None:
            print(f"  {why}: 1 contract of {t} stays in the demo account")
            continue
        res = ex.ledger.reserve(strategy="democheck", ticker=t, side="yes", action="sell", count=1, limit_price=str(bid),
                                max_cost_per_contract=Decimal(1) - Decimal(str(bid)) + fee_bound(1 - Decimal(str(bid)), 1, t_mult),
                                fee_multiplier=t_mult, detail={"step": "engine flatten"})
        kex = KalshiExecutor(client)
        r = chk.step(f"POST IOC sell 1 x {t} @ {bid} (engine flatten)", lambda t=t, bid=bid, res=res: kex.execute(
            kex.plan(t, "sell", "yes", 1, bid, time_in_force="immediate_or_cancel", client_order_id=res.client_order_id), confirm=True))
        if r is not None:
            ex.ledger.accepted(res.intent_id, r.get("response") or {})
            ids.append(str(((r.get("response") or {}).get("order") or r.get("response") or {}).get("order_id") or ""))
    print(f"  ledger after the engine steps: {ex.ledger.status()['by_state']}, committed today {ex.ledger.status()['committed_today']}")
    return [i for i in ids if i]


def maker_path(chk: "Check", client: KalshiClient, led_path: str, ticker: str) -> list[str]:
    """Step 6c: the maker's broker (strategy/broker.KalshiBroker) on demo - a post-only bid
    at $0.01 (never fills) recorded in the ledger before it is sent, read back by the broker's
    poll and booked, cancelled, and its final state reconciled. Returns the order ids."""
    from arb_engine.strategy.broker import KalshiBroker

    kb = KalshiBroker(client, confirm=True, ledger=OrderLedger.for_client(client, path=led_path), settle_s=READ_SETTLE_S)
    o = chk.step(f"maker: KalshiBroker.place post-only bid 1 x {ticker} @ 0.01 through the ledger",
                 lambda: kb.place(ticker, "yes", 0.01, 1, watch_key=f"democheck:{ticker}|maker", event_key=f"democheck:{ticker}", game_key=f"democheck:{ticker}", kickoff=time.time() + 3600))
    if o is None:
        return []
    row = kb.ledger.get(o.intent_id) or {}
    chk.expect("the maker's order is in the ledger, accepted, with the ledger's client_order_id", row.get("state") == "accepted" and row.get("order_id") == o.order_id
               and o.payload.get("client_order_id") == row.get("client_order_id"), f"state={row.get('state')} strategy={row.get('strategy')}")
    polled, _ = settle(lambda: (kb.poll([o], {}), kb.ledger.get(o.intent_id))[1], lambda r: bool(r and r.get("fill_count") is not None and "still-resting"
                       in {e["kind"] for e in kb.ledger.events(o.intent_id)}))
    chk.expect("the broker's poll books the order row in the ledger", "still-resting" in {e["kind"] for e in kb.ledger.events(o.intent_id)},
               f"fill_count={(polled or {}).get('fill_count')}")
    chk.step("maker: cancel", lambda: kb.cancel(o))
    t0 = time.time()
    while time.time() - t0 < READ_SETTLE_S + 10:
        kb.reconcile(force=True)
        if (kb.ledger.get(o.intent_id) or {}).get("state") == DONE:
            break
        time.sleep(1.0)
    row = kb.ledger.get(o.intent_id) or {}
    chk.expect("after the cancel the ledger has its final state (done, nothing filled)", row.get("state") == DONE and float(row.get("fill_count") or 0) == 0,
               f"state={row.get('state')} fill={row.get('fill_count')}")
    return [o.order_id]


# The sport of each series the demo check can trade (for the settlement registry's rule).
SERIES_SPORT = {"KXNFLGAME": "nfl", "KXNCAAFGAME": "ncaaf", "KXNBAGAME": "nba", "KXNHLGAME": "nhl", "KXMLBGAME": "mlb"}


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
    ap.add_argument("--ledger", help="order-ledger file for the ledger steps (default: a new temporary file, never out/orders)")
    ap.add_argument("--fill", action="store_true", help="step 7: buy 1 contract IOC at a demo ask through the ledger, check its fees, sell it back")
    ap.add_argument("--confirm-demo", action="store_true", help="required with --fill: yes, spend demo play money on a filling order")
    ap.add_argument("--max-price", type=float, default=0.95, help="--fill: most to pay for the one contract (default $0.95)")
    ap.add_argument("--fill-series", default="KXNFLGAME,KXNCAAFGAME,KXMLBGAME,KXNHLGAME,KXNBAGAME", help="--fill: series searched for a demo ask")
    args = ap.parse_args(argv)
    if args.fill and not args.confirm_demo:
        print("refusing: --fill places a filling order on the demo exchange; add --confirm-demo")
        return 2

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
    import tempfile

    led = OrderLedger.for_client(client, path=args.ledger or os.path.join(tempfile.mkdtemp(prefix="kalshi_demo_check_"), "ledger.sqlite3"))
    print(f"  ledger: {led.path}")
    chk.expect("the ledger is bound to this key's account (GET /communications/id; fingerprints only)", bool(led.identity.account_fp),
               led.identity.error or f"account {led.identity.account_fp[:14]}...")
    lost = ledger_lost_answer(chk, client, ex, led, ticker)
    if lost:
        more.append(lost)
    more.extend(engine_path(chk, client, led.path, [x.strip() for x in args.fill_series.split(",") if x.strip()] if args.fill else [args.series],
                            args.fill, args.max_price))
    more.extend(maker_path(chk, client, led.path, ticker))
    if args.fill:
        more.extend(ledger_fill(chk, client, ex, led, [x.strip() for x in args.fill_series.split(",") if x.strip()], args.max_price))
    if not args.fill or "fills_v2" not in chk.recorded:
        chk.step("GET /portfolio/fills", lambda: client.fills_v2(limit=5), "fills_v2" if not args.fill else None)
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

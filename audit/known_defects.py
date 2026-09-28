"""Reproduce the eight open defects of the prior audit at 6b81375, offline.

    python3 -B audit/known_defects.py

Each block prints OBSERVED (what this tree does) and EXPECTED (what the safety rule in
AGENTS.md rule 3a / the ledger module docstring requires).  Nothing here sends an order,
reads a credential or touches the network.
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (DEMO_URL, DEN, KEY, TICKER, Clock, FakeKalshi, Fills, check, executor,  # noqa: E402
                      fill_row, maker, order_row, quotes, report, sig, tmp)

from arb_engine.execution.ledger import (CONTRADICTED, Budget, OrderLedger, fills_evidence,  # noqa: E402
                                         settlement_identity, worst_cost)

KC = settlement_identity("kalshi", KEY, "KC", "yes", "0.5", True)
DENS = settlement_identity("kalshi", KEY, "DEN", "yes", "0.5", True)


def lag_entry(led, count=10, price="0.50", ticker=TICKER, side="yes", settle=KC, now=None):
    res = led.reserve(strategy="lag", ticker=ticker, side=side, count=count, limit_price=price, event_key=KEY, game_key=KEY,
                      fee_multiplier=1, settlement=settle, now=now)
    assert res.ok, res.reason
    return res


# --------------------------------------------------------------------------- H6
class SkewedClockKalshi(FakeKalshi):
    """A Kalshi whose ``min_ts`` filter runs on the *exchange's* clock.  Here the exchange is
    ``skew`` seconds behind the local one, exactly the live hazard: the ledger asks for
    ``local_created_ts - 60`` and the exchange drops every row older than that on its own
    clock, so the order it holds is filtered out of the answer."""

    skew = 120.0                                  # local clock is this far ahead of the exchange

    def paged(self, path, key, params):
        rows, truncated = super().paged(path, key, params)
        if path != "/portfolio/orders":
            return rows, truncated
        min_ts = params.get("min_ts")
        if min_ts is None:
            return rows, truncated
        keep = []
        for r in rows:
            created_on_exchange = self.rows[r["order_id"]]["created"] - self.skew
            if created_on_exchange >= float(min_ts):
                keep.append(r)
        return keep, truncated


def h06_min_ts_clock_skew():
    print("\nH6  reconcile's min_ts is the *local* clock, Kalshi filters on its own")
    clock = Clock(1000.0)
    c = SkewedClockKalshi(clock)
    ex = executor(c, clock)
    c.lose_answer = __import__("arb_engine.venues.http", fromlist=["HttpError"]).HttpError(0, DEMO_URL, "timeout")
    rec = ex.on_signal(sig(ts=clock.t, contracts=10), quotes(), now=clock.t)
    assert rec["status"] == "UNKNOWN", rec
    oid = next(iter(c.rows))
    assert c.rows[oid]["status"] == "executed", c.rows[oid]        # the exchange HAS the order, filled
    c.lose_answer = None
    for t in (1005.0, 1040.0, 1080.0):                             # past not_found_s, two complete listings
        clock.t = t
        ex.reconcile(now=t, force=True)
    row = ex.ledger.rows()[0]
    released = row["state"] == "rejected"
    check("H6: an accepted, filled order survives a 120 s local clock lead", not released,
          f"OBSERVED state={row['state']} reason={row['reason']!r}; the exchange row is "
          f"{c.rows[oid]['status']} with {c.rows[oid]['fill_count_fp']} filled. "
          "EXPECTED: never released; a listing that may have filtered the order out proves nothing.")


# --------------------------------------------------------------------------- H10
def h10_broker_poll_missing_fill_count():
    print("\nH10 KalshiBroker.poll reads a missing fill count as zero")
    clock = Clock(1000.0)
    c = FakeKalshi(clock)
    b = maker(c, clock)
    o = b.place(TICKER, "yes", 0.40, 10, watch_key=KEY + "|kalshi:yes")
    c.fill_resting(o.order_id, 10)                                  # someone lifted the whole resting order
    c.omit = {"fill_count_fp", "fill_count"}                        # the row comes back without the count
    clock.t = 1010.0
    fills = b.poll([o], {})
    check("H10: a filled resting order whose row omits the fill count is not reported as 0 fills",
          bool(fills) or o.status == "resting",
          f"OBSERVED poll returned {fills!r}, order status {o.status!r} (tracking stops, so no hedge is ever raised); "
          "the exchange row has remaining_count_fp 0.00 after 10 maker fills. "
          "EXPECTED: an absent fill count is unknown, never zero - the hedge must be raised (or the order kept tracked).")


# --------------------------------------------------------------------------- H11
def h11_fill_cost_unbounded():
    print("\nH11 a fill cost far below what the order could have cost is booked as paid")
    clock = Clock(1000.0)
    led = OrderLedger(tmp(), "demo", DEMO_URL, clock=clock)
    res = lag_entry(led, count=10, price="0.50")
    led.accepted(res.intent_id, {"order_id": "o1", "fill_count": "10.00", "remaining_count": "0.00"})
    before = led.exposure()
    row = order_row("o1", fill_count_fp="10.00", remaining_count_fp="0.00", initial_count_fp="10.00",
                    taker_fill_cost_dollars="0.0001", taker_fees_dollars="0.0000")
    client = Fills([fill_row("f1", "o1", count="10.00", fee="0.0000", yes_price_dollars="0.5000")])
    note = led.apply_row(res.intent_id, row, client=client)
    after = led.exposure()
    check("H11: a fill cost below the cheapest possible fill is not booked as what was paid",
          after >= Decimal("0.10"),
          f"OBSERVED {note}; exposure {before} -> {after}. The fills listing itself prices the same 10 "
          "contracts at $0.50 each. EXPECTED: a cost the order could not have had (below 1c a contract, "
          "or disagreeing with the listing's own prices) never replaces the reservation.")


# --------------------------------------------------------------------------- H13
def h13_sell_exposure():
    print("\nH13 an accepted manual IOC SELL is exposed to 1 - price, not price")
    clock = Clock(1000.0)
    led = OrderLedger(tmp(), "demo", DEMO_URL, clock=clock)
    # `kalshi order --side-action sell --side yes --price 0.10 --count 10`: short 10 x $0.90.
    per = (Decimal("0.90") * 10 + Decimal("0.10")) / 10
    res = led.reserve(strategy="manual", ticker=TICKER, side="yes", action="sell", count=10, limit_price="0.10",
                      max_cost_per_contract=per, fee_multiplier=1)
    assert res.ok, res.reason
    led.accepted(res.intent_id, {"order_id": "o1", "fill_count": "1.00", "remaining_count": "0.00"})
    got = led.exposure()
    want = Decimal("0.90") + Decimal("0.01")          # 1 contract short of $0.90 plus at most 1c of fee
    check("H13: a partly filled manual sell counts its short side", got >= Decimal("0.90"),
          f"OBSERVED exposure ${got} for 1 of 10 sold at $0.10 (short $0.90 a contract). "
          f"EXPECTED about ${want} (count x (1 - price) + the fee bound), as the reservation itself was sized.")


# --------------------------------------------------------------------------- H14
def h14_daily_budget_midnight():
    print("\nH14 the daily budget resets at local midnight over an unreconciled order")
    clock = Clock(1000.0)
    led = OrderLedger(tmp(), "demo", DEMO_URL, clock=clock)
    budget = Budget(daily=Decimal("11.00"))
    yesterday = led.reserve(strategy="lag", ticker=TICKER, side="yes", count=10, limit_price="0.99",
                            event_key=KEY, game_key=KEY, fee_multiplier=1, budget=budget, settlement=KC)
    assert yesterday.ok, yesterday.reason
    # An accepted order whose fills are not final: the whole worst case stays reserved and it
    # does not block (only pending / ambiguous do), so the only guard left is the budget.
    led.accepted(yesterday.intent_id, {"order_id": "o1", "fill_count": "3.00"})
    spent = led.exposure(strategy="lag", day=led.day_of(clock.t))
    assert led.blocked() is None, led.blocked()
    # Midnight: the same wall clock, one local day later.  Nothing about the open order changed.
    tomorrow = clock.t + 86400.0
    while led.day_of(tomorrow) == led.day_of(clock.t):
        tomorrow += 3600.0
    again = led.reserve(strategy="lag", ticker=TICKER, side="yes", count=10, limit_price="0.99", event_key=KEY,
                        game_key=KEY, fee_multiplier=1, budget=budget, settlement=KC, now=tomorrow)
    total = led.exposure()
    check("H14: yesterday's unreconciled order still counts against today's daily cap",
          total <= Decimal("11.00"),
          f"OBSERVED yesterday committed ${spent} of an ${budget.daily} daily cap and is still open (accepted, not "
          f"reconciled); after midnight a second order for {again.count if again.ok else 0} was "
          f"{'allowed' if again.ok else 'refused'} and the open worst case is now ${total}. "
          "EXPECTED: an order still open from an earlier day keeps counting, so the cap bounds open exposure.")


# --------------------------------------------------------------------------- H16
def h16_fill_id_before_trade_id():
    print("\nH16 fills are keyed by fill_id before trade_id")
    rows = [fill_row("f1", "o1", count="1.00"), fill_row("f2", "o1", count="1.00", trade_id="tf1")]
    ev = fills_evidence(rows, False, None, order_id="o1")
    check("H16: one trade listed under two fill ids counts once", ev.count == Decimal("1.00"),
          f"OBSERVED fills_evidence counts {ev.count} contracts from two rows of trade tf1. "
          "EXPECTED 1.00: a trade matches one order once, so a repeated trade_id is a duplicate.")
    # And the effect end to end: the listing then shows more than the order row.
    clock = Clock(1000.0)
    led = OrderLedger(tmp(), "demo", DEMO_URL, clock=clock)
    res = lag_entry(led, count=10, price="0.50")
    led.accepted(res.intent_id, {"order_id": "o1", "fill_count": "1.00", "remaining_count": "0.00"})
    note = led.apply_row(res.intent_id, order_row("o1", status="canceled", fill_count_fp="1.00", initial_count_fp="10.00"),
                         client=Fills(rows))
    row = led.get(res.intent_id)
    check("H16: a duplicated trade does not contradict a correct order row", row["fill_state"] != CONTRADICTED,
          f"OBSERVED {note!r}; fill_state={row['fill_state']}, fill_seen={row['fill_seen']}. EXPECTED: done at 1 filled - "
          "a contradicted entry can never be hedged by a lock leg, so a duplicate leaves the position naked.")


# --------------------------------------------------------------------------- s15
def two_entries(led, clock, fill="10.00", count=10):
    """Two verified LAG entries of ``count`` on TICKER/yes in one bound ledger."""
    from tests.test_lock_identity import settle

    out = []
    for i in (1, 2):
        res = lag_entry(led, count=count)
        oid = f"e{i}"
        led.accepted(res.intent_id, {"order_id": oid, "fill_count": fill})
        settle(led, res.intent_id, oid, fill, count, "0.50", TICKER)
        assert led.get(res.intent_id)["fill_state"] == "verified", led.get(res.intent_id)
        out.append(res)
    return out


def s15_lock_cap_is_the_whole_holding():
    print("\ns15 buy_lock caps a lock leg at the account's whole holding of the ticker")
    from arb_engine.execution.kalshi import KalshiExecutor
    from arb_engine.strategy.lagexec import LagExecutor
    from tests.test_lock_identity import FakeKalshi as LockKalshi, kq

    clock = Clock(1000.0)
    # Two entries of 10 in the ledger, but the exchange shows only 10 contracts left: the
    # other 10 were sold by hand, outside the ledger.  One entry's inventory is gone.
    client = LockKalshi(holding="10.00")
    ex = LagExecutor(mode="demo", executor=KalshiExecutor(client), intents_path=tmp("lag.jsonl"), ledger_path=tmp(), clock=clock)
    a, b = two_entries(ex.ledger, clock)
    clock.t = 1001.0
    rec = ex.buy_lock(kq(DEN, "DEN", 0.40, clock.t), 10, 0.40, KEY, clock.t, parent_id=a.intent_id)
    sent = list(client.sent)
    check("s15: the lock leg is capped by the contracts this entry still has, not the market's whole position",
          rec.get("status") != "SUBMITTED" and not sent,
          f"OBSERVED the exchange shows 10 of the ledger's 20 recorded contracts on {TICKER} and buy_lock "
          f"status={rec.get('status')!r} sent={len(sent)} order(s) (exchange_holding={rec.get('exchange_holding')}, "
          f"others={rec.get('exchange_holding_others')}, reason={str(rec.get('reason'))[:120]!r}). "
          "EXPECTED: with two entries sharing one market position the caller cannot attribute it, so the entry whose "
          "contracts may be gone hedges nothing (the watch keeps waiting).")


# --------------------------------------------------------------------------- s17
def s17_another_entrys_hedge_counts_as_an_exit():
    print("\ns17 the exits query counts another entry's hedge as this entry's exit")
    from arb_engine.execution.ledger import Identity

    clock = Clock(1000.0)
    led = OrderLedger(tmp(), "demo", DEMO_URL, clock=clock)
    led.bind(Identity(key_fp="key:audit", account_fp="account:audit"))
    a, b = two_entries(led, clock)                     # two LAG entries on the same ticker
    clock.t = 1001.0
    lock_a = led.reserve(strategy="lock", ticker=DEN, side="yes", count=10, limit_price="0.40", event_key=KEY, game_key=KEY,
                         parent_id=a.intent_id, fee_multiplier=1, settlement=DENS, quote_ts=1001.0, now=1001.0)
    clock.t = 1002.0
    lock_b = led.reserve(strategy="lock", ticker=TICKER, side="no", count=10, limit_price="0.40", event_key=KEY, game_key=KEY,
                         parent_id=b.intent_id, fee_multiplier=1, settlement=DENS, quote_ts=1002.0, now=1002.0)
    check("s17: entry B can still hedge its own 10 contracts after entry A hedged its own",
          lock_b.ok and lock_b.count == 10,
          f"OBSERVED entry A's lock reserved {lock_a.count if lock_a.ok else 0} on {DEN}; entry B's lock on {TICKER}/no was "
          f"{'reserved ' + str(lock_b.count) if lock_b.ok else 'refused: ' + lock_b.reason}. "
          "EXPECTED both hedge 10: a lock leg parented to A is A's hedge, never B's exit.")
    # The mirror case: A hedges on the same ticker's NO leg, and then B's own hedge is read as an exit.
    led2 = OrderLedger(tmp(), "demo", DEMO_URL, clock=clock)
    led2.bind(Identity(key_fp="key:audit", account_fp="account:audit"))
    clock.t = 1000.0
    a2, b2 = two_entries(led2, clock)
    clock.t = 1001.0
    la = led2.reserve(strategy="lock", ticker=TICKER, side="no", count=10, limit_price="0.40", event_key=KEY, game_key=KEY,
                      parent_id=a2.intent_id, fee_multiplier=1, settlement=DENS, quote_ts=1001.0, now=1001.0)
    clock.t = 1002.0
    lb = led2.reserve(strategy="lock", ticker=TICKER, side="no", count=10, limit_price="0.40", event_key=KEY, game_key=KEY,
                      parent_id=b2.intent_id, fee_multiplier=1, settlement=DENS, quote_ts=1002.0, now=1002.0)
    check("s17b: A's NO lock on the entry ticker is not counted as B's exit",
          lb.ok and lb.count == 10,
          f"OBSERVED A's lock reserved {la.count if la.ok else 0}; B's lock was "
          f"{'reserved ' + str(lb.count) if lb.ok else 'refused: ' + lb.reason}. EXPECTED both hedge 10.")


if __name__ == "__main__":
    h06_min_ts_clock_skew()
    h10_broker_poll_missing_fill_count()
    h11_fill_cost_unbounded()
    h13_sell_exposure()
    h14_daily_budget_midnight()
    h16_fill_id_before_trade_id()
    s15_lock_cap_is_the_whole_holding()
    s17_another_entrys_hedge_counts_as_an_exit()
    raise SystemExit(report("known defects"))

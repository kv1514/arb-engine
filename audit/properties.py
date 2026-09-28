"""The 22 execution-ledger safety properties, each as a small offline reproduction.

    python3 -B audit/properties.py

Fake HTTP clients, temporary SQLite ledgers, injected clocks.  No network, no order, no
credential.  Every money figure is Decimal and hand-checkable from the fee schedule in
docs/VENUES.md (Kalshi taker fee = ceil(0.07 x multiplier x p x (1-p) x C) to the cent).
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import (DEMO_URL, DEN, KEY, TICKER, Clock, FakeKalshi, Fills, check, executor,  # noqa: E402
                      fill_row, maker, order_row, quotes, report, sig, tmp)

from arb_engine.execution.ledger import (ACCEPTED, CONTRADICTED, DONE, HEDGEABLE, PROVISIONAL, VERIFIED, Budget,  # noqa: E402
                                         Identity, LedgerError, OrderLedger, fee_bound, fingerprint, fills_evidence,
                                         order_evidence, settlement_identity, worst_cost)
from tests.test_lock_identity import settle  # noqa: E402

KC = settlement_identity("kalshi", KEY, "KC", "yes", "0.5", True)
DENS = settlement_identity("kalshi", KEY, "DEN", "yes", "0.5", True)
ACCOUNT = Identity(fingerprint("key", "demo", "k1"), fingerprint("account", "demo", "a1"))
OTHER = Identity(fingerprint("key", "demo", "k2"), fingerprint("account", "demo", "a2"))
# One contract at $0.50 on a multiplier-1 series: fee bound = ceil(0.07 x 0.25) = $0.02.
BOUND_1 = Decimal("0.52")


def led(clock=None, identity=ACCOUNT, path=None):
    l = OrderLedger(path or tmp(), "demo", DEMO_URL, clock=clock or Clock())
    if identity is not None:
        l.bind(identity)
    return l


def entry(l, count=10, price="0.50", ticker=TICKER, side="yes", settlement=KC, tif="immediate_or_cancel", strategy="lag", now=None):
    r = l.reserve(strategy=strategy, ticker=ticker, side=side, count=count, limit_price=price, event_key=KEY, game_key=KEY,
                  fee_multiplier=1, settlement=settlement, tif=tif, now=now)
    assert r.ok, r.reason
    return r


# ---------------------------------------------------------------- 1, 2, 3, 8, 9, 10
def p1_fill_counts_never_decrease():
    print("\n1. an accepted fill count can never decrease silently")
    l = led()
    r = entry(l, count=10)
    l.accepted(r.intent_id, {"order_id": "o1", "fill_count": "10.00", "remaining_count": "0.00"})
    before = l.exposure()
    note = l.apply_row(r.intent_id, order_row("o1", status="canceled", fill_count_fp="4.00", remaining_count_fp="0.00",
                                              initial_count_fp="10.00", taker_fill_cost_dollars="2.00", taker_fees_dollars="0.07"),
                       client=Fills([fill_row("f1", "o1", count="4.00", fee="0.07")]))
    row = l.get(r.intent_id)
    check("1. a later row reporting 4 after an answer showed 10 is contradicted, nothing released",
          row["fill_state"] == CONTRADICTED and l.exposure() == before == Decimal(r.max_cost) and row["fill_count"] is None,
          f"state={row['state']} fill_state={row['fill_state']} fill_count={row['fill_count']} fill_seen={row['fill_seen']} "
          f"exposure {before} -> {l.exposure()}; note={note[:120]!r}")
    # And it survives a restart: fill_seen is on disk.
    l2 = OrderLedger(l.path, "demo", DEMO_URL, clock=Clock())
    check("1b. the highest count seen survives a restart", l2.get(r.intent_id)["fill_seen"] == "10.00",
          f"fill_seen after reopen = {l2.get(r.intent_id)['fill_seen']}")


def p2_zero_fill_cancel_after_a_fill():
    print("\n2. a zero-fill cancellation after a positive fill cannot release real exposure")
    l = led()
    r = entry(l, count=1, price="0.50")
    l.accepted(r.intent_id, {"order_id": "o1", "fill_count": "1.00", "remaining_count": "0.00"})
    client = Fills([fill_row("f1", "o1")])
    note = l.apply_row(r.intent_id, {"order_id": "o1", "status": "canceled", "fill_count_fp": "0", "remaining_count_fp": "0"}, client=client)
    row = l.get(r.intent_id)
    check("2. the reported reproduction: canceled/0 after a create answer of 1 releases nothing",
          row["state"] == ACCEPTED and row["fill_state"] == CONTRADICTED and l.exposure() == BOUND_1,
          f"state={row['state']} fill_state={row['fill_state']} exposure={l.exposure()} (worst case {BOUND_1}); note={note[:120]!r}")


def p3_duplicate_fills_idempotent():
    print("\n3. duplicate fills are idempotent")
    same = [fill_row("f1", "o1"), fill_row("f1", "o1")]
    ev = fills_evidence(same, False, None, order_id="o1")
    check("3. the same fill id listed twice, identical, counts once and stays conclusive",
          ev.count == Decimal("1.00") and ev.conclusive and ev.conflicts == 0,
          f"count={ev.count} conclusive={ev.conclusive} conflicts={ev.conflicts} notes={ev.notes}")
    clash = [fill_row("f1", "o1"), fill_row("f1", "o1", count="9.00")]
    ev2 = fills_evidence(clash, False, None, order_id="o1")
    check("3b. one fill id with two different contents is a conflict and not conclusive",
          not ev2.conclusive and ev2.conflicts == 1,
          f"count={ev2.count} conclusive={ev2.conclusive} conflicts={ev2.conflicts}")
    # End to end: applying the identical duplicate twice books the same thing.
    l = led()
    r = entry(l, count=1)
    l.accepted(r.intent_id, {"order_id": "o1", "fill_count": "1.00", "remaining_count": "0.00"})
    l.apply_row(r.intent_id, order_row("o1"), client=Fills(same))
    first = dict(l.get(r.intent_id))
    l.apply_row(r.intent_id, order_row("o1"), client=Fills(same))
    check("3c. re-applying the same final row twice leaves the booked cost unchanged",
          first["state"] == DONE and first["fill_cost"] == l.get(r.intent_id)["fill_cost"],
          f"state={first['state']} cost={first['fill_cost']} -> {l.get(r.intent_id)['fill_cost']}")


def p4_fills_of_another_order():
    print("\n4. fills from another order cannot satisfy the intent")
    l = led()
    r = entry(l, count=1)
    l.accepted(r.intent_id, {"order_id": "o1", "fill_count": "1.00", "remaining_count": "0.00"})
    # A listing that ignored its order_id filter: the row belongs to o2.
    ev = fills_evidence([fill_row("f9", "o2")], False, None, order_id="o1")
    note = l.apply_row(r.intent_id, order_row("o1"), client=Fills([fill_row("f9", "o2")]))
    row = l.get(r.intent_id)
    check("4. a fills listing carrying only another order's rows never finishes this intent",
          row["state"] == ACCEPTED and not ev.conclusive,
          f"fills_evidence(conclusive={ev.conclusive}, count={ev.count}); state={row['state']}; note={note[:140]!r}")
    ev2 = fills_evidence([fill_row("f1", "o1"), fill_row("f9", "o2")], False, None, order_id="o1")
    check("4b. a mixed listing (this order plus a foreign row) is not conclusive",
          not ev2.conclusive and ev2.notes.get("fills_foreign") == 1, f"{ev2.notes}")
    ev3 = fills_evidence([fill_row("f1", "o1", ticker=DEN)], False, None, order_id="o1", ticker=TICKER, bside="bid")
    check("4c. this order's fill on another market is out of scope and contradicts the row",
          not ev3.conclusive and ev3.out_of_scope == 1 and ev3.contradiction(Decimal(1), Decimal(1)),
          f"{ev3.notes}")


def p5_fills_from_another_account():
    print("\n5. fills from another account cannot satisfy the intent")
    l = led(identity=ACCOUNT)
    r = entry(l, count=1)
    l.accepted(r.intent_id, {"order_id": "o1", "fill_count": "1.00", "remaining_count": "0.00"})

    class OtherAccount(Fills):
        env, base_url, api_key = "demo", DEMO_URL, "k2"

        def communications_id(self):
            return "a2"

        def order(self, oid):
            return order_row(oid)

    client = OtherAccount([fill_row("f1", "o1")])
    raised = None
    try:
        l.reconcile(client)
    except LedgerError as e:
        raised = str(e)
    row = l.get(r.intent_id)
    check("5. a client of another Kalshi account cannot reconcile this ledger",
          raised is not None and row["state"] == ACCEPTED,
          f"reconcile raised {raised!r}; state={row['state']}")
    bad = None
    try:
        l.bind(OTHER)
    except LedgerError as e:
        bad = str(e)
    check("5b. binding a second account to one ledger is refused", bad is not None, f"bind raised {bad!r}")
    # A per-intent read by an unprovable client releases nothing either.
    l2 = led(identity=Identity(key_fp=None, account_fp=None, error="account unreadable"))
    r2 = entry(l2, count=1)
    l2.ambiguous(r2.intent_id, "timeout")

    class Blind:
        env, base_url, api_key = "demo", DEMO_URL, "k9"

        def communications_id(self):
            raise RuntimeError("no")

        def paged(self, path, key, params):
            return [], False

    out = l2.reconcile(Blind(), now=2000.0)
    check("5c. an order absent from an unprovable account's listing is not released",
          l2.get(r2.intent_id)["state"] != "rejected",
          f"state={l2.get(r2.intent_id)['state']}; note={out and out[0].get('note')!r}")


def p6_truncated_or_failed_fills():
    print("\n6. truncated or failed fills pagination holds the reservation")
    for label, client in (("truncated", Fills([fill_row("f1", "o1")], truncated=True)),
                          ("read error", Fills([fill_row("f1", "o1")], error=RuntimeError("boom"))),
                          ("no fills client", None)):
        l = led()
        r = entry(l, count=1)
        l.accepted(r.intent_id, {"order_id": "o1", "fill_count": "1.00", "remaining_count": "0.00"})
        note = l.apply_row(r.intent_id, order_row("o1"), client=client)
        row = l.get(r.intent_id)
        check(f"6. a {label} fills listing does not finish the intent",
              row["state"] == ACCEPTED and row["fill_state"] != VERIFIED and l.exposure() > Decimal("0.51"),
              f"state={row['state']} fill_state={row['fill_state']} exposure={l.exposure()}; note={note[:130]!r}")


def p7_missing_fees_hold():
    print("\n7. missing fees hold the reservation")
    l = led()
    r = entry(l, count=1)
    l.accepted(r.intent_id, {"order_id": "o1", "fill_count": "1.00", "remaining_count": "0.00"})
    row_no_fee = order_row("o1", taker_fees_dollars=None)
    note = l.apply_row(r.intent_id, row_no_fee, client=Fills([fill_row("f1", "o1", fee=None)]))
    row = l.get(r.intent_id)
    check("7. a final row and listing that state no fee keep the fill at the limit plus the fee bound",
          row["state"] == ACCEPTED and row["fill_state"] == VERIFIED and l.exposure() == BOUND_1,
          f"state={row['state']} exposure={l.exposure()} (limit+bound {BOUND_1}); note={note[:140]!r}")
    check("7b. an explicit 0.0000 fee is zero, not unknown",
          order_evidence(order_row("o1", taker_fees_dollars="0.0000"), 1, True).fees == Decimal("0.0000"),
          f"fees={order_evidence(order_row('o1', taker_fees_dollars='0.0000'), 1, True).fees}")
    # Late fees land on a later read.
    note2 = l.apply_row(r.intent_id, order_row("o1"), client=Fills([fill_row("f1", "o1")]))
    check("7c. the fees reported later finish the intent at what was paid",
          l.get(r.intent_id)["state"] == DONE and l.exposure() == Decimal("0.500000") + Decimal("0.017500"),
          f"state={l.get(r.intent_id)['state']} exposure={l.exposure()}; note={note2[:90]!r}")


def p8_conflicting_evidence():
    print("\n8. conflicting order and fills evidence holds the reservation")
    cases = {
        "listing shows more than the row": (order_row("o1", fill_count_fp="1.00", status="canceled", initial_count_fp="10.00"),
                                            [fill_row("f1", "o1"), fill_row("f2", "o1")]),
        "listing shows more than ordered": (order_row("o1", fill_count_fp="1.00", status="canceled", initial_count_fp="10.00"),
                                            [fill_row("f1", "o1", count="99.00")]),
        "a fill without an id": (order_row("o1", fill_count_fp="1.00", status="canceled", initial_count_fp="10.00"),
                                 [fill_row("f1", "o1", fill_id=None, trade_id=None)]),
        "the row is on another market": (order_row("o1", ticker=DEN, fill_count_fp="1.00", status="canceled", initial_count_fp="10.00"),
                                         [fill_row("f1", "o1")]),
        "the row carries another client_order_id": (order_row("o1", client_order_id="somebody-else", fill_count_fp="1.00",
                                                              status="canceled", initial_count_fp="10.00"), [fill_row("f1", "o1")]),
    }
    for label, (row, fills) in cases.items():
        l = led()
        r = entry(l, count=10)
        l.accepted(r.intent_id, {"order_id": "o1", "fill_count": "1.00"})
        note = l.apply_row(r.intent_id, row, client=Fills(fills))
        got = l.get(r.intent_id)
        check(f"8. {label}: nothing released",
              got["state"] == ACCEPTED and l.exposure() == Decimal(r.max_cost),
              f"state={got['state']} fill_state={got['fill_state']} exposure={l.exposure()} of {r.max_cost}; note={note[:130]!r}")


def p9_genuine_zero_fill_cancellation():
    print("\n9. a genuine zero-fill cancellation still completes safely")
    l = led()
    r = entry(l, count=10)
    l.accepted(r.intent_id, {"order_id": "o1", "fill_count": "0.00", "remaining_count": "0.00"})
    note = l.apply_row(r.intent_id, order_row("o1", status="canceled", fill_count_fp="0.00", initial_count_fp="10.00",
                                              taker_fill_cost_dollars="0.0000", taker_fees_dollars="0.0000"),
                       client=Fills([]))
    row = l.get(r.intent_id)
    check("9. an empty complete listing finishes a zero-fill cancellation at $0",
          row["state"] == DONE and l.exposure() == Decimal(0),
          f"state={row['state']} exposure={l.exposure()}; note={note[:110]!r}")


def p10_partial_ioc():
    print("\n10. a partial IOC cancels its remainder and preserves the filled quantity")
    l = led()
    r = entry(l, count=10, price="0.50")
    l.accepted(r.intent_id, {"order_id": "o1", "fill_count": "4.00", "remaining_count": "0.00"})
    note = l.apply_row(r.intent_id, order_row("o1", status="canceled", fill_count_fp="4.00", initial_count_fp="10.00",
                                              taker_fill_cost_dollars="2.000000", taker_fees_dollars="0.070000"),
                       client=Fills([fill_row("f1", "o1", count="4.00", fee="0.070000")]))
    row = l.get(r.intent_id)
    check("10. 4 of 10 filled, 6 cancelled: exposure is exactly 4 x $0.50 + $0.07 of fee",
          row["state"] == DONE and row["fill_count"] == "4.00" and l.exposure() == Decimal("2.070000"),
          f"state={row['state']} filled={row['fill_count']} exposure={l.exposure()} (want 2.070000); note={note[:110]!r}")


def p11_resting_orders():
    print("\n11. resting orders stay reserved until final evidence")
    l = led()
    r = entry(l, count=10, price="0.40", tif="good_till_canceled", strategy="maker")
    l.accepted(r.intent_id, {"order_id": "o1", "status": "resting", "fill_count": "0.00", "remaining_count": "10.00"})
    worst = Decimal(r.max_cost)
    note = l.apply_row(r.intent_id, {"order_id": "o1", "status": "resting", "fill_count_fp": "3.00", "remaining_count_fp": "7.00",
                                     "initial_count_fp": "10.00"}, client=Fills([fill_row("f1", "o1", count="3.00")]))
    check("11. a resting order 3/10 filled still reserves its whole worst case",
          l.get(r.intent_id)["state"] == ACCEPTED and l.exposure() == worst,
          f"exposure={l.exposure()} of worst case {worst}; note={note[:110]!r}")
    note2 = l.apply_row(r.intent_id, {"order_id": "o1", "status": "canceled", "fill_count_fp": "3.00", "remaining_count_fp": "0.00",
                                      "initial_count_fp": "10.00", "maker_fill_cost_dollars": "1.200000", "maker_fees_dollars": "0.000000",
                                      "taker_fill_cost_dollars": "0.000000", "taker_fees_dollars": "0.000000"},
                       client=Fills([fill_row("f1", "o1", count="3.00", fee="0.000000", is_taker=False,
                                              yes_price_dollars="0.4000")]))
    check("11b. only the final row plus a complete listing books it at 3 x $0.40",
          l.get(r.intent_id)["state"] == DONE and l.exposure() == Decimal("1.200000"),
          f"state={l.get(r.intent_id)['state']} exposure={l.exposure()}; note={note2[:110]!r}")


def p12_hedge_needs_resolved_entry():
    print("\n12. a dependent hedge cannot use unresolved entry evidence")
    clock = Clock()
    l = led(clock)
    cases = {}
    # (a) create answer only - provisional.
    r = entry(l, count=10)
    l.accepted(r.intent_id, {"order_id": "o1", "fill_count": "10.00", "remaining_count": "0.00"})
    cases["a create answer alone"] = r.intent_id
    # (b) contradicted.
    r2 = entry(l, count=10)
    l.accepted(r2.intent_id, {"order_id": "o2", "fill_count": "10.00", "remaining_count": "0.00"})
    l.apply_row(r2.intent_id, order_row("o2", status="canceled", fill_count_fp="0.00", initial_count_fp="10.00",
                                        taker_fill_cost_dollars=None, taker_fees_dollars=None), client=Fills([]))
    cases["a contradicted entry"] = r2.intent_id
    # (c) a trailing fills listing.
    r3 = entry(l, count=10)
    l.accepted(r3.intent_id, {"order_id": "o3", "fill_count": "10.00", "remaining_count": "0.00"})
    l.apply_row(r3.intent_id, order_row("o3", fill_count_fp="10.00", initial_count_fp="10.00",
                                        taker_fill_cost_dollars="5.00", taker_fees_dollars="0.18"), client=Fills([]))
    cases["a listing that trails the row"] = r3.intent_id
    for label, pid in cases.items():
        res = l.reserve(strategy="lock", ticker=DEN, side="yes", count=10, limit_price="0.40", event_key=KEY, parent_id=pid,
                        fee_multiplier=1, settlement=DENS, quote_ts=clock(), now=clock())
        check(f"12. {label} sizes no lock leg", not res.ok, f"reason={res.reason[:150]!r}")
    # And a verified one does.
    r4 = entry(l, count=10)
    l.accepted(r4.intent_id, {"order_id": "o4", "fill_count": "10.00"})
    settle(l, r4.intent_id, "o4", "10.00", 10, "0.50", TICKER)
    ok = l.reserve(strategy="lock", ticker=DEN, side="yes", count=10, limit_price="0.40", event_key=KEY, parent_id=r4.intent_id,
                   fee_multiplier=1, settlement=DENS, quote_ts=clock(), now=clock())
    check("12b. a verified entry can be hedged", ok.ok and ok.count == 10, f"ok={ok.ok} count={ok.count} reason={ok.reason!r}")


def p13_residual_inventory():
    print("\n13. residual inventory is carried forward conservatively")
    clock = Clock()
    l = led(clock)
    r = entry(l, count=10)
    l.accepted(r.intent_id, {"order_id": "o1", "fill_count": "10.00"})
    settle(l, r.intent_id, "o1", "10.00", 10, "0.50", TICKER)
    first = l.reserve(strategy="lock", ticker=DEN, side="yes", count=10, limit_price="0.40", event_key=KEY, parent_id=r.intent_id,
                      fee_multiplier=1, settlement=DENS, quote_ts=clock(), now=clock())
    assert first.ok
    second = l.reserve(strategy="lock", ticker=DEN, side="yes", count=10, limit_price="0.40", event_key=KEY, parent_id=r.intent_id,
                       fee_multiplier=1, settlement=DENS, quote_ts=clock(), now=clock())
    check("13. an unresolved earlier lock leg counts at its full size, so nothing is left to hedge",
          not second.ok and "waiting" in second.reason,
          f"reason={second.reason[:160]!r}")
    settle(l, first.intent_id, "l1", "4.00", 10, "0.40", DEN)      # it filled only 4
    third = l.reserve(strategy="lock", ticker=DEN, side="yes", count=10, limit_price="0.40", event_key=KEY, parent_id=r.intent_id,
                      fee_multiplier=1, settlement=DENS, quote_ts=clock(), now=clock())
    check("13b. once it is reconciled at 4, exactly the remaining 6 may be hedged",
          third.ok and third.count == 6, f"ok={third.ok} count={third.count} reason={third.reason!r}")
    # A sale of the entry's own contracts reduces the inventory too.
    l2 = led(Clock())
    r2 = entry(l2, count=10)
    l2.accepted(r2.intent_id, {"order_id": "e2", "fill_count": "10.00"})
    settle(l2, r2.intent_id, "e2", "10.00", 10, "0.50", TICKER)
    sale = l2.reserve(strategy="manual", ticker=TICKER, side="yes", action="sell", count=7, limit_price="0.70",
                      max_cost_per_contract="0.32", settlement=KC)
    l2.accepted(sale.intent_id, {"order_id": "s1", "fill_count": "7.00"})
    settle(l2, sale.intent_id, "s1", "7.00", 7, "0.70", TICKER, action="sell", side="yes")
    hedge = l2.reserve(strategy="lock", ticker=DEN, side="yes", count=10, limit_price="0.40", event_key=KEY, parent_id=r2.intent_id,
                       fee_multiplier=1, settlement=DENS, quote_ts=1000.0, now=1000.0)
    check("13c. 7 of 10 sold leaves 3 to hedge", hedge.ok and hedge.count == 3,
          f"ok={hedge.ok} count={hedge.count} reason={hedge.reason!r}")


def p14_manual_release():
    print("\n14. manual release only for a never-accepted intent, by the same bound account")
    l = led()
    r = entry(l, count=1)
    l.ambiguous(r.intent_id, "timeout")
    out = l.release(r.intent_id, "checked the exchange by hand: no such order")
    check("14. a pending / unknown intent of this account is released", out["state"] == "rejected" and l.exposure() == Decimal(0),
          f"state={out['state']} exposure={l.exposure()}")
    for label, prepare in (("accepted", lambda i: l.accepted(i, {"order_id": "ox", "fill_count": "0.00"})),
                           ("showed fills", lambda i: l.accepted(i, {"order_id": "oy", "fill_count": "1.00"}))):
        r2 = entry(l, count=1)
        prepare(r2.intent_id)
        why = None
        try:
            l.release(r2.intent_id, "release it")
        except LedgerError as e:
            why = str(e)
        check(f"14b. an intent that is {label} is refused", why is not None, f"{why!r}"[:200])
    l3 = led(identity=Identity(key_fp="key:x", account_fp=None, error="unreadable"))
    r3 = entry(l3, count=1)
    l3.ambiguous(r3.intent_id, "timeout")
    l3.identity = Identity(key_fp="key:other", account_fp=None, error="unreadable")
    why = None
    try:
        l3.release(r3.intent_id, "release it")
    except LedgerError as e:
        why = str(e)
    check("14c. a client that is not provably the sending account is refused", why is not None, f"{why!r}"[:200])
    why = None
    try:
        l.release(r.intent_id, "")
    except LedgerError as e:
        why = str(e)
    check("14d. a release without a reason is refused", why is not None, f"{why!r}"[:120])


def p15_identity_across_restart_and_rotation():
    print("\n15. account identity survives restart and key rotation")
    path = tmp()
    l = led(identity=ACCOUNT, path=path)
    r = entry(l, count=1)
    l.close()
    l2 = OrderLedger(path, "demo", DEMO_URL, clock=Clock())
    rotated = Identity(fingerprint("key", "demo", "k1-rotated"), ACCOUNT.account_fp)
    eff = l2.bind(rotated)
    check("15. a new key of the same account is accepted after a restart", eff.account_fp == ACCOUNT.account_fp,
          f"bound account {eff.account_fp!r}")
    why = None
    try:
        l2.bind(OTHER)
    except LedgerError as e:
        why = str(e)
    check("15b. another account is still refused on the same file", why is not None, f"{why!r}"[:160])
    # A key whose account read failed is completed from the key map, and the old intent is
    # still recognised as this account's.
    l3 = OrderLedger(path, "demo", DEMO_URL, clock=Clock())
    blind = Identity(ACCOUNT.key_fp, None, "GET /communications/id failed")
    eff3 = l3.bind(blind)
    check("15c. a key already mapped identifies its account when the account read fails",
          eff3.account_fp == ACCOUNT.account_fp, f"{eff3}")
    check("15d. the pre-restart intent is still this account's", l3._relation(l3.conn.execute(
        "SELECT * FROM intents WHERE intent_id = ?", (r.intent_id,)).fetchone(), eff3) == "same", "")


def p16_quote_timestamps():
    print("\n16. NaN / infinity / missing / negative / future quote timestamps are rejected")
    clock = Clock(1000.0)
    l = led(clock)
    r = entry(l, count=10)
    l.accepted(r.intent_id, {"order_id": "o1", "fill_count": "10.00"})
    settle(l, r.intent_id, "o1", "10.00", 10, "0.50", TICKER)
    bad = {"NaN": float("nan"), "+inf": float("inf"), "-inf": float("-inf"), "missing": None, "a string": "1000",
           "a bool": True, "far past": 0.0, "the future": 1100.0}
    for label, qts in bad.items():
        res = l.reserve(strategy="lock", ticker=DEN, side="yes", count=1, limit_price="0.40", event_key=KEY, parent_id=r.intent_id,
                        fee_multiplier=1, settlement=DENS, quote_ts=qts, now=clock())
        check(f"16. quote_ts {label} is refused", not res.ok, f"reason={res.reason[:130]!r}")
    ok = l.reserve(strategy="lock", ticker=DEN, side="yes", count=1, limit_price="0.40", event_key=KEY, parent_id=r.intent_id,
                   fee_multiplier=1, settlement=DENS, quote_ts=995.0, now=clock())
    check("16b. a fresh finite quote time is accepted", ok.ok, f"reason={ok.reason!r}")


def p17_non_finite_inputs():
    print("\n17. NaN / infinity quantities, prices, fee multipliers and times are rejected")
    l = led()
    bad = {"count NaN": {"count": float("nan")}, "count inf": {"count": float("inf")}, "count 0": {"count": 0},
           "count -1": {"count": -1}, "price NaN": {"limit_price": float("nan")}, "price inf": {"limit_price": float("inf")},
           "price 0": {"limit_price": 0}, "price 1": {"limit_price": 1}, "price -0.5": {"limit_price": "-0.5"},
           "multiplier NaN": {"fee_multiplier": float("nan")}, "multiplier inf": {"fee_multiplier": float("inf")},
           "multiplier -1": {"fee_multiplier": -1}, "now NaN": {"now": float("nan")}, "now inf": {"now": float("inf")},
           "now a string": {"now": "1000"}, "per-contract NaN": {"max_cost_per_contract": float("nan")}}
    for label, kw in bad.items():
        base = dict(strategy="lag", ticker=TICKER, side="yes", count=1, limit_price="0.50", fee_multiplier=1, event_key=KEY)
        base.update(kw)
        try:
            res = l.reserve(**base)
            ok, why = not res.ok, f"reason={res.reason[:120]!r}"
        except Exception as e:  # noqa: BLE001 - reserve must refuse, never raise something a caller does not catch
            ok, why = False, f"reserve RAISED {type(e).__name__}: {e} - callers catch only LedgerError, so this kills the loop"
        check(f"17. reserve with {label} is refused", ok, why)
    for label, kw in (("no multiplier at all", {"fee_multiplier": None}),):
        base = dict(strategy="lag", ticker=TICKER, side="yes", count=1, limit_price="0.50", event_key=KEY)
        base.update(kw)
        res = l.reserve(**base)
        check(f"17b. reserve with {label} is refused", not res.ok, f"reason={res.reason[:120]!r}")
    # The other entry points raise instead of returning.
    r = entry(l, count=1)
    for name, call in (("accepted", lambda: l.accepted(r.intent_id, {"order_id": "o1"}, now=float("nan"))),
                       ("ambiguous", lambda: l.ambiguous(r.intent_id, "x", now=float("inf"))),
                       ("done", lambda: l.done(r.intent_id, Decimal(1), Decimal(1), Decimal(0), now=float("nan"))),
                       ("blocked", lambda: l.blocked(now=float("nan"))),
                       ("status", lambda: l.status(now=float("inf")))):
        why = None
        try:
            call()
        except LedgerError as e:
            why = str(e)
        check(f"17c. {name}() refuses a non-finite time", why is not None, f"{why!r}"[:110])
    check("17d. fee_bound refuses a non-finite multiplier",
          _raises(lambda: fee_bound("0.50", 1, float("nan"))) and _raises(lambda: fee_bound("0.50", 1, float("inf"))), "")


def _raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    return False


# ------------------------------------------------------------ 18-22: the caller layers
def button(mode="paper", **kw):
    from arb_engine.fees.registry import fee_model_for_quote
    from arb_engine.strategy.arbbutton import ArbButton

    b = ArbButton(mode=mode, cmd_url="https://ntfy.sh/topic-cmd", fee_for=fee_model_for_quote, http_get=False,
                  journal_path=tmp("arb_button.jsonl"), clock=lambda: 1000.0, **kw)
    b.ledger_path = tmp()
    b._reconcile_later = lambda *d: None
    return b


def rh_quote(book_id="rothera", ask=0.44):
    from arb_engine.models import OutcomeQuote

    return OutcomeQuote("robinhood", "rh-1", KEY, "DEN", ask=ask, bid=ask - 0.01, ask_size=500, ts=1000.0,
                        fee_params={"exchange": "rothera"}, book_id=book_id,
                        meta={"contract_id": "rh-1", "side": "yes", "exchange": "rothera"})


def k_quote(ask=0.52):
    from arb_engine.models import OutcomeQuote

    return OutcomeQuote("kalshi", TICKER, KEY, "KC", ask=ask, bid=ask - 0.01, ask_size=500, ts=1000.0,
                        fee_params={"fee_type": "quadratic", "fee_multiplier": 1},
                        meta={"ticker": TICKER, "side": "yes", "exchange_index": 1})


def sized(k_ask=0.52, r_ask=0.44, n=10):
    return {"contracts": n, "margin": 0.02, "total_cost": 0.98,
            "legs": [{"venue": "kalshi", "market_id": TICKER, "price": k_ask, "side": "yes", "outcome": "KC", "label": "KC"},
                     {"venue": "robinhood", "market_id": "rh-1", "price": r_ask, "side": "yes", "outcome": "DEN", "label": "DEN"}]}


def p18_duplicate_marks():
    print("\n18. duplicate marks cannot consume the same displayed liquidity twice")
    b = button()
    qs = {"kalshi": [k_quote()], "robinhood": [rh_quote()]}
    spec = b.register(KEY, "DEN @ KC", sized(), qs, now=1000.0)
    assert spec is not None
    b.live_prices = lambda s: {"kalshi_asks": [(0.52, 10)], "rh_ask": 0.44, "rh_bid": 0.43, "rh_state": "open", "at": 1000.1}
    first = b.fire(spec["token"], now=1000.1)
    second = b.fire(spec["token"], now=1000.2)
    check("18. a button token is single use: a second tap consumes no liquidity",
          first is not None and second is None,
          f"first={'fired' if first else None} ({(first or {}).get('filled')} filled); second={second!r}")
    check("18b. a token this process never issued fires nothing", b.fire("deadbeefdeadbeef", now=1e9) is None, "")
    b2 = button()
    s2 = b2.register(KEY, "DEN @ KC", sized(), qs, now=1000.0)
    b2.live_prices = lambda s: {"kalshi_asks": [(0.52, 10)], "rh_ask": 0.44, "rh_bid": 0.43, "rh_state": "open", "at": 1e9}
    expired = b2.fire(s2["token"], now=1000.0 + b2.ttl_s + 1)
    check("18e. a tap after the token expired buys nothing",
          expired["status"] == "expired" and expired["filled"] == 0, f"{expired['status']} filled={expired['filled']}")
    # The ledger's own duplicate guard: one dedupe key, one order, whatever the caller does.
    l = led()
    a = l.reserve(strategy="button", ticker=TICKER, side="yes", count=10, limit_price="0.52", dedupe_key="button:tok",
                  fee_multiplier=1, event_key=KEY)
    dup = l.reserve(strategy="button", ticker=TICKER, side="yes", count=10, limit_price="0.52", dedupe_key="button:tok",
                    fee_multiplier=1, event_key=KEY)
    check("18c. the ledger refuses a second reservation for the same signal", a.ok and not dup.ok and "duplicate" in dup.reason,
          f"second: {dup.reason!r}")
    # The lock book spaces its retries so one displayed ask is not chased every tick.
    from arb_engine.strategy.laglock import LagLockBook

    book = LagLockBook(watch_s=600.0, lock_retry_s=10.0)
    check("18d. the lock book has a retry spacing (an unfilled IOC is not re-sent every tick)",
          book.lock_retry_s > 0, f"lock_retry_s={book.lock_retry_s}")


def p19_unwind_only_what_filled():
    print("\n19. failed second legs unwind only the actual filled first-leg quantity")
    b = button()
    qs = {"kalshi": [k_quote()], "robinhood": [rh_quote()]}
    spec = b.register(KEY, "DEN @ KC", sized(n=10), qs, now=1000.0)
    b.live_prices = lambda s: {"kalshi_asks": [(0.52, 3)], "rh_ask": 0.44, "rh_bid": 0.43, "rh_state": "open", "at": 1000.1}
    rec = b.fire(spec["token"], now=1000.1)
    check("19. a Kalshi leg that only finds 3 of 10 reports 7 unhedged, not 0",
          rec["filled"] == 3 and rec["unhedged"] == 7 and rec["would_lock"] is False,
          f"filled={rec['filled']} unhedged={rec['unhedged']} would_lock={rec['would_lock']} locked_sets={rec.get('locked_sets')}")
    # An unknown outcome is not a number of hedged contracts.
    from arb_engine.execution.kalshi import KalshiExecutor
    from arb_engine.venues.http import HttpError

    lost = FakeKalshi(Clock(1000.0))
    lost.lose_answer = HttpError(0, DEMO_URL, "curl: (28) Operation timed out")
    b2 = button(mode="demo", executor=KalshiExecutor(lost))
    spec2 = b2.register(KEY, "DEN @ KC", sized(n=10), qs, now=1000.0)
    b2.live_prices = lambda s: {"kalshi_asks": [(0.52, 10)], "rh_ask": 0.44, "rh_bid": 0.43, "rh_state": "open", "at": 1000.1}
    rec2 = b2.fire(spec2["token"], now=1000.1)
    check("19b. a Kalshi leg whose outcome is unknown reports unhedged=None, never 0",
          rec2.get("unhedged") is None and rec2.get("would_lock") is False,
          f"status={rec2.get('status')!r} unhedged={rec2.get('unhedged')!r} reason={str(rec2.get('reason'))[:90]!r}")
    # The lock book only counts contracts an order actually reported filled.
    from arb_engine.strategy.laglock import LagLockBook

    class Exec:
        def __init__(self, recs):
            self.recs, self.asked = list(recs), []

        def buy_lock(self, quote, count, price, event_key, now, parent_id=None):
            self.asked.append(count)
            return self.recs.pop(0)

    ex = Exec([{"status": "SUBMITTED", "fill_count": "4.00"}, {"status": "UNKNOWN", "reason": "timeout"},
               {"status": "SUBMITTED", "fill_count": "6.00"}])
    bk = LagLockBook(watch_s=600.0, executor=ex, require_tie_safe=False, lock_retry_s=0.0, executable={"kalshi"})
    p = bk.open("k", KEY, "KC", "DEN", "kalshi", 10, 0.50, 0.52, 1000.0, "demo", entry_tie=0.5, parent_id="pid")
    qs2 = {"kalshi": [k_quote_other(0.40, 1000.0)]}
    for t in (1001.0, 1002.0, 1003.0):
        bk.observe(KEY, qs2, now=t)
    check("19c. the lock book counts only reported fills and asks for exactly the remainder",
          ex.asked == [10, 6, 6] and p.locked_contracts == 10,
          f"asked={ex.asked} locked_contracts={p.locked_contracts} status={p.status}")


def k_quote_other(ask, t):
    from arb_engine.models import OutcomeQuote

    return OutcomeQuote("kalshi", DEN, KEY, "DEN", ask=ask, bid=ask - 0.01, ask_size=500, ts=t,
                        fee_params={"fee_type": "quadratic", "fee_multiplier": 1}, meta={"ticker": DEN, "side": "yes"})


def p20_both_venues_fees_depth_latency():
    print("\n20. unwind latency, depth, haircut and BOTH venues' fees are included")
    from arb_engine.strategy.arbbutton import all_in, max_price
    from arb_engine.fees.kalshi import KalshiFees
    from arb_engine.fees.registry import fee_model_for_quote

    kfee, rfee = KalshiFees(multiplier=Decimal(1)), fee_model_for_quote(rh_quote())
    n = 10
    # Hand numbers (docs/VENUES.md): Kalshi taker at $0.52 x 10 = ceil(0.07 x 0.52 x 0.48 x 10)
    # = $0.18, i.e. 1.8c a contract -> all-in 0.538.  Robinhood Rothera = $0.01 commission +
    # $0.01 exchange fee a contract -> all-in 0.46.  The set costs 0.538 + 0.46 = 0.998.
    k_all_in, r_all_in = all_in(kfee, 0.52, n), all_in(rfee, 0.44, n)
    check("20. both venues' fees are in the all-in prices",
          abs(k_all_in - 0.538) < 1e-9 and abs(r_all_in - 0.46) < 1e-9,
          f"kalshi all-in {k_all_in} (want 0.538), robinhood all-in {r_all_in} (want 0.46)")
    cap = max_price(kfee, r_all_in, n, 0.01)
    check("20b. the Kalshi limit is the highest price that still locks against the other leg's all-in",
          cap is not None and all_in(kfee, cap, n) + r_all_in <= 1.0 + 1e-12 and all_in(kfee, round(cap + 0.01, 2), n) + r_all_in > 1.0,
          f"limit {cap}: {all_in(kfee, cap, n) + r_all_in} <= 1, one cent up {all_in(kfee, round(cap + 0.01, 2), n) + r_all_in} > 1")
    b = button()
    qs = {"kalshi": [k_quote()], "robinhood": [rh_quote()]}
    spec = b.register(KEY, "DEN @ KC", sized(n=10), qs, now=1000.0)
    b.live_prices = lambda s: {"kalshi_asks": [(0.52, 4), (0.53, 4), (0.75, 50)], "rh_ask": 0.44, "rh_bid": 0.43,
                               "rh_state": "open", "at": 1002.5}
    rec = b.fire(spec["token"], now=1000.0)
    check("20c. the fill walks the displayed depth and stops at the limit",
          rec["filled"] == 4 and rec["levels"] == [(0.52, 4)] and rec["kalshi_limit"] == 0.52,
          f"filled={rec['filled']} levels={rec.get('levels')} limit={rec['kalshi_limit']} "
          "(the $0.53 level is above the limit that still locks against Robinhood's all-in)")
    check("20d. the latency from tap to the price read is recorded", rec["check_s"] == 2.5, f"check_s={rec.get('check_s')}")
    check("20e. the set is re-priced at live prices on both venues before buying",
          rec.get("live_set_cost") is not None and rec.get("still_locks_live") is not None and rec.get("rh_within_max") is not None,
          f"live_set_cost={rec.get('live_set_cost')} still_locks_live={rec.get('still_locks_live')} rh_within_max={rec.get('rh_within_max')}")


def p21_unknown_settlement_is_not_a_lock():
    print("\n21. unknown settlement identity cannot be classified as guaranteed arbitrage")
    clock = Clock()
    l = led(clock)
    r = entry(l, count=10)
    l.accepted(r.intent_id, {"order_id": "o1", "fill_count": "10.00"})
    settle(l, r.intent_id, "o1", "10.00", 10, "0.50", TICKER)
    unknown_tie = settlement_identity("kalshi", KEY, "DEN", "yes", None, True)
    res = l.reserve(strategy="lock", ticker=DEN, side="yes", count=10, limit_price="0.40", event_key=KEY, parent_id=r.intent_id,
                    fee_multiplier=1, settlement=unknown_tie, quote_ts=clock(), now=clock())
    check("21. a lock whose tie payout is unknown on a market that can tie is refused",
          not res.ok and "tie" in res.reason, f"reason={res.reason[:150]!r}")
    half = settlement_identity("kalshi", KEY, "DEN", "yes", "0.0", True)
    res2 = l.reserve(strategy="lock", ticker=DEN, side="yes", count=10, limit_price="0.40", event_key=KEY, parent_id=r.intent_id,
                     fee_multiplier=1, settlement=half, quote_ts=clock(), now=clock())
    check("21b. a pair paying under $1 on a tie is not complementary",
          not res2.ok and "not complementary" in res2.reason, f"reason={res2.reason[:150]!r}")
    # An entry recorded without a settlement identity can never be hedged under the exemption.
    l2 = led(Clock())
    r2 = entry(l2, count=10, settlement=None)
    l2.accepted(r2.intent_id, {"order_id": "o9", "fill_count": "10.00"})
    settle(l2, r2.intent_id, "o9", "10.00", 10, "0.50", TICKER)
    res3 = l2.reserve(strategy="lock", ticker=DEN, side="yes", count=10, limit_price="0.40", event_key=KEY, parent_id=r2.intent_id,
                      fee_multiplier=1, settlement=DENS, quote_ts=1000.0, now=1000.0)
    check("21c. an entry with no recorded settlement identity is never hedged under the exemption",
          not res3.ok and "settlement identity" in res3.reason, f"reason={res3.reason[:150]!r}")
    # The caller's own layer: an unverified settlement rule stops the whole LAG signal.
    from arb_engine.strategy.lagexec import settlement_of

    sd, why = settlement_of(rh_quote(), KEY)
    check("21d. settlement_of refuses a quote whose venue rule is not verified", sd is None, f"{why!r}")
    sd2, why2 = settlement_of(k_quote(), KEY)
    check("21e. a verified Kalshi NFL moneyline rule gives the tie payout",
          sd2 is not None and sd2["tie_payout"] == "0.5" and sd2["can_tie"] is True, f"{sd2} {why2!r}")
    # And the lock book will not call a pair a lock when the tie payout is unknown.
    from arb_engine.strategy.laglock import LagLockBook

    bk = LagLockBook(watch_s=600.0, require_tie_safe=True, executable={"kalshi"})
    p = bk.open("k2", KEY, "KC", "DEN", "kalshi", 10, 0.50, 0.52, 1000.0, "paper", entry_tie=None)
    lines = bk.observe(KEY, {"kalshi": [k_quote_other(0.10, 1000.0)]}, now=1001.0)
    check("21f. the lock book does not lock a pair whose tie payout is unknown",
          p.status == "watching" and not lines, f"status={p.status} lines={lines}")


def p22_same_book_routes():
    print("\n22. same-book Kalshi / direct-Robinhood routes are rejected with an empty ledger")
    b = button()
    qs = {"kalshi": [k_quote()], "robinhood": [rh_quote(book_id="kalshi")]}
    spec = b.register(KEY, "DEN @ KC", sized(), qs, now=1000.0)
    check("22. no button is issued for a Robinhood quote that is Kalshi's own book", spec is None,
          f"register returned {type(spec).__name__}")
    from arb_engine.strategy.broker import SelfMatchGuard

    g = SelfMatchGuard()
    check("22b. the maker refuses to rest against a hedge on Kalshi's own book, with nothing resting",
          g.check(TICKER, "yes", 0.40, hedge_book_id="kalshi", resting=[]) is not None,
          f"{g.check(TICKER, 'yes', 0.40, hedge_book_id='kalshi', resting=[])!r}")
    check("22c. and the ledger is not consulted for that decision (an empty ledger changes nothing)",
          led().exposure() == Decimal(0) and b.register(KEY, "t", sized(), qs, now=1000.0) is None, "")
    from arb_engine.compliance import executable_venues

    check("22d. Polymarket is not an executable venue by default (signal only)",
          "polymarket" not in {v.lower() for v in executable_venues({})},
          f"executable = {sorted(executable_venues({}))}")


if __name__ == "__main__":
    for fn in (p1_fill_counts_never_decrease, p2_zero_fill_cancel_after_a_fill, p3_duplicate_fills_idempotent,
               p4_fills_of_another_order, p5_fills_from_another_account, p6_truncated_or_failed_fills,
               p7_missing_fees_hold, p8_conflicting_evidence, p9_genuine_zero_fill_cancellation, p10_partial_ioc,
               p11_resting_orders, p12_hedge_needs_resolved_entry, p13_residual_inventory, p14_manual_release,
               p15_identity_across_restart_and_rotation, p16_quote_timestamps, p17_non_finite_inputs,
               p18_duplicate_marks, p19_unwind_only_what_filled, p20_both_venues_fees_depth_latency,
               p21_unknown_settlement_is_not_a_lock, p22_same_book_routes):
        fn()
    raise SystemExit(report("properties 1-22"))

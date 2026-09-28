#!/usr/bin/env python3
"""The five causality defects this audit reproduced but did NOT fix, because the fix would
either land in a file another agent owns or move a published number.

Each one is demonstrated, not argued from the source.  Synthetic data only; no database, no
network, no order.

    python3 audit/repro_reported_only.py

1. ``scripts/arb_backtest.py`` ``Ledger.available`` (NOT OWNED by this audit)
   ``taken = sum(k for tt, k in self.used[key] if t - tt <= USED_WINDOW_S)`` has no lower
   bound, so a purchase stamped AFTER ``t`` already eats the depth available at ``t``.

2. ``arb_engine/quant/eventstudy.py`` ``_series`` (NOT OWNED)
   ``pts.sort()`` on ``(ts, price)`` orders prints of one timestamp by PRICE, so the tape's
   own sequence is discarded and the "last price at t" is the highest, not the latest.

3. ``scripts/microstructure_eval.py`` ``chrono_training`` (OWNED, but the fix moves a
   rendered number: ``docs/MODEL.md`` table ``micro_discovery_forecast``, B3 ridge)
   a game may train on a game whose last SAMPLE is earlier, while that game's LABELS read
   marks up to ``h + max(1, .2h)`` seconds later - past the scored game's first decision.

4. ``arb_engine/strategy/paperlag.py`` (NOT OWNED - audit M4's other half)
5. ``arb_engine/strategy/laglock.py`` (NOT OWNED - audit M4's other half)
   both candidate filters are ``_fresh(quote, now, window)``, which has no lower bound at
   the moment the order or the position was created, so a quote observed BEFORE the order
   existed fills it / hedges it.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def report(n, title, expected, observed, broken):
    print(f"--- {n}. {title} ---")
    print(f"  EXPECTED: {expected}")
    print(f"  OBSERVED: {observed}")
    print(f"  -> {'REPRODUCED' if broken else 'not reproduced'}")
    print()
    if broken:
        FAILURES.append(title)


def backtest_ledger():
    from scripts.arb_backtest import Ledger

    led = Ledger()
    key = ("kalshi", "KXNFL-A", "yes")
    led.take(key, 100.0, 40)                       # a purchase 40 s in the FUTURE
    report(1, "arb_backtest.Ledger.available counts takes stamped after t",
           "at t=60 nothing has been bought yet, so all 50 contracts are available",
           f"available(t=60, shown=50) = {led.available(key, 60.0, 50)}",
           led.available(key, 60.0, 50) != 50)


def eventstudy_series():
    from arb_engine.quant.eventstudy import _series, price_at

    tape = [(10.0, .60), (10.0, .40)]              # two prints, one timestamp, in tape order
    ts, px = _series(tape)
    ts2, px2 = _series(list(reversed(tape)))
    report(2, "eventstudy breaks ties by price rather than sequence",
           "the tape's own order decides which print is last at t=10 (.40 here)",
           f"price_at(t=10) = {price_at(ts, px, 10.0)} in both tape orders (prices {px} / {px2})",
           px == px2 == [.40, .60] and price_at(ts, px, 10.0) != .40)


def chrono_labels():
    from scripts.microstructure_eval import chrono_training

    samples = [{"event_key": "nfl:A|B:2026-09-20", "t": float(t)} for t in range(0, 101)]
    samples += [{"event_key": "nfl:C|D:2026-09-20", "t": float(t)} for t in range(101, 200)]
    samples += [{"event_key": f"nfl:E{i}|F:2026-09-20", "t": float(t)} for i in range(3) for t in range(0, 100)]
    plan = chrono_training(samples)
    trains_on = plan.get("nfl:C|D:2026-09-20", [])
    # A|B's last sample is t=100 and C|D's first decision is t=101, so A|B is "earlier".  Its
    # own 15 s label, though, is read from a mark at t+15 - up to t=115, well after C|D started.
    report(3, "chrono_training labels can read marks after the next game's first decision",
           "a training game's LABEL window must also end before the scored game's first decision",
           f"nfl:C|D (first decision t=101) trains on {trains_on} - nfl:A|B's t=100 sample is "
           f"labelled from a mark at t=115",
           "nfl:A|B:2026-09-20" in trains_on)


def paperlag_stale_fill():
    from arb_engine.models import OutcomeQuote
    from arb_engine.strategy.paperlag import LagPaperBook

    class Sig:
        event_key, follower, outcome, leader = "nfl:A|B:2026-09-27", "robinhood", "A", "kalshi"
        follower_ask, follower_all_in, edge, depth, suggested_contracts = .50, .51, .03, 10, 10
        tie_value = .0

    book = LagPaperBook(fill_window_s=10.0)
    book.open(Sig(), now=100.0)                      # the order exists from t=100
    stale = OutcomeQuote("robinhood", "RH-A", Sig.event_key, "A", bid=.48, ask=.50, bid_size=50, ask_size=50,
                         ts=95.0, quote_time=95.0, meta={"side": "yes"}, book_id="rothera")
    lines = book.observe(Sig.event_key, {"robinhood": [stale]}, now=101.0)
    filled = [o for o in book.orders if o.filled_at is not None]
    report(4, "paperlag fills an order from a quote observed before the order existed",
           "a quote stamped t=95 cannot fill an order that was only sent at t=100",
           f"filled = {bool(filled)}" + (f" at {filled[0].fill_price} ({lines[0] if lines else ''})" if filled else ""),
           bool(filled))


def laglock_stale_hedge():
    from arb_engine.models import OutcomeQuote
    from arb_engine.strategy.laglock import LagLockBook, LockPosition

    book = LagLockBook(watch_s=600.0, executable={"kalshi"}, require_tie_safe=False, fresh_s=10.0)
    pos = LockPosition(key="k", event_key="nfl:A|B:2026-09-27", outcome="A", lock_outcome="B", venue="robinhood",
                       contracts=10, entry_price=.50, entry_all_in=.51, opened=100.0, source="paper", entry_tie=.0)
    stale = OutcomeQuote("kalshi", "KXB", pos.event_key, "B", bid=.44, ask=.45, bid_size=50, ask_size=50,
                         ts=95.0, quote_time=95.0, meta={"side": "yes"}, book_id="kalshi",
                         fee_params={"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1, "series": "KXNFLGAME"})
    got = book._cheapest(pos, {"kalshi": [stale]}, now=101.0)
    report(5, "laglock hedges a position from a quote observed before the position existed",
           "a quote stamped t=95 cannot hedge a position that was only opened at t=100",
           "no candidate" if got is None else f"cheapest lock = {got[0]} @ {got[1]} (quote ts {got[3].ts})",
           got is not None)


def main() -> int:
    print(__doc__.split("\n\n")[0])
    print()
    for fn in (backtest_ledger, eventstudy_series, chrono_labels, paperlag_stale_fill, laglock_stale_hedge):
        try:
            fn()
        except Exception as exc:                     # a probe that cannot run is reported, never hidden
            print(f"  !! probe failed to run: {exc!r}\n")
    print(f"{len(FAILURES)} of 5 reproduced: {FAILURES}")
    print("None of these are fixed on this branch; see the audit report for why.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

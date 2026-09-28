"""Look-ahead defects the evaluator audit found in files outside its own subsystem.

Three places let a decision use information it could not have had:

* ``quant/eventstudy._series`` sorted ``(ts, price)`` pairs, so prints sharing one timestamp
  came back ordered by price and "the last print at or before t" was the *highest* price of a
  sweep rather than its last print;
* ``strategy/paperlag.LagPaperBook.observe`` filled a paper order from any quote inside the
  fill window, including one observed *before* the order was placed;
* ``scripts/arb_backtest.Ledger.available`` counted contracts taken at any time within the
  window, including takes stamped *after* the moment being priced, so liquidity vanished
  before it was consumed.

Each test fails on the code as it stood at 6b81375.
"""
from __future__ import annotations

import importlib.util
import pathlib
import unittest

from arb_engine.models import OutcomeQuote
from arb_engine.quant.eventstudy import _series, price_at
from arb_engine.strategy.paperlag import LagPaperBook

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_arb_backtest():
    spec = importlib.util.spec_from_file_location("arb_backtest_under_test", ROOT / "scripts" / "arb_backtest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class EventStudySeriesTests(unittest.TestCase):
    def test_prints_sharing_a_timestamp_keep_the_tapes_order(self):
        """A sweep at one timestamp must read as its last print, not its dearest."""
        tape = [(10.0, 0.60), (10.0, 0.40)]
        ts, px = _series(tape)
        self.assertEqual(px, [0.60, 0.40], "the tape's own order must survive")
        self.assertEqual(price_at(ts, px, 10.0), 0.40, "the last print at t=10 is 0.40")

    def test_the_answer_does_not_depend_on_which_print_was_listed_first(self):
        """Reversing the tape reverses the answer - that is the tape speaking, not the sort."""
        a_ts, a_px = _series([(10.0, 0.60), (10.0, 0.40)])
        b_ts, b_px = _series([(10.0, 0.40), (10.0, 0.60)])
        self.assertEqual((price_at(a_ts, a_px, 10.0), price_at(b_ts, b_px, 10.0)), (0.40, 0.60))
        # ... and neither is the by-price answer the old sort always gave.
        self.assertNotEqual(price_at(a_ts, a_px, 10.0), 0.60)

    def test_ordering_across_timestamps_is_unchanged(self):
        ts, px = _series([(30.0, 0.5), (10.0, 0.9), (20.0, 0.1)])
        self.assertEqual(ts, [10.0, 20.0, 30.0])
        self.assertEqual(px, [0.9, 0.1, 0.5])


def _quote(ts: float, ask: float = 0.47, *, event_key: str = "nfl:DEN|KC:2026-09-27",
           outcome: str = "KC", venue: str = "kalshi") -> OutcomeQuote:
    return OutcomeQuote(venue=venue, venue_market_id=f"{venue}:{outcome}", event_key=event_key,
                        outcome=outcome, bid=ask - 0.02, ask=ask, ask_size=500, bid_size=500,
                        ts=ts, quote_time=ts, meta={"side": "yes"})


class _Signal:
    event_key = "nfl:DEN|KC:2026-09-27"
    follower = "kalshi"
    outcome = "KC"
    leader = "robinhood"
    follower_ask = 0.47
    follower_all_in = 0.4785
    edge = 0.05
    tie_value = 0.5
    suggested_contracts = 10
    depth = 500


class PaperFillCausalityTests(unittest.TestCase):
    """A paper fill may only use a quote observed at or after the order was opened."""

    def setUp(self):
        self.book = LagPaperBook(store=None, fill_window_s=10.0)

    def _open_at(self, opened: float):
        order = self.book.open(_Signal(), opened)
        self.assertIsNotNone(order)
        return order

    def test_a_quote_observed_before_the_order_never_fills_it(self):
        order = self._open_at(100.0)
        stale = {"kalshi": [_quote(95.0)]}                       # inside the window, but from before
        self.book.observe(_Signal.event_key, stale, now=100.5)
        self.assertIsNone(order.filled_at, "a price from before the order existed cannot fill it")
        self.assertIsNone(order.fill_price)

    def test_a_quote_observed_after_the_order_still_fills_it(self):
        order = self._open_at(100.0)
        self.book.observe(_Signal.event_key, {"kalshi": [_quote(100.5)]}, now=101.0)
        self.assertEqual((order.filled_at, order.fill_price), (101.0, 0.47))

    def test_a_quote_stamped_exactly_at_the_open_fills_it(self):
        order = self._open_at(100.0)
        self.book.observe(_Signal.event_key, {"kalshi": [_quote(100.0)]}, now=100.4)
        self.assertIsNotNone(order.filled_at, "the boundary is inclusive")


class LockHedgeCausalityTests(unittest.TestCase):
    """A lock leg may only be priced from a quote observed after the position opened."""

    def _book(self):
        from arb_engine.strategy.laglock import LagLockBook
        return LagLockBook(store=None, executor=None, fresh_s=10.0)

    def _position(self, book, opened: float):
        return book.open(key=f"lock-{opened}", event_key="nfl:DEN|KC:2026-09-27", outcome="KC",
                         lock_outcome="DEN", venue="kalshi", contracts=10, entry_price=0.62,
                         entry_all_in=0.6370, now=opened, source="lag", entry_tie=0.5)

    def test_a_quote_from_before_the_position_never_hedges_it(self):
        book = self._book()
        p = self._position(book, 100.0)
        stale = {"kalshi": [_quote(95.0, ask=0.36, outcome="DEN")]}
        self.assertIsNone(book._cheapest(p, stale, now=100.5),
                          "a price seen before the position existed cannot hedge it")

    def test_a_quote_from_after_the_position_still_hedges_it(self):
        book = self._book()
        p = self._position(book, 100.0)
        fresh = {"kalshi": [_quote(100.5, ask=0.36, outcome="DEN")]}
        self.assertIsNotNone(book._cheapest(p, fresh, now=101.0))


class ArbBacktestLedgerTests(unittest.TestCase):
    """Liquidity taken later has not been taken yet."""

    def setUp(self):
        self.mod = _load_arb_backtest()

    def test_a_take_stamped_later_does_not_remove_liquidity_now(self):
        led = self.mod.Ledger()
        key = ("nfl:DEN|KC:2026-09-27", "kalshi", "KC")
        led.take(key, t=100.0, k=40)
        self.assertEqual(led.available(key, t=60.0, shown=50), 50,
                         "a take at t=100 cannot have consumed the book at t=60")

    def test_a_take_already_made_still_removes_liquidity(self):
        led = self.mod.Ledger()
        key = ("nfl:DEN|KC:2026-09-27", "kalshi", "KC")
        led.take(key, t=60.0, k=40)
        self.assertEqual(led.available(key, t=100.0, shown=50), 10)

    def test_a_take_older_than_the_window_has_been_replenished(self):
        led = self.mod.Ledger()
        key = ("nfl:DEN|KC:2026-09-27", "kalshi", "KC")
        led.take(key, t=0.0, k=40)
        self.assertEqual(led.available(key, t=self.mod.USED_WINDOW_S + 1.0, shown=50), 50)


if __name__ == "__main__":
    unittest.main()

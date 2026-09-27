"""Watch keys, order identity and the maker's per-market / per-game limits.

The maker's watch key is ``f"{event_key}|kalshi:{outcome}"`` and event keys contain ``|``
themselves (``nfl:BUF|DET:2026-09-20:spread:BUF-1.5``). Cutting a key at its first ``|`` kept
``nfl:BUF`` - no opponent, date or market: the maker's resting-order count never matched the
event key its limit looked up (so the limit reset every tick), and the ledger booked every
BUF game, market and date under one ``game_key``. These tests pin the full identity through
several reconciliation ticks and a restart.
"""
from __future__ import annotations

import json
import unittest
from decimal import Decimal

from arb_engine.fees import KalshiFees, RobinhoodFees
from arb_engine.strategy.alerts import Alerter
from arb_engine.strategy.broker import PaperBroker, RestingOrder, order_identity, watch_identity, watch_key_for
from arb_engine.strategy.maker import MakerConfig, MakerRunner, Watch
from arb_engine.venues.http import HttpError
from tests.test_order_ledger import DEMO_URL, Clock, FakeKalshi, maker, tmp

ML = "nfl:BUF|DET:2026-09-20"
SPREAD = "nfl:BUF|DET:2026-09-20:spread:BUF-1.5"
TOTAL = "nfl:BUF|DET:2026-09-20:total:44.5"
NEXT_WEEK = "nfl:BUF|MIA:2026-09-27"          # same team, another opponent and date
OTHER = "nfl:ARI|BUF:2026-10-04"               # BUF sorts second: used to be booked under "nfl:ARI"


def w(event_key, outcome, ticker, other, margin, price=0.40):
    x = Watch(key=watch_key_for(event_key, outcome), event_key=event_key, title=event_key, kalshi_ticker=ticker, kalshi_side="yes",
              kalshi_outcome=outcome, kalshi_label=outcome, kalshi_fee=KalshiFees(maker_fees=True), exchange_index=0,
              hedge_venue="robinhood", hedge_id="c-" + ticker, hedge_side="yes", hedge_outcome=other, hedge_label=other,
              hedge_fee=RobinhoodFees(), hedge_url=None, kalshi_bid=price, kalshi_ask=round(price + 0.02, 2), hedge_ask=0.55, hedge_size=1000.0)
    x.desired_price, x.margin_if_filled = price, margin
    return x


def slate():
    """Both outcomes of the BUF-DET moneyline, one of its spreads, one of its totals, and BUF's
    next two games - ranked by margin in this order."""
    return [w(ML, "BUF", "KXNFLGAME-26SEP20BUFDET-BUF", "DET", .050),
            w(NEXT_WEEK, "BUF", "KXNFLGAME-26SEP27BUFMIA-BUF", "MIA", .045),
            w(ML, "DET", "KXNFLGAME-26SEP20BUFDET-DET", "BUF", .040),
            w(OTHER, "BUF", "KXNFLGAME-26OCT04ARIBUF-BUF", "ARI", .035),
            w(SPREAD, "BUF-1.5", "KXNFLSPREAD-26SEP20BUFDET-BUF1", "DET+1.5", .030),
            w(TOTAL, "over", "KXNFLTOTAL-26SEP20BUFDET-45", "under", .020)]


class _Feed:
    kalshi = None


def runner(broker=None, **cfg):
    c = MakerConfig(size=10, min_margin=0.01, max_orders=20, max_notional=10_000, hedge_cash=0, interval=0, rescan=1e9, **cfg)
    r = MakerRunner(c, _Feed(), broker or PaperBroker(), Alerter(journal_path=tmp("maker.jsonl"), quiet=True, desktop=False, webhook=""), settings={})
    r.watches = {x.key: x for x in slate()}
    return r


def resting_by(r):
    out = {}
    for o in r.orders:
        if o.status == "resting":
            m, g = o.identity
            out.setdefault("market", {}).setdefault(m, 0)
            out["market"][m] += 1
            out.setdefault("game", {}).setdefault(g, 0)
            out["game"][g] += 1
    return out


class WatchKeyTests(unittest.TestCase):
    def test_a_watch_key_splits_on_its_last_kalshi_marker(self):
        for ev, oc in ((ML, "BUF"), (SPREAD, "BUF-1.5"), (TOTAL, "over"), ("ncaaf:TOW|DSU:2026-09-26:total:44.5", "under")):
            self.assertEqual(watch_identity(watch_key_for(ev, oc)), (ev, oc))
        self.assertEqual(order_identity(None, None, watch_key_for(SPREAD, "BUF-1.5")), (SPREAD, ML))
        self.assertEqual(order_identity(None, None, watch_key_for(TOTAL, "over")), (TOTAL, ML))
        # Different opponents and dates never collapse (the old cut gave "nfl:BUF" for all three).
        self.assertEqual(len({order_identity(None, None, watch_key_for(e, "BUF"))[1] for e in (ML, NEXT_WEEK, OTHER)}), 3)
        # A key without the marker is kept whole - never cut at a "|".
        self.assertEqual(watch_identity("democheck:KX-1|maker"), ("democheck:KX-1|maker", ""))
        self.assertEqual(RestingOrder("o", "t", "yes", .4, 10, watch_key=watch_key_for(SPREAD, "BUF-1.5")).identity, (SPREAD, ML))


class MakerLimitTests(unittest.TestCase):
    def test_one_order_per_market_holds_across_reconciliation_ticks(self):
        r = runner(max_per_event=1)
        for tick in range(4):
            r.reconcile()
            by = resting_by(r)
            self.assertEqual(max(by["market"].values()), 1, tick)            # never both moneyline outcomes
            self.assertEqual(sorted(by["market"]), sorted([ML, NEXT_WEEK, OTHER, SPREAD, TOTAL]), tick)
        self.assertEqual(len(r.orders), 5)                                     # nothing placed after the first tick
        ml = [o for o in r.orders if o.identity[0] == ML]
        self.assertEqual([o.ticker for o in ml], ["KXNFLGAME-26SEP20BUFDET-BUF"])   # the better margin

    def test_a_whole_game_limit_counts_every_market_of_that_game_and_nothing_else(self):
        r = runner(max_per_event=1, max_per_game=2)
        for tick in range(3):
            r.reconcile()
            by = resting_by(r)
            self.assertEqual(by["game"][ML], 2, tick)                           # moneyline + spread; the total waits
            self.assertEqual((by["game"][NEXT_WEEK], by["game"][OTHER]), (1, 1), tick)   # BUF's other games are not this one
        self.assertEqual(sorted(o.identity[0] for o in r.orders if o.identity[1] == ML), [ML, SPREAD])
        # The moneyline order fills: its game has room again, for the next-best market only.
        next(o for o in r.orders if o.identity[0] == ML).status = "filled"
        r.reconcile()
        self.assertEqual(sorted(o.identity[0] for o in r.orders if o.status == "resting" and o.identity[1] == ML), [ML, SPREAD])
        self.assertEqual(max(resting_by(r)["market"].values()), 1)

    def test_an_order_whose_cancel_failed_still_counts(self):
        class Stuck(PaperBroker):
            def cancel(self, order):
                raise RuntimeError("exchange busy")
        r = runner(Stuck(), max_per_event=1)
        r.reconcile()
        o = next(x for x in r.orders if x.identity[0] == ML)
        r.watches[o.watch_key].desired_price = 0.39            # re-price: the cancel fails, the old order still rests
        r.reconcile()
        self.assertEqual(o.status, "resting")
        self.assertEqual(sum(1 for x in r.orders if x.status == "resting" and x.identity[0] == ML), 1)


class LedgerAttributionTests(unittest.TestCase):
    def test_ledger_rows_keep_market_and_game_through_repeated_reconciliation(self):
        clock = Clock(1000.0)
        c = FakeKalshi(clock)
        b = maker(c, clock)
        r = runner(b, max_per_event=1)
        r.reconcile()
        placed = {o.intent_id: o for o in r.orders}
        spread = next(o for o in placed.values() if o.identity[0] == SPREAD)
        c.fill_resting(spread.order_id, 4)
        for t in (1003.0, 1010.0, 1020.0, 1030.0):          # poll + throttled reconcile, several times
            clock.t = t
            b.poll(r.orders, {})
            r.reconcile()
        rows = {row["intent_id"]: row for row in b.ledger.rows(limit=100)}
        self.assertEqual(len(rows), 5)
        for iid, o in placed.items():
            self.assertEqual((rows[iid]["event_key"], rows[iid]["game_key"]), o.identity)
            self.assertEqual(json.loads(rows[iid]["detail"])["watch"], o.watch_key)
        self.assertEqual({row["game_key"] for row in rows.values()}, {ML, NEXT_WEEK, OTHER})
        # Exposure (fills plus what the resting remainder reserves) splits exactly by game: the
        # BUF-DET game carries its three markets, BUF's other games one each, "nfl:BUF" nothing.
        exp = {g: b.ledger.exposure(strategy="maker", game_key=g) for g in (ML, NEXT_WEEK, OTHER, "nfl:BUF", "nfl:ARI")}
        self.assertEqual(exp[ML] + exp[NEXT_WEEK] + exp[OTHER], b.ledger.exposure(strategy="maker"))
        self.assertEqual((exp["nfl:BUF"], exp["nfl:ARI"]), (Decimal("0"), Decimal("0")))
        self.assertEqual(exp[NEXT_WEEK], exp[OTHER])
        self.assertEqual(exp[ML], 3 * exp[NEXT_WEEK])                   # three equal orders on that game, one elsewhere
        self.assertEqual(len([o for o in r.orders if o.status == "resting"]), 5)   # 4 of 10 filled, still resting

    def test_a_restart_counts_what_the_dead_process_left_until_it_is_gone(self):
        clock, path = Clock(1000.0), tmp()
        c = FakeKalshi(clock)
        a = runner(maker(c, clock, path, owner="maker-host:101"), max_per_event=1)
        a.watches = {x.key: x for x in slate()[:1]}             # the moneyline BUF order only
        a.reconcile()
        (old,) = a.orders
        # The process dies without its shutdown cancel. Its successor's recovery cancel fails once.
        clock.t = 1100.0
        b = maker(c, clock, path, owner="maker-host:202", owner_alive=lambda owner: owner != "maker-host:101")
        cancel = c.cancel_order

        def busy(*args, **kw):
            raise HttpError(503, DEMO_URL, "busy")
        c.cancel_order = busy
        b.recover()
        c.cancel_order = cancel
        self.assertEqual(c.rows[old.order_id]["status"], "resting")
        self.assertEqual([x["event_key"] for x in b.untracked_open()], [ML])
        r = runner(b, max_per_event=1)
        for _ in range(2):
            r.reconcile()
        # Neither moneyline outcome may rest while the predecessor's order does; every other market may.
        self.assertEqual(sorted(o.identity[0] for o in r.orders), sorted([NEXT_WEEK, OTHER, SPREAD, TOTAL]))
        # The next reconciliation cancels it and books its end: the market is free again.
        clock.t = 1110.0
        b.reconcile(force=True)
        clock.t = 1120.0
        b.reconcile(force=True)
        self.assertEqual(b.untracked_open(), [])
        r.reconcile()
        ml = [o for o in r.orders if o.status == "resting" and o.identity[0] == ML]
        self.assertEqual([o.ticker for o in ml], ["KXNFLGAME-26SEP20BUFDET-BUF"])
        self.assertEqual(max(resting_by(r)["market"].values()), 1)

    def test_another_live_makers_orders_count_too(self):
        clock, path = Clock(1000.0), tmp()
        c = FakeKalshi(clock)
        a = runner(maker(c, clock, path, owner="maker-host:101"), max_per_event=1, max_per_game=1)
        a.watches = {x.key: x for x in slate()[4:5]}            # the spread
        a.reconcile()
        b = runner(maker(c, clock, path, owner="maker-host:202", owner_alive=lambda owner: True), max_per_event=1, max_per_game=1)
        b.reconcile()
        # One order on the BUF-DET game across both processes; BUF's other games are separate.
        self.assertEqual(sorted(o.identity[0] for o in b.orders), sorted([NEXT_WEEK, OTHER]))

    def test_rows_written_with_a_cut_key_are_read_from_their_full_watch_key(self):
        clock = Clock(1000.0)
        c = FakeKalshi(clock)
        b = maker(c, clock)
        res = b.ledger.reserve(strategy="maker", ticker="KXNFLSPREAD-26SEP20BUFDET-BUF1", side="yes", count=10, limit_price="0.40",
                               tif="good_till_canceled", event_key="nfl:BUF", game_key="nfl:BUF", fee_multiplier=1,
                               detail={"watch": watch_key_for(SPREAD, "BUF-1.5"), "post_only": True})
        self.assertTrue(res.ok)
        self.assertEqual(b.untracked_open(), [{"intent_id": res.intent_id, "ticker": "KXNFLSPREAD-26SEP20BUFDET-BUF1", "event_key": SPREAD, "game_key": ML}])



class CliTests(unittest.TestCase):
    def test_max_per_game_reaches_the_config_and_defaults_off(self):
        import contextlib
        import io
        import os
        from unittest import mock

        from arb_engine.cli_plugins import maker_flags
        from tests.test_maker import CliPluginTests, _runner

        seen = {}

        def factory(cfg, settings, venues):
            seen["cfg"] = cfg
            r = _runner(cfg)
            r.scan_fn = lambda: []
            return r
        p, sub, parsers = CliPluginTests()._parser()
        maker_flags.register(sub, parsers)
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.dict(os.environ, {"EXECUTABLE_VENUES": "", "MAKER_HEDGE_CASH": "", "MAKER_HEDGE_VENUES": ""}):
            for argv, want in ((["maker"], (1, 0)), (["maker", "--max-per-event", "2", "--max-per-game", "3"], (2, 3))):
                maker_flags.run_maker(p.parse_args(argv + ["--iterations", "1", "--duration", "1", "--interval", "0"]), {}, runner_factory=factory)
                self.assertEqual((seen["cfg"].max_per_event, seen["cfg"].max_per_game), want)

if __name__ == "__main__":
    unittest.main()

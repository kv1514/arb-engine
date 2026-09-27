"""Lock legs are hedges, not exemptions: OrderLedger.reserve(parent_id=...) and
LagExecutor.buy_lock grant the budget exemption only to a proven hedge of the entry.

Ledger layer (``OrderLedger._lock_check``, atomic with the reservation): the entry is this
account's verified purchase with a recorded settlement identity; the lock buys the same
market (event key and Kalshi event ticker), pays on the other outcome, the pair's payoffs are
complementary, the quote is fresh, no related order is unknown, and the remaining inventory
(fill - exits - earlier lock legs) bounds it.

Caller layer (``LagExecutor.buy_lock``): the quote is a fresh Kalshi quote of the entry's
market priced at or under its ask, the lock contract's settlement identity comes from the
registry, and the exchange still shows the entry's contracts.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from arb_engine.execution.kalshi import KalshiExecutor
from arb_engine.execution.ledger import Identity, OrderLedger, settlement_identity
from arb_engine.models import OutcomeQuote
from arb_engine.strategy.lagexec import LagExecutor, can_tie, settlement_of
from arb_engine.venues.http import HttpError

DEMO_URL = "https://external-api.demo.kalshi.co/trade-api/v2"
KEY = "nfl:DEN|KC:2026-09-21"                    # game A (moneyline)
KEY_B = "nfl:BUF|MIA:2026-09-21"                 # game B, unrelated
KC, DEN = "KXNFLGAME-26SEP21DENKC-KC", "KXNFLGAME-26SEP21DENKC-DEN"
BUF = "KXNFLGAME-26SEP21BUFMIA-BUF"
KFEE = {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}
NO_QUOTE_TIME = object()


def tmp(name="ledger.sqlite3"):
    return os.path.join(tempfile.mkdtemp(prefix="arb_test_"), name)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def sd(outcome, side="yes", tie="0.5", event=KEY, tied=True, venue="kalshi"):
    return settlement_identity(venue, event, outcome, side, tie, tied)


class LedgerLockProofTests(unittest.TestCase):
    """OrderLedger.reserve(parent_id=...) - everything the ledger's records can prove."""

    def setUp(self):
        self.clock = Clock()
        self.led = OrderLedger(tmp(), "demo", DEMO_URL, clock=self.clock)
        self.led.bind(Identity(key_fp="key:test-A1", account_fp="account:test-A"))

    def tearDown(self):
        self.led.close()

    def entry(self, ticker=KC, outcome="KC", fill="10.00", event=KEY, side="yes", settlement="default"):
        r = self.led.reserve(strategy="lag", ticker=ticker, side=side, count=10, limit_price="0.60", event_key=event, game_key=event,
                             fee_multiplier=1, settlement=sd(outcome, side, event=event) if settlement == "default" else settlement)
        self.assertTrue(r.ok, r.reason)
        self.led.accepted(r.intent_id, {"order_id": f"e-{r.intent_id[:6]}", "fill_count": fill})
        return r.intent_id

    def lock(self, parent, ticker=DEN, outcome="DEN", side="yes", tie="0.5", event=KEY, count=10, quote_ts=None, settlement="default",
             action="buy", tied=True):
        return self.led.reserve(strategy="lock", ticker=ticker, side=side, count=count, limit_price="0.36", action=action, event_key=event,
                                parent_id=parent, fee_multiplier=1,
                                settlement=sd(outcome, side, tie, event, tied) if settlement == "default" else settlement,
                                quote_ts=None if quote_ts is NO_QUOTE_TIME else (self.clock() if quote_ts is None else quote_ts))

    def test_the_reported_case_an_unrelated_market_in_another_game_is_refused(self):
        p = self.entry()
        r = self.lock(p, ticker=BUF, outcome="BUF", event=KEY_B)
        self.assertFalse(r.ok)
        self.assertIn("unrelated market", r.reason)
        # The same ticker with a settlement identity that claims game A: the Kalshi event says otherwise.
        forged = self.lock(p, ticker=BUF, outcome="DEN", event=KEY)
        self.assertFalse(forged.ok)
        self.assertIn(f"not in the entry's Kalshi event KXNFLGAME-26SEP21DENKC", forged.reason)
        self.assertEqual(len(self.led.rows()), 1)                         # nothing reserved

    def test_a_proven_hedge_is_granted_and_bounded_by_the_entry(self):
        p = self.entry()
        r = self.lock(p, count=25)
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.count, 10)
        row = self.led.get(r.intent_id)
        self.assertEqual((row["strategy"], row["parent_id"]), ("lock", p))
        self.assertIn('"outcome": "DEN"', row["settlement"])

    def test_same_side_additions_are_refused(self):
        p = self.entry()
        own = self.lock(p, ticker=KC, outcome="KC")
        self.assertFalse(own.ok)
        self.assertIn("the lock buys the entry's own contract", own.reason)
        # A NO on DEN pays when DEN does not win - on KC: the same side, dressed as the other market.
        no_den = self.lock(p, ticker=DEN, outcome="KC", side="no")
        self.assertFalse(no_den.ok)
        self.assertIn("pays on the entry's own outcome KC", no_den.reason)

    def test_the_no_of_the_entrys_own_market_is_a_complement(self):
        p = self.entry()
        r = self.lock(p, ticker=KC, outcome="DEN", side="no", tie="0.5")   # NO-KC: $1 if DEN wins, 1 - 0.5 on a tie
        self.assertTrue(r.ok, r.reason)

    def test_payoffs_must_be_complementary_and_known(self):
        p = self.entry()
        short = self.lock(p, tie="0")                                     # 0.5 + 0 on a tie: loses half a dollar
        self.assertFalse(short.ok)
        self.assertIn("not complementary: the pair pays $0.5 on a tie", short.reason)
        unknown = self.lock(p, tie=None)
        self.assertFalse(unknown.ok)
        self.assertIn("what the pair pays on a tie is unknown", unknown.reason)
        # A market that cannot tie (college football) needs no tie payout at all.
        college = "ncaaf:ORE|WASH:2026-09-26"
        pc = self.entry(ticker="KXNCAAFGAME-26SEP26OREWASH-ORE", outcome="ORE", event=college, settlement=sd("ORE", tie=None, event=college, tied=False))
        ok = self.lock(pc, ticker="KXNCAAFGAME-26SEP26OREWASH-WASH", outcome="WASH", tie=None, event=college, tied=False)
        self.assertTrue(ok.ok, ok.reason)

    def test_unknown_settlement_identity_is_refused(self):
        p = self.entry()
        self.assertIn("unknown settlement identity of the lock contract", self.lock(p, settlement=None).reason)
        partial = {"venue": "kalshi", "event_key": KEY, "outcome": "DEN", "tie_payout": "0.5", "can_tie": True}   # no side
        self.assertIn("incomplete settlement identity", self.lock(p, settlement=partial).reason)
        bare = self.entry(settlement=None)                                # an entry recorded without one
        self.assertIn("the entry's settlement identity is unknown", self.lock(bare).reason)

    def test_an_outcome_the_market_does_not_name_is_refused(self):
        p = self.entry()
        r = self.lock(p, ticker=DEN, outcome="BUF")
        self.assertFalse(r.ok)
        self.assertIn("outcome not of this market", r.reason)

    def test_a_stale_or_undated_quote_is_refused(self):
        p = self.entry()
        stale = self.lock(p, quote_ts=self.clock() - 30.0)
        self.assertFalse(stale.ok)
        self.assertIn("stale quote: 30.0s old", stale.reason)
        self.assertIn("no quote time", self.lock(p, quote_ts=NO_QUOTE_TIME).reason)
        self.assertIn("in the future", self.lock(p, quote_ts=self.clock() + 60.0).reason)

    def test_remaining_inventory_counts_exits_and_earlier_hedges(self):
        p = self.entry()
        self.clock.t += 1
        # An exit: 3 of the entry's KC sold (a manual order - any strategy counts).
        sell = self.led.reserve(strategy="manual", ticker=KC, side="yes", action="sell", count=3, limit_price="0.70", max_cost_per_contract="0.32")
        self.led.accepted(sell.intent_id, {"order_id": "s1", "fill_count": "3.00"})
        # An earlier lock leg of this entry: 2 filled.
        first = self.lock(p, count=2)
        self.led.accepted(first.intent_id, {"order_id": "l1", "fill_count": "2.00"})
        # A NO of the entry's market bought by something else nets the position down too (in flight: full count).
        self.led.reserve(strategy="manual", ticker=KC, side="no", count=1, limit_price="0.40", fee_multiplier=1)
        r = self.lock(p, count=10)
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.count, 4)                                      # 10 - 3 exited - 2 hedged - 1 netted
        done = self.lock(p, count=10)
        self.assertFalse(done.ok)
        self.assertIn("nothing left to hedge (filled 10.00, exited 4.00, hedged or unresolved 6.00)", done.reason)

    def test_an_exit_placed_before_the_entry_is_not_its_exit(self):
        self.led.reserve(strategy="manual", ticker=KC, side="yes", action="sell", count=5, limit_price="0.70", max_cost_per_contract="0.32")
        self.clock.t += 1
        p = self.entry()
        self.assertEqual(self.lock(p).count, 10)

    def test_an_unrelated_unknown_order_does_not_stop_a_hedge_but_a_related_one_does(self):
        p = self.entry()
        other = self.led.reserve(strategy="lag", ticker=BUF, side="yes", count=5, limit_price="0.50", event_key=KEY_B, fee_multiplier=1,
                                 budget=None, settlement=sd("BUF", event=KEY_B))
        self.led.ambiguous(other.intent_id, "timeout")                    # game B's order: outcome unknown
        new_entry = self.led.reserve(strategy="lag", ticker=DEN, side="yes", count=5, limit_price="0.40", event_key=KEY, fee_multiplier=1)
        self.assertFalse(new_entry.ok)                                    # new exposure stays blocked...
        self.assertIn("unknown outcome", new_entry.reason)
        hedge = self.lock(p)
        self.assertTrue(hedge.ok, hedge.reason)                           # ...a proven hedge of game A is not
        self.led.ambiguous(hedge.intent_id, "timeout")                    # now its own lock leg is unknown
        again = self.lock(p)
        self.assertFalse(again.ok)
        self.assertIn("related order(s) with unknown outcome", again.reason)

    def test_an_exit_of_the_entrys_contract_with_an_unknown_outcome_stops_the_hedge(self):
        p = self.entry()
        sell = self.led.reserve(strategy="manual", ticker=KC, side="yes", action="sell", count=2, limit_price="0.70", max_cost_per_contract="0.32")
        self.led.ambiguous(sell.intent_id, "timeout")                     # did 2 KC go or not? The inventory is unknown
        r = self.lock(p)
        self.assertFalse(r.ok)
        self.assertIn("related order(s) with unknown outcome (manual KXNFLGAME-26SEP21DENKC-KC)", r.reason)

    def test_an_entry_the_client_cannot_prove_is_its_own_is_not_hedged(self):
        p = self.entry()
        self.led.bind(Identity(key_fp="key:test-X9", account_fp=None, error="GET /communications/id failed"))
        r = self.lock(p)
        self.assertFalse(r.ok)
        self.assertIn("not sent by this account", r.reason)

    def test_only_a_verified_purchase_can_be_hedged_and_only_by_a_purchase(self):
        pending = self.led.reserve(strategy="lag", ticker=KC, side="yes", count=10, limit_price="0.60", event_key=KEY, fee_multiplier=1, settlement=sd("KC"))
        self.assertIn("the entry's fill is not verified", self.lock(pending.intent_id).reason)
        self.assertIn("unknown entry intent", self.lock("no-such-intent").reason)
        p = self.entry()
        leg = self.lock(p)
        self.led.accepted(leg.intent_id, {"order_id": "l1", "fill_count": "10.00"})
        self.assertIn("the entry is itself a lock leg", self.lock(leg.intent_id, ticker=KC, outcome="KC").reason)
        sale = self.led.reserve(strategy="manual", ticker=KC, side="yes", action="sell", count=3, limit_price="0.70",
                                max_cost_per_contract="0.32", settlement=sd("KC"))
        self.led.accepted(sale.intent_id, {"order_id": "s1", "fill_count": "3.00"})
        self.assertIn("the entry is not a purchase", self.lock(sale.intent_id).reason)
        p3 = self.entry()
        self.assertIn("selling is an exit, not a hedge", self.lock(p3, action="sell").reason)


class FakeKalshi:
    """A demo account: IOC orders fill per ``fills``; positions per ``holding``."""

    env, base_url, has_credentials, api_key = "demo", DEMO_URL, True, "lock-caller-key"

    def __init__(self, fills=None, holding="10.00"):
        self.sent, self.fills, self.holding = [], list(fills or []), holding
        self.positions_error = None

    def create_order(self, payload):
        self.sent.append(payload)
        fill = self.fills.pop(0) if self.fills else payload["count"] + ".00"
        if isinstance(fill, BaseException):
            raise fill
        return {"order_id": f"o{len(self.sent)}", "fill_count": fill, "remaining_count": "0.00"}

    def positions(self, **params):
        if self.positions_error is not None:
            raise self.positions_error
        if self.holding is None:
            return {"market_positions": []}
        return {"market_positions": [{"ticker": params.get("ticker"), "position_fp": self.holding}]}

    def series(self, ticker):
        return {"ticker": ticker, "fee_type": "quadratic", "fee_multiplier": 1}

    def communications_id(self):
        return "comms-lock-caller"


def kq(ticker, outcome, ask, t, event=KEY, side="yes", venue="kalshi", **meta):
    return OutcomeQuote(venue, ticker, event, outcome, ask=ask, bid=round(ask - 0.01, 2), ask_size=500, ts=t, fee_params=KFEE,
                        meta={"ticker": ticker, "side": side, **meta})


class BuyLockCallerTests(unittest.TestCase):
    """LagExecutor.buy_lock, called directly - what the caller checks before the ledger."""

    def setUp(self):
        from arb_engine.strategy.leadlag import LagSignal

        self.clock = Clock()
        self.client = FakeKalshi()
        self.ex = LagExecutor(mode="demo", executor=KalshiExecutor(self.client), intents_path=tmp("lag.jsonl"), ledger_path=tmp(), clock=self.clock)
        sig = LagSignal(event_key=KEY, title="DEN @ KC", leader="robinhood", follower="kalshi", outcome="KC", label="Kansas City", lead_move=0.08,
                        follower_move=0.0, leader_mid=0.675, follower_ask=0.60, follower_all_in=0.617, edge=0.058, depth=300,
                        suggested_contracts=10, lag_s=0.0, ts=self.clock())
        self.entry = self.ex.on_signal(sig, {"kalshi": [kq(KC, "KC", 0.60, self.clock())]})
        self.assertEqual((self.entry["status"], self.entry["fill_count"]), ("SUBMITTED", "10.00"))
        self.pid = self.entry["intent_id"]
        self.sent_before = len(self.client.sent)

    def buy_lock(self, quote, count=10, price=None, event=KEY):
        return self.ex.buy_lock(quote, count, quote.ask if price is None else price, event, self.clock(), parent_id=self.pid)

    def test_the_entry_records_its_settlement_identity(self):
        row = self.ex.ledger.get(self.pid)
        self.assertIn('"outcome": "KC"', row["settlement"])
        self.assertIn('"tie_payout": "0.5"', row["settlement"])          # Kalshi NFL moneyline: half on a tie (registry)

    def test_a_proven_hedge_goes_out(self):
        rec = self.buy_lock(kq(DEN, "DEN", 0.36, self.clock()))
        self.assertEqual((rec["status"], rec["count"]), ("SUBMITTED", 10))
        self.assertEqual(self.client.sent[-1]["ticker"], DEN)

    def test_the_reported_case_an_unrelated_market_is_refused_by_the_caller_and_the_ledger(self):
        # The position's event, a quote from another game: the caller sees the mismatch.
        rec = self.buy_lock(kq(BUF, "BUF", 0.50, self.clock(), event=KEY_B))
        self.assertEqual(rec["status"], "skipped")
        self.assertIn("unrelated market: the quote is for nfl:BUF|MIA", rec["reason"])
        # Quote and event argument both game B: the entry is in game A.
        rec2 = self.ex.buy_lock(kq(BUF, "BUF", 0.50, self.clock(), event=KEY_B), 10, 0.50, KEY_B, self.clock(), parent_id=self.pid)
        self.assertIn("unrelated market: the entry is in nfl:DEN|KC", rec2["reason"])
        # A game-B ticker on a quote that claims game A: the ledger's Kalshi-event check refuses it.
        rec3 = self.buy_lock(kq(BUF, "DEN", 0.50, self.clock()))
        self.assertIn("not in the entry's Kalshi event", rec3["reason"])
        self.assertEqual(len(self.client.sent), self.sent_before)

    def test_a_same_side_addition_is_refused(self):
        rec = self.buy_lock(kq(KC, "KC", 0.61, self.clock()))
        self.assertEqual(rec["status"], "skipped")
        self.assertIn("same-side addition", rec["reason"])
        self.assertEqual(len(self.client.sent), self.sent_before)

    def test_the_no_of_the_entrys_market_is_a_hedge(self):
        rec = self.buy_lock(kq(KC, "DEN", 0.41, self.clock(), side="no", no_of="KC"))
        self.assertEqual(rec["status"], "SUBMITTED")
        self.assertEqual(self.client.sent[-1]["side"], "ask")              # buy NO = the YES ask side in V2

    def test_stale_undated_and_overpriced_quotes_are_refused(self):
        self.assertIn("stale quote: 30.0s old", self.buy_lock(kq(DEN, "DEN", 0.36, self.clock() - 30.0))["reason"])
        undated = kq(DEN, "DEN", 0.36, None)
        self.assertIn("the quote has no time", self.buy_lock(undated)["reason"])
        self.assertIn("is not at or under the quote's ask", self.buy_lock(kq(DEN, "DEN", 0.36, self.clock()), price=0.45)["reason"])
        self.assertIn("not a Kalshi quote", self.buy_lock(kq(DEN, "DEN", 0.36, self.clock(), venue="robinhood"))["reason"])
        self.assertEqual(len(self.client.sent), self.sent_before)

    def test_an_unknown_settlement_identity_is_refused(self):
        from unittest import mock

        with mock.patch("arb_engine.matching.settlement_rules.lookup", return_value=None):
            rec = self.buy_lock(kq(DEN, "DEN", 0.36, self.clock()))
        self.assertIn("unknown settlement identity (no settlement rule", rec["reason"])
        with mock.patch("arb_engine.matching.settlement_rules.lookup", return_value={"tie": "half", "status": "draft"}):
            rec = self.buy_lock(kq(DEN, "DEN", 0.36, self.clock()))
        self.assertIn("is not verified (draft)", rec["reason"])
        self.assertEqual(len(self.client.sent), self.sent_before)

    def test_the_exchange_must_still_show_the_entrys_contracts(self):
        self.client.holding = "4.00"                                       # 6 sold on the website, outside the ledger
        rec = self.buy_lock(kq(DEN, "DEN", 0.36, self.clock()))
        self.assertEqual((rec["status"], rec["count"], rec["exchange_holding"]), ("SUBMITTED", 4, "4.00"))
        self.client.holding = None                                         # nothing left
        self.clock.t += 11                                                 # (a fresh quote)
        self.assertIn("the exchange shows none of the entry's", self.buy_lock(kq(DEN, "DEN", 0.36, self.clock()))["reason"])
        self.client.positions_error = HttpError(503, DEMO_URL, "down")
        self.assertIn("cannot be verified on the exchange", self.buy_lock(kq(DEN, "DEN", 0.36, self.clock()))["reason"])

    def test_exits_and_earlier_hedges_are_counted(self):
        led = self.ex.ledger
        sell = led.reserve(strategy="manual", ticker=KC, side="yes", action="sell", count=3, limit_price="0.70", max_cost_per_contract="0.32")
        led.accepted(sell.intent_id, {"order_id": "s1", "fill_count": "3.00"})
        self.client.fills = ["2.00", "5.00"]
        first = self.buy_lock(kq(DEN, "DEN", 0.36, self.clock()), count=10)
        self.assertEqual((first["count"], first["fill_count"]), (7, "2.00"))     # 10 - 3 exited, of which 2 filled
        second = self.buy_lock(kq(DEN, "DEN", 0.36, self.clock()), count=10)
        self.assertEqual(second["count"], 5)                               # 10 - 3 exited - 2 hedged

    def test_a_legitimate_hedge_goes_out_while_an_unrelated_order_is_unknown(self):
        from arb_engine.strategy.leadlag import LagSignal

        self.client.fills = [HttpError(0, DEMO_URL, "curl: (28) Operation timed out")]
        sig_b = LagSignal(event_key=KEY_B, title="MIA @ BUF", leader="robinhood", follower="kalshi", outcome="BUF", label="Buffalo", lead_move=0.08,
                          follower_move=0.0, leader_mid=0.55, follower_ask=0.50, follower_all_in=0.52, edge=0.03, depth=300,
                          suggested_contracts=5, lag_s=0.0, ts=self.clock() + 0.5)
        unknown = self.ex.on_signal(sig_b, {"kalshi": [kq(BUF, "BUF", 0.50, self.clock(), event=KEY_B)]})
        self.assertEqual(unknown["status"], "UNKNOWN")
        rec = self.buy_lock(kq(DEN, "DEN", 0.36, self.clock()))
        self.assertEqual(rec["status"], "SUBMITTED")                      # game B's unknown order does not stop game A's hedge
        blocked = self.ex.on_signal(sig_b.__class__(**{**sig_b.__dict__, "ts": self.clock() + 1}), {"kalshi": [kq(BUF, "BUF", 0.50, self.clock(), event=KEY_B)]})
        self.assertEqual(blocked["status"], "blocked")                    # while new exposure stays blocked


class LockBookTests(unittest.TestCase):
    """strategy/laglock.py with the executor: a transient refusal keeps the watch; only a
    terminal one (attempts used up, nothing left to hedge) ends it."""

    def test_a_transient_refusal_after_a_partial_lock_keeps_watching(self):
        from arb_engine.strategy.laglock import LagLockBook
        from arb_engine.strategy.leadlag import LagSignal

        clock = Clock()
        client = FakeKalshi(fills=["10.00", "4.00", "6.00"])
        ex = LagExecutor(mode="demo", executor=KalshiExecutor(client), intents_path=tmp("lag.jsonl"), ledger_path=tmp(), clock=clock)
        sig = LagSignal(event_key=KEY, title="DEN @ KC", leader="robinhood", follower="kalshi", outcome="KC", label="Kansas City", lead_move=0.08,
                        follower_move=0.0, leader_mid=0.675, follower_ask=0.60, follower_all_in=0.617, edge=0.058, depth=300,
                        suggested_contracts=10, lag_s=0.0, ts=clock())
        entry = ex.on_signal(sig, {"kalshi": [kq(KC, "KC", 0.60, clock())]})
        book = LagLockBook(store=None, watch_s=600, executable={"kalshi"}, executor=ex, require_tie_safe=True)
        book.open("e1", KEY, "KC", "DEN", "kalshi", 10, 0.60, 0.617, clock(), "demo", entry_tie=0.5, parent_id=entry["intent_id"])
        clock.t += 5
        book.observe(KEY, {"kalshi": [kq(DEN, "DEN", 0.36, clock())]}, clock())          # 4 of 10 hedged
        self.assertEqual(book.positions[0].locked_contracts, 4)
        client.positions_error = HttpError(503, DEMO_URL, "demo hiccup")
        clock.t += 11
        lines = book.observe(KEY, {"kalshi": [kq(DEN, "DEN", 0.36, clock())]}, clock())  # the position read fails: not sent
        self.assertEqual(book.positions[0].status, "watching")                           # ...and the watch goes on
        self.assertIn("still watching", lines[0])
        client.positions_error = None
        clock.t += 11
        book.observe(KEY, {"kalshi": [kq(DEN, "DEN", 0.36, clock())]}, clock())          # the other 6
        self.assertEqual((book.positions[0].status, book.positions[0].locked_contracts), ("locked", 10))


class CanTieTests(unittest.TestCase):
    def test_which_markets_can_end_level(self):
        self.assertTrue(can_tie("nfl:DEN|KC:2026-09-21"))
        self.assertFalse(can_tie("ncaaf:ORE|WASH:2026-09-26"))
        self.assertTrue(can_tie("nhl:BOS|TOR:2026-10-10", {"tie": "half"}))      # the venue states a tie clause
        self.assertTrue(can_tie("xyz:A|B:2026-10-10"))                          # unknown sport: assume it can
        self.assertFalse(can_tie("nfl:DEN|KC:2026-09-21:spread:KC-3.5"))        # half-point line: no push
        self.assertTrue(can_tie("nfl:DEN|KC:2026-09-21:spread:KC-3"))           # integer line: push
        self.assertFalse(can_tie("nfl:DEN|KC:2026-09-21:total:47.5"))
        self.assertTrue(can_tie("nfl:DEN|KC:2026-09-21:total:47"))

    def test_settlement_of_a_kalshi_nfl_moneyline(self):
        s, why = settlement_of(kq(DEN, "DEN", 0.36, 1.0), KEY)
        self.assertIsNone(why)
        self.assertEqual((s["outcome"], s["side"], s["tie_payout"], s["can_tie"]), ("DEN", "yes", "0.5", True))
        no, _ = settlement_of(kq(KC, "DEN", 0.41, 1.0, side="no"), KEY)
        self.assertEqual((no["outcome"], no["side"], no["tie_payout"]), ("DEN", "no", "0.5"))
        spread, why = settlement_of(kq("KXNFLSPREAD-26SEP21DENKC-KC3", "KC", 0.5, 1.0, event=KEY + ":spread:KC-3"), KEY + ":spread:KC-3")
        self.assertIsNone(spread)                                           # an integer line can push; the rule states no payout
        self.assertIn("states no tie payout", why)


if __name__ == "__main__":
    unittest.main()

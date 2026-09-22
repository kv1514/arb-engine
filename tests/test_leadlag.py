"""Lead-lag signals (strategy/leadlag.py) and the ntfy alert sink (strategy/alerts.py)."""

import os
import unittest

from arb_engine.models import OutcomeQuote
from arb_engine.strategy.alerts import Alerter, ntfy_url
from arb_engine.strategy.leadlag import LeadLagTracker

KEY = "nfl:DEN|KC:2026-09-21"
KFEE = {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}


def _q(venue, outcome, bid, ask, ts, quote_time=None, size=500, side=None):
    meta = {"exchange": "rothera"} if venue == "robinhood" else {}
    if side:
        meta["side"] = side
    return OutcomeQuote(venue, f"{venue}-{outcome}", KEY, outcome, ask=ask, bid=bid, ask_size=size, ts=ts, quote_time=quote_time, meta=meta, fee_params=KFEE if venue == "kalshi" else ({"exchange": "rothera"} if venue == "robinhood" else {}))


def _book(t, kc_k, kc_rh, kc_pm=None, rh_qt=None):
    """Quotes for one poll: (bid, ask) on KC (the home side) per venue; DEN is the complement."""
    by = {
        "kalshi": [_q("kalshi", "KC", *kc_k, t), _q("kalshi", "DEN", 1 - kc_k[1], 1 - kc_k[0], t)],
        "robinhood": [_q("robinhood", "KC", *kc_rh, t, quote_time=rh_qt if rh_qt is not None else t), _q("robinhood", "DEN", 1 - kc_rh[1], 1 - kc_rh[0], t, quote_time=rh_qt if rh_qt is not None else t)],
    }
    if kc_pm:
        by["polymarket"] = [_q("polymarket", "KC", *kc_pm, t), _q("polymarket", "DEN", 1 - kc_pm[1], 1 - kc_pm[0], t)]
    return by


OUT, LABELS = ["DEN", "KC"], {"DEN": "Denver", "KC": "Kansas City"}


class LeadLagTests(unittest.TestCase):
    def _tracker(self, **kw):
        kw.setdefault("executable", {"kalshi", "robinhood"})
        return LeadLagTracker(move=0.05, window_s=30, min_edge=0.02, cooldown_s=60, fresh_s=10, **kw)

    def test_leader_moves_follower_lags_signals_the_follower(self):
        tr = self._tracker()
        for t in (0, 5, 10):
            self.assertEqual(tr.observe(KEY, "DEN @ KC", OUT, LABELS, _book(t, (0.59, 0.60), (0.58, 0.61)), now=t), [])
        # Robinhood reprices KC up 8c on a play; Kalshi still shows the old book.
        sigs = tr.observe(KEY, "DEN @ KC", OUT, LABELS, _book(15, (0.59, 0.60), (0.66, 0.69)), now=15, bankroll=500)
        self.assertEqual(len(sigs), 1)
        s = sigs[0]
        self.assertEqual((s.leader, s.follower, s.outcome), ("robinhood", "kalshi", "KC"))
        self.assertAlmostEqual(s.lead_move, 0.08, places=6)
        self.assertAlmostEqual(s.follower_ask, 0.60)
        self.assertGreater(s.edge, 0.02)                      # 0.675 leader mid vs 0.60 + Kalshi fee
        self.assertEqual(s.suggested_contracts, min(500, int(500 * 0.25 / 0.60)))
        self.assertIn("buy Kansas City on kalshi", s.text())
        # Same poll again within the cooldown and no bigger edge: silent.
        self.assertEqual(tr.observe(KEY, "DEN @ KC", OUT, LABELS, _book(20, (0.59, 0.60), (0.66, 0.69)), now=20), [])

    def test_follower_that_already_moved_is_not_a_lag(self):
        tr = self._tracker()
        tr.observe(KEY, "DEN @ KC", OUT, LABELS, _book(0, (0.59, 0.60), (0.58, 0.61)), now=0)
        # Both venues repriced together: nothing to buy.
        self.assertEqual(tr.observe(KEY, "DEN @ KC", OUT, LABELS, _book(10, (0.66, 0.67), (0.66, 0.69)), now=10), [])

    def test_downward_move_signals_the_other_side(self):
        tr = self._tracker()
        tr.observe(KEY, "DEN @ KC", OUT, LABELS, _book(0, (0.59, 0.60), (0.58, 0.61)), now=0)
        sigs = tr.observe(KEY, "DEN @ KC", OUT, LABELS, _book(10, (0.59, 0.60), (0.48, 0.51)), now=10)
        self.assertEqual([(s.follower, s.outcome) for s in sigs], [("kalshi", "DEN")])   # DEN on Kalshi still 0.40/0.41 vs Rothera mid 0.505
        self.assertAlmostEqual(sigs[0].follower_ask, 0.41)

    def test_non_executable_follower_and_stale_leader_are_skipped(self):
        tr = self._tracker(executable={"kalshi"})
        tr.observe(KEY, "DEN @ KC", OUT, LABELS, _book(0, (0.59, 0.60), (0.58, 0.61), kc_pm=(0.59, 0.60)), now=0)
        # Polymarket leads: Kalshi (executable) is signalled, Robinhood (not executable here) is not.
        sigs = tr.observe(KEY, "DEN @ KC", OUT, LABELS, _book(10, (0.59, 0.60), (0.58, 0.61), kc_pm=(0.67, 0.68)), now=10)
        self.assertEqual({(s.leader, s.follower) for s in sigs}, {("polymarket", "kalshi")})
        # A stale Robinhood print (quote_time 40 s old) never leads.
        tr2 = self._tracker()
        tr2.observe(KEY, "DEN @ KC", OUT, LABELS, _book(0, (0.59, 0.60), (0.58, 0.61)), now=0)
        self.assertEqual(tr2.observe(KEY, "DEN @ KC", OUT, LABELS, _book(10, (0.59, 0.60), (0.66, 0.69), rh_qt=-30), now=10), [])

    def test_no_side_rows_and_small_edges_are_ignored(self):
        tr = self._tracker()
        book = _book(0, (0.59, 0.60), (0.58, 0.61))
        book["robinhood"].append(_q("robinhood", "KC", 0.30, 0.40, 0, quote_time=0, side="no"))   # the other contract's NO: not a KC quote
        tr.observe(KEY, "DEN @ KC", OUT, LABELS, book, now=0)
        # Leader up 6c but the follower's ask is already within 2c of the new mid: no edge.
        self.assertEqual(tr.observe(KEY, "DEN @ KC", OUT, LABELS, _book(10, (0.62, 0.63), (0.64, 0.66)), now=10), [])


class NtfyTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.path = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"alerts_test_{os.getpid()}.jsonl")

    def _alerter(self, **kw):
        kw.setdefault("ntfy", "arb-test-topic")
        return Alerter(journal_path=self.path, quiet=True, desktop=False, webhook="", transport=lambda url, body, headers: self.sent.append((url, body.decode(), headers)), **kw)

    def test_topic_becomes_ntfy_sh_url_and_default_kinds_push(self):
        self.assertEqual(ntfy_url("arb-x"), "https://ntfy.sh/arb-x")
        self.assertEqual(ntfy_url("https://ntfy.example.org/t"), "https://ntfy.example.org/t")
        self.assertIsNone(ntfy_url(""))
        a = self._alerter()
        a.alert("ARB", "PHI vs TEN: ARB +1.2% after fees", event="nfl:PHI|TEN")
        self.assertEqual(len(self.sent), 1)
        url, body, headers = self.sent[0]
        self.assertEqual(url, "https://ntfy.sh/arb-test-topic")
        self.assertIn("ARB +1.2%", body)
        self.assertEqual((headers["Title"], headers["Priority"]), ("ARB", "4"))
        # STEAL is not pushed unless opted in.
        a.alert("STEAL", "x", event="nfl:PHI|TEN")
        self.assertEqual(len(self.sent), 1)
        b = self._alerter(ntfy_kinds=["ARB", "STEAL"])
        b.alert("STEAL", "y", event="nfl:PHI|TEN")
        self.assertEqual(len(self.sent), 2)

    def test_throttle_per_title_event_side_but_never_hedge_now(self):
        a = self._alerter(min_interval_s=60)
        a.alert("LAG", "one", event="e1", outcome="KC", venue="kalshi", ask=0.6, all_in=0.61, fair=0.68)
        a.alert("LAG", "two", event="e1", outcome="KC", venue="kalshi", ask=0.6, all_in=0.61, fair=0.68)   # throttled
        a.alert("LAG", "three", event="e1", outcome="DEN", venue="kalshi", ask=0.4, all_in=0.41, fair=0.5)  # other side: pushed
        self.assertEqual([b for _, b, _ in self.sent], ["one", "three"])
        self.assertTrue(any(e["kind"] == "ntfy_throttled" for e in a.events))
        for i in range(3):
            a.alert("HEDGE NOW", f"hedge {i}", watch="w1")
        self.assertEqual(len(self.sent), 5)
        self.assertEqual(self.sent[-1][2]["Priority"], "5")

    def test_transport_failure_is_journalled_not_raised(self):
        def boom(url, body, headers):
            raise OSError("ntfy down")
        a = Alerter(journal_path=self.path, quiet=True, desktop=False, webhook="", ntfy="t", transport=boom)
        a.alert("ARB", "x", event="e")
        self.assertTrue(any(e["kind"] == "ntfy_error" for e in a.events))

    def test_no_topic_means_no_push(self):
        a = Alerter(journal_path=self.path, quiet=True, desktop=False, webhook="", ntfy="", transport=lambda *x: self.sent.append(x))
        a.alert("ARB", "x", event="e")
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()


class LiveWiringTests(unittest.TestCase):
    def test_lag_alert_is_journalled_and_recorded_with_signal_kind(self):
        """The slate's LAG alert carries structured fields; ``signal_kind`` (never ``kind``,
        which is Alerter.journal's positional) reaches the store's extra_json."""
        import json
        from arb_engine.store import Store

        db = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"lag_store_{os.getpid()}.db")
        if os.path.exists(db):
            os.remove(db)
        st = Store(db)
        a = Alerter(journal_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), f"lag_alerts_{os.getpid()}.jsonl"), quiet=True, desktop=False, webhook="", ntfy="", store=st)
        a.alert("LAG", "DEN @ KC: LAG …", event=KEY, outcome="KC", venue="kalshi", ask=0.60, all_in=0.617, fair=0.675, edge=0.058, market_p=0.675, suggested_contracts=100, signal_kind="lag", leader="robinhood", lead_move=0.08, follower_move=0.0, ts=1_800_000_000.0)
        rows = st.conn.execute("select outcome, venue, extra_json from steal_observations").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0][0], rows[0][1]), ("KC", "kalshi"))
        self.assertEqual(json.loads(rows[0][2])["signal_kind"], "lag")
        st.close() if hasattr(st, "close") else None



class FastLaneTests(unittest.TestCase):
    """strategy/fastlane.py: batched 1 s refreshes that keep the snapshot's quote objects."""

    def _kalshi_client(self, prices):
        calls = []

        class C:
            def get(self, path, params=None, **kw):
                calls.append((path, dict(params or {})))
                out = []
                for t in params["tickers"].split(","):
                    if t in prices:
                        b, a = prices[t]
                        out.append({"ticker": t, "yes_bid_dollars": str(b), "yes_ask_dollars": str(a), "yes_ask_size_fp": "120.00", "yes_bid_size_fp": "80.00", "last_price_dollars": str(b)})
                return {"markets": out}
        return C(), calls

    def _robinhood(self, quotes):
        class R:
            venue = "robinhood"

            def quotes(self, ids, workers=8):
                return {i: quotes[i] for i in ids if i in quotes}
        return R()

    def test_refresh_kalshi_updates_prices_keeps_fee_params_and_no_rows(self):
        from arb_engine.strategy.fastlane import refresh_kalshi

        q_yes = OutcomeQuote("kalshi", "KXNFLGAME-26SEP21DENKC-KC", KEY, "KC", ask=0.60, bid=0.59, ask_size=10, ts=1.0, fee_params=KFEE, meta={"ticker": "KXNFLGAME-26SEP21DENKC-KC", "side": "yes"})
        q_den = OutcomeQuote("kalshi", "KXNFLGAME-26SEP21DENKC-DEN", KEY, "DEN", ask=0.41, bid=0.40, ts=1.0, fee_params=KFEE, meta={"ticker": "KXNFLGAME-26SEP21DENKC-DEN", "side": "yes"})
        client, calls = self._kalshi_client({"KXNFLGAME-26SEP21DENKC-KC": (0.66, 0.67), "KXNFLGAME-26SEP21DENKC-DEN": (0.33, 0.34)})
        out = refresh_kalshi(client, [q_yes, q_den], now=50.0)
        self.assertEqual(len(calls), 1)                       # one batched call for both tickers
        self.assertEqual(sorted(calls[0][1]["tickers"].split(",")), sorted([q_den.meta["ticker"], q_yes.meta["ticker"]]))
        kc = next(q for q in out if q.outcome == "KC")
        self.assertEqual((kc.bid, kc.ask, kc.ask_size, kc.bid_size, kc.ts), (0.66, 0.67, 120.0, 80.0, 50.0))
        self.assertEqual(kc.fee_params, KFEE)                 # untouched: fees, book id, ids
        self.assertEqual(kc.venue_market_id, q_yes.venue_market_id)
        # A ticker missing from the answer keeps its old quote.
        client2, _ = self._kalshi_client({"KXNFLGAME-26SEP21DENKC-KC": (0.70, 0.71)})
        out2 = refresh_kalshi(client2, [q_yes, q_den], now=60.0)
        self.assertEqual(next(q for q in out2 if q.outcome == "DEN").ask, 0.41)

    def test_refresh_robinhood_updates_yes_and_no_rows_with_quote_time(self):
        from arb_engine.strategy.fastlane import refresh_robinhood

        yes = OutcomeQuote("robinhood", "cid-1", KEY, "KC", ask=0.61, bid=0.58, ts=1.0, fee_params={"exchange": "rothera"}, meta={"contract_id": "cid-1", "side": "yes", "exchange": "rothera"}, book_id="rothera")
        no = OutcomeQuote("robinhood", "cid-1#no", KEY, "DEN", ask=0.42, bid=0.39, ts=1.0, fee_params={"exchange": "rothera"}, meta={"contract_id": "cid-1", "side": "no", "exchange": "rothera"}, book_id="rothera")
        rh = self._robinhood({"cid-1": {"yes_ask_price": "0.69", "yes_bid_price": "0.66", "no_ask_price": "0.34", "no_bid_price": "0.31", "ask_size": "300", "bid_size": "250", "ask_venue_timestamp": "2026-09-21T02:00:00Z", "state": "active"}})
        out = refresh_robinhood(rh, [yes, no], now=70.0)
        y = next(q for q in out if q.meta["side"] == "yes")
        n = next(q for q in out if q.meta["side"] == "no")
        self.assertEqual((y.ask, y.bid, y.ask_size, y.bid_size), (0.69, 0.66, 300.0, 250.0))
        self.assertEqual((n.ask, n.bid, n.ask_size, n.bid_size), (0.34, 0.31, 250.0, 300.0))   # NO's ask depth is the YES bid depth
        self.assertIsNotNone(y.quote_time)
        self.assertEqual(y.book_id, "rothera")

    def test_lane_step_merges_refreshed_venues_and_survives_a_failing_one(self):
        from arb_engine.strategy.fastlane import FastLane

        kq = OutcomeQuote("kalshi", "T-KC", KEY, "KC", ask=0.60, bid=0.59, ts=1.0, fee_params=KFEE, meta={"ticker": "T-KC", "side": "yes"})
        rq = OutcomeQuote("robinhood", "c1", KEY, "KC", ask=0.61, bid=0.58, ts=1.0, meta={"contract_id": "c1", "side": "yes", "exchange": "rothera"})
        pq = OutcomeQuote("polymarket", "tok", KEY, "KC", ask=0.62, bid=0.60, ts=1.0)
        client, _ = self._kalshi_client({"T-KC": (0.63, 0.64)})

        class Boom:
            venue = "robinhood"

            def quotes(self, ids, workers=8):
                raise RuntimeError("robinhood 503")
        lane = FastLane(kalshi_client=client, robinhood=Boom(), clock=lambda: 100.0)
        lane.seed({KEY: {"kalshi": [kq], "robinhood": [rq], "polymarket": [pq]}})
        by, errs = lane.step()
        self.assertEqual(by[KEY]["kalshi"][0].ask, 0.64)       # refreshed
        self.assertEqual(by[KEY]["robinhood"][0].ask, 0.61)    # kept: the venue failed
        self.assertEqual(by[KEY]["polymarket"][0].ask, 0.62)   # never refreshed on the lane
        self.assertEqual(len(errs), 1)
        self.assertIn("robinhood 503", errs[0])
        self.assertEqual(lane.steps, 1)

    def test_slate_fast_step_runs_signals_on_refreshed_quotes(self):
        """A LiveSlate with fast=1 sees the lane's fresh Kalshi book: a Rothera-led move that
        the full tick missed becomes a LAG on the very next second."""
        from arb_engine.matching.matcher import MergedEvent
        from arb_engine.models import EventInfo
        from arb_engine.strategy.inplay import InplayView
        from arb_engine.strategy.live import LiveSlate

        info = EventInfo(event_key=KEY, sport="nfl", market_type="moneyline", outcomes=OUT, labels=LABELS, in_play=True)
        t0 = 1_800_000_000.0
        kq = [OutcomeQuote("kalshi", "T-KC", KEY, "KC", ask=0.60, bid=0.59, ask_size=400, ts=t0, fee_params=KFEE, meta={"ticker": "T-KC", "side": "yes"}), OutcomeQuote("kalshi", "T-DEN", KEY, "DEN", ask=0.41, bid=0.40, ask_size=400, ts=t0, fee_params=KFEE, meta={"ticker": "T-DEN", "side": "yes"})]
        rq = [OutcomeQuote("robinhood", "c1", KEY, "KC", ask=0.61, bid=0.58, ts=t0, quote_time=t0, fee_params={"exchange": "rothera"}, meta={"contract_id": "c1", "side": "yes", "exchange": "rothera"}, book_id="rothera"), OutcomeQuote("robinhood", "c2", KEY, "DEN", ask=0.42, bid=0.39, ts=t0, quote_time=t0, fee_params={"exchange": "rothera"}, meta={"contract_id": "c2", "side": "yes", "exchange": "rothera"}, book_id="rothera")]
        me = MergedEvent(KEY, info, {"kalshi": kq, "robinhood": rq})
        rh_prices = {"c1": {"yes_ask_price": "0.61", "yes_bid_price": "0.58", "ask_venue_timestamp": t0}, "c2": {"yes_ask_price": "0.42", "yes_bid_price": "0.39", "ask_venue_timestamp": t0}}
        client, _ = self._kalshi_client({"T-KC": (0.59, 0.60), "T-DEN": (0.40, 0.41)})
        rh = self._robinhood(rh_prices)
        rh.client = None
        slate = LiveSlate([], settings={"executable_venues": "kalshi,robinhood"}, alerter=Alerter(journal_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), f"fast_{os.getpid()}.jsonl"), quiet=True, desktop=False, webhook="", ntfy=""), bankroll=500, fast=1.0, interval=5.0)
        slate.fastlane.kalshi, slate.fastlane.robinhood = client, rh
        view = InplayView(event_key=KEY, title="DEN @ KC", live=True, game_line="Q2", fair_line="", sides=[], actions=[], blend={}, game_state={"period": 2}, total_cost=0.0, payout_if={}, locked_pnl=None, balanced=False)
        slate._live_priced = {KEY: (None, me, view)}
        slate.fastlane.seed({KEY: me.quotes_by_venue})
        # Second 1: nothing moved.
        self.assertEqual(slate.fast_step(t0 + 1).lags, [])
        # Second 2: Rothera reprices KC +8c (fresh venue timestamp); Kalshi's book is unchanged.
        rh_prices["c1"].update({"yes_ask_price": "0.69", "yes_bid_price": "0.66", "ask_venue_timestamp": t0 + 2})
        rh_prices["c2"].update({"yes_ask_price": "0.34", "yes_bid_price": "0.31", "ask_venue_timestamp": t0 + 2})
        lags, arbs, errors = [], [], []
        for t in (2, 3):
            ft = slate.fast_step(t0 + t)
            lags += ft.lags
            arbs += ft.arbs
            errors += ft.errors
        self.assertTrue(any("buy Kansas City on kalshi at 0.60" in l for l in lags), lags)
        # The same stale Kalshi book is also a fresh two-leg lock (DEN 0.34 on Rothera + KC 0.60 on Kalshi).
        self.assertTrue(any("ARB +" in a and "KC on kalshi @ 0.60" in a for a in arbs), arbs)
        self.assertEqual(errors, [])
        self.assertTrue(any(e["kind"] == "alert" and e["title"] == "LAG" for e in slate.alerts.events))


class PaperLagTests(unittest.TestCase):
    """strategy/paperlag.py: paper fills judged on the next quotes."""

    def _sig(self, ask=0.60, depth=300, contracts=100, t=1000.0):
        from arb_engine.strategy.leadlag import LagSignal
        return LagSignal(event_key=KEY, title="DEN @ KC", leader="robinhood", follower="kalshi", outcome="KC", label="Kansas City", lead_move=0.08, follower_move=0.0, leader_mid=0.675, follower_ask=ask, follower_all_in=ask + 0.017, edge=0.058, depth=depth, suggested_contracts=contracts, lag_s=0.0, ts=t)

    def _quotes(self, t, ask, bid, size=300):
        return {"kalshi": [_q("kalshi", "KC", bid, ask, t, size=size), _q("kalshi", "DEN", 1 - ask, 1 - bid, t, size=size)]}

    def test_fill_mark_settle_and_summary(self):
        from arb_engine.store import Store
        from arb_engine.strategy.paperlag import LagPaperBook

        db = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"paperlag_{os.getpid()}.db")
        if os.path.exists(db):
            os.remove(db)
        book = LagPaperBook(store=Store(db), fill_window_s=10)
        o = book.open(self._sig(), 1000.0)
        self.assertIsNotNone(o)
        self.assertIsNone(book.open(self._sig(), 1001.0))                   # one open order per (event, follower, side)
        # +1 s: the ask is still there -> filled at 0.60.
        lines = book.observe(KEY, self._quotes(1001.0, 0.60, 0.59), 1001.0)
        self.assertTrue(lines and "filled 100 x KC on kalshi @ 0.60" in lines[0], lines)
        self.assertEqual(o.fill_price, 0.60)
        # Marks at +30 / +60 s from the bid; settlement from the final score.
        book.observe(KEY, self._quotes(1031.0, 0.67, 0.66), 1031.0)
        book.observe(KEY, self._quotes(1062.0, 0.69, 0.68), 1062.0)
        self.assertEqual((o.marks.get("bid_30"), o.marks.get("bid_60")), (0.66, 0.68))
        self.assertEqual(book.settle(KEY, "KC"), 1)
        s = book.summary()
        self.assertEqual((s["orders"], s["filled"], s["expired"]), (1, 1, 0))
        self.assertAlmostEqual(s["pnl_bid_60"]["mean"], 0.68 - 0.617, places=6)
        self.assertAlmostEqual(s["pnl_settle"]["mean"], 1.0 - 0.617, places=6)
        row = book.store.conn.execute("select filled_at, fill_price, bid_60, settled, settle_value, pnl_settle from lag_paper").fetchone()
        self.assertEqual((row[1], row[2], row[3], row[4]), (0.60, 0.68, 1, 1.0))
        self.assertAlmostEqual(row[5], 1.0 - 0.617, places=4)

    def test_order_expires_when_the_ask_is_gone(self):
        from arb_engine.strategy.paperlag import LagPaperBook

        book = LagPaperBook(store=None, fill_window_s=10)
        book.open(self._sig(ask=0.60), 1000.0)
        # Kalshi caught up within a second: ask 0.67, never back to 0.60.
        for t in (1001.0, 1005.0, 1009.0):
            self.assertEqual(book.observe(KEY, self._quotes(t, 0.67, 0.66), t), [])
        lines = book.observe(KEY, self._quotes(1011.0, 0.67, 0.66), 1011.0)
        self.assertTrue(lines and "expired unfilled" in lines[0], lines)
        self.assertEqual(book.summary()["expired"], 1)
        # A zero-size ask does not fill either.
        book.open(self._sig(ask=0.60, t=1020.0), 1020.0)
        self.assertEqual(book.observe(KEY, self._quotes(1021.0, 0.60, 0.59, size=0), 1021.0), [])


class RepricingTests(unittest.TestCase):
    def test_a_pulled_ask_does_not_lead(self):
        """Only a two-sided move counts as the leader repricing: an ask that jumps while the
        bid stays (a pulled quote / widened spread) moves the mid but is not a price."""
        tr = LeadLagTracker(move=0.05, window_s=30, min_edge=0.02, cooldown_s=60, fresh_s=10, executable={"kalshi", "robinhood"})
        tr.observe(KEY, "DEN @ KC", OUT, LABELS, _book(0, (0.59, 0.60), (0.58, 0.61)), now=0)
        # Rothera ask 0.61 -> 0.75, bid unchanged: mid +7c but no repricing.
        self.assertEqual(tr.observe(KEY, "DEN @ KC", OUT, LABELS, _book(10, (0.59, 0.60), (0.58, 0.75)), now=10), [])
        # Both sides up: a real move.
        sigs = tr.observe(KEY, "DEN @ KC", OUT, LABELS, _book(20, (0.59, 0.60), (0.66, 0.69)), now=20)
        self.assertEqual([(s.leader, s.follower) for s in sigs], [("robinhood", "kalshi")])
        self.assertIsNone(sigs[0].url)   # the fixture quotes carry no url; the analyzer's do


class LagExecutorTests(unittest.TestCase):
    """strategy/lagexec.py: intents, caps, IOC plans through a fake executor."""

    def _sig(self, follower="kalshi", ask=0.60, edge=0.058, contracts=100, depth=300, event=KEY):
        from arb_engine.strategy.leadlag import LagSignal
        return LagSignal(event_key=event, title="DEN @ KC", leader="robinhood", follower=follower, outcome="KC", label="Kansas City", lead_move=0.08, follower_move=0.0, leader_mid=0.675, follower_ask=ask, follower_all_in=ask + 0.017, edge=edge, depth=depth, suggested_contracts=contracts, lag_s=0.0, ts=1000.0)

    def _quotes(self):
        return {"kalshi": [OutcomeQuote("kalshi", "KXNFLGAME-26SEP21DENKC-KC", KEY, "KC", ask=0.60, bid=0.59, meta={"ticker": "KXNFLGAME-26SEP21DENKC-KC", "side": "yes", "exchange_index": 3}), OutcomeQuote("kalshi", "KXNFLGAME-26SEP21DENKC-DEN", KEY, "DEN", ask=0.41, bid=0.40, meta={"ticker": "KXNFLGAME-26SEP21DENKC-DEN", "side": "yes"})]}

    def test_intent_mode_journals_without_an_executor(self):
        from arb_engine.strategy.lagexec import LagExecutor

        path = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"lag_intents_{os.getpid()}.jsonl")
        if os.path.exists(path):
            os.remove(path)
        ex = LagExecutor(mode="intent", intents_path=path, max_contracts=50, clock=lambda: 1000.0)
        rec = ex.on_signal(self._sig(), self._quotes())
        self.assertEqual((rec["status"], rec["ticker"], rec["side"], rec["count"]), ("intent", "KXNFLGAME-26SEP21DENKC-KC", "yes", 50))  # 100 suggested, capped at 50
        with open(path) as f:
            self.assertEqual(len(f.read().splitlines()), 1)
        self.assertEqual(ex.on_signal(self._sig(follower="robinhood"), self._quotes())["reason"], "follower is not kalshi")
        self.assertIn("edge", ex.on_signal(self._sig(edge=0.01), self._quotes())["reason"])
        self.assertIsNone(LagExecutor(mode="off").on_signal(self._sig(), self._quotes()))

    def test_demo_mode_sends_an_ioc_buy_and_respects_caps(self):
        from arb_engine.execution.kalshi import KalshiExecutor
        from arb_engine.strategy.lagexec import LagExecutor

        sent = []

        class Client:
            env, base_url, has_credentials = "demo", "https://demo", True

            def create_order(self, payload):
                sent.append(payload)
                return {"order_id": f"o{len(sent)}", "fill_count": int(float(payload["count"])), "remaining_count": 0}  # V2 sends counts as fixed-point strings
        ex = LagExecutor(mode="demo", executor=KalshiExecutor(Client()), intents_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), f"lag_demo_{os.getpid()}.jsonl"), max_contracts=50, max_notional_per_game=45.0, daily_notional=100.0, clock=lambda: 1000.0)
        rec = ex.on_signal(self._sig(), self._quotes())
        self.assertEqual(rec["status"], "SUBMITTED")
        self.assertEqual((rec["count"], rec["fill_count"], rec["order_id"]), (50, 50, "o1"))
        self.assertEqual(rec["count"] * 0.60, 30.0)
        p = sent[0]
        # V2 spells a YES buy as side=bid with fixed-point strings; IOC never rests, so no expiry.
        self.assertEqual((p["ticker"], p["side"], int(float(p["count"])), p["price"]), ("KXNFLGAME-26SEP21DENKC-KC", "bid", 50, "0.6000"))
        self.assertEqual(p.get("time_in_force"), "immediate_or_cancel")
        self.assertFalse(p.get("post_only"))
        self.assertNotIn("expiration_time", p)
        self.assertEqual(p.get("exchange_index"), 3)
        # Per-game cap: $45 with $30 already sent leaves $15 -> 25 contracts at 0.60.
        rec2 = ex.on_signal(self._sig(), self._quotes())
        self.assertEqual((rec2["status"], rec2["count"]), ("SUBMITTED", 25))
        self.assertEqual(ex.on_signal(self._sig(), self._quotes())["reason"], "notional cap reached")
        # Another game still has room under the daily cap ($100 - $45).
        rec4 = ex.on_signal(self._sig(event="nfl:BUF|MIA:2026-09-21"), {"kalshi": [OutcomeQuote("kalshi", "T-KC", "nfl:BUF|MIA:2026-09-21", "KC", ask=0.60, bid=0.59, meta={"ticker": "T-KC", "side": "yes"})]})
        self.assertEqual((rec4["status"], rec4["count"]), ("SUBMITTED", 50))
        self.assertAlmostEqual(ex.sent_notional, 75.0)

    def test_live_mode_needs_the_flag_and_no_row_is_skipped(self):
        from arb_engine.strategy.lagexec import LagExecutor

        os.environ.pop("ARB_LIVE_TRADING", None)
        with self.assertRaises(RuntimeError):
            LagExecutor(mode="live", executor=object())
        ex = LagExecutor(mode="intent", intents_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), f"lag_i2_{os.getpid()}.jsonl"))
        # Only a NO row for the outcome: nothing to buy on Kalshi's own market.
        q = {"kalshi": [OutcomeQuote("kalshi", "T-DEN#no", KEY, "KC", ask=0.60, bid=0.59, meta={"ticker": "T-DEN", "side": "no"})]}
        self.assertEqual(ex.on_signal(self._sig(), q)["reason"], "no Kalshi YES row for this outcome")

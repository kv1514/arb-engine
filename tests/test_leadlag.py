"""Lead-lag signals (strategy/leadlag.py) and the ntfy alert sink (strategy/alerts.py)."""

import os
import threading
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
        self.assertEqual(s.suggested_contracts, 202)
        self.assertLessEqual(s.suggested_contracts * s.follower_ask + s.fee_total, 125)
        self.assertAlmostEqual(s.follower_all_in, s.follower_ask + s.fee_total / s.suggested_contracts)
        # The text is the order ticket: sport, venue, count, price, the fee for *that* order
        # (Kalshi rounds up per order, so it is not 208 x the per-contract fee) and the cash.
        txt = s.text()
        self.assertTrue(txt.startswith("NFL - DEN @ KC - LAG:"), txt)
        self.assertIn(f"KALSHI: buy {s.suggested_contracts} x Kansas City at the ask $0.60", txt)
        self.assertTrue(s.fee_detail and abs(sum(d["amount"] for d in s.fee_detail) - s.fee_total) < 1e-9)
        self.assertIn(f"+ Kalshi taker fee: {s.fee_detail[0]['formula']}", txt)
        self.assertIn(f"= you pay ${0.60 * s.suggested_contracts + s.fee_total:,.2f}", txt)
        self.assertGreater(s.fee_total, 0)
        self.assertIn("edge vs robinhood mid", txt)
        # Same poll again within the cooldown and no bigger edge: silent.
        self.assertEqual(tr.observe(KEY, "DEN @ KC", OUT, LABELS, _book(20, (0.59, 0.60), (0.66, 0.69)), now=20), [])

    def test_signals_are_graded_hard_lag_agreement_and_lock(self):
        """Rothera reprices KC to 0.66/0.69; Kalshi still offers KC at 0.60. Buying KC on Kalshi
        costs 0.60 + fee, below Rothera's *bid* 0.66: a hard lag. The cheapest DEN is now on
        Rothera itself (1 - 0.66 = 0.34), so the lag is already a lock - an arbitrage - though a
        Kalshi YES + Rothera YES pair pays only $0.50 on a tie. Polymarket moving the same way
        counts as agreement."""
        tr = self._tracker()
        tr.observe(KEY, "DEN @ KC", OUT, LABELS, _book(0, (0.59, 0.60), (0.58, 0.61), kc_pm=(0.58, 0.62)), now=0)
        s = tr.observe(KEY, "DEN @ KC", OUT, LABELS, _book(15, (0.59, 0.60), (0.66, 0.69), kc_pm=(0.65, 0.69)), now=15, bankroll=500)
        s = [x for x in s if x.follower == "kalshi"][0]
        self.assertAlmostEqual(s.leader_bid, 0.66)
        self.assertTrue(s.hard_lag)
        self.assertEqual(s.agree, 1)
        self.assertEqual(s.lock_venue, "robinhood")
        self.assertAlmostEqual(s.lock_ask, 0.34)
        self.assertTrue(s.lock_now)
        self.assertLess(s.follower_all_in + s.lock_all_in, 1.0)
        self.assertEqual(s.lock_tie_sum, 0.5)              # Kalshi $0.50 + Rothera YES $0 on a tie
        self.assertFalse(s.lock_now_tie_safe)              # Kalshi's own DEN (0.41) does not lock yet

    def test_a_kalshi_only_lock_needs_the_other_side_to_get_cheaper(self):
        """Executable on Kalshi only: DEN at 0.41 + fee does not lock yet. The lock price is
        the most DEN may cost, fees in, for the pair to pay back its cost."""
        from arb_engine.fees.kalshi import KalshiFees

        tr = LeadLagTracker(move=0.05, window_s=30, min_edge=0.02, cooldown_s=60, fresh_s=60, executable={"kalshi"})
        tr.observe(KEY, "DEN @ KC", OUT, LABELS, _book(0, (0.59, 0.60), (0.58, 0.61)), now=0)
        s = tr.observe(KEY, "DEN @ KC", OUT, LABELS, _book(15, (0.59, 0.60), (0.66, 0.69)), now=15, bankroll=500)[0]
        self.assertEqual(s.lock_venue, "kalshi")
        self.assertAlmostEqual(s.lock_ask, 0.41)
        self.assertFalse(s.lock_now)
        n = s.suggested_contracts
        at = lambda p: p + float(KalshiFees().fee(p, n)) / n
        self.assertLessEqual(s.follower_all_in + at(s.lock_price), 1.0 + 1e-9)          # locks at the lock price
        self.assertGreater(s.follower_all_in + at(round(s.lock_price + 0.01, 2)), 1.0)  # and not a cent above it
        self.assertEqual(s.lock_tie_sum, 1.0)              # Kalshi KC + Kalshi DEN pay $0.50 each on a tie

    def test_settlement_gate_is_the_contract_bought_not_the_leader(self):
        """LAG buys only the follower and holds or sells it on the follower's venue; the
        leader is never traded. A Rothera-led Kalshi buy must stay executable (Kalshi's rule
        is verbatim) with the Rothera/Kalshi tie difference kept as information; a buy on
        Rothera (rule unverified) is signal-only."""
        from arb_engine.strategy.lagexec import LagExecutor

        tr = self._tracker()
        tr.observe(KEY, "DEN @ KC", OUT, LABELS, _book(0, (0.59, 0.60), (0.58, 0.61)), now=0)
        s = tr.observe(KEY, "DEN @ KC", OUT, LABELS, _book(15, (0.59, 0.60), (0.66, 0.69)), now=15, bankroll=500)[0]
        self.assertEqual((s.leader, s.follower), ("robinhood", "kalshi"))
        self.assertEqual(s.settlement_flags, ())
        self.assertIn("settlement-mismatch:tie", s.pair_flags)
        self.assertNotIn("SIGNAL ONLY", s.text())
        ex = LagExecutor(mode="intent", intents_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), f"lag_gate_{os.getpid()}.jsonl"))
        self.assertEqual(ex.on_signal(s, _book(15, (0.59, 0.60), (0.66, 0.69)))["status"], "intent")
        # Kalshi leads, Rothera lags: buying Rothera means holding an unverified contract.
        tr2 = LeadLagTracker(move=0.05, window_s=30, min_edge=0.02, cooldown_s=60, fresh_s=60)
        tr2.observe(KEY, "DEN @ KC", OUT, LABELS, _book(0, (0.58, 0.61), (0.58, 0.61)), now=0)
        s2 = tr2.observe(KEY, "DEN @ KC", OUT, LABELS, _book(15, (0.66, 0.69), (0.58, 0.61)), now=15, bankroll=500)   # (kalshi, robinhood)
        rothera = [x for x in s2 if x.follower == "robinhood"]
        self.assertTrue(rothera and rothera[0].settlement_flags, s2)
        self.assertIn("SIGNAL ONLY", rothera[0].text())

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

    def test_lag_is_journalled_but_not_pushed_unless_opted_in(self):
        # A LAG is a one-sided bet, not an arb, and failed the executable re-run: it runs in
        # the background (journal, paper book, demo executor) and is pushed only on request.
        a = self._alerter()
        a.alert("LAG", "bet", event="e1", outcome="KC", venue="kalshi", ask=0.6, all_in=0.61, fair=0.68)
        a.alert("ARB", "lock", event="e1")
        self.assertEqual([b for _, b, _ in self.sent], ["lock"])
        self.assertTrue(any(e["kind"] == "alert" and e["title"] == "LAG" for e in a.events))
        b = self._alerter(ntfy_kinds=["ARB", "LAG"])
        b.alert("LAG", "bet", event="e1", outcome="KC", venue="kalshi", ask=0.6, all_in=0.61, fair=0.68)
        self.assertEqual(self.sent[-1][1], "bet")

    def test_throttle_per_title_event_side_but_never_hedge_now(self):
        a = self._alerter(min_interval_s=60)
        a.alert("ARB", "one", event="e1", outcome="KC", venue="kalshi", ask=0.6, all_in=0.61, fair=0.68)
        a.alert("ARB", "two", event="e1", outcome="KC", venue="kalshi", ask=0.6, all_in=0.61, fair=0.68)   # throttled
        a.alert("ARB", "three", event="e1", outcome="DEN", venue="kalshi", ask=0.4, all_in=0.41, fair=0.5)  # other side: pushed
        self.assertEqual([b for _, b, _ in self.sent], ["one", "three"])
        self.assertTrue(any(e["kind"] == "ntfy_throttled" for e in a.events))
        for i in range(3):
            a.alert("HEDGE NOW", f"hedge {i}", watch="w1")
        self.assertEqual(len(self.sent), 5)
        self.assertEqual(self.sent[-1][2]["Priority"], "5")

    def test_taker_arb_throttles_per_game_not_per_line(self):
        # The maker rates every spread/total line of a game in one pass: one push per game
        # per minute (the first, best-margin line), not one per line; another game still goes.
        # (TAKER ARB is journal-only by default since the week scanner; opted in here.)
        a = self._alerter(min_interval_s=60, ntfy_kinds=["TAKER ARB"])
        from arb_engine.matching import game_event_key
        keys = ("nfl:NYG|TEN:2026-09-27:spread:NYG-14.5", "nfl:NYG|TEN:2026-09-27:spread:TEN-5.5", "nfl:NYG|TEN:2026-09-27:total:44.5")
        for k in keys:
            a.alert("TAKER ARB", f"line {k}", watch=k + "|kalshi:x", event=game_event_key(k))
        a.alert("TAKER ARB", "other game", watch="nfl:DEN|KC:2026-09-27:spread:KC-3.5|kalshi:x", event=game_event_key("nfl:DEN|KC:2026-09-27:spread:KC-3.5"))
        self.assertEqual([b for _, b, _ in self.sent], [f"line {keys[0]}", "other game"])
        self.assertEqual(game_event_key("nfl:DEN|KC:2026-09-27"), "nfl:DEN|KC:2026-09-27")

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

    def test_request_and_observation_times_bracket_the_call_and_carried_quotes_keep_theirs(self):
        """obs_ts is when the answer was in hand and req_ts when the request left; a quote the
        venue did not answer (or a venue that failed) keeps the time it was really seen."""
        from arb_engine.strategy.fastlane import FastLane

        ticks = iter([100.0, 100.4, 101.0, 101.3])     # req, obs of step 1; req, obs of step 2
        client, _ = self._kalshi_client({"T-KC": (0.59, 0.60)})
        kq = OutcomeQuote("kalshi", "T-KC", KEY, "KC", ask=0.60, bid=0.59, ts=90.0, fee_params=KFEE, meta={"ticker": "T-KC", "side": "yes"})
        gone = OutcomeQuote("kalshi", "T-XX", KEY, "DEN", ask=0.41, bid=0.40, ts=90.0, fee_params=KFEE, meta={"ticker": "T-XX", "side": "yes"})
        pq = OutcomeQuote("polymarket", "tok", KEY, "KC", ask=0.62, bid=0.60, ts=85.0)
        lane = FastLane(kalshi_client=client, clock=lambda: next(ticks))
        lane.seed({KEY: {"kalshi": [kq, gone], "polymarket": [pq]}})
        by, _ = lane.step()
        k = {q.venue_market_id: q for q in by[KEY]["kalshi"]}
        self.assertEqual((k["T-KC"].meta["req_ts"], k["T-KC"].meta["obs_ts"], k["T-KC"].ts), (100.0, 100.4, 100.4))
        self.assertTrue(k["T-KC"].meta["refreshed"])
        self.assertEqual((k["T-XX"].meta["obs_ts"], k["T-XX"].meta["refreshed"]), (90.0, False))   # not answered: carried
        self.assertEqual((by[KEY]["polymarket"][0].meta["obs_ts"], by[KEY]["polymarket"][0].meta["refreshed"]), (85.0, False))
        by, _ = lane.step()
        self.assertEqual(by[KEY]["polymarket"][0].meta["obs_ts"], 85.0)   # still the real time, step after step
        self.assertEqual({q.venue_market_id: q.meta["obs_ts"] for q in by[KEY]["kalshi"]}["T-KC"], 101.3)

    def test_trade_prints_page_back_keep_same_second_prints_and_poll_round_robin(self):
        from arb_engine.store import Store
        from arb_engine.strategy.fastlane import FastLane
        from tests.helpers import load

        pages = [load("trades/kalshi_trades_page1.json"), load("trades/kalshi_trades_page2.json")]
        calls = []

        class C:
            def get(self, path, params=None, **kw):
                calls.append(dict(params or {}))
                if params.get("ticker") != "T-A":
                    return {"trades": [], "cursor": ""}
                return pages[1] if params.get("cursor") == pages[0]["cursor"] else pages[0]
        st = Store(":memory:")
        lane = FastLane(kalshi_client=C())
        lane.seed({KEY: {"kalshi": [OutcomeQuote("kalshi", "T-A", KEY, "KC", ask=0.6, bid=0.59, meta={"ticker": "T-A"}),
                                    OutcomeQuote("kalshi", "T-B", KEY, "DEN", ask=0.41, bid=0.4, meta={"ticker": "T-B"})]}})
        n = lane.poll_trades(st, per_step=1)                      # T-A: both pages
        self.assertEqual(n, len(pages[0]["trades"]) + len(pages[1]["trades"]))
        self.assertEqual([c.get("cursor") for c in calls], [None, "abc123"])
        newest = max(pages[0]["trades"], key=lambda t: t["created_time"])["created_time"]
        from arb_engine.strategy.fastlane import _epoch
        self.assertEqual(lane.trade_cursor["T-A"], int(_epoch(newest)))   # that second, not one past it
        lane.poll_trades(st, per_step=1)                          # round-robin: T-B next
        self.assertEqual(calls[-1]["ticker"], "T-B")
        self.assertEqual(lane.poll_trades(st, per_step=1), 0)      # T-A again: repeats dropped by trade_id

    def test_trade_backlog_is_not_committed_past_until_last_page_and_restart_is_inclusive(self):
        from arb_engine.store import Store
        from arb_engine.strategy.fastlane import FastLane, _epoch
        from tests.helpers import load

        pages = [load("trades/kalshi_trades_page1.json"), load("trades/kalshi_trades_page2.json")]
        ticker = pages[0]["trades"][0]["ticker"]
        calls = []
        class C:
            def get(self, path, params=None, **kw):
                calls.append(dict(params or {}))
                return pages[1] if params.get("cursor") == pages[0]["cursor"] else pages[0]
        st = Store(":memory:")
        lane = FastLane(kalshi_client=C())
        lane.seed({KEY: {"kalshi": [OutcomeQuote("kalshi", ticker, KEY, "KC", meta={"ticker": ticker})]}})
        self.assertEqual(lane.poll_trades(st, now=10, max_pages=1), 0)
        self.assertEqual(st.conn.execute("select count(*) from trade_prints").fetchone()[0], 0)
        self.assertTrue(lane.trade_poll_status(10)[ticker]["backlog"])
        self.assertGreater(lane.poll_trades(st, now=11, max_pages=1), 0)
        self.assertFalse(lane.trade_poll_status(11)[ticker]["backlog"])

        restart_calls = []
        class Restart:
            def get(self, path, params=None, **kw):
                restart_calls.append(dict(params or {})); return {"trades": [], "cursor": ""}
        restarted = FastLane(kalshi_client=Restart())
        restarted.seed(lane.last)
        restarted.poll_trades(st, now=20)
        newest = int(max(_epoch(t["created_time"]) for p in pages for t in p["trades"]))
        self.assertEqual(restart_calls[0]["min_ts"], newest)       # inclusive, never newest + 1

    def test_trade_poll_cadence_is_measured_and_all_due_tickers_are_polled(self):
        from arb_engine.store import Store
        from arb_engine.strategy.fastlane import FastLane
        calls = []
        class C:
            def get(self, path, params=None, **kw):
                calls.append((params["ticker"], params.get("min_ts"))); return {"trades": [], "cursor": ""}
        lane = FastLane(kalshi_client=C())
        lane.seed({KEY: {"kalshi": [OutcomeQuote("kalshi", "T-A", KEY, "KC", meta={"ticker": "T-A"}),
                                    OutcomeQuote("kalshi", "T-B", KEY, "DEN", meta={"ticker": "T-B"})]}})
        st = Store(":memory:")
        lane.poll_trades(st, now=1, cadence_s=5)
        lane.poll_trades(st, now=5, cadence_s=5)
        self.assertEqual([t for t, _ in calls], ["T-A", "T-B"])
        lane.poll_trades(st, now=6, cadence_s=5)
        self.assertEqual([t for t, _ in calls], ["T-A", "T-B", "T-A", "T-B"])
        self.assertEqual({v["last_gap_s"] for v in lane.trade_poll_status(6).values()}, {5.0})

    def test_background_trade_poll_can_be_flushed_before_store_close(self):
        from arb_engine.store import Store
        from arb_engine.strategy.fastlane import FastLane
        entered, release = threading.Event(), threading.Event()
        class C:
            def get(self, path, params=None, **kw):
                entered.set()
                release.wait(1)
                return {"trades": [], "cursor": ""}
        lane = FastLane(kalshi_client=C())
        lane.seed({KEY: {"kalshi": [OutcomeQuote("kalshi", "T-A", KEY, "KC", meta={"ticker": "T-A"})]}})
        store = Store(":memory:")
        lane.poll_trades(store, background=True)
        self.assertTrue(entered.wait(1))
        self.assertFalse(lane.wait_for_trade_polls(timeout=0))
        release.set()
        self.assertTrue(lane.wait_for_trade_polls(timeout=1))
        store.close()

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
        self.assertTrue(any("KALSHI: buy" in l and "Kansas City at the ask $0.60" in l for l in lags), lags)
        self.assertTrue(all(l.startswith("NFL - DEN @ KC - LAG:") for l in lags), lags)   # sport first, title once
        lag_alerts = [e for e in slate.alerts.events if e["kind"] == "alert" and e["title"] == "LAG"]
        self.assertTrue(lag_alerts and lag_alerts[0]["msg"].startswith("NFL - DEN @ KC - LAG:"), lag_alerts)
        # The same stale Kalshi book is also a fresh two-leg lock (DEN 0.34 on Rothera + KC 0.60 on Kalshi),
        # and it is reported as an order ticket: sport, per-venue counts, prices, fees, totals.
        # A 2.3c lock is the ARB tier: sized to arb_stake_fraction_arb (5 %) of the $500 bankroll,
        # so 25 sets cost <= $25 with fees (a BIG ARB would get arb_stake_fraction, 20 %).
        self.assertTrue(any(a.startswith("NFL - ") and "ARB +" in a and "KALSHI: buy 25 x KC YES at the ask $0.60" in a
                            and "ROBINHOOD: buy 25 x DEN YES at the ask $0.34" in a and "+ Kalshi taker fee:" in a and "total: $" in a
                            and "stake $25.00 = 5% of your $500.00" in a for a in arbs), arbs)
        import re
        total = float(re.search(r"= \$([0-9.]+) -> pays", arbs[0]).group(1))
        self.assertLessEqual(total, 25.0)
        self.assertEqual(errors, [])
        self.assertTrue(any(e["kind"] == "alert" and e["title"] == "LAG" for e in slate.alerts.events))



    def test_slate_lag_fill_then_lock_end_to_end(self):
        """Through fast_step: a Rothera repricing -> LAG -> paper buy of KC on Kalshi fills ->
        Kalshi's DEN falls -> the lock watch locks the pair (Rothera's cheaper DEN YES is
        skipped: it pays nothing on a tie)."""
        from arb_engine.matching.matcher import MergedEvent
        from arb_engine.models import EventInfo
        from arb_engine.store import Store
        from arb_engine.strategy.inplay import InplayView
        from arb_engine.strategy.live import LiveSlate

        info = EventInfo(event_key=KEY, sport="nfl", market_type="moneyline", outcomes=OUT, labels=LABELS, in_play=True)
        t0 = 1_800_000_000.0
        kq = [OutcomeQuote("kalshi", "T-KC", KEY, "KC", ask=0.60, bid=0.59, ask_size=400, ts=t0, fee_params=KFEE, meta={"ticker": "T-KC", "side": "yes"}), OutcomeQuote("kalshi", "T-DEN", KEY, "DEN", ask=0.41, bid=0.40, ask_size=400, ts=t0, fee_params=KFEE, meta={"ticker": "T-DEN", "side": "yes"})]
        rq = [OutcomeQuote("robinhood", "c1", KEY, "KC", ask=0.61, bid=0.58, ts=t0, quote_time=t0, fee_params={"exchange": "rothera"}, meta={"contract_id": "c1", "side": "yes", "exchange": "rothera"}, book_id="rothera"), OutcomeQuote("robinhood", "c2", KEY, "DEN", ask=0.42, bid=0.39, ts=t0, quote_time=t0, fee_params={"exchange": "rothera"}, meta={"contract_id": "c2", "side": "yes", "exchange": "rothera"}, book_id="rothera")]
        me = MergedEvent(KEY, info, {"kalshi": kq, "robinhood": rq})
        rh_prices = {"c1": {"yes_ask_price": "0.61", "yes_bid_price": "0.58", "ask_venue_timestamp": t0}, "c2": {"yes_ask_price": "0.42", "yes_bid_price": "0.39", "ask_venue_timestamp": t0}}
        k_prices = {"T-KC": (0.59, 0.60), "T-DEN": (0.40, 0.41)}
        client, _ = self._kalshi_client(k_prices)
        rh = self._robinhood(rh_prices)
        rh.client = None
        st = Store(":memory:")
        slate = LiveSlate([], settings={"executable_venues": "kalshi,robinhood"}, store=st, alerter=Alerter(journal_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), f"lock_e2e_{os.getpid()}.jsonl"), quiet=True, desktop=False, webhook="", ntfy=""), bankroll=500, fast=1.0, interval=5.0)
        slate.fastlane.kalshi, slate.fastlane.robinhood = client, rh
        view = InplayView(event_key=KEY, title="DEN @ KC", live=True, game_line="Q2", fair_line="", sides=[], actions=[], blend={}, game_state={"period": 2}, total_cost=0.0, payout_if={}, locked_pnl=None, balanced=False)
        slate._live_priced = {KEY: (None, me, view)}
        slate.fastlane.seed({KEY: me.quotes_by_venue})
        slate.fast_step(t0 + 1)
        rh_prices["c1"].update({"yes_ask_price": "0.69", "yes_bid_price": "0.66", "ask_venue_timestamp": t0 + 2})
        rh_prices["c2"].update({"yes_ask_price": "0.34", "yes_bid_price": "0.31", "ask_venue_timestamp": t0 + 2})
        lags = []
        for t in (2, 3, 4):
            rh_prices["c1"]["ask_venue_timestamp"] = rh_prices["c2"]["ask_venue_timestamp"] = t0 + t
            lags += slate.fast_step(t0 + t).lags
        self.assertTrue(lags)
        self.assertTrue(any(o.filled_at is not None for o in slate.paper.orders))       # the paper buy filled
        self.assertEqual([p.status for p in slate.laglock.positions], ["watching"])      # DEN still too dear
        k_prices["T-DEN"] = (0.34, 0.35)                                                 # Kalshi's DEN drops
        for t in (5, 6):
            rh_prices["c1"]["ask_venue_timestamp"] = rh_prices["c2"]["ask_venue_timestamp"] = t0 + t
            slate.fast_step(t0 + t)
        p = slate.laglock.positions[0]
        self.assertEqual((p.status, p.lock_venue), ("locked", "kalshi"))
        self.assertGreater(p.lock_margin, 0)
        self.assertEqual(st.conn.execute("select status from lag_locks").fetchone()[0], "locked")

    def test_production_fast_step_with_a_moving_clock_still_signals(self):
        """In production fast_step gets no pinned time: each quote is stamped when its answer
        arrives, after the step began, and a venue clock may run slightly ahead of ours. The
        signals must be judged when the quotes are in hand, or every fresh quote reads as
        "from the future" and LAG never fires (a regression caught before merge)."""
        from arb_engine.matching.matcher import MergedEvent
        from arb_engine.models import EventInfo
        from arb_engine.strategy.inplay import InplayView
        from arb_engine.strategy.leadlag import _fresh
        from arb_engine.strategy.live import LiveSlate

        self.assertTrue(_fresh(OutcomeQuote("robinhood", "c", KEY, "KC", ask=.6, bid=.59, ts=100.0, quote_time=100.05), 100.0, 10.0))
        self.assertFalse(_fresh(OutcomeQuote("kalshi", "k", KEY, "KC", ask=.6, bid=.59, ts=106.0), 100.0, 10.0))
        info = EventInfo(event_key=KEY, sport="nfl", market_type="moneyline", outcomes=OUT, labels=LABELS, in_play=True)
        t0 = 1_800_000_000.0
        clock = {"t": t0}

        def tick():   # every read advances 0.2 s: requests take time
            clock["t"] += 0.2
            return clock["t"]
        kq = [OutcomeQuote("kalshi", "T-KC", KEY, "KC", ask=0.60, bid=0.59, ask_size=400, ts=t0, fee_params=KFEE, meta={"ticker": "T-KC", "side": "yes"}), OutcomeQuote("kalshi", "T-DEN", KEY, "DEN", ask=0.41, bid=0.40, ask_size=400, ts=t0, fee_params=KFEE, meta={"ticker": "T-DEN", "side": "yes"})]
        rq = [OutcomeQuote("robinhood", "c1", KEY, "KC", ask=0.61, bid=0.58, ts=t0, quote_time=t0, fee_params={"exchange": "rothera"}, meta={"contract_id": "c1", "side": "yes", "exchange": "rothera"}, book_id="rothera"), OutcomeQuote("robinhood", "c2", KEY, "DEN", ask=0.42, bid=0.39, ts=t0, quote_time=t0, fee_params={"exchange": "rothera"}, meta={"contract_id": "c2", "side": "yes", "exchange": "rothera"}, book_id="rothera")]
        me = MergedEvent(KEY, info, {"kalshi": kq, "robinhood": rq})
        rh_prices = {"c1": {"yes_ask_price": "0.61", "yes_bid_price": "0.58", "ask_venue_timestamp": t0}, "c2": {"yes_ask_price": "0.42", "yes_bid_price": "0.39", "ask_venue_timestamp": t0}}
        client, _ = self._kalshi_client({"T-KC": (0.59, 0.60), "T-DEN": (0.40, 0.41)})
        rh = self._robinhood(rh_prices)
        rh.client = None
        slate = LiveSlate([], settings={"executable_venues": "kalshi,robinhood"}, alerter=Alerter(journal_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), f"fastc_{os.getpid()}.jsonl"), quiet=True, desktop=False, webhook="", ntfy=""), bankroll=500, fast=1.0, interval=5.0)
        slate.fastlane.kalshi, slate.fastlane.robinhood, slate.fastlane.clock = client, rh, tick
        view = InplayView(event_key=KEY, title="DEN @ KC", live=True, game_line="Q2", fair_line="", sides=[], actions=[], blend={}, game_state={"period": 2}, total_cost=0.0, payout_if={}, locked_pnl=None, balanced=False)
        slate._live_priced = {KEY: (None, me, view)}
        slate.fastlane.seed({KEY: me.quotes_by_venue})
        self.assertEqual(slate.fast_step().lags, [])
        # Rothera reprices KC +8c, its venue clock 50 ms ahead of ours; Kalshi's book is unchanged.
        rh_prices["c1"].update({"yes_ask_price": "0.69", "yes_bid_price": "0.66", "ask_venue_timestamp": clock["t"] + 1.05})
        rh_prices["c2"].update({"yes_ask_price": "0.34", "yes_bid_price": "0.31", "ask_venue_timestamp": clock["t"] + 1.05})
        lags = []
        for _ in range(3):
            clock["t"] += 1.0
            lags += slate.fast_step().lags
        self.assertTrue(any("KALSHI: buy" in l and "Kansas City at the ask $0.60" in l for l in lags), lags)

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
        self.assertAlmostEqual(s["pnl_bid_60"]["mean"], 0.68 - o.marks["exit_fee_60"] - 0.617, places=6)
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

    def test_shallow_or_unknown_depth_does_not_claim_a_full_fill(self):
        from arb_engine.strategy.paperlag import LagPaperBook

        for size in (None, 99):
            book = LagPaperBook(store=None, fill_window_s=10)
            book.open(self._sig(contracts=100), 1000.0)
            self.assertEqual(book.observe(KEY, self._quotes(1001.0, 0.60, 0.59, size=size), 1001.0), [])


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

    def test_settlement_mismatch_is_observable_but_never_executed(self):
        from arb_engine.strategy.lagexec import LagExecutor

        path = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"lag_settlement_{os.getpid()}.jsonl")
        sig = self._sig()
        sig.settlement_flags = ("settlement-mismatch:tie",)
        rec = LagExecutor(mode="intent", intents_path=path).on_signal(sig, self._quotes())
        self.assertEqual(rec["status"], "skipped")
        self.assertIn("settlement-mismatch:tie", rec["reason"])

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

    def _demo(self, create_order, alerter=None):
        from arb_engine.execution.kalshi import KalshiExecutor
        from arb_engine.strategy.lagexec import LagExecutor

        class Client:
            env, base_url, has_credentials = "demo", "https://demo", True

        c = Client()
        c.create_order = create_order
        return LagExecutor(mode="demo", executor=KalshiExecutor(c), intents_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), f"lag_x_{os.getpid()}.jsonl"),
                           max_contracts=50, max_notional_per_game=100.0, daily_notional=500.0, alerter=alerter, clock=lambda: 1000.0)

    def test_caps_count_what_filled_not_what_was_sent(self):
        # An immediate-or-cancel order that finds nothing is no exposure: it must not eat the cap.
        ex = self._demo(lambda payload: {"order_id": "o1", "fill_count": "0.00", "remaining_count": "0.00"})
        rec = ex.on_signal(self._sig(), self._quotes())
        self.assertEqual((rec["status"], rec["filled_notional"]), ("SUBMITTED", 0.0))
        self.assertEqual(ex.sent_notional, 0.0)
        ex2 = self._demo(lambda payload: {"order_id": "o2", "fill_count": "12.00", "remaining_count": "0.00"})
        ex2.on_signal(self._sig(), self._quotes())
        self.assertAlmostEqual(ex2.sent_notional, 12 * 0.60)
        ex3 = self._demo(lambda payload: {"order_id": "o3"})           # no fill count reported: count it all
        ex3.on_signal(self._sig(), self._quotes())
        self.assertAlmostEqual(ex3.sent_notional, 50 * 0.60)

    def test_a_failed_order_is_pushed_as_exec_error_and_described(self):
        from arb_engine.venues.http import HttpError

        pushed = []
        alerts = Alerter(journal_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), f"lag_err_{os.getpid()}.jsonl"), quiet=True, desktop=False, webhook="", ntfy="t", transport=lambda url, body, headers: pushed.append((headers["Title"], body.decode())))

        def reject(payload):
            raise HttpError(400, "https://demo/portfolio/events/orders", '{"error":{"code":"invalid_parameters"}}')
        ex = self._demo(reject, alerter=alerts)
        rec = ex.on_signal(self._sig(), self._quotes())
        self.assertEqual(rec["status"], "error")
        self.assertEqual(len(pushed), 1)
        self.assertEqual(pushed[0][0], "EXEC ERROR")
        self.assertIn("LAG auto-trade (demo) failed", pushed[0][1])
        self.assertIn("invalid_parameters", pushed[0][1])
        self.assertTrue(ex.describe(rec).startswith("AUTO (demo): FAILED"))
        # A second failure in the same game inside a minute is not a second buzz.
        ex.on_signal(self._sig(ask=0.61), self._quotes())
        self.assertEqual(len(pushed), 1)

    def test_describe_says_what_the_bot_did(self):
        from arb_engine.strategy.lagexec import LagExecutor

        ex = self._demo(lambda payload: {"order_id": "o1", "fill_count": "12.00", "remaining_count": "0.00"})
        line = ex.describe(ex.on_signal(self._sig(), self._quotes()))
        self.assertEqual(line, "AUTO (demo): sent IOC buy 50 x KXNFLGAME-26SEP21DENKC-KC @ 0.6 -> filled 12.00 ($7.20)")
        intent = LagExecutor(mode="intent", intents_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), f"lag_i3_{os.getpid()}.jsonl"))
        self.assertTrue(intent.describe(intent.on_signal(self._sig(), self._quotes())).startswith("AUTO (intent): would send IOC buy 50"))
        self.assertIsNone(LagExecutor(mode="off").describe({"status": "intent"}))

    def test_live_mode_needs_the_flag_and_no_row_is_skipped(self):
        from arb_engine.strategy.lagexec import LagExecutor

        os.environ.pop("ARB_LIVE_TRADING", None)
        with self.assertRaises(RuntimeError):
            LagExecutor(mode="live", executor=object())
        ex = LagExecutor(mode="intent", intents_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), f"lag_i2_{os.getpid()}.jsonl"))
        # Only a NO row for the outcome: nothing to buy on Kalshi's own market.
        q = {"kalshi": [OutcomeQuote("kalshi", "T-DEN#no", KEY, "KC", ask=0.60, bid=0.59, meta={"ticker": "T-DEN", "side": "no"})]}
        self.assertEqual(ex.on_signal(self._sig(), q)["reason"], "no Kalshi YES row for this outcome")


class TicketTests(unittest.TestCase):
    """Alerts must be typeable into two order tickets without doing arithmetic on a phone."""

    def _result(self, size=47.0):
        from arb_engine.quant.arbitrage import Leg, evaluate
        from arb_engine.fees.kalshi import KalshiFees
        from arb_engine.fees.robinhood import RobinhoodFees

        return evaluate([Leg("KC", "kalshi", 0.60, KalshiFees(), label="Kansas City"),
                         Leg("DEN", "robinhood", 0.36, RobinhoodFees(), label="Denver")], size)

    def test_arb_ticket_names_the_sport_venue_count_price_fee_and_cash(self):
        from arb_engine.strategy import ticket

        r = self._result()
        txt = ticket.arb_ticket("DEN @ KC", r, size_note="bankroll $500.00", sport="nfl:DEN|KC:2026-09-24")
        lines = txt.splitlines()
        self.assertTrue(lines[0].startswith("NFL - DEN @ KC - ARB "), lines[0])
        self.assertIn("KALSHI: buy 47 x Kansas City at the ask $0.60 (60c)", txt)
        self.assertIn("ROBINHOOD: buy 47 x Denver at the ask $0.36 (36c)", txt)
        # Each leg is a receipt: price x count, every fee item with its formula, the total.
        for leg in r.legs:
            self.assertIn(f"price: 47 x ${leg.price:.2f} = ${leg.price * leg.contracts:,.2f}", txt)
            self.assertAlmostEqual(sum(d["amount"] for d in leg.fee_detail), leg.fee, places=9)
            for d in leg.fee_detail:
                self.assertIn(f"+ {d['label']}: {d['formula']}", txt)
            self.assertIn(f"= you pay ${leg.cost:,.2f}", txt)
        self.assertIn("0.07 x 47 x 0.6 x 0.4 = $0.7896 -> $0.79 (rounded up to the cent)", txt)   # Kalshi, checkable by hand
        self.assertIn("Robinhood commission: 0.1 x 47 x 0.36 x 0.64", txt)
        self.assertIn("Rothera exchange fee: $0.01 x 47 = $0.47", txt)
        costs = " + ".join(f"${l.cost:,.2f}" for l in r.legs)
        self.assertIn(f"total: {costs} = ${r.total_cost:,.2f} -> pays ${r.payout:,.2f} whoever wins", txt)
        self.assertIn("fees are entry-only", txt)
        self.assertIn("bankroll $500.00", txt)

    def test_ticket_fees_are_the_order_fee_not_a_per_contract_fee_times_size(self):
        from arb_engine.strategy import ticket

        one, many = self._result(1.0), self._result(100.0)
        self.assertLess(many.legs[0].fee, one.legs[0].fee * 100)      # Kalshi rounds up per order
        txt = ticket.arb_ticket("t", many)
        self.assertIn("+ Kalshi taker fee: 0.07 x 100 x 0.6 x 0.4 = $1.6800", txt)   # exact: no rounding to show
        self.assertIn(f"= you pay ${many.legs[0].cost:,.2f}", txt)
        self.assertIn("-> $0.02 (rounded up to the cent)", ticket.arb_ticket("t", one))   # one contract: 0.0168 -> 0.02

    def test_a_ticket_that_loses_on_a_tie_says_so(self):
        from arb_engine.quant.arbitrage import Leg, evaluate
        from arb_engine.fees.kalshi import KalshiFees
        from arb_engine.strategy import ticket

        # Kalshi YES-A + a Rothera YES-B pays $0.50 on a tie: the "lock" loses.
        r = evaluate([Leg("A", "kalshi", 0.50, KalshiFees(), tie_payout=0.5),
                      Leg("B", "robinhood", 0.47, KalshiFees(), tie_payout=0.0)], 10)
        self.assertIn("LOSES on a tie", ticket.arb_ticket("A @ B", r))

    def test_near_arb_ticket_prints_the_price_each_leg_must_reach(self):
        from arb_engine.strategy import ticket

        rep = {"outcomes": [
            {"outcome": "KC", "label": "Kansas City", "best_buy_venue": "kalshi",
             "venues": [{"venue": "kalshi", "ask": 0.62, "all_in": 0.637, "max_buy_price": 0.61, "ask_size": 120}]},
            {"outcome": "DEN", "label": "Denver", "best_buy_venue": "robinhood",
             "venues": [{"venue": "robinhood", "ask": 0.38, "all_in": 0.39, "max_buy_price": 0.37, "ask_size": 45}]}]}
        txt = ticket.near_arb_ticket("DEN @ KC", rep, -0.027, sport="nfl", bankroll=500)
        self.assertTrue(txt.startswith("NFL - DEN @ KC - ARB CLOSE -2.7"), txt)
        self.assertIn("KALSHI Kansas City @ 0.62 (all-in 0.6370) - locks at 0.61, 1.0\u00a2 away, depth 120", txt)
        self.assertIn("ROBINHOOD Denver @ 0.38", txt)
        self.assertIn("set costs $1.03/ct with fees", txt)
        self.assertIn("ready for 45 ct (depth)", txt)   # $500 buys 486 sets; Robinhood only shows 45


class BudgetSizingTests(unittest.TestCase):
    def _legs(self):
        from arb_engine.quant.arbitrage import Leg
        from arb_engine.fees.kalshi import KalshiFees
        from arb_engine.fees.robinhood import RobinhoodFees

        return [Leg("KC", "kalshi", 0.60, KalshiFees()), Leg("DEN", "robinhood", 0.36, RobinhoodFees())]

    def test_size_for_budget_fits_the_all_in_cost_not_the_gross_prices(self):
        from arb_engine.quant.arbitrage import size_for_budget, size_from_books

        legs = self._legs()
        r = size_for_budget(legs, budget=100.0, max_contracts=500)
        self.assertIsNotNone(r)
        self.assertLessEqual(r.total_cost, 100.0)
        # The gross set is 0.96, so a price-only cap would say 104 contracts and overspend.
        self.assertLess(r.contracts, 100.0 / 0.96)
        self.assertGreater(r.contracts, 90)
        self.assertGreater(r.profit, 0)
        # No budget = depth only (the old behaviour).
        self.assertEqual(size_for_budget(legs, budget=None, max_contracts=50).contracts,
                         size_from_books(legs, max_contracts=50).contracts)

    def test_a_budget_too_small_for_one_contract_set_is_no_trade(self):
        from arb_engine.quant.arbitrage import size_for_budget

        self.assertIsNone(size_for_budget(self._legs(), budget=0.5, max_contracts=500))


class NearArbAlertTests(unittest.TestCase):
    """A pair that is close to locking is worth a heads-up before it crosses."""

    def _slate(self, kalshi_ask, rh_ask, **kw):
        from arb_engine.matching.matcher import MergedEvent
        from arb_engine.models import EventInfo
        from arb_engine.strategy.inplay import InplayView
        from arb_engine.strategy.live import LiveSlate

        t0 = 1_800_000_000.0
        info = EventInfo(event_key=KEY, sport="nfl", market_type="moneyline", outcomes=OUT, labels=LABELS, in_play=True)
        kq = [OutcomeQuote("kalshi", "T-KC", KEY, "KC", ask=kalshi_ask, bid=kalshi_ask - 0.01, ask_size=400, ts=t0, fee_params=KFEE, meta={"ticker": "T-KC", "side": "yes"})]
        rq = [OutcomeQuote("robinhood", "c2", KEY, "DEN", ask=rh_ask, bid=rh_ask - 0.01, ask_size=120, ts=t0, quote_time=t0, fee_params={"exchange": "rothera"}, meta={"contract_id": "c2", "side": "yes", "exchange": "rothera"}, book_id="rothera")]
        me = MergedEvent(KEY, info, {"kalshi": kq, "robinhood": rq})
        view = InplayView(event_key=KEY, title="DEN @ KC", live=True, game_line="Q2", fair_line="", sides=[], actions=[], blend={}, game_state={"period": 2}, total_cost=0.0, payout_if={}, locked_pnl=None, balanced=False)
        alerts = Alerter(journal_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), f"near_{os.getpid()}.jsonl"), quiet=True, desktop=False, webhook="", ntfy="")
        slate = LiveSlate([], settings={"executable_venues": "kalshi,robinhood", **kw}, alerter=alerts, bankroll=500, interval=5.0)
        return slate, me, view, t0

    def _run(self, slate, me, view, now):
        from arb_engine.strategy.live import SlateTick

        out = SlateTick(at=now, views=[], games=[])
        slate._market_signals(me, view, out, now)
        return out

    def test_within_the_buffer_alerts_arb_close_with_the_prices_that_would_lock(self):
        # 0.61 + 0.38 = 0.99 gross, 1.027 all-in with both fees: not a lock, 2.7c inside the buffer.
        slate, me, view, t0 = self._slate(0.61, 0.38)
        out = self._run(slate, me, view, t0 + 1)
        alerts = [e for e in slate.alerts.events if e["kind"] == "alert"]
        self.assertEqual([a["title"] for a in alerts], ["ARB CLOSE"])
        msg = alerts[0]["msg"]
        self.assertTrue(msg.startswith("NFL - DEN @ KC - ARB CLOSE -"), msg)
        self.assertIn("KALSHI Kansas City @ 0.61", msg)
        self.assertIn("ROBINHOOD Denver @ 0.38", msg)
        self.assertIn("locks at", msg)
        self.assertTrue(any("ARB CLOSE" in a for a in out.arbs), out.arbs)
        # Same gap again inside arb_near_every_s: silent (a phone buzzing every 5 s is noise).
        self._run(slate, me, view, t0 + 20)
        self.assertEqual(len([e for e in slate.alerts.events if e["kind"] == "alert"]), 1)

    def test_a_gap_wider_than_the_buffer_is_silent(self):
        slate, me, view, t0 = self._slate(0.62, 0.45)     # ~7c over: not close
        self._run(slate, me, view, t0 + 1)
        self.assertEqual([e for e in slate.alerts.events if e["kind"] == "alert"], [])

    def test_an_actual_lock_alerts_arb_not_arb_close(self):
        slate, me, view, t0 = self._slate(0.58, 0.36)     # 0.94 gross: a real lock
        self._run(slate, me, view, t0 + 1)
        alerts = [e for e in slate.alerts.events if e["kind"] == "alert"]
        self.assertEqual([a["title"] for a in alerts], ["ARB"])
        self.assertIn("total: $", alerts[0]["msg"])
        self.assertIn("KALSHI: buy", alerts[0]["msg"])
        self.assertIn("+ Kalshi taker fee:", alerts[0]["msg"])

    def _pushed(self, slate):
        sent = []
        slate.alerts.ntfy = "https://ntfy.sh/t"
        slate.alerts._post = lambda url, body, headers: sent.append((headers["Title"], headers["Priority"], body.decode()))
        return sent

    def test_arb_tiers_small_is_logged_normal_pushed_big_is_top_priority(self):
        """Replayed by hand (a person on the Robinhood leg), arbs under 1c lost money and arbs of
        3c+ made money: under arb_push_min_margin is journalled only, 3c+ is BIG ARB."""
        for (k, r), kind, pushed in (((0.59, 0.365), "ARB SMALL", False), ((0.58, 0.36), "ARB", True), ((0.55, 0.36), "BIG ARB", True)):
            slate, me, view, t0 = self._slate(k, r)
            sent = self._pushed(slate)
            self._run(slate, me, view, t0 + 1)
            titles = [e["title"] for e in slate.alerts.events if e["kind"] == "alert"]
            self.assertEqual(titles, [kind], (k, r))
            self.assertEqual(bool(sent), pushed, (k, r, sent))
            if kind == "BIG ARB":
                self.assertEqual((sent[0][0], sent[0][1]), ("BIG ARB +5.3c - NFL DEN @ KC", "5"))
                # The phone gets only what to buy, where, at what price, and the result ...
                lines = sent[0][2].splitlines()
                self.assertRegex(lines[0], r"^1\) (Kalshi|Robinhood): buy \d+ (KC|DEN) YES at \d+(\.\d+)?\u00a2$")
                self.assertRegex(lines[1], r"^2\) (Kalshi|Robinhood): buy \d+ (KC|DEN) YES at \d+(\.\d+)?\u00a2")
                self.assertRegex(lines[2], r"^Cost \$[0-9.,]+, pays \$[0-9.,]+ = \+\$[0-9.,]+$")
                self.assertNotIn("fee", sent[0][2])
                # ... while the journal keeps the full itemised ticket.
                full = [e for e in slate.alerts.events if e["kind"] == "alert"][0]["msg"]
                self.assertIn("+ Kalshi taker fee:", full)

    def test_short_push_style_can_be_switched_back_to_full(self):
        slate, me, view, t0 = self._slate(0.55, 0.36, arb_push_style="full")
        sent = self._pushed(slate)
        self._run(slate, me, view, t0 + 1)
        self.assertEqual(sent[0][0], "BIG ARB NFL")
        self.assertIn("+ Kalshi taker fee:", sent[0][2])

    def test_arb_ticket_says_which_leg_first_its_age_and_the_second_legs_limit(self):
        """Rothera moved (DEN cheaper there); Kalshi's KC has not followed: Kalshi is the stale
        price, so it goes first; the Robinhood leg shows the most it may cost and still lock."""
        slate, me, view, t0 = self._slate(0.58, 0.40)
        self._run(slate, me, view, t0)                       # history: no arb yet
        slate2, me2, view2, _ = self._slate(0.58, 0.36)      # Rothera's DEN falls 4c, Kalshi's KC unchanged
        from arb_engine.matching.matcher import MergedEvent
        for qs in me2.quotes_by_venue.values():
            for q in qs:
                q.ts = q.quote_time = t0 + 15 if q.quote_time is not None else None
                q.ts = t0 + 15
        me2 = MergedEvent(me2.event_key, me2.info, me2.quotes_by_venue)
        out = self._run(slate, me2, view, t0 + 15)
        msg = [e for e in slate.alerts.events if e["kind"] == "alert" and "ARB" in e["title"]][-1]["msg"]
        lines = msg.splitlines()
        self.assertTrue(lines[1].startswith("tie-proof on paper (Robinhood's tie rule"), msg)   # default $0.50 tie payouts; Rothera's rule unverified
        self.assertTrue(lines[2].startswith("window: arbs this size lasted a median"), msg)   # how long it will last
        self.assertTrue(lines[3].startswith("1) KALSHI: buy"), msg)             # the stale leg first
        self.assertIn("BUY THIS FIRST - KALSHI has not followed ROBINHOOD", msg)
        self.assertIn("price seen 0s ago", msg)
        self.assertRegex(msg, r"2\) ROBINHOOD: buy[\s\S]*still locks if you pay up to \$0\.3[0-9]")

    def test_the_stake_follows_the_tier(self):
        """BIG ARB is sized to arb_stake_fraction (20 %), a 1-3c ARB to arb_stake_fraction_arb
        (5 %); 0 turns the 1-3c tier off (nothing it could buy)."""
        def ticket(ask_kc, ask_den, **kw):
            slate, me, view, t0 = self._slate(ask_kc, ask_den, **kw)
            self._run(slate, me, view, t0 + 1)
            return [e for e in slate.alerts.events if e["kind"] == "alert" and "ARB" in e["title"]]
        big = ticket(0.55, 0.38)[-1]["msg"]
        self.assertIn("BIG ARB", big.splitlines()[0])
        self.assertIn("= 20% of your $500.00", big)
        small = ticket(0.57, 0.37)[-1]["msg"]
        self.assertIn(" - ARB +", small.splitlines()[0])
        self.assertIn("stake $25.00 = 5% of your $500.00", small)
        self.assertEqual([e["title"] for e in ticket(0.57, 0.37, arb_stake_fraction_arb=0.0)], [])

    def test_the_buffer_is_a_setting(self):
        slate, me, view, t0 = self._slate(0.62, 0.45, arb_near_margin=0.15)   # 10.7c short, buffer 15c
        self._run(slate, me, view, t0 + 1)
        self.assertEqual([e["title"] for e in slate.alerts.events if e["kind"] == "alert"], ["ARB CLOSE"])


class LagLockTests(unittest.TestCase):
    """strategy/laglock.py: a filled LAG position watches the other outcome for a lock."""

    def _q(self, venue, outcome, ask, t, size=500, side="yes", exch=None):
        meta = {"ticker": f"T-{outcome}", "side": side}
        if exch:
            meta["exchange"] = exch
        return OutcomeQuote(venue, f"{venue}-{outcome}", KEY, outcome, ask=ask, bid=round(ask - 0.01, 2), ask_size=size, ts=t,
                            fee_params=KFEE if venue == "kalshi" else {"exchange": exch or "rothera"}, meta=meta,
                            book_id="rothera" if venue == "robinhood" else venue)

    def _book(self, **kw):
        from arb_engine.strategy.laglock import LagLockBook

        return LagLockBook(store=None, watch_s=600, executable={"kalshi", "robinhood"}, **kw)

    def test_paper_position_locks_when_the_other_side_gets_cheap_enough(self):
        from arb_engine.fees.kalshi import KalshiFees

        b = self._book()
        entry_all_in = 0.60 + float(KalshiFees().fee(0.60, 50)) / 50        # KC bought at 0.60 on Kalshi
        b.open("p1", KEY, "KC", "DEN", "kalshi", 50, 0.60, entry_all_in, 0.0, "paper", entry_tie=0.5)
        self.assertEqual(b.observe(KEY, {"kalshi": [self._q("kalshi", "DEN", 0.41, 5.0)]}, 5.0), [])   # 0.617 + 0.417 > 1
        # 0.37 is not enough: 0.6168 + 0.37 + fee 0.0164 = 1.0032 > $1.
        self.assertEqual(b.observe(KEY, {"kalshi": [self._q("kalshi", "DEN", 0.37, 30.0)]}, 30.0), [])
        lines = b.observe(KEY, {"kalshi": [self._q("kalshi", "DEN", 0.36, 40.0)]}, 40.0)   # 0.6168 + 0.3762 = 0.993
        p = b.positions[0]
        self.assertEqual(p.status, "locked")
        den_all_in = 0.36 + float(KalshiFees().fee(0.36, 50)) / 50
        self.assertAlmostEqual(p.lock_margin, 1 - entry_all_in - den_all_in)
        self.assertGreater(p.lock_margin, 0)
        self.assertEqual(p.lock_tie_sum, 1.0)
        self.assertIn("LAG LOCKED", lines[0])
        self.assertIn(f"+${p.lock_margin * 50:.2f} locked", lines[0])
        s = b.summary()
        self.assertEqual((s["locked"], s["conversion"], s["median_seconds_to_lock"]), (1, 1.0, 40.0))

    def test_a_pair_that_loses_on_a_tie_is_not_a_lock_and_positions_expire(self):
        b = self._book()
        b.open("p1", KEY, "KC", "DEN", "kalshi", 10, 0.60, 0.62, 0.0, "paper", entry_tie=0.5)
        # Rothera's DEN YES pays nothing on a tie: $0.50 + $0 < $1 -> not locked however cheap.
        b.observe(KEY, {"robinhood": [self._q("robinhood", "DEN", 0.30, 5.0, exch="rothera")]}, 5.0)
        self.assertEqual(b.positions[0].status, "watching")
        b.observe(KEY, {}, 601.0)
        self.assertEqual(b.positions[0].status, "expired")
        self.assertEqual(b.summary()["conversion"], 0.0)
        # Opting out of tie safety locks it (and reports the tie payout).
        b2 = self._book(require_tie_safe=False)
        b2.open("p2", KEY, "KC", "DEN", "kalshi", 10, 0.60, 0.62, 0.0, "paper", entry_tie=0.5)
        b2.observe(KEY, {"robinhood": [self._q("robinhood", "DEN", 0.30, 5.0, exch="rothera")]}, 5.0)
        self.assertEqual((b2.positions[0].status, b2.positions[0].lock_tie_sum), ("locked", 0.5))

    def test_demo_position_sends_the_lock_leg_and_only_a_fill_locks(self):
        from arb_engine.execution.kalshi import KalshiExecutor
        from arb_engine.strategy.lagexec import LagExecutor

        fills = ["0.00", "10.00"]
        sent = []

        class Client:
            env, base_url, has_credentials = "demo", "https://demo", True

            def create_order(self, payload):
                sent.append(payload)
                return {"order_id": f"o{len(sent)}", "fill_count": fills.pop(0), "remaining_count": "0.00"}
        ex = LagExecutor(mode="demo", executor=KalshiExecutor(Client()), intents_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), f"lock_{os.getpid()}.jsonl"),
                         max_notional_per_game=0.01, clock=lambda: 0.0)   # caps exhausted: a lock leg must still go
        b = self._book(executor=ex)
        b.open("e1", KEY, "KC", "DEN", "kalshi", 10, 0.60, 0.62, 0.0, "demo", entry_tie=0.5)
        b.observe(KEY, {"kalshi": [self._q("kalshi", "DEN", 0.36, 5.0)]}, 5.0)          # IOC found nothing
        self.assertEqual(b.positions[0].status, "watching")
        b.observe(KEY, {"kalshi": [self._q("kalshi", "DEN", 0.36, 6.0)]}, 6.0)          # filled: locked
        self.assertEqual(b.positions[0].status, "locked")
        self.assertEqual([(p["ticker"], p["time_in_force"], int(float(p["count"]))) for p in sent], [("T-DEN", "immediate_or_cancel", 10)] * 2)

    def test_a_lock_only_on_robinhood_is_flagged_for_a_person(self):
        from arb_engine.strategy.lagexec import LagExecutor

        ex = LagExecutor(mode="intent", intents_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), f"lock2_{os.getpid()}.jsonl"))
        b = self._book(executor=ex, require_tie_safe=False)
        b.open("e1", KEY, "KC", "DEN", "kalshi", 10, 0.60, 0.62, 0.0, "demo", entry_tie=0.5)
        lines = b.observe(KEY, {"robinhood": [self._q("robinhood", "DEN", 0.30, 5.0, exch="rothera")]}, 5.0)
        self.assertEqual(b.positions[0].status, "lockable")
        self.assertIn("needs a person on robinhood", lines[0])

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

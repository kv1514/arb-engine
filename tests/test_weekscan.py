"""strategy/weekscan.py: arbs all week - full sweeps, a fast watch, the shared alert rules."""

import os
import unittest
from types import SimpleNamespace

from arb_engine.matching.matcher import MergedEvent
from arb_engine.models import EventInfo, OutcomeQuote
from arb_engine.scanner import analyze_event
from arb_engine.store import Store
from arb_engine.strategy.alerts import Alerter
from arb_engine.strategy.weekscan import WeekScanner

KFEE = {"fee_type": "quadratic", "fee_multiplier": 1}
T0 = 1_800_000_000.0


def _event(key, kc_ask, den_ask, live=False, mtype="moneyline", t=T0):
    info = EventInfo(event_key=key, sport="nfl", market_type=mtype, outcomes=["DEN", "KC"], labels={"DEN": "Denver", "KC": "Kansas City"}, in_play=live)
    k = [OutcomeQuote("kalshi", f"T-{key}-KC", key, "KC", ask=kc_ask, bid=round(kc_ask - .01, 2), ask_size=500, ts=t, fee_params=KFEE, meta={"ticker": f"T-{key}-KC", "side": "yes"})]
    r = [OutcomeQuote("robinhood", f"c-{key}-DEN", key, "DEN", ask=den_ask, bid=round(den_ask - .01, 2), ask_size=500, ts=t, quote_time=t,
                      fee_params={"exchange": "rothera"}, meta={"contract_id": f"c-{key}-DEN", "side": "yes", "exchange": "rothera"}, book_id="rothera")]
    return MergedEvent(key, info, {"kalshi": k, "robinhood": r})


class _Scan:
    """A fake scanner.scan: the given events analysed by the real analyze_event."""

    def __init__(self, events):
        self.events = events
        self.calls = 0

    def __call__(self, sport, adapters, settings=None, executable_venues=None, keep_merged=False, now=None, **kw):
        import dataclasses

        self.calls += 1
        reps = []
        # a real sweep fetches fresh prices: stamp them with the sweep's time
        self.events = [MergedEvent(me.event_key, me.info, {v: [dataclasses.replace(q, ts=now, quote_time=now if q.quote_time is not None else None) for q in qs]
                                                           for v, qs in me.quotes_by_venue.items()}) for me in self.events]
        for me in self.events:
            rep = analyze_event(me, settings or {}, now=now, executable_venues=executable_venues, budget=None)
            rep.live = me.info.in_play
            reps.append(rep)
        return SimpleNamespace(sport=sport, fetched_at=now, events=reps, merged={me.event_key: me for me in self.events})


class _Lane:
    """A fake fast lane: step() returns whatever quotes the test has set."""

    def __init__(self):
        self.quotes, self.seeded, self.steps = {}, {}, 0

    def seed(self, events):
        self.seeded = dict(events)

    def step(self, keys, now=None):
        self.steps += 1
        return {k: self.quotes.get(k, self.seeded.get(k)) for k in keys if k in self.seeded}, []


class WeekScanTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.alerts = Alerter(journal_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), f"week_{os.getpid()}.jsonl"), quiet=True, desktop=False,
                              webhook="", ntfy="t", min_interval_s=0, transport=lambda url, body, headers: self.sent.append((headers["Title"], body.decode())))

    def _ws(self, events, **kw):
        lane = _Lane()
        ws = WeekScanner(["nfl"], [], self.alerts, {}, bankroll=500, scan_fn=_Scan(events), fastlane=lane, **kw)
        return ws, lane

    def test_a_weekday_arb_pushes_and_a_near_lock_is_watched_quietly(self):
        arb = _event("nfl:DEN|KC:2026-09-27", 0.55, 0.36)          # ~+5c: BIG ARB
        near = _event("nfl:BUF|MIA:2026-09-27", 0.61, 0.38)        # -2.7c with fees: inside the 3c band
        far = _event("nfl:NE|NYJ:2026-09-27", 0.62, 0.45)          # ~-10c: ignored
        ws, lane = self._ws([arb, near, far])
        res = ws.full_sweep(T0)
        self.assertEqual([k for k, _ in res["found"] if k != "ARB CLOSE"], ["BIG ARB"])
        self.assertEqual([t for t, _ in self.sent], ["BIG ARB NFL"])       # only the arb reached the phone
        self.assertIn("pre-game", self.sent[0][1])
        self.assertEqual(set(ws.watch), {arb.event_key, near.event_key})
        self.assertEqual(set(lane.seeded), set(ws.watch))
        self.assertTrue(any(e["kind"] == "info" and "near-lock" in e.get("msg", "") for e in self.alerts.events))

    def test_in_play_moneylines_are_left_to_the_live_slate(self):
        live = _event("nfl:DEN|KC:2026-09-27", 0.55, 0.36, live=True)
        ws, _ = self._ws([live])
        self.assertEqual(ws.full_sweep(T0)["found"], [])
        self.assertEqual(self.sent, [])
        line = _event("nfl:DEN|KC:2026-09-27:spread:KC-3.5", 0.55, 0.36, live=True, mtype="spread")
        ws2, _ = self._ws([line])
        self.assertEqual([k for k, _ in ws2.full_sweep(T0)["found"]], ["BIG ARB"])   # in-play lines are ours

    def test_the_fast_watch_catches_a_near_lock_that_crosses_between_sweeps(self):
        near = _event("nfl:BUF|MIA:2026-09-27", 0.61, 0.38)
        ws, lane = self._ws([near], full_every_s=120)
        ws.full_sweep(T0)
        self.assertEqual(self.sent, [])
        lane.quotes[near.event_key] = _event(near.event_key, 0.55, 0.36, t=T0 + 5).quotes_by_venue   # Robinhood's MIA side drops
        found = ws.step(T0 + 5)                                  # a fast step, not a sweep
        self.assertEqual(lane.steps, 1)
        self.assertIn("BIG ARB", [k for k, _ in found])
        self.assertEqual([t for t, _ in self.sent], ["BIG ARB NFL"])

    def test_a_standing_pregame_arb_is_one_push_until_it_grows(self):
        arb = _event("nfl:DEN|KC:2026-09-27", 0.55, 0.36)
        ws, lane = self._ws([arb], prematch_every_s=600)
        ws.full_sweep(T0)
        ws.full_sweep(T0 + 120)
        ws.full_sweep(T0 + 240)
        self.assertEqual(len(self.sent), 1)                     # the same lock is one message
        lane.quotes[arb.event_key] = _event(arb.event_key, 0.53, 0.36, t=T0 + 250).quotes_by_venue   # it grew by 2c
        ws.fast_step(T0 + 250)
        self.assertEqual(len(self.sent), 2)
        ws.full_sweep(T0 + 900)                                  # back at the old price, past the throttle
        self.assertEqual(len(self.sent), 3)

    def test_watch_is_capped_and_forgets_markets_that_drift_away(self):
        evs = [_event(f"nfl:A{i}|B{i}:2026-09-27", 0.61, 0.37 + i * 0.001) for i in range(5)]
        ws, lane = self._ws(evs, max_watch=3, drop_after_s=300)
        ws.full_sweep(T0)
        self.assertEqual(len(ws.watch), 3)
        kept = next(iter(ws.watch))
        lane.quotes = {k: _event(k, 0.70, 0.45, t=T0 + 5).quotes_by_venue for k in ws.watch}   # all drift far away
        ws.fast_step(T0 + 5)
        self.assertIn(kept, ws.watch)                           # not yet: outside for 0 s
        ws.fast_step(T0 + 400)
        self.assertEqual(ws.watch, {})

    def test_only_arbs_and_watched_markets_are_recorded(self):
        st = Store(":memory:")
        arb, near, far = _event("nfl:DEN|KC:2026-09-27", 0.55, 0.36), _event("nfl:BUF|MIA:2026-09-27", 0.61, 0.38), _event("nfl:NE|NYJ:2026-09-27", 0.62, 0.45)
        ws = WeekScanner(["nfl"], [], self.alerts, {}, store=st, bankroll=500, scan_fn=_Scan([arb, near, far]), fastlane=_Lane())
        ws.full_sweep(T0)
        keys = {r[0] for r in st.conn.execute("select event_key from scans")}
        self.assertEqual(keys, {arb.event_key, near.event_key})


if __name__ == "__main__":
    unittest.main()

    def test_run_sweeps_in_the_background_while_the_fast_watch_keeps_going(self):
        """A slow sweep must not stop the fast watch: sweeps run on their own thread."""
        import time as _t

        near = _event("nfl:BUF|MIA:2026-09-27", 0.61, 0.38)
        slow = _Scan([near])

        def slow_scan(*a, **k):
            _t.sleep(0.4)                                    # a sweep far slower than a fast step
            return slow(*a, **k)
        lane = _Lane()
        ws = WeekScanner(["nfl"], [], self.alerts, {}, bankroll=500, scan_fn=slow_scan, fastlane=lane, full_every_s=0.5, fast_every_s=0.05)
        lines = []
        ws.run(duration=1.2, printer=lines.append)
        self.assertGreaterEqual(ws.stats["sweeps"], 1)
        self.assertGreater(lane.steps, 5)                    # fast checks ran during and between sweeps
        self.assertTrue(any("fast checks since the last one" in l for l in lines), lines)
        self.assertFalse(any("error" in l for l in lines), lines)


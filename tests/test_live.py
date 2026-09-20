import os
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from arb_engine.matching.matcher import merge_snapshots
from arb_engine.strategy.alerts import Alerter
from arb_engine.strategy.inplay import InplayView, SideView
from arb_engine.strategy.live import LiveSlate, format_tick
from arb_engine.venues.espn import GameState

from .test_scanner import _adapters


@dataclass
class GS2(GameState):
    """GameState with the P02 fields the recorder and the pre-game anchor read."""
    last_play_id: Optional[str] = None
    suspect: bool = False
    review_pending: bool = False
    sportsbook_ml_home: Optional[float] = None
    sportsbook_ml_away: Optional[float] = None


class FakeStore:
    """Every P07 table writer, recording the shapes it was handed."""

    def __init__(self):
        self.calls: dict[str, list] = {k: [] for k in ("record_espn_tick", "record_tick", "record_steal", "update_ladder", "record_pregame_line")}

    def record_espn_tick(self, gs):
        self.calls["record_espn_tick"].append(gs)

    def record_tick(self, view, quotes_by_venue=None, freshness=None):
        self.calls["record_tick"].append((view, quotes_by_venue, freshness))

    def record_steal(self, **fields):
        self.calls["record_steal"].append(fields)

    def update_ladder(self, now, offsets=(10, 60, 300, 900)):
        self.calls["update_ladder"].append(now)

    def record_pregame_line(self, event_key, sportsbook_ml_home, sportsbook_ml_away, kalshi_mid, ts):
        self.calls["record_pregame_line"].append((event_key, sportsbook_ml_home, sportsbook_ml_away, kalshi_mid, ts))


class OldStore:
    """A recorder from before the tick-level tables: only ``record_tick(view)``."""

    def __init__(self):
        self.views = []

    def record_tick(self, view):
        self.views.append(view)


def _quiet():
    return Alerter(journal_path=os.path.join(os.environ.get("TMPDIR", "/tmp"), "live_test.jsonl"), quiet=True, desktop=False, webhook="")


class FakeFeed:
    def __init__(self, games):
        self._games = games
        self.enriched = 0

    def games(self, date=None):
        return list(self._games)

    def enrich(self, g):
        self.enriched += 1
        g.espn_home_wp = 0.61
        g.enriched = True
        return g


def _undercut_polymarket(merged_events, key, by=0.10):
    """Wrap ``LiveSlate.merged_events`` so the event's Polymarket asks undercut every other
    venue by ``by``: the cheapest ask on the slate then sits on the signal-only venue."""
    def wrapped(errors):
        m = merged_events(errors)
        for q in m[key].quotes_by_venue.get("polymarket", []):
            if q.ask is not None:
                q.ask, q.bid = max(0.02, q.ask - by), max(0.01, (q.bid if q.bid is not None else q.ask) - by)
        return m
    return wrapped


def _moneyline_key(adapters):
    merged = merge_snapshots([ad.fetch("nfl") for ad in adapters])
    for k, me in merged.items():
        if me.info.market_type == "moneyline" and len(me.quotes_by_venue) >= 2:
            return k, me
    raise AssertionError("no two-venue moneyline in fixtures")


class LiveSlateTests(unittest.TestCase):
    def test_tick_prices_live_games_and_reports_missing(self):
        adapters = _adapters()
        key, me = _moneyline_key(adapters)
        away, home = me.info.outcomes[0], me.info.outcomes[1]
        now = 1_800_000_000.0
        live = GameState(event_id="1", home=home, away=away, home_score=14, away_score=10, status="live", period=3, clock_seconds_remaining_in_period=600, game_seconds_remaining=1500, possession="home", down=2, distance=7, yardline_100=45, home_timeouts=3, away_timeouts=2, event_key=key)
        soon = GameState(event_id="2", home="ZZZ", away="YYY", status="pre", start_time=datetime.fromtimestamp(now + 1800, tz=timezone.utc), event_key="nfl:YYY|ZZZ:2026-09-20")
        later = GameState(event_id="3", home="QQQ", away="PPP", status="pre", start_time=datetime.fromtimestamp(now + 5 * 3600, tz=timezone.utc), event_key="nfl:PPP|QQQ:2026-09-20")
        done = GameState(event_id="4", home="A", away="B", status="final", event_key="nfl:A|B:2026-09-13")
        feed = FakeFeed([live, soon, later, done])
        slate = LiveSlate(adapters, feed=feed, settings={}, pre_hours=1.0, steal_edge=0.03, alerter=_quiet())
        tick = slate.tick(now)
        self.assertEqual(tick.games, 2)                       # live + the one starting within 1h
        self.assertEqual(len(tick.views), 1)
        self.assertEqual(len(tick.missing), 1)                # the pre game has no venue quotes in fixtures
        v = tick.views[0]
        self.assertTrue(v.live)
        self.assertIn("Q3", v.game_line)
        self.assertEqual(feed.enriched, 1)
        for sv in v.sides:
            self.assertIsNotNone(sv.fair)
            self.assertIsNotNone(sv.model_p)
            self.assertAlmostEqual(sum(s.fair for s in v.sides), 1.0, places=6)
        self.assertEqual(v.sides[0].espn_p, 0.61 if v.sides[0].outcome == home else 0.39)
        # Second tick within the refresh window reuses the summary.
        slate.tick(now + 5)
        self.assertEqual(feed.enriched, 1)
        slate.tick(now + 40)
        self.assertEqual(feed.enriched, 2)
        text = format_tick(tick)
        self.assertIn("LIVE", text)
        self.assertIn("no quotes:", text)

    def test_recording_call_sites_and_pregame_anchor(self):
        adapters = _adapters()
        key, me = _moneyline_key(adapters)
        away, home = me.info.outcomes[0], me.info.outcomes[1]
        now = 1_800_000_000.0
        live = GS2(event_id="1", home=home, away=away, home_score=14, away_score=10, status="live", period=3, clock_seconds_remaining_in_period=600, game_seconds_remaining=1500, possession="home", down=2, distance=7, yardline_100=45, home_timeouts=3, away_timeouts=2, event_key=key, last_play_id="p7")
        store = FakeStore()
        slate = LiveSlate(adapters, feed=FakeFeed([live]), settings={}, steal_edge=0.03, store=store, alerter=_quiet(), bankroll=500.0)
        tick = slate.tick(now)
        self.assertEqual(tick.errors, [])
        view = tick.views[0]
        # ESPN state, the priced tick with venue L1 + freshness dict, the ladder update.
        self.assertIs(store.calls["record_espn_tick"][0], live)
        v, qbv, fresh = store.calls["record_tick"][0]
        self.assertIs(v, view)
        self.assertEqual(sorted(qbv), sorted(me.quotes_by_venue))      # the merged event's per-venue quotes
        self.assertEqual(set(fresh["mids"]), set(qbv))
        self.assertEqual(set(fresh) >= {"last_state_change_ts", "last_score_change_ts", "mids"}, True)
        self.assertEqual(fresh["last_state_change_ts"], now)
        self.assertEqual(store.calls["update_ladder"], [now])
        # The fixture state is a STEAL (the alerts prove it); the structured record carries it.
        steals = store.calls["record_steal"]
        self.assertEqual(len(steals), 1)
        rec = steals[0]
        self.assertEqual({"ts", "event_key", "outcome", "venue", "ask", "all_in", "fair", "edge", "market_p", "model_p", "espn_p", "gated", "reasons", "period", "clock", "last_play_id", "action"} <= set(rec), True)
        self.assertEqual((rec["event_key"], rec["gated"], rec["reasons"], rec["last_play_id"], rec["period"]), (key, False, [], "p7", 3))
        self.assertTrue(rec["action"].startswith("STEAL"))
        alert = next(e for e in slate.alerts.events if e["kind"] == "alert")
        self.assertEqual(alert["steal"]["outcome"], rec["outcome"])
        # Three identical polls at 10 s are ESPN's normal update lag, not a frozen clock: no gate
        # (the poll-count rule gated a real slate on the first college night).
        slate.tick(now + 10)
        slate.tick(now + 20)
        self.assertEqual([r for r in store.calls["record_steal"] if r["gated"]], [])
        # Same state for 30 s (max(3 x 10 s, 30 s)) with a venue mid 0.03 away from where it was
        # at the last state change: clock-frozen -> a GATED record, journaled as info, no new alert.
        # (stale_after_s is lifted so the feed-stale rule cannot fire on the same move; Kalshi
        # moves *down* so it stays the best ask - the Robinhood fixture quotes are days old.)
        for f in slate._fresh.values():
            f.stale_after_s = 1e9
        orig = slate.merged_events

        def moved(errors):
            m = orig(errors)
            for q in m[key].quotes_by_venue.get("kalshi", []):
                if q.ask is not None and q.bid is not None and q.bid > 0.05:
                    q.ask, q.bid = q.ask - 0.03, q.bid - 0.03
            return m
        slate.merged_events = moved
        slate.tick(now + 30)
        gated = [r for r in store.calls["record_steal"] if r["gated"]]
        self.assertEqual(len(gated), 1)
        self.assertEqual(gated[0]["reasons"], ["clock-frozen"])
        self.assertTrue(any(e["kind"] == "info" and e["msg"].split(": ", 1)[1].startswith("GATED STEAL: wait: clock-frozen") for e in slate.alerts.events))
        self.assertEqual(sum(1 for e in slate.alerts.events if e["kind"] == "alert"), 1)
        self.assertEqual(len(store.calls["record_espn_tick"]), 4)
        slate.merged_events = orig
        # Pre-game: one line anchor per event (sportsbook moneylines + Kalshi mid), not repeated.
        pre = GS2(event_id="1", home=home, away=away, status="pre", start_time=datetime.fromtimestamp(now + 1800, tz=timezone.utc), event_key=key, sportsbook_ml_home=-150, sportsbook_ml_away=130)
        slate2 = LiveSlate(adapters, feed=FakeFeed([pre]), settings={}, store=store, alerter=_quiet())
        slate2.tick(now)
        slate2.tick(now + 10)
        self.assertEqual(len(store.calls["record_pregame_line"]), 1)
        ek, mlh, mla, kmid, ts = store.calls["record_pregame_line"][0]
        self.assertEqual((ek, mlh, mla, ts), (key, -150, 130, now))
        kq = next(q for q in me.quotes_by_venue["kalshi"] if q.outcome == home)
        self.assertAlmostEqual(kmid, kq.mid)

    def test_old_store_without_the_new_hooks_is_tolerated(self):
        adapters = _adapters()
        key, me = _moneyline_key(adapters)
        away, home = me.info.outcomes[0], me.info.outcomes[1]
        live = GameState(event_id="1", home=home, away=away, home_score=14, away_score=10, status="live", period=3, clock_seconds_remaining_in_period=600, game_seconds_remaining=1500, possession="home", event_key=key)
        store = OldStore()
        slate = LiveSlate(adapters, feed=FakeFeed([live]), settings={}, store=store, alerter=_quiet())
        tick = slate.tick(1_800_000_000.0)
        self.assertEqual(tick.errors, [])
        self.assertEqual(len(store.views), 1)

    def test_slate_cap_scales_stakes_proportionally(self):
        def view(key, label, stake, contracts, all_in):
            sv = SideView(outcome=label, label=label, held=0, cost=0, avg_all_in=None, fair=all_in + 0.1, best_venue="kalshi", best_ask=all_in - 0.01, best_all_in=all_in, steal_edge=0.1, steal=True, kelly_stake=stake, kelly_contracts=contracts, suggested_contracts=contracts)
            return InplayView(event_key=key, title=key, live=True, sides=[sv], total_cost=0, payout_if={}, locked_pnl=None, balanced=False, actions=[f"STEAL: {label} all-in {all_in:.3f} on kalshi vs fair {all_in + 0.1:.3f} (+10.0%) → buy {contracts} contracts (0.25×Kelly ${stake:.2f} of $1,000, 500 offered)"])
        views = [view("a", "A", 300.0, 600, 0.5), view("b", "B", 100.0, 250, 0.4)]
        slate = LiveSlate([], feed=FakeFeed([]), settings={}, bankroll=1000.0, slate_cap=200.0, alerter=_quiet())
        scale = slate.apply_slate_cap(views)
        self.assertAlmostEqual(scale, 0.5)
        a, b = views[0].sides[0], views[1].sides[0]
        self.assertEqual((a.suggested_contracts, b.suggested_contracts), (300, 125))
        self.assertEqual((a.kelly_stake, b.kelly_stake), (150.0, 50.0))
        self.assertIn("→ buy 300 contracts (slate cap 50%: 0.25×Kelly $300.00", views[0].actions[0])
        # Under the cap nothing changes; the cap defaults to the bankroll and honours the setting.
        views2 = [view("a", "A", 100.0, 200, 0.5)]
        self.assertEqual(slate.apply_slate_cap(views2), 1.0)
        self.assertEqual(views2[0].sides[0].suggested_contracts, 200)
        self.assertEqual(LiveSlate([], feed=FakeFeed([]), settings={}, bankroll=750.0, alerter=_quiet()).slate_cap, 750.0)
        self.assertEqual(LiveSlate([], feed=FakeFeed([]), settings={"inplay_slate_cap": 400}, bankroll=750.0, alerter=_quiet()).slate_cap, 400.0)

    def test_tick_reports_the_scale(self):
        adapters = _adapters()
        key, me = _moneyline_key(adapters)
        away, home = me.info.outcomes[0], me.info.outcomes[1]
        live = GameState(event_id="1", home=home, away=away, home_score=14, away_score=10, status="live", period=3, clock_seconds_remaining_in_period=600, game_seconds_remaining=1500, possession="home", event_key=key)
        slate = LiveSlate(adapters, feed=FakeFeed([live]), settings={}, bankroll=1000.0, slate_cap=5.0, alerter=_quiet())
        tick = slate.tick(1_800_000_000.0)
        self.assertLess(tick.stake_scale, 1.0)
        self.assertIn("slate cap", format_tick(tick))
        self.assertTrue(any("→ buy" in a and "slate cap" in a for v in tick.views for a in v.actions), [v.actions for v in tick.views])

    def test_cli_plugin_registers_gate_flags_once(self):
        import argparse

        import arb_engine.cli as cli
        from arb_engine.cli_plugins.live_flags import apply_flags, register
        saved = (cli.cmd_live, cli.cmd_inplay)
        ap = argparse.ArgumentParser()
        sub = ap.add_subparsers(dest="cmd")
        lv = sub.add_parser("live")
        lv.add_argument("--record", metavar="DB")          # cli.py already has it: must not be added twice
        ip = sub.add_parser("inplay")
        # A fake cmd_live in place before registration: the wrapper is built around it and
        # installed as cli.cmd_live, which is what maker_flags.run_live (the winning override) calls.
        seen = {}
        cli.cmd_live = lambda a, settings=None: seen.update({"args": a, "settings": settings}) or 0
        try:
            handlers = register(sub, {"live": lv, "inplay": ip, "scan": sub.add_parser("scan")})
        except BaseException:
            cli.cmd_live, cli.cmd_inplay = saved
            raise
        self.assertEqual(sorted(handlers), ["inplay", "live"])
        self.assertIs(cli.cmd_live, handlers["live"])
        self.assertEqual(cli.cmd_live.__name__, "cmd_live_with_gates")
        args = ap.parse_args(["live", "--stale-after", "20", "--cdna-haircut", "0.03", "--slate-cap", "500", "--record", "x.db"])
        settings = apply_flags(args, {})
        self.assertEqual(settings, {"inplay_stale_after_s": 20.0, "inplay_delay_haircut_cdna": 0.03, "inplay_slate_cap": 500.0})
        self.assertEqual(os.environ.get("INPLAY_STALE_AFTER_S"), "20.0")
        for k in ("INPLAY_STALE_AFTER_S", "INPLAY_DELAY_HAIRCUT_CDNA", "INPLAY_SLATE_CAP"):
            os.environ.pop(k, None)
        ipargs = ap.parse_args(["inplay", "--record", "y.db"])
        self.assertEqual((ipargs.record, ipargs.stale_after), ("y.db", None))
        self.assertFalse(hasattr(ipargs, "slate_cap"))
        try:
            # Registering again is a no-op for the flags and the wrapper (idempotent under a double plugin load).
            again = register(sub, {"live": lv, "inplay": ip})
            self.assertEqual(sum(1 for a in lv._actions if "--stale-after" in a.option_strings), 1)
            self.assertIs(again["live"], handlers["live"])
            self.assertIs(cli.cmd_live, handlers["live"])
            # The wrapped handler forwards to cmd_live with the settings dict filled in.
            rc = handlers["live"](args, {"robinhood_gold": True})
            self.assertEqual(rc, 0)
            self.assertEqual(seen["settings"]["inplay_stale_after_s"], 20.0)
            self.assertTrue(seen["settings"]["robinhood_gold"])
            # The real dispatch path: maker_flags.run_live wins the `live` override (m > l in
            # name order) and calls cli.cmd_live; the flags still land (they were silently
            # dropped before the wrapper was installed on the module).
            from arb_engine.cli_plugins.maker_flags import run_live
            seen.clear()
            qargs = ap.parse_args(["live", "--quiet", "--slate-cap", "50"])
            qargs.venues = "kalshi,robinhood"
            run_live(qargs, {})
            self.assertEqual(seen["settings"], {"inplay_slate_cap": 50.0, "inplay_quiet": True})
            # A fake swapped in *after* registration is still what the wrapper calls.
            later = {}
            cli.cmd_live = lambda a, settings=None: later.update({"settings": settings}) or 0
            handlers["live"](args, {})
            self.assertEqual(later["settings"]["inplay_stale_after_s"], 20.0)
        finally:
            cli.cmd_live, cli.cmd_inplay = saved
            for k in ("INPLAY_STALE_AFTER_S", "INPLAY_DELAY_HAIRCUT_CDNA", "INPLAY_SLATE_CAP", "INPLAY_QUIET"):
                os.environ.pop(k, None)

    def test_feed_stale_in_the_slate_loop(self):
        """Fixture-driven loop: ESPN unchanged, venues move -> 'wait: feed-stale' instead of STEAL."""
        adapters = _adapters()
        key, me = _moneyline_key(adapters)
        away, home = me.info.outcomes[0], me.info.outcomes[1]
        live = GameState(event_id="1", home=home, away=away, home_score=14, away_score=10, status="live", period=3, clock_seconds_remaining_in_period=600, game_seconds_remaining=1500, possession="home", event_key=key)
        slate = LiveSlate(adapters, feed=FakeFeed([live]), settings={"inplay_stale_after_s": 15.0}, alerter=_quiet())
        now = 1_800_000_000.0
        first = slate.tick(now)
        self.assertTrue(any(a.startswith("STEAL") for v in first.views for a in v.actions))
        # Move every venue mid by 0.05 with the same ESPN state 20 s later.
        orig = slate.merged_events

        def moved(errors):
            m = orig(errors)
            for q in m[key].quotes_by_venue.get("kalshi", []) + m[key].quotes_by_venue.get("robinhood", []) + m[key].quotes_by_venue.get("polymarket", []):
                if q.ask is not None and q.bid is not None and q.ask < 0.9:
                    q.ask, q.bid = q.ask + 0.05, q.bid + 0.05
            return m
        slate.merged_events = moved
        second = slate.tick(now + 20)
        acts = [a for v in second.views for a in v.actions]
        self.assertFalse(any(a.startswith("STEAL") for a in acts), acts)
        self.assertTrue(any(a.startswith("GATED STEAL: wait: feed-stale") for a in acts), acts)
        self.assertIn("[gated: feed-stale]", format_tick(second))

    def test_executable_set_is_resolved_once_and_polymarket_is_signal_only(self):
        """The slate resolves the compliance table once and hands it to every evaluation:
        a STEAL line never names Polymarket for a US account, whatever its ask."""
        adapters = _adapters()
        key, me = _moneyline_key(adapters)
        away, home = me.info.outcomes[0], me.info.outcomes[1]
        live = GameState(event_id="1", home=home, away=away, home_score=14, away_score=10, status="live", period=3, clock_seconds_remaining_in_period=600, game_seconds_remaining=1500, possession="home", event_key=key)
        slate = LiveSlate(adapters, feed=FakeFeed([live]), settings={}, steal_edge=0.03, alerter=_quiet(), bankroll=500.0)
        self.assertEqual(slate.executable_venues, {"kalshi", "robinhood"})
        self.assertEqual(LiveSlate([], feed=FakeFeed([]), settings={"executable_venues": "all"}).executable_venues, None)
        self.assertEqual(LiveSlate([], feed=FakeFeed([]), settings={}, executable_venues=["Kalshi"]).executable_venues, {"kalshi"})
        # Undercut every executable ask with a Polymarket quote 0.10 cheaper on both sides.
        slate.merged_events = _undercut_polymarket(slate.merged_events, key)
        tick = slate.tick(1_800_000_000.0)
        self.assertEqual(tick.errors, [])
        v = tick.views[0]
        self.assertEqual(v.executable_venues, ["kalshi", "robinhood"])
        acts = [a for a in v.actions if a.startswith(("STEAL", "GATED STEAL"))]
        self.assertTrue(acts, v.actions)
        self.assertFalse(any("on polymarket" in a.split("[")[0] for a in acts), acts)
        for sv in v.sides:
            self.assertNotEqual(sv.exec_venue, "polymarket")
            if sv.steal or sv.steal_gated:
                self.assertIn(sv.best_venue, ("kalshi", "robinhood"))
            if sv.best_ineligible:
                self.assertIsNone(sv.suggested_contracts)
        text = format_tick(tick)
        self.assertIn("signal only", text)
        self.assertNotIn("SIGNAL ONLY  →", text)                      # a signal-only side never carries a size
        # Every alert names an executable venue.
        for e in slate.alerts.events:
            if e["kind"] == "alert":
                self.assertIn(e["steal"]["venue"], ("kalshi", "robinhood"))
        # Opted in, the same slate steals on Polymarket.
        opted = LiveSlate(adapters, feed=FakeFeed([live]), settings={"executable_venues": "all"}, steal_edge=0.03, alerter=_quiet(), bankroll=500.0)
        opted.merged_events = _undercut_polymarket(opted.merged_events, key)
        got = opted.tick(1_800_000_000.0)
        self.assertTrue(any(a.startswith("STEAL") and "on polymarket" in a for v in got.views for a in v.actions), [v.actions for v in got.views])

    def test_quiet_tick_prints_signals_and_one_summary_line(self):
        adapters = _adapters()
        key, me = _moneyline_key(adapters)
        away, home = me.info.outcomes[0], me.info.outcomes[1]
        now = 1_800_000_000.0
        live = GameState(event_id="1", home=home, away=away, home_score=14, away_score=10, status="live", period=3, clock_seconds_remaining_in_period=600, game_seconds_remaining=1500, possession="home", event_key=key)
        soon = GameState(event_id="2", home="ZZZ", away="YYY", status="pre", start_time=datetime.fromtimestamp(now + 1800, tz=timezone.utc), event_key="nfl:YYY|ZZZ:2026-09-20")
        loud = LiveSlate(adapters, feed=FakeFeed([live, soon]), settings={}, steal_edge=0.03, alerter=_quiet(), bankroll=500.0)
        self.assertFalse(loud.quiet)                                      # default unchanged
        tick = loud.tick(now)
        full, quiet = format_tick(tick), format_tick(tick, quiet=True)
        self.assertIn("[mkt ", full)                                     # the per-side rows
        self.assertIn("no quotes:", full)
        self.assertGreater(len(full.splitlines()), len(quiet.splitlines()))
        lines = quiet.splitlines()
        self.assertIn("1 game(s) priced, 1 without venue quotes; 1 live, ", lines[0])
        self.assertRegex(lines[0], r"\d+ STEAL, \d+ LOCK, \d+ gated, \d+ signal-only$")
        self.assertTrue(all(l.startswith("    LIVE ") and "  ->  " in l for l in lines[1:]), lines)
        self.assertTrue(all(("STEAL" in l or "LOCK" in l or "GATED" in l) for l in lines[1:]), lines)
        self.assertNotIn("[mkt ", quiet)                                  # no per-side rows
        self.assertNotIn("no quotes:", quiet)
        # The slate's own flag (--quiet / INPLAY_QUIET) rides on the tick, so run() prints it quiet.
        q1 = LiveSlate(adapters, feed=FakeFeed([live, soon]), settings={}, steal_edge=0.03, alerter=_quiet(), quiet=True)
        q2 = LiveSlate(adapters, feed=FakeFeed([live, soon]), settings={"inplay_quiet": True}, steal_edge=0.03, alerter=_quiet())
        for slate in (q1, q2):
            self.assertTrue(slate.quiet)
            t = slate.tick(now)
            self.assertTrue(t.quiet)
            self.assertEqual(format_tick(t), format_tick(t, quiet=True))
        printed = []
        q1.run(interval=0, duration=0.5, max_iterations=1, printer=printed.append)
        self.assertEqual(len(printed), 1)
        self.assertNotIn("[mkt ", printed[0])
        # Errors still print in quiet mode.
        t.errors.append("kalshi: boom")
        self.assertIn("    error: kalshi: boom", format_tick(t))
        # No games: the same one-liner either way.
        empty = LiveSlate(adapters, feed=FakeFeed([]), settings={}, quiet=True).tick(now)
        self.assertIn("no live games", format_tick(empty))

    def test_cli_quiet_and_bare_record_flags(self):
        from arb_engine.cli import build_parser
        parser, _ = build_parser()
        a = parser.parse_args(["live", "--quiet", "--record"])
        self.assertEqual((a.quiet, a.record), (True, "out/history.db"))
        a = parser.parse_args(["live", "--record", "x.db"])
        self.assertEqual((a.quiet, a.record), (False, "x.db"))
        self.assertIsNone(parser.parse_args(["live"]).record)
        self.assertEqual(parser.parse_args(["inplay", "https://robinhood.com/x", "--record"]).record, "out/history.db")
        # The nargs='?' gotcha: a bare --record directly before the url takes the url as the DB
        # path; another flag (or the url first) ends the optional. Documented, not fought.
        self.assertEqual(parser.parse_args(["inplay", "--record", "--once", "https://robinhood.com/x"]).record, "out/history.db")
        self.assertEqual(parser.parse_args(["inplay", "--record", "z.db", "https://robinhood.com/x"]).record, "z.db")
        self.assertEqual(parser.parse_args(["scan", "--record"]).record, "out/history.db")
        self.assertEqual(parser.parse_args(["scan", "--record", "y.db"]).record, "y.db")
        from arb_engine.cli_plugins.live_flags import apply_flags
        settings = apply_flags(parser.parse_args(["live", "--quiet"]), {})
        self.assertTrue(settings["inplay_quiet"])
        self.assertEqual(os.environ.pop("INPLAY_QUIET", None), "1")

    def test_no_games(self):
        slate = LiveSlate(_adapters(), feed=FakeFeed([]), settings={})
        tick = slate.tick(1_800_000_000.0)
        self.assertEqual((tick.games, tick.views), (0, []))
        self.assertIn("no live games", format_tick(tick))


if __name__ == "__main__":
    unittest.main()

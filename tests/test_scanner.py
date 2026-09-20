"""End-to-end: three recorded venues -> one merged NFL event -> fee-aware report."""

import contextlib
import json
import sys
import types
import unittest

from arb_engine.matching.matcher import MergedEvent, merge_snapshots
from arb_engine.models import EventInfo, OutcomeQuote
from arb_engine.scanner import EventReport, ScanResult, _dedupe_same_book, analyze_event, cross_book_pairs, is_gated, resolve_executable_venues, scan
from arb_engine.venues.kalshi import KalshiAdapter, KalshiClient
from arb_engine.venues.polymarket import PolymarketAdapter
from arb_engine.venues.robinhood import RobinhoodAdapter

from .helpers import FIXTURE_NOW, FakeHttp, load


@contextlib.contextmanager
def stub_module(name, **attrs):
    """Install (attrs) or remove (attrs empty -> ImportError) a module for the duration."""
    saved = sys.modules.get(name, "<absent>")
    sys.modules[name] = types.ModuleType(name) if attrs else None  # None makes `import` raise ImportError
    if attrs:
        for k, v in attrs.items():
            setattr(sys.modules[name], k, v)
    try:
        yield
    finally:
        if saved == "<absent>":
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved


KEY = "nfl:AAA|BBB:2026-09-20"


def _q(venue, mid, outcome, ask, **kw):
    fee = kw.pop("fee_params", {"fee_type": "quadratic_with_maker_fees"} if venue == "kalshi" else {"exchange": "rothera"} if venue == "robinhood" else {"feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True}, "feesEnabled": True})
    return OutcomeQuote(venue, mid, KEY, outcome, ask=ask, bid=round(ask - 0.02, 4), fee_params=fee, ts=1000.0, **kw)


def _tie_event(rothera_yes_b=0.47, rothera_no_a=0.47, kalshi_yes_a=0.52, extra=None):
    """Kalshi YES-A vs Rothera YES-B / NO-A (the plan's worked example)."""
    info = EventInfo(event_key=KEY, sport="nfl", market_type="moneyline", outcomes=["AAA", "BBB"], labels={"AAA": "Aaa", "BBB": "Bbb"}, tie_rule="half", venues={"kalshi": {}, "robinhood": {}})
    rh = [
        _q("robinhood", "ra", "AAA", 0.55, meta={"side": "yes", "tie_payout": 0.0, "exchange": "rothera"}, book_id="rothera", quote_time=900.0),
        _q("robinhood", "rb", "BBB", rothera_yes_b, meta={"side": "yes", "tie_payout": 0.0, "exchange": "rothera"}, book_id="rothera", quote_time=900.0),
        _q("robinhood", "ra#no", "BBB", rothera_no_a, meta={"side": "no", "tie_payout": 1.0, "exchange": "rothera"}, book_id="rothera", quote_time=950.0),
        _q("robinhood", "rb#no", "AAA", 0.56, meta={"side": "no", "tie_payout": 1.0, "exchange": "rothera"}, book_id="rothera", quote_time=950.0),
    ]
    kal = [_q("kalshi", "ka", "AAA", kalshi_yes_a, meta={"side": "yes", "tie_payout": 0.5}), _q("kalshi", "kb", "BBB", 0.50, meta={"side": "yes", "tie_payout": 0.5})]
    qbv = {"kalshi": kal, "robinhood": rh}
    if extra:
        qbv.update(extra)
    return MergedEvent(KEY, info, qbv)


def _adapters():
    lines = load("kalshi_markets_nfl_lines.json")
    kal = FakeHttp({"series_ticker=KXNFLGAME&": load("kalshi_markets_nfl.json"), "series_ticker=KXNFLSPREAD&": {"markets": lines["spreads"]}, "series_ticker=KXNFLTOTAL&": {"markets": lines["totals"]}, "/series/KXNFLGAME": load("kalshi_series_kxnflgame.json"), "/series/": {"series": {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}}})
    events = load("polymarket_events_nfl.json")
    poly = FakeHttp({"gamma-api.polymarket.com/events": lambda: list(events)})
    pp = load("robinhood_page_props_nfl.json")
    html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": pp}}) + "</script>"
    rh = FakeHttp({"/us/en/prediction-markets/nfl/": html})
    return [KalshiAdapter(client=KalshiClient(env="prod", http=kal)), PolymarketAdapter(http=poly), RobinhoodAdapter(http=rh, refresh_quotes=False)]


class ScannerTests(unittest.TestCase):
    def test_scan_merges_three_venues(self):
        # executable_venues="all": all three venues may be legs (the compliance default keeps
        # Polymarket signal-only; see test_scan_default_keeps_polymarket_signal_only).
        res = scan("nfl", _adapters(), now=FIXTURE_NOW, settings={"robinhood_gold": False, "executable_venues": "all"})
        self.assertEqual(res.errors, {})
        keys = {e.event_key for e in res.events}
        self.assertIn("nfl:BUF|DET:2026-09-17", keys)
        ev = next(e for e in res.events if e.event_key == "nfl:BUF|DET:2026-09-17")
        self.assertEqual(ev.venues, ["kalshi", "polymarket", "robinhood"])
        self.assertEqual(ev.tie_rule, "half")
        self.assertIsNotNone(ev.margin)
        # Kalshi DET 0.33 + Polymarket BUF 0.67 = 1.00 gross -> negative after fees.
        self.assertAlmostEqual(ev.gross_sum, 1.00, places=6)
        self.assertLess(ev.margin, 0)
        det = next(o for o in ev.outcomes if o.outcome == "DET")
        self.assertEqual(det.label, "Detroit")
        self.assertAlmostEqual(det.fair, 0.33, delta=0.02)
        venues = {v.venue: v for v in det.venues}
        self.assertEqual(set(venues), {"kalshi", "polymarket", "robinhood"})
        self.assertEqual(venues["robinhood"].exchange, "rothera")
        self.assertIsNone(venues["robinhood"].mirror_of)  # Rothera is its own book
        # Robinhood all-in = 0.34 + 0.02 fees; Kalshi = 0.33 + 0.0155.
        self.assertAlmostEqual(venues["robinhood"].all_in, 0.36, places=6)
        self.assertAlmostEqual(venues["kalshi"].all_in, 0.33 + 0.0155, places=6)
        # Max-buy prices exist and maker >= taker.
        for v in det.venues:
            self.assertIsNotNone(v.max_buy_price)
            self.assertGreaterEqual(v.max_buy_maker, v.max_buy_price)
        self.assertLess(venues["kalshi"].max_buy_price, 0.33)  # no arb today
        self.assertEqual(res.arbs(), [])

    def test_scan_default_keeps_polymarket_signal_only(self):
        res = scan("nfl", _adapters(), now=FIXTURE_NOW, settings={"robinhood_gold": False})
        ev = next(e for e in res.events if e.event_key == "nfl:BUF|DET:2026-09-17")
        self.assertIn("signal-only:polymarket", ev.flags)
        self.assertNotIn("polymarket", {l["venue"] for l in ev.arb["legs"]})
        self.assertEqual(ev.venues, ["kalshi", "polymarket", "robinhood"])  # the row stays for the fair value

    def test_spread_and_total_merge_across_three_venues(self):
        res = scan("nfl", _adapters(), now=FIXTURE_NOW, settings={})
        sp = next(e for e in res.events if e.event_key == "nfl:BUF|DET:2026-09-17:spread:BUF-1.5")
        self.assertEqual(sp.venues, ["kalshi", "polymarket", "robinhood"])
        self.assertEqual((sp.market_type, sp.line, sp.tie_rule), ("spread", 1.5, "no_push"))
        self.assertEqual(sp.title, "Buffalo -1.5 vs Detroit +1.5")
        cover = next(o for o in sp.outcomes if o.outcome == "BUF-1.5")
        self.assertEqual({v.venue for v in cover.venues}, {"kalshi", "polymarket", "robinhood"})
        tot = next(e for e in res.events if e.event_key == "nfl:BUF|DET:2026-09-17:total:49.5")
        self.assertEqual(tot.venues, ["kalshi", "polymarket", "robinhood"])
        self.assertEqual(tot.title, "DET @ BUF total 49.5 (over / under)")
        under = next(o for o in tot.outcomes if o.outcome == "under")
        kal = next(v for v in under.venues if v.venue == "kalshi")
        self.assertEqual(kal.ask, 0.38)  # NO side of the over market
        # Market-type filter.
        only_ml = scan("nfl", _adapters(), now=FIXTURE_NOW, settings={}, market_types={"moneyline"})
        self.assertTrue(all(e.market_type == "moneyline" for e in only_ml.events))
        self.assertEqual(len(only_ml.events), 2)

    def test_thin_arb_is_not_fillable(self):
        adapters = _adapters()
        snaps = [a.fetch("nfl") for a in adapters]
        # Force a fee-beating price on the Robinhood under with tiny size.
        for q in snaps[2].quotes:
            if q.outcome == "under":
                q.ask, q.ask_size = 0.30, 0.5
        merged = merge_snapshots(snaps)
        rep = analyze_event(merged["nfl:BUF|DET:2026-09-17:total:49.5"], settings={})
        self.assertGreater(rep.margin, 0)
        self.assertFalse(rep.fillable)
        self.assertIn("thin", rep.flags)
        for q in snaps[2].quotes:
            if q.outcome == "under":
                q.ask_size = 50
        merged = merge_snapshots(snaps)
        rep = analyze_event(merged["nfl:BUF|DET:2026-09-17:total:49.5"], settings={})
        self.assertTrue(rep.fillable)
        self.assertEqual(rep.sized_arb["contracts"], 50)

    def test_kalshi_routed_robinhood_quote_is_marked_mirror(self):
        adapters = _adapters()
        snaps = [a.fetch("nfl") for a in adapters]
        rh = snaps[2]
        for q in rh.quotes:
            q.book_id = "kalshi"
            q.fee_params["exchange"] = "kalshi"
        merged = merge_snapshots(snaps)
        rep = analyze_event(merged["nfl:BUF|DET:2026-09-17"], settings={})
        det = next(o for o in rep.outcomes if o.outcome == "DET")
        rhp = next(v for v in det.venues if v.venue == "robinhood")
        self.assertEqual(rhp.mirror_of, "kalshi")
        # The arb legs must never pair the mirror with its own book.
        self.assertNotIn("robinhood", {l["venue"] for l in rep.arb["legs"]})

    def test_stale_snapshot_excluded(self):
        adapters = _adapters()
        snaps = [a.fetch("nfl") for a in adapters]
        for q in snaps[0].quotes:
            q.ts = 1.0  # ancient
        merged = merge_snapshots(snaps)
        rep = analyze_event(merged["nfl:BUF|DET:2026-09-17"], settings={}, max_quote_age=60, now=1000.0)
        self.assertIn("stale-quote", rep.flags)
        self.assertNotIn("kalshi", {l["venue"] for l in rep.arb["legs"]})




class TieAwareTests(unittest.TestCase):
    def test_rothera_no_leg_survives_dedupe_and_wins_at_equal_ask(self):
        me = _tie_event()
        kept, mirrors = _dedupe_same_book(me.quotes_for_outcome("BBB"))
        self.assertEqual({q.venue_market_id for q in kept}, {"kb", "rb", "ra#no"})  # YES-B and NO-A are different contracts
        self.assertEqual(mirrors, {})
        rep = analyze_event(me, {}, now=1000.0)
        legs = {l["outcome"]: l for l in rep.arb["legs"]}
        self.assertEqual((legs["AAA"]["venue"], legs["BBB"]["market_id"], legs["BBB"]["side"]), ("kalshi", "ra#no", "no"))
        self.assertAlmostEqual(rep.tie_payout_total, 1.5)
        self.assertAlmostEqual(rep.tie_margin, rep.margin + 0.5)
        self.assertIn("tie-rule-mismatch:kalshi=0.5,robinhood=1", rep.flags)
        bbb = next(o for o in rep.outcomes if o.outcome == "BBB")
        rows = {v.market_id: v for v in bbb.venues}
        self.assertEqual((rows["ra#no"].side, rows["ra#no"].tie_payout, rows["rb"].tie_payout), ("no", 1.0, 0.0))
        # Both Rothera contracts sit on the same book; the NO leg is not a mirror of the YES.
        self.assertIsNone(rows["ra#no"].mirror_of)

    def test_yes_b_lock_is_flagged_and_loses_on_a_tie(self):
        me = _tie_event(rothera_no_a=0.60)  # NO-A too dear: YES-B is the leg
        rep = analyze_event(me, {}, now=1000.0)
        legs = {l["outcome"]: l for l in rep.arb["legs"]}
        self.assertEqual(legs["BBB"]["market_id"], "rb")
        self.assertAlmostEqual(rep.tie_payout_total, 0.5)
        self.assertLess(rep.tie_margin, 0)
        self.assertAlmostEqual(rep.tie_margin, rep.margin - 0.5)
        self.assertIn("tie-rule-mismatch:kalshi=0.5,robinhood=0", rep.flags)

    def test_mismatch_flag_only_when_the_chosen_legs_differ(self):
        # Kalshi both sides (mirror-free): payouts agree -> no flag.
        info = EventInfo(event_key=KEY, sport="nfl", market_type="moneyline", outcomes=["AAA", "BBB"], tie_rule="half")
        me = MergedEvent(KEY, info, {"kalshi": [_q("kalshi", "ka", "AAA", 0.52, meta={"tie_payout": 0.5}), _q("kalshi", "kb", "BBB", 0.46, meta={"tie_payout": 0.5})]})
        rep = analyze_event(me, {}, now=1000.0)
        self.assertFalse(any(f.startswith("tie-rule-mismatch") for f in rep.flags))
        self.assertAlmostEqual(rep.tie_margin, rep.margin)
        # Fixture scan: every Kalshi x Rothera pair reports a tie margin.
        res = scan("nfl", _adapters(), now=FIXTURE_NOW, settings={})
        ev = next(e for e in res.events if e.event_key == "nfl:BUF|DET:2026-09-17")
        self.assertIsNotNone(ev.tie_margin)
        pairs = [p for p in cross_book_pairs(_merged_nfl()["nfl:BUF|DET:2026-09-17"], {}) if {l["book"] for l in p["legs"]} == {"kalshi", "rothera"}]
        self.assertTrue(pairs)
        for p in pairs:
            sides = tuple(l["side"] for l in p["legs"])
            if "no" in sides:
                self.assertGreater(p["tie_margin"], p["margin"])   # a NO leg adds $0.50 or more on a tie
            else:
                self.assertAlmostEqual(p["tie_margin"], p["margin"] - 0.5)  # YES + YES loses half the stake

    def test_scan_emits_no_legs_by_setting(self):
        res = scan("nfl", _adapters(), now=FIXTURE_NOW, settings={})
        ev = next(e for e in res.events if e.event_key == "nfl:BUF|DET:2026-09-17")
        ids = {v.market_id for o in ev.outcomes for v in o.venues if v.venue == "robinhood"}
        self.assertEqual(sum(1 for i in ids if i.endswith("#no")), 2)
        res_off = scan("nfl", _adapters(), now=FIXTURE_NOW, settings={"rothera_no_leg": False})
        ev = next(e for e in res_off.events if e.event_key == "nfl:BUF|DET:2026-09-17")
        ids = {v.market_id for o in ev.outcomes for v in o.venues if v.venue == "robinhood"}
        self.assertFalse(any(i.endswith("#no") for i in ids))
        self.assertEqual(sum(1 for o in ev.outcomes for v in o.venues if v.venue == "robinhood"), 2)

    def test_no_leg_does_not_move_the_consensus_fair_value(self):
        # consensus_fair_value keeps the last quote per (venue, outcome); the NO legs sit on the
        # other team's outcome and would overwrite the YES quotes, so fair/edge must come from
        # the YES-side view whether or not the NO legs are emitted.
        on = {e.event_key: e for e in scan("nfl", _adapters(), now=FIXTURE_NOW, settings={"rothera_no_leg": True}).events}
        off = {e.event_key: e for e in scan("nfl", _adapters(), now=FIXTURE_NOW, settings={"rothera_no_leg": False}).events}
        self.assertEqual(set(on), set(off))
        checked = 0
        for key, ev in on.items():
            for a, b in zip(ev.outcomes, off[key].outcomes):
                self.assertEqual(a.outcome, b.outcome)
                self.assertEqual(a.fair, b.fair, key)
                checked += 1
        self.assertGreater(checked, 0)
        ev = on["nfl:PHI|TEN:2026-09-20"]
        self.assertAlmostEqual(next(o for o in ev.outcomes if o.outcome == "PHI").fair, 0.7562, places=4)

    def test_fair_value_view_prefers_yes_and_falls_back_to_no(self):
        from arb_engine.scanner import fair_value_view
        mk = lambda o, mid, side, **m: OutcomeQuote(venue="robinhood", venue_market_id=f"{o}-{side}", event_key="k", outcome=o, outcome_label=o, ask=mid + 0.01, bid=mid - 0.01, meta={"side": side, **m})  # noqa: E731
        yes_a, no_a, yes_b, no_b = mk("A", 0.60, "yes"), mk("B", 0.45, "no", no_of="A"), mk("B", 0.40, "yes"), mk("A", 0.55, "no", no_of="B")
        view = fair_value_view({"robinhood": [yes_a, no_a, yes_b, no_b], "kalshi": [mk("A", 0.61, "yes")]})
        self.assertEqual([q.venue_market_id for q in view["robinhood"]], ["A-yes", "B-yes"])
        self.assertEqual(len(view["kalshi"]), 1)
        # Contract B missing: its NO side is the only quote on A, so it stays.
        view = fair_value_view({"robinhood": [no_b, yes_b]})
        self.assertEqual([q.venue_market_id for q in view["robinhood"]], ["A-no", "B-yes"])

    def test_kalshi_routed_no_leg_still_collapses_into_kalshi_direct(self):
        adapters = _adapters()
        snaps = [a.fetch("nfl") if a.venue != "robinhood" else a.fetch("nfl", emit_no_side=True) for a in adapters]
        for q in snaps[2].quotes:
            q.book_id = "kalshi"
            q.fee_params["exchange"] = "kalshi"
        merged = merge_snapshots(snaps)
        rep = analyze_event(merged["nfl:BUF|DET:2026-09-17"], settings={})
        self.assertNotIn("robinhood", {l["venue"] for l in rep.arb["legs"]})
        det = next(o for o in rep.outcomes if o.outcome == "DET")
        self.assertTrue(all(v.mirror_of == "kalshi" for v in det.venues if v.venue == "robinhood"))


def _merged_nfl():
    adapters = _adapters()
    return merge_snapshots([a.fetch("nfl") if a.venue != "robinhood" else a.fetch("nfl", emit_no_side=True) for a in adapters])


class SignalOnlyAndSizeTests(unittest.TestCase):
    def test_signal_only_row_kept_but_never_a_leg(self):
        pm = [_q("polymarket", "pa", "AAA", 0.50, meta={"tick": 0.01}), _q("polymarket", "pb", "BBB", 0.45, meta={"tick": 0.01})]
        me = _tie_event(extra={"polymarket": pm})
        rep = analyze_event(me, {}, now=1000.0, executable_venues={"kalshi", "robinhood"})
        self.assertIn("signal-only:polymarket", rep.flags)
        self.assertNotIn("polymarket", {l["venue"] for l in rep.arb["legs"]})
        bbb = next(o for o in rep.outcomes if o.outcome == "BBB")
        row = next(v for v in bbb.venues if v.venue == "polymarket")
        self.assertEqual(row.ineligible, "not executable")
        self.assertIsNotNone(row.all_in)
        self.assertNotEqual(bbb.best_buy_venue, "polymarket")
        self.assertIsNotNone(bbb.fair)  # still feeds the consensus
        # Unrestricted: the cheaper Polymarket leg is chosen and no row is ineligible.
        rep2 = analyze_event(me, {}, now=1000.0)
        self.assertIn("polymarket", {l["venue"] for l in rep2.arb["legs"]})
        self.assertFalse(any(v.ineligible for o in rep2.outcomes for v in o.venues))

    def test_restricted_meta_makes_the_venue_signal_only(self):
        # The compliance table (not the per-quote flag) decides: Polymarket is signal-only by
        # default and an explicit executable_venues override may still allow it.
        pm = [_q("polymarket", "pa", "AAA", 0.50, meta={"restricted": True}), _q("polymarket", "pb", "BBB", 0.45, meta={"restricted": True})]
        rep = analyze_event(_tie_event(extra={"polymarket": pm}), {}, now=1000.0, executable_venues=resolve_executable_venues({}))
        self.assertIn("signal-only:polymarket", rep.flags)
        self.assertNotIn("polymarket", {l["venue"] for l in rep.arb["legs"]})
        rep2 = analyze_event(_tie_event(extra={"polymarket": pm}), {}, now=1000.0, executable_venues=resolve_executable_venues({"executable_venues": "kalshi,robinhood,polymarket"}))
        self.assertNotIn("signal-only:polymarket", rep2.flags)

    def test_resolve_executable_venues(self):
        self.assertEqual(resolve_executable_venues({}), {"kalshi", "robinhood"})  # the compliance table
        # None = unset (config.load_settings emits every declared key): still the table; "all" lifts it.
        self.assertEqual(resolve_executable_venues({"executable_venues": None}), {"kalshi", "robinhood"})
        self.assertIsNone(resolve_executable_venues({"executable_venues": "all"}))
        self.assertIsNone(resolve_executable_venues({"executable_venues": ["*"]}))
        self.assertEqual(resolve_executable_venues({"executable_venues": "kalshi, robinhood"}), {"kalshi", "robinhood"})
        self.assertEqual(resolve_executable_venues({"executable_venues": ["kalshi"]}), {"kalshi"})
        self.assertEqual(resolve_executable_venues({}, explicit=["robinhood"]), {"robinhood"})
        with stub_module("arb_engine.compliance", executable_venues=lambda settings=None, home_state=None: ["kalshi"]):
            self.assertEqual(resolve_executable_venues({}), {"kalshi"})
        with stub_module("arb_engine.compliance"):
            self.assertIsNone(resolve_executable_venues({}))

    def test_polymarket_tail_uses_its_0_001_grid_and_min_size(self):
        info = EventInfo(event_key=KEY, sport="nfl", market_type="moneyline", outcomes=["AAA", "BBB"], tie_rule="half")
        pm_tail = _q("polymarket", "pa", "AAA", 0.004, ask_size=3, meta={"tick": 0.001, "min_size": 5})
        kal = _q("kalshi", "kb", "BBB", 0.97, ask_size=500)
        me = MergedEvent(KEY, info, {"polymarket": [pm_tail], "kalshi": [kal]})
        rep = analyze_event(me, {}, now=1000.0)
        self.assertTrue(rep.arb["is_arb"])
        self.assertIn("below-min-size", rep.flags)   # 3 shares on offer, Polymarket wants 5
        self.assertIn("thin", rep.flags)
        self.assertFalse(rep.fillable)
        aaa = next(o for o in rep.outcomes if o.outcome == "AAA")
        row = aaa.venues[0]
        self.assertEqual(row.tick, 0.001)
        self.assertEqual(row.max_buy_price, 0.026)   # three decimals: the 0.001 grid
        self.assertEqual(row.max_buy_maker, 0.027)   # no taker fee for a resting order: one more 0.001 tick
        self.assertTrue(is_gated(rep.flags))
        pm_tail.ask_size = 40
        rep2 = analyze_event(me, {}, now=1000.0)
        self.assertNotIn("below-min-size", rep2.flags)
        self.assertTrue(rep2.fillable)
        self.assertEqual(rep2.sized_arb["contracts"], 40)
        self.assertNotIn("thin", rep2.flags)


class RegistryGateTests(unittest.TestCase):
    def test_settlement_flags_present_only_when_the_registry_imports(self):
        seen = []

        def pair_flags(a, b, sport, market_type):
            seen.append((a.venue, b.venue, sport, market_type))
            return ["tie-rule-derived"]

        def tennis_pair_flags(pair, settings):
            return []

        with stub_module("arb_engine.matching.settlement_rules", pair_flags=pair_flags, tennis_pair_flags=tennis_pair_flags):
            rep = analyze_event(_tie_event(), {}, now=1000.0)
        self.assertIn("tie-rule-derived", rep.flags)
        self.assertEqual(seen, [("kalshi", "robinhood", "nfl", "moneyline")])
        with stub_module("arb_engine.matching.settlement_rules"):
            rep = analyze_event(_tie_event(), {}, now=1000.0)
        self.assertNotIn("tie-rule-derived", rep.flags)
        self.assertFalse(any(f.startswith("settlement-rules") for f in rep.flags))

    def test_walkover_exposed_tennis_pair_is_excluded_from_arbs_by_default(self):
        key = "tennis:aa|bb:2026-09-19"
        info = EventInfo(event_key=key, sport="tennis", market_type="moneyline", outcomes=["aa", "bb"], tie_rule="void")
        pm = OutcomeQuote("polymarket", "pa", key, "aa", ask=0.40, ask_size=100, fee_params={"feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True}}, ts=1000.0)
        kal = OutcomeQuote("kalshi", "kb", key, "bb", ask=0.55, ask_size=100, fee_params={"fee_type": "quadratic_with_maker_fees"}, ts=1000.0)
        me = MergedEvent(key, info, {"polymarket": [pm], "kalshi": [kal]})
        calls = []

        def tennis_pair_flags(pair, settings):
            calls.append((pair.margin, sorted(l.venue for l in pair.legs)))
            return ["walkover-exposed", "tier:challenger"]

        with stub_module("arb_engine.matching.settlement_rules", pair_flags=lambda a, b, s, m: [], tennis_pair_flags=tennis_pair_flags):
            rep = analyze_event(me, {}, now=1000.0)
        self.assertTrue(rep.fillable)
        self.assertGreater(rep.margin, 0)
        self.assertEqual(calls[0][1], ["kalshi", "polymarket"])
        self.assertIn("walkover-exposed", rep.flags)
        res = ScanResult(sport="tennis", fetched_at=1000.0, venues=["kalshi", "polymarket"], events=[rep], errors={})
        self.assertEqual(res.arbs(), [])
        self.assertEqual(res.arbs(include_thin=True), [rep])
        # A registry that blows up is reported, not trusted.
        with stub_module("arb_engine.matching.settlement_rules", pair_flags=lambda *a: 1 / 0, tennis_pair_flags=tennis_pair_flags):
            rep = analyze_event(me, {}, now=1000.0)
        self.assertIn("settlement-rules-error", rep.flags)

    def test_line_fair_hook_is_opt_in_and_guarded(self):
        calls = []

        def line_fair_for_event(event, moneyline_p=None, spread_home=None, total=None, state=None):
            calls.append((event.event_key, moneyline_p, spread_home, total))
            return {"fair": {"over": 0.5, "under": 0.5}, "ml_spread_gap": True}

        with stub_module("arb_engine.quant.lines", line_fair_for_event=line_fair_for_event):
            off = scan("nfl", _adapters(), now=FIXTURE_NOW, settings={})
            self.assertEqual(calls, [])
            self.assertTrue(all(e.line_fair is None for e in off.events))
            res = scan("nfl", _adapters(), now=FIXTURE_NOW, settings={"line_fair": True})
        tot = next(e for e in res.events if e.market_type == "total")
        self.assertEqual(tot.line_fair["fair"], {"over": 0.5, "under": 0.5})
        self.assertIn("ml-spread-gap", tot.flags)
        ml_args = next(c for c in calls if c[0] == tot.event_key)
        self.assertEqual(set(ml_args[1]), {"BUF", "DET"})      # the same game's moneyline consensus
        self.assertEqual(ml_args[3], 49.5)
        with stub_module("arb_engine.quant.lines"):
            res = scan("nfl", _adapters(), now=FIXTURE_NOW, settings={"line_fair": True})
        self.assertTrue(all(e.line_fair is None for e in res.events))


class FixtureMetricsTests(unittest.TestCase):
    """The counts docs/ tables quote for this item come from the committed fixture scans and
    are regenerated here so they cannot drift from the code."""

    @staticmethod
    def _ncaaf_adapters():
        from .test_ncaaf import _adapters as ncaaf_adapters
        return ncaaf_adapters()

    @classmethod
    def _metrics(cls, sport, adapters):
        merged = merge_snapshots([a.fetch(sport) if a.venue != "robinhood" else a.fetch(sport, emit_no_side=True) for a in adapters])
        res = scan(sport, adapters, now=FIXTURE_NOW, settings={}, market_types={"moneyline"})
        reports = {e.event_key: e for e in res.events}
        pairs = [p for me in merged.values() if me.info.market_type == "moneyline" for p in cross_book_pairs(me, {})]
        kr = [p for p in pairs if {l["book"] for l in p["legs"]} == {"kalshi", "rothera"}]
        no_dominates = 0
        for me in merged.values():
            if me.info.market_type != "moneyline":
                continue
            for o in me.info.outcomes:
                rh = [q for q in me.quotes_for_outcome(o) if q.book_id == "rothera" and q.ask is not None]
                yes = [q for q in rh if q.meta.get("side") == "yes"]
                no = [q for q in rh if q.meta.get("side") == "no"]
                if yes and no and min(q.ask for q in no) <= min(q.ask for q in yes):
                    no_dominates += 1
        return {
            "moneyline_events": sum(1 for me in merged.values() if me.info.market_type == "moneyline"),
            "cross_book_pairs": len(pairs),
            "kalshi_x_rothera_pairs": len(kr),
            "kalshi_x_rothera_negative_tie_margin": sum(1 for p in kr if p["tie_margin"] < 0),
            "arbs": sum(1 for p in pairs if p["is_arb"]),
            "arbs_negative_tie_margin": sum(1 for p in pairs if p["is_arb"] and p["tie_margin"] < 0),
            "rothera_no_leg_dominates": no_dominates,
            "reports_with_no_leg_chosen": sum(1 for r in reports.values() if r.arb and any(l["side"] == "no" and l["venue"] == "robinhood" for l in r.arb["legs"])),
            "tie_rule_mismatch_flags": sum(1 for r in reports.values() if any(f.startswith("tie-rule-mismatch") for f in r.flags)),
            "below_min_size_flags": sum(1 for r in reports.values() if "below-min-size" in r.flags),
        }

    def test_results_fixture_is_reproducible(self):
        got = {"item": "P09", "fixtures": {"nfl": self._metrics("nfl", _adapters()), "ncaaf": self._metrics("ncaaf", self._ncaaf_adapters())}}
        expected = load("results/arb_fixture_p09.json")
        self.assertEqual(got, {k: expected[k] for k in got})
        nfl = got["fixtures"]["nfl"]
        self.assertGreater(nfl["kalshi_x_rothera_pairs"], 0)
        # Every Kalshi YES x Rothera YES pair loses half its stake on a tie; the NO legs fix that.
        self.assertGreater(nfl["kalshi_x_rothera_negative_tie_margin"], 0)
        self.assertGreater(nfl["rothera_no_leg_dominates"], 0)


class SettlementTests(unittest.TestCase):
    def test_tennis_walkover_rules_differ_between_kalshi_and_polymarket(self):
        from arb_engine.matching.matcher import MergedEvent
        from arb_engine.models import EventInfo, OutcomeQuote
        from arb_engine.scanner import settlement_mismatches
        from arb_engine.venues.kalshi import TENNIS_SETTLEMENT as K
        from arb_engine.venues.polymarket import TENNIS_SETTLEMENT as P

        key = "tennis:a|b:2026-09-19"
        info = EventInfo(event_key=key, sport="tennis", market_type="moneyline", outcomes=["a", "b"], labels={"a": "A", "b": "B"}, tie_rule="void", venues={"kalshi": {"settlement": K}, "polymarket": {"settlement": P}, "robinhood": {"settlement": dict(K)}})
        q = lambda v, o, ask: OutcomeQuote(v, f"{v}-{o}", key, o, ask=ask, bid=ask - 0.02, fee_params={})  # noqa: E731
        me = MergedEvent(key, info, {"kalshi": [q("kalshi", "a", 0.6), q("kalshi", "b", 0.42)], "polymarket": [q("polymarket", "a", 0.58), q("polymarket", "b", 0.44)]})
        flags = settlement_mismatches(info, me.quotes_by_venue)
        self.assertEqual(flags, ["settlement-mismatch:cancelled", "settlement-mismatch:postponed", "settlement-mismatch:walkover"])
        self.assertEqual(K["retirement"], P["retirement"])   # retirements after the first ball agree
        # Kalshi + its Robinhood mirror only: same rules, no flag.
        me2 = MergedEvent(key, info, {"kalshi": me.quotes_by_venue["kalshi"], "robinhood": [q("robinhood", "a", 0.6), q("robinhood", "b", 0.42)]})
        self.assertEqual(settlement_mismatches(info, me2.quotes_by_venue), [])
        report = analyze_event(me, {}, contracts=100)
        self.assertIn("settlement-mismatch:walkover", report.flags)

if __name__ == "__main__":
    unittest.main()

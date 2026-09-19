"""Margin distributions -> spread/total fair values, middles and the ml-spread-gap flag."""

import math
import unittest
from statistics import NormalDist

from arb_engine.models import EventInfo
from arb_engine.quant.lines import (
    EmpiricalMargin,
    LineLeg,
    NormalMargin,
    NormalTotal,
    SPREAD_INPLAY_SD_FLOOR,
    TOTAL_INPLAY_SD_FLOOR,
    default_params,
    frac_remaining_from_state,
    game_phase,
    line_fair_for_event,
    load_margin_table,
    middle_candidates,
    middle_ev,
    overtime_params,
    parse_spread_key,
    spread_from_p,
)
from arb_engine.quant.margintable import build_margin_dist, parse_games_csv
from arb_engine.venues.espn import GameState

from .helpers import load_text

SIGMA = 13.5


def _spread_event(fav: str, dog: str, line: float, tie_rule: str = "no_push") -> EventInfo:
    codes = sorted([fav, dog])
    return EventInfo(event_key=f"nfl:{'|'.join(codes)}:2026-09-17:spread:{fav}-{line:g}", sport="nfl", market_type="spread",
                     outcomes=[f"{fav}-{line:g}", f"{dog}+{line:g}"], line=line, tie_rule=tie_rule)


def _total_event(line: float, tie_rule: str = "no_push") -> EventInfo:
    return EventInfo(event_key=f"nfl:BUF|DET:2026-09-17:total:{line:g}", sport="nfl", market_type="total", outcomes=["over", "under"], line=line, tie_rule=tie_rule)


def _live(home_score: int, away_score: int, gsr: int) -> GameState:
    return GameState(event_id="x", home="BUF", away="DET", home_score=home_score, away_score=away_score, status="live", period=3, game_seconds_remaining=gsr)


class NormalMarginTests(unittest.TestCase):
    def test_p_win_at_minus_3(self):
        d = NormalMargin.from_spread(-3, SIGMA)
        self.assertAlmostEqual(d.p_win(), 0.59, delta=0.01)
        self.assertAlmostEqual(d.p_win() + d.p_tie() + d.p_lt(0), 1.0, places=9)
        self.assertAlmostEqual(d.mean(), 3.0, delta=0.1)
        # explicit tie mass replaces the normal's ~3% lattice cell at 0
        self.assertAlmostEqual(d.p_tie(), default_params("nfl")[2], places=9)
        self.assertLess(d.p_tie(), 0.01)
        self.assertEqual(NormalMargin(3.0, SIGMA, tie_mass=0.0).p_tie(), 0.0)

    def test_push_equals_lattice_mass_at_integer_lines(self):
        d = NormalMargin(3.0, SIGMA, tie_mass=None)  # raw continuity-corrected lattice
        nd = NormalDist(3.0, SIGMA)
        for line in (-3, 0, 3, 7):
            self.assertAlmostEqual(d.p_push(line), nd.cdf(line + 0.5) - nd.cdf(line - 0.5), places=9)
            cover, push, lose = d.p_cover(-line)
            self.assertAlmostEqual(cover + push + lose, 1.0, places=9)
        self.assertEqual(d.p_push(2.5), 0.0)
        self.assertEqual(d.p_cover(-2.5)[1], 0.0)

    def test_spread_from_p_inverts_p_win(self):
        for s in (-13.5, -7, -3, -1.5, 0, 2.5, 6, 10.5):
            p = NormalMargin.from_spread(s, SIGMA).p_win()
            self.assertAlmostEqual(spread_from_p(p, SIGMA), s, delta=0.05, msg=f"spread {s}")
        self.assertLess(spread_from_p(0.75), spread_from_p(0.6))

    def test_spread_from_p_is_clamped_to_the_reachable_range(self):
        """p_win tops out below 1 - tie_mass, so an extreme consensus must not run to the -60 bound."""
        hi, lo = spread_from_p(0.999), spread_from_p(0.001)
        self.assertGreater(hi, -45)
        self.assertLess(lo, 45)
        self.assertAlmostEqual(hi, -lo, delta=1.5)            # near-symmetric (ties sit on the deficit side)
        self.assertGreater(spread_from_p(0.997), -40)         # a 0.997 favourite is ~ -35, not -60
        self.assertLess(spread_from_p(0.997), spread_from_p(0.99))
        self.assertGreater(spread_from_p(1.0), -45)
        self.assertLess(spread_from_p(0.0), 45)

    def test_tie_mass_never_exceeds_the_lattice_cell(self):
        # 21 up with six minutes left: the normal has ~1e-5 at 0, the unconditional 0.36% would be absurd
        late = NormalMargin.in_play(21, -3, 360 / 3600, SIGMA)
        self.assertLess(late.p_tie(), 1e-3)
        self.assertLess(late.p_tie(), default_params("nfl")[2])
        self.assertAlmostEqual(late.p_win() + late.p_tie() + late.p_lt(0), 1.0, places=9)
        # pre-game the fitted rate is below the cell, so it is used as is
        self.assertAlmostEqual(NormalMargin.from_spread(-3, SIGMA).p_tie(), default_params("nfl")[2], places=9)

    def test_in_play_scaling(self):
        d = NormalMargin.in_play(margin_home=7, spread_home=-4, frac_remaining=0.25, sigma=SIGMA, sd_floor=0.0)
        self.assertAlmostEqual(d.mu, 7 + 4 * 0.25)
        self.assertAlmostEqual(d.sigma, SIGMA * math.sqrt(0.25))
        floored = NormalMargin.in_play(7, -4, 0.25, SIGMA)
        self.assertAlmostEqual(floored.sigma, math.sqrt(SIGMA ** 2 * 0.25 + SPREAD_INPLAY_SD_FLOOR ** 2))
        self.assertGreater(d.p_win(), NormalMargin.from_spread(-4, SIGMA).p_win())
        # decided game: point mass at the margin
        done = NormalMargin.in_play(3, -4, 0.0, SIGMA)
        self.assertEqual(done.pmf(3), 1.0)
        self.assertEqual(done.p_win(), 1.0)
        self.assertEqual(NormalMargin.in_play(-3, -4, 0.0, SIGMA).p_win(), 0.0)

    def test_frac_from_state(self):
        self.assertAlmostEqual(frac_remaining_from_state(_live(10, 3, 900)), 0.25)
        self.assertIsNone(frac_remaining_from_state(None))
        self.assertIsNone(frac_remaining_from_state(GameState(event_id="x", home="BUF", away="DET", status="pre")))
        self.assertIsNone(frac_remaining_from_state(GameState(event_id="x", home="BUF", away="DET", status="live", game_seconds_remaining=None)))
        self.assertIsNone(frac_remaining_from_state(GameState(event_id="x", home="BUF", away="DET", status="final", game_seconds_remaining=0)))

    def test_game_phase(self):
        self.assertEqual(game_phase(None), ("pre", None))
        self.assertEqual(game_phase(_live(10, 3, 900)), ("live", 0.25))
        self.assertEqual(game_phase(_live(10, 3, 0)), ("live", 0.0))                   # decided
        self.assertEqual(game_phase(_live(20, 20, 0)), ("overtime", 600 / 3600))       # tied at 0:00: OT pending
        self.assertEqual(game_phase(GameState(event_id="x", home="BUF", away="DET", status="live")), ("clock_unknown", None))
        self.assertEqual(game_phase(GameState(event_id="x", home="BUF", away="DET", status="final", home_score=3, game_seconds_remaining=0)), ("final", None))
        self.assertEqual(game_phase(GameState(event_id="x", home="BUF", away="DET", status="other")), ("other", None))
        secs, tie = overtime_params("nfl")
        self.assertEqual(secs, 600)
        self.assertGreater(tie, 0.03)   # P(tie | OT) is an order of magnitude above the unconditional rate
        self.assertLess(tie, 0.15)
        self.assertEqual(overtime_params("ncaaf")[1], 0.0)


class EmpiricalMarginTests(unittest.TestCase):
    def setUp(self):
        self.table = build_margin_dist(parse_games_csv(load_text("games_trim.csv")))

    def test_reproduces_key_number_mass_from_fixture(self):
        b = self.table["buckets"]["3"]
        n, c3 = b["n"], b["pmf"].get("3", 0)
        d = EmpiricalMargin.from_table(self.table, -3, SIGMA, tie_mass=0.0)
        w = n / (n + self.table["shrink_n0"])
        self.assertAlmostEqual(d.weight, w)
        normal = NormalMargin.from_spread(-3, SIGMA, tie_mass=0.0)
        self.assertAlmostEqual(d.pmf(3), w * c3 / n + (1 - w) * normal.pmf(3), places=12)
        self.assertAlmostEqual(sum(p for _, p in d.items()), 1.0, places=9)
        self.assertEqual(d.source, "empirical")
        # away favoured by 3: the same bucket, mirrored onto the home margin
        away = EmpiricalMargin.from_table(self.table, +3, SIGMA, tie_mass=0.0)
        self.assertAlmostEqual(away.pmf(-3), d.pmf(3), places=12)
        self.assertAlmostEqual(away.p_win(), d.p_lt(0), places=12)

    def test_shrinks_to_normal_at_low_n(self):
        thin = EmpiricalMargin.from_table(self.table, -20.5, SIGMA, tie_mass=0.0)  # no such bucket in the fixture
        normal = NormalMargin.from_spread(-20.5, SIGMA, tie_mass=0.0)
        self.assertEqual(thin.weight, 0.0)
        self.assertIsNone(thin.bucket)
        for k in (10, 20, 25):
            self.assertAlmostEqual(thin.pmf(k), normal.pmf(k), places=12)
        # a one-game bucket is ~2% empirical
        table = {"shrink_n0": 50, "buckets": {"9": {"n": 1, "mean": 9, "pmf": {"9": 1}}}}
        one = EmpiricalMargin.from_table(table, -9, SIGMA, tie_mass=0.0)
        self.assertAlmostEqual(one.weight, 1 / 51)
        self.assertAlmostEqual(one.pmf(9), (1 / 51) + (50 / 51) * NormalMargin(9, SIGMA, tie_mass=0.0).pmf(9), places=12)

    def test_committed_table_loads(self):
        d = EmpiricalMargin.from_table(None, -3)
        self.assertGreater(d.n, 200)
        self.assertGreater(d.pmf(3), 0.06)   # the lump at 3 survives shrinkage
        self.assertIsNotNone(load_margin_table("nfl"))
        self.assertIsNone(load_margin_table("nope"))


class TotalTests(unittest.TestCase):
    def test_pregame_total(self):
        t = NormalTotal.from_line(47.5, 13.2)
        over, push, under = t.p_over(47.5)
        self.assertAlmostEqual(over, 0.5, places=6)
        self.assertEqual(push, 0.0)
        self.assertGreater(t.p_over(44)[1], 0.02)
        self.assertEqual(t.p_lt(0), 0.0)

    def test_in_play_total_is_clipped_and_pace_blended(self):
        t = NormalTotal.in_play(points_so_far=30, total_line=47.5, frac_remaining=0.25, sigma=13.2, sd_floor=0.0, pace_weight=0.3)
        self.assertEqual(t.p_lt(30), 0.0)
        self.assertAlmostEqual(t.sigma, 13.2 * 0.5)
        pregame_mu = 30 + 47.5 * 0.25
        pace_mu = 30 + (30 / 0.75) * 0.25
        self.assertLess(t.mu, pregame_mu)   # pace (40 pts/game) below the 47.5 line pulls it down
        self.assertGreater(t.mu, pace_mu)
        default = NormalTotal.in_play(30, 47.5, 0.25, 13.2)
        self.assertAlmostEqual(default.mu, pregame_mu)   # pace weight defaults to 0 (hurt on the week-1 replay)
        self.assertAlmostEqual(default.sigma, math.sqrt(13.2 ** 2 * 0.25 + TOTAL_INPLAY_SD_FLOOR ** 2))
        done = NormalTotal.in_play(41, 47.5, 0.0, 13.2)
        self.assertEqual(done.p_over(40.5)[0], 1.0)


class LineFairForEventTests(unittest.TestCase):
    def test_spread_fair_and_gap_flag(self):
        ev = _spread_event("BUF", "DET", 1.5)
        # moneyline says BUF ~59% (~ -3) while the line consensus says BUF -6.5: gap > 2 -> flag
        res = line_fair_for_event(ev, moneyline_p=0.59, spread_home=-6.5, total=None, home="BUF", sigma=SIGMA, empirical=False)
        self.assertIn("ml_spread_gap", res.flags)
        self.assertAlmostEqual(res.ml_spread, -3.0, delta=0.15)
        self.assertAlmostEqual(res.ml_spread_gap, 3.5, delta=0.2)
        self.assertEqual(res.source, "normal")
        d = NormalMargin.from_spread(-6.5, SIGMA)
        self.assertAlmostEqual(res.fair["BUF-1.5"], d.p_gt(1.5), places=9)
        self.assertAlmostEqual(res.fair["DET+1.5"], 1 - d.p_gt(1.5), places=9)
        # consistent lines: no flag, empirical source by default
        ok = line_fair_for_event(ev, moneyline_p=0.59, spread_home=-3, total=None, home="BUF")
        self.assertNotIn("ml_spread_gap", ok.flags)
        self.assertEqual(ok.source, "empirical")
        self.assertGreater(ok.fair["BUF-1.5"], 0.5)
        self.assertAlmostEqual(sum(ok.fair.values()), 1.0, places=9)

    def test_spread_when_favourite_is_away(self):
        ev = _spread_event("DET", "BUF", 2.5)
        res = line_fair_for_event(ev, moneyline_p=None, spread_home=+3.0, total=None, home="BUF", sigma=SIGMA, empirical=False)
        d = NormalMargin.from_spread(3.0, SIGMA)
        self.assertAlmostEqual(res.fair["DET-2.5"], d.p_lt(-2.5), places=9)
        self.assertGreater(res.fair["DET-2.5"], 0.5)
        self.assertEqual(res.flags, [])

    def test_spread_from_moneyline_when_no_line(self):
        ev = _spread_event("BUF", "DET", 2.5)
        res = line_fair_for_event(ev, moneyline_p=0.59, spread_home=None, total=None, home="BUF", sigma=SIGMA)
        self.assertIn("spread_from_moneyline", res.flags)
        self.assertAlmostEqual(res.spread_home, -3.0, delta=0.15)
        self.assertIsNotNone(res.fair["BUF-2.5"])
        none = line_fair_for_event(ev, moneyline_p=None, spread_home=None, total=None, home="BUF")
        self.assertIn("no_spread", none.flags)
        self.assertIsNone(none.fair["BUF-2.5"])
        unknown = line_fair_for_event(ev, moneyline_p=None, spread_home=-3, total=None)
        self.assertIn("home_unknown", unknown.flags)

    def test_push_rules_on_integer_spread(self):
        d = NormalMargin.from_spread(-3, SIGMA)
        cover, push, _ = d.p_cover(-3)
        default = line_fair_for_event(_spread_event("BUF", "DET", 3, "push_possible"), None, -3, None, home="BUF", sigma=SIGMA, empirical=False)
        self.assertAlmostEqual(default.fair["BUF-3"], cover, places=9)          # exactly 3 resolves NO on Kalshi
        self.assertAlmostEqual(default.push, push, places=9)
        half = line_fair_for_event(_spread_event("BUF", "DET", 3, "half"), None, -3, None, home="BUF", sigma=SIGMA, empirical=False)
        self.assertAlmostEqual(half.fair["BUF-3"], cover + 0.5 * push, places=9)
        void = line_fair_for_event(_spread_event("BUF", "DET", 3, "void"), None, -3, None, home="BUF", sigma=SIGMA, empirical=False)
        self.assertAlmostEqual(void.fair["BUF-3"], cover / (1 - push), places=9)

    def test_total_pre_and_in_play(self):
        ev = _total_event(47.5)
        pre = line_fair_for_event(ev, None, None, total=47.5, sigma=13.2)
        self.assertAlmostEqual(pre.fair["over"], 0.5, places=6)
        self.assertFalse(pre.in_play)
        live = line_fair_for_event(ev, None, None, total=47.5, state=_live(20, 10, 900), sigma=13.2)
        self.assertTrue(live.in_play)
        self.assertAlmostEqual(live.frac_remaining, 0.25)
        self.assertLess(live.fair["over"], 0.5)   # 30 points with a quarter left: under favoured
        self.assertEqual(live.source, "normal_total")
        missing = line_fair_for_event(ev, None, None, total=None)
        self.assertIn("no_total", missing.flags)

    def test_in_play_spread_uses_state(self):
        ev = _spread_event("BUF", "DET", 2.5)
        live = line_fair_for_event(ev, moneyline_p=0.9, spread_home=-3, total=None, state=_live(24, 10, 900), sigma=SIGMA)
        self.assertTrue(live.in_play)
        self.assertIsNone(live.ml_spread)   # the moneyline is in-play too; the gap check is pre-game only
        self.assertEqual(live.source, "normal")
        self.assertGreater(live.fair["BUF-2.5"], 0.95)
        self.assertEqual(live.flags, [])

    def test_tied_at_zero_is_overtime_not_decided(self):
        tied = GameState(event_id="x", home="BUF", away="DET", home_score=20, away_score=20, status="live", period=4, clock_seconds_remaining_in_period=0, game_seconds_remaining=0)
        ml = EventInfo(event_key="nfl:BUF|DET:2026-09-17", sport="nfl", market_type="moneyline", outcomes=["BUF", "DET"], tie_rule="half")
        res = line_fair_for_event(ml, moneyline_p=None, spread_home=-3, total=None, state=tied, sigma=SIGMA)
        self.assertIn("overtime", res.flags)
        self.assertTrue(res.in_play)
        self.assertAlmostEqual(res.frac_remaining, 600 / 3600)
        self.assertGreater(res.fair["BUF"], 0.5)          # the 3-point favourite keeps an edge in OT
        self.assertLess(res.fair["BUF"], 0.7)
        self.assertAlmostEqual(res.fair["BUF"] + res.fair["DET"], 1.0, places=9)
        self.assertGreater(res.push, 0.04)                                       # OT tie mass, not the 0.36% average
        self.assertLessEqual(res.push, overtime_params("nfl")[1] + 1e-9)         # capped by the lattice cell at 0
        sp = line_fair_for_event(_spread_event("BUF", "DET", 2.5), None, -3, None, state=tied, sigma=SIGMA)
        self.assertGreater(sp.fair["BUF-2.5"], 0.15)
        self.assertLess(sp.fair["BUF-2.5"], 0.6)
        tot = line_fair_for_event(_total_event(40.5), None, None, total=40.5, state=tied, sigma=13.2)
        self.assertGreater(tot.fair["over"], 0.5)          # 40 on the board, OT to come: over 40.5 likely
        self.assertLess(tot.fair["over"], 1.0)
        self.assertIn("overtime", tot.flags)
        # a decided game at 0:00 is still a point mass
        done = line_fair_for_event(ml, None, -3, None, state=GameState(event_id="x", home="BUF", away="DET", home_score=23, away_score=20, status="live", period=4, game_seconds_remaining=0), sigma=SIGMA)
        self.assertEqual(done.fair["BUF"], 1.0)
        self.assertNotIn("overtime", done.flags)

    def test_unknown_clock_and_final_get_no_fair(self):
        ev = _spread_event("BUF", "DET", 2.5)
        unknown = GameState(event_id="x", home="BUF", away="DET", home_score=24, away_score=10, status="live", game_seconds_remaining=None)
        res = line_fair_for_event(ev, moneyline_p=0.9, spread_home=-3, total=None, state=unknown, sigma=SIGMA)
        self.assertIn("clock_unknown", res.flags)
        self.assertNotIn("ml_spread_gap", res.flags)       # no pre-game gap check on an in-play moneyline
        self.assertIsNone(res.ml_spread)
        self.assertEqual(res.fair, {"BUF-2.5": None, "DET+2.5": None})
        self.assertFalse(res.in_play)
        final = GameState(event_id="x", home="BUF", away="DET", home_score=24, away_score=10, status="final", game_seconds_remaining=0)
        res = line_fair_for_event(_total_event(40.5), None, None, total=40.5, state=final, sigma=13.2)
        self.assertIn("final", res.flags)
        self.assertEqual(res.fair, {"over": None, "under": None})
        other = GameState(event_id="x", home="BUF", away="DET", status="other")
        self.assertIn("other", line_fair_for_event(ev, None, -3, None, state=other).flags)

    def test_moneyline_event(self):
        ev = EventInfo(event_key="nfl:BUF|DET:2026-09-17", sport="nfl", market_type="moneyline", outcomes=["BUF", "DET"], tie_rule="half")
        res = line_fair_for_event(ev, moneyline_p=None, spread_home=-3, total=None, home="BUF", sigma=SIGMA, empirical=False)
        d = NormalMargin.from_spread(-3, SIGMA)
        self.assertAlmostEqual(res.fair["BUF"], d.p_win() + 0.5 * d.p_tie(), places=9)
        self.assertAlmostEqual(res.fair["BUF"] + res.fair["DET"], 1.0, places=9)
        self.assertEqual(parse_spread_key("nfl:BUF|DET:2026-09-17:spread:BUF-1.5"), ("BUF", 1.5))
        self.assertIsNone(parse_spread_key("nfl:BUF|DET:2026-09-17"))
        self.assertEqual(res.as_dict()["market_type"], "moneyline")


class MiddleTests(unittest.TestCase):
    def test_middle_around_3_from_committed_table(self):
        fav = EmpiricalMargin.from_table(None, -3)   # favourite orientation == home orientation when home is favoured
        m = middle_ev(fav, LineLeg("fav", 2.5, 0.515, venue="kalshi"), LineLeg("dog", 3.5, 0.515, venue="robinhood"))
        self.assertAlmostEqual(m.cost, 1.03)
        self.assertAlmostEqual(m.ev, 0.06, delta=0.02)
        self.assertAlmostEqual(m.p_middle, fav.pmf(3), places=9)
        self.assertTrue(m.never_an_arb)
        self.assertEqual(m.p_push_any, 0.0)
        # the normal has no lump at 3, so the same pair is roughly break-even
        n = middle_ev(NormalMargin.from_spread(-3), LineLeg("fav", 2.5, 0.515), LineLeg("dog", 3.5, 0.515))
        self.assertLess(n.ev, 0.01)
        self.assertGreater(m.ev, n.ev)

    def test_push_handling_per_leg(self):
        d = NormalMargin(3.0, SIGMA, tie_mass=0.0)
        p3 = d.pmf(3)
        a, b = LineLeg("fav", 3, 0.50, tie_rule="push_possible"), LineLeg("dog", 3.5, 0.50)
        kalshi = middle_ev(d, a, b)                                          # exactly 3: fav leg loses, dog wins
        half = middle_ev(d, LineLeg("fav", 3, 0.50, tie_rule="half"), b)     # exactly 3: fav pays 0.50
        void = middle_ev(d, LineLeg("fav", 3, 0.50, tie_rule="void"), b)     # exactly 3: stake back
        self.assertAlmostEqual(half.ev - kalshi.ev, 0.5 * p3, places=9)
        self.assertAlmostEqual(void.ev - kalshi.ev, 0.5 * p3, places=9)
        self.assertAlmostEqual(kalshi.p_push_any, p3, places=9)
        self.assertEqual(kalshi.p_middle, 0.0)

    def test_candidates_sorted_and_filtered(self):
        d = EmpiricalMargin.from_table(None, -3)
        pairs = [
            (LineLeg("fav", 2.5, 0.515), LineLeg("dog", 3.5, 0.515)),
            (LineLeg("fav", 2.5, 0.55), LineLeg("dog", 3.5, 0.55)),   # $1.10: negative EV
            (LineLeg("fav", 1.5, 0.50), LineLeg("dog", 3.5, 0.52)),   # wider middle (2 and 3)
        ]
        out = middle_candidates(pairs, d)
        self.assertEqual(len(out), 2)
        self.assertGreaterEqual(out[0].ev, out[1].ev)
        self.assertTrue(all(m.never_an_arb for m in out))
        self.assertEqual(middle_candidates(pairs, d, min_ev=1.0), [])
        with self.assertRaises(ValueError):
            middle_ev(d, LineLeg("yes", 2.5, 0.5), LineLeg("dog", 3.5, 0.5))


class EvalScriptTests(unittest.TestCase):
    """scripts/eval_lines.py offline: fixture summary + shifted candle fixture -> bracketed table."""

    @staticmethod
    def _module():
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "scripts" / "eval_lines.py"
        spec = importlib.util.spec_from_file_location("eval_lines_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _http(self):
        import json

        from .helpers import FakeHttp, load

        summary = load("espn/summary_401872932.json")
        from arb_engine.venues.history import espn_timeline

        plays, meta = espn_timeline(summary)
        # Shift the KXNFLGAME candle fixture so it spans this game's play window (1-minute bars).
        src = load("history/kalshi_candles_BUF.json")["candlesticks"]
        t0 = int(meta["kickoff"].timestamp()) - 20 * 60
        n = int((plays[-1].ts + 600 - t0) // 60) + 1
        candles = []
        for i in range(n):
            cc = json.loads(json.dumps(src[i % len(src)]))
            cc["end_period_ts"] = t0 + 60 * i
            candles.append(cc)
        calls = []
        routes = {"/summary": summary, "/candlesticks": lambda: {"candlesticks": candles}}
        http = FakeHttp(routes)
        http.calls = calls
        return http, plays

    def test_evaluate_game_scores_both_brackets(self):
        mod = self._module()
        from arb_engine.backtest import GameReplayer
        from arb_engine.venues.espn import ESPNClient
        from arb_engine.venues.history import HistoryClient

        http, plays = self._http()
        espn, hist = ESPNClient(http=http), HistoryClient(http=http)
        scorer = mod.Scorer()
        info = mod.evaluate_game({"id": "401872932", "name": "DET @ BUF"}, espn, hist, GameReplayer(espn=espn, history=hist), scorer, None, 30, 10)
        self.assertNotIn("skipped", info)
        self.assertEqual(info["close"]["spread_home"], -5.5)
        self.assertEqual(info["close"]["total"], 54.5)
        self.assertEqual(info["tickers"]["spread"], "KXNFLSPREAD-26SEP17DETBUF-BUF6")
        self.assertEqual(info["tickers"]["total"], "KXNFLTOTAL-26SEP17DETBUF-55")
        self.assertEqual(info["outcomes"], {"fav_margin": 10, "cover": 1, "total_points": 72, "over": 1})
        self.assertEqual(info["scored_lines"], {"spread": 5.5, "total": 54.5})
        # integer closes are scored on the Kalshi market's half-point line, for truth and model alike
        self.assertEqual(mod.kalshi_line(3.0), 2.5)
        self.assertEqual(mod.kalshi_line(7.0), 6.5)
        self.assertEqual(mod.kalshi_line(5.5), 5.5)
        self.assertEqual(mod.kalshi_line(47.0), 46.5)
        self.assertEqual(mod.line_tickers("26SEP13NESEA", "SEA", 3.0, 44.5)["spread"], "KXNFLSPREAD-26SEP13NESEA-SEA3")
        self.assertGreater(info["n_inplay"], 0)
        self.assertTrue(info["devig"]["heavy"] is False)
        self.assertGreater(info["devig"]["range"], 0)
        for mkt in ("spread", "total"):
            for phase in ("pre", "inplay"):
                m = scorer.metrics((mkt, phase, "normal", "-"))
                self.assertGreater(m["n"], 0, (mkt, phase))
                self.assertIsNotNone(m["log_loss"])
                for al in mod.ALIGNMENTS:
                    k = scorer.metrics((mkt, phase, "kalshi", al))
                    self.assertGreater(k["n"], 0, (mkt, phase, al))
        self.assertGreater(scorer.metrics(("spread", "pre", "empirical", "-"))["n"], 0)
        self.assertEqual(scorer.metrics(("spread", "inplay", "empirical", "-"))["n"], 0)
        # the two brackets read different candles for a play in the middle of a minute
        from arb_engine.venues.history import bar_at

        bars = hist.kalshi_candles(info["tickers"]["spread"], 0, 10 ** 10)
        ts = bars[3].ts + 30
        self.assertLessEqual(mod.bar_before(bars, ts).ts, ts)
        self.assertGreaterEqual(bar_at(bars, ts, "kalshi").ts, ts)
        report, metrics = mod.build_report(2026, 2, [info], scorer, (12.7, 13.2, 0.0036))
        self.assertIn("spread/inplay", report)
        self.assertIn("kalshi_before", report)
        self.assertEqual(metrics["games_scored"], 1)
        self.assertEqual(set(metrics["table"]), {"spread/pre", "spread/inplay", "total/pre", "total/inplay"})
        self.assertEqual(set(metrics["table"]["spread/pre"]), {"normal", "empirical", "kalshi_before", "kalshi"})
        self.assertIn("ceil(close) - 0.5", metrics["scoring"])
        self.assertEqual(metrics["tables"]["seasons"], [2016, 2025])

    def test_results_fixture_is_metrics_only_and_paired(self):
        import json
        from pathlib import Path

        p = Path(__file__).resolve().parent / "fixtures" / "results" / "lines_eval_p13.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(d["item"], "P13")
        self.assertTrue(d["paired"])
        self.assertGreater(d["games_scored"], 0)
        self.assertEqual(set(d["table"]), {"spread/pre", "spread/inplay", "total/pre", "total/inplay"})
        for row in d["table"].values():
            ns = {k: v["n"] for k, v in row.items() if v["n"]}
            self.assertEqual(len(set(ns.values())), 1, row)   # every scored column on the same rows
        self.assertNotIn("games", d)   # metrics only: no per-play rows, no raw payloads
        self.assertIn("ceil(close) - 0.5", d["scoring"])                 # one binary per market for truth, model and mid
        self.assertNotIn(d["season"], range(d["tables"]["seasons"][0], d["tables"]["seasons"][1] + 1))   # evaluated season not in the tables
        self.assertIn(d["season"], d["tables"]["excluded_incomplete_seasons"])
        self.assertLess(len(p.read_text(encoding="utf-8")), 20_000)

    def test_offline_cache_misses_are_skipped(self):
        import tempfile
        from pathlib import Path

        mod = self._module()
        with tempfile.TemporaryDirectory() as d:
            http = mod.CachingHttp(Path(d), offline=True, http=None)
            with self.assertRaises(RuntimeError):
                http.get("https://example.invalid/x", {"a": 1})
            self.assertEqual(http.misses, 0)

    def test_cli_plugin_registers_lines_eval(self):
        import argparse

        from arb_engine.cli_plugins.lines_flags import register

        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="cmd")
        created = register(sub, {})
        self.assertIn("lines-eval", created)
        args = p.parse_args(["lines-eval", "--week", "2", "--offline", "--limit", "3"])
        self.assertEqual((args.week, args.offline, args.limit), (2, True, 3))
        self.assertTrue(callable(args.func))
        self.assertEqual(register(sub, {"lines-eval": created["lines-eval"]}), {})


if __name__ == "__main__":
    unittest.main()

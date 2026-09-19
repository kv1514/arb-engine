"""The sportsbook slot with a de-vig range and the opt-in line prior leave the consensus path unchanged."""

import unittest

from arb_engine.models import OutcomeQuote
from arb_engine.quant.fairvalue import DEFAULT_VENUE_WEIGHTS, consensus_fair_value
from arb_engine.quant.odds import sportsbook_probs_from_moneylines


def _q(venue: str, outcome: str, bid: float, ask: float) -> OutcomeQuote:
    return OutcomeQuote(venue=venue, venue_market_id=f"{venue}-{outcome}", event_key="nfl:BUF|NYJ:2026-09-14", outcome=outcome, bid=bid, ask=ask)


QUOTES = {
    "kalshi": [_q("kalshi", "BUF", 0.66, 0.68), _q("kalshi", "NYJ", 0.32, 0.34)],
    "polymarket": [_q("polymarket", "BUF", 0.67, 0.69), _q("polymarket", "NYJ", 0.31, 0.33)],
    "robinhood": [_q("robinhood", "BUF", 0.64, 0.70), _q("robinhood", "NYJ", 0.30, 0.36)],
}
OUTCOMES = ["BUF", "NYJ"]


class ConsensusUnchangedTests(unittest.TestCase):
    def test_venue_only_consensus_numbers(self):
        """Pinned output of the pre-existing path (no sportsbook, no prior)."""
        fv = consensus_fair_value(QUOTES, OUTCOMES)
        self.assertAlmostEqual(fv["BUF"].fair, 0.6742, places=3)
        self.assertAlmostEqual(fv["BUF"].fair + fv["NYJ"].fair, 1.0, places=9)
        self.assertEqual(fv["BUF"].n_sources, 3)
        self.assertIsNone(fv["BUF"].sportsbook_range)
        self.assertNotIn("line_prior", fv["BUF"].by_venue)
        # weights = venue weight / spread: kalshi 1/0.02, polymarket 1/0.02, robinhood 0.7/0.06
        self.assertAlmostEqual(fv["BUF"].weights["kalshi"], 50.0)
        self.assertAlmostEqual(fv["BUF"].weights["robinhood"], 0.7 / 0.06)

    def test_plain_float_sportsbook_keeps_historical_weight(self):
        fv = consensus_fair_value(QUOTES, OUTCOMES, sportsbook_probs={"BUF": 0.70, "NYJ": 0.30})
        self.assertAlmostEqual(fv["BUF"].weights["sportsbook"], DEFAULT_VENUE_WEIGHTS["sportsbook"] / 0.01)
        self.assertEqual(fv["BUF"].by_venue["sportsbook"], 0.70)
        self.assertIsNone(fv["BUF"].sportsbook_range)
        self.assertEqual(fv["BUF"].n_sources, 4)
        # the sportsbook pulls the fair towards 0.70 but the venues still count
        base = consensus_fair_value(QUOTES, OUTCOMES)["BUF"].fair
        self.assertGreater(fv["BUF"].fair, base)
        self.assertLess(fv["BUF"].fair, 0.70)

    def test_range_form_downweights_wide_devig_disagreement(self):
        sp = sportsbook_probs_from_moneylines(-1200, 700)  # de-vig range ~0.03 on the favourite
        m = sp.as_mapping("BUF", "NYJ")
        fv = consensus_fair_value(QUOTES, OUTCOMES, sportsbook_probs=m)
        self.assertEqual(fv["BUF"].sportsbook_range, (sp.fair_min, sp.fair_max))
        self.assertAlmostEqual(fv["BUF"].weights["sportsbook"], DEFAULT_VENUE_WEIGHTS["sportsbook"] / sp.range)
        self.assertLess(fv["BUF"].weights["sportsbook"], DEFAULT_VENUE_WEIGHTS["sportsbook"] / 0.01)
        # a tight range keeps the full weight (floored at min_spread)
        tight = sportsbook_probs_from_moneylines(-110, -110).as_mapping("BUF", "NYJ")
        fv2 = consensus_fair_value(QUOTES, OUTCOMES, sportsbook_probs=tight)
        self.assertAlmostEqual(fv2["BUF"].weights["sportsbook"], DEFAULT_VENUE_WEIGHTS["sportsbook"] / 0.01)

    def test_line_prior_is_opt_in_with_documented_weight(self):
        prior = {"BUF": 0.60, "NYJ": 0.40}
        fv = consensus_fair_value(QUOTES, OUTCOMES, line_prior=prior)
        self.assertEqual(fv["BUF"].by_venue["line_prior"], 0.60)
        self.assertAlmostEqual(fv["BUF"].weights["line_prior"], DEFAULT_VENUE_WEIGHTS["line_prior"] / 0.01)
        base = consensus_fair_value(QUOTES, OUTCOMES)["BUF"].fair
        self.assertLess(fv["BUF"].fair, base)
        self.assertGreater(fv["BUF"].fair, 0.60)
        # None entries and unknown outcomes are ignored, so an empty prior is a no-op
        same = consensus_fair_value(QUOTES, OUTCOMES, line_prior={"BUF": None, "XXX": 0.5})
        self.assertAlmostEqual(same["BUF"].fair, base, places=12)
        custom = consensus_fair_value(QUOTES, OUTCOMES, line_prior=prior, venue_weights={"line_prior": 0.0})
        self.assertAlmostEqual(custom["BUF"].fair, base, places=12)


if __name__ == "__main__":
    unittest.main()

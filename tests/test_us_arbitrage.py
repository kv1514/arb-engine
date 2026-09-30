"""Offline, hand-computed tests for US three-venue read-only price pairs."""

import copy
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest import mock

from arb_engine.fees.registry import fee_model_for
from arb_engine.matching.matcher import merge_snapshots
from arb_engine.models import EventInfo, OutcomeQuote, VenueSnapshot
from arb_engine.quant.us_arbitrage import find_candidates
from arb_engine.scanner import analyze_event

NOW = 1800000000.0
KEY = "nfl:CHI|NYJ:2027-01-15"


def quote(venue, outcome, price, *, size=100, book_id=None, ts=NOW, side="yes", **meta):
    return OutcomeQuote(venue, venue + outcome + "#" + side, KEY, outcome, outcome,
                        ask=price, bid=price - .01, ask_size=size, bid_size=size, ts=ts,
                        book_id=book_id or venue,
                        fee_params={"taker_theta": ".0695"} if venue == "polymarket_us" else
                                   {"fee_multiplier": 1} if venue == "kalshi" else {"exchange": "rothera"},
                        meta={"side": side, "tie_payout": .5 if venue == "kalshi" else None,
                              "req_ts": ts, "obs_ts": ts, "refreshed": True, "approx_time": False, **meta})


def snapshots(*qs):
    out = []
    for venue in sorted({q.venue for q in qs}):
        rows = [q for q in qs if q.venue == venue]
        keys = {q.event_key for q in rows}
        events = {k: EventInfo(k, "nfl", "moneyline", ["CHI", "NYJ"],
                              start_time=datetime.fromtimestamp(NOW + 3600, timezone.utc)) for k in keys}
        out.append(VenueSnapshot(venue, events, rows, NOW))
    return out


class CandidateTests(unittest.TestCase):
    def pair(self, **kwargs):
        return find_candidates(snapshots(quote("kalshi", "CHI", .4), quote("polymarket_us", "NYJ", .5)),
                               now=NOW, **kwargs)

    def test_hand_computed_both_fees_cash_cap_and_unknown_rules(self):
        candidate = self.pair()["candidates"][0]
        # PM 48 * .5 + round_even(.0695*48*.25)=24.83 <=25;
        # Kalshi split bound 48*.02=.96 =>20.16. Win profit=48-44.99.
        self.assertEqual(candidate["contracts"], 48)
        self.assertEqual(candidate["total_cost"], 44.99)
        self.assertEqual(candidate["profit"], 3.01)
        self.assertEqual(candidate["legs"][0]["fee"], .81)
        self.assertEqual(candidate["legs"][0]["fee_bound"], .96)
        self.assertEqual(candidate["legs"][1]["fee"], .83)
        self.assertEqual(candidate["classification"], "conditional")
        self.assertIn("tie-payout-unknown:polymarket_us", candidate["gates"])
        self.assertIsNone(candidate["tie_profit"])
        self.assertTrue(all(l["cost"] <= 25 for l in candidate["legs"]))

    def test_gross_gap_killed_by_fees(self):
        data = snapshots(quote("kalshi", "CHI", .49), quote("polymarket_us", "NYJ", .5))
        self.assertEqual(find_candidates(data, now=NOW)["candidates"], [])

    def test_displayed_size_never_added_for_duplicate_rows(self):
        k, p = quote("kalshi", "CHI", .4, size=7), quote("polymarket_us", "NYJ", .5, size=9)
        report = find_candidates(snapshots(k, copy.deepcopy(k), p), now=NOW)
        self.assertEqual(len(report["candidates"]), 1)
        self.assertEqual(report["candidates"][0]["contracts"], 7)

    def test_missing_or_invalid_depth_rejected(self):
        for size in (None, 0, -1, float("nan"), float("inf")):
            with self.subTest(size=size):
                data = snapshots(quote("kalshi", "CHI", .4, size=size), quote("polymarket_us", "NYJ", .5))
                self.assertEqual(find_candidates(data, now=NOW)["candidates"], [])

    def test_stale_future_carried_and_failed_observations_rejected(self):
        rows = [quote("kalshi", "CHI", .4, ts=NOW-7), quote("kalshi", "CHI", .4, ts=NOW+1),
                quote("kalshi", "CHI", .4, refreshed=0), quote("kalshi", "CHI", .4, arb_ineligible="book-unavailable"),
                quote("kalshi", "CHI", .4, obs_ts=NOW+1), quote("kalshi", "CHI", .4, req_ts=NOW+1)]
        for row in rows:
            with self.subTest(meta=row.meta, ts=row.ts):
                self.assertEqual(find_candidates(snapshots(row, quote("polymarket_us", "NYJ", .5)), now=NOW)["candidates"], [])

    def test_quote_exchange_timestamp_age_and_future(self):
        for quote_time in (NOW-11, NOW+1, float("nan")):
            k = quote("kalshi", "CHI", .4)
            k.quote_time = quote_time
            self.assertEqual(find_candidates(snapshots(k, quote("polymarket_us", "NYJ", .5)), now=NOW)["candidates"], [])

    def test_crossed_one_sided_out_of_range_book_rejected(self):
        for bid in (.5, None, 0, float("nan")):
            k = quote("kalshi", "CHI", .4)
            k.bid = bid
            self.assertEqual(find_candidates(snapshots(k, quote("polymarket_us", "NYJ", .5)), now=NOW)["candidates"], [])

    def test_robinhood_kx_and_direct_kalshi_never_cross_book(self):
        data = snapshots(quote("kalshi", "CHI", .3), quote("robinhood", "NYJ", .4, book_id="kalshi"))
        self.assertEqual(find_candidates(data, now=NOW)["candidates"], [])

    def test_global_polymarket_not_silently_used(self):
        data = snapshots(quote("kalshi", "CHI", .3), quote("polymarket", "NYJ", .4))
        self.assertEqual(find_candidates(data, now=NOW)["candidates"], [])

    def test_operator_venue_restriction_respected(self):
        self.assertEqual(self.pair(settings={"executable_venues": "kalshi,robinhood"})["candidates"], [])

    def test_later_paid_rebate_does_not_inflate_candidate_profit(self):
        self.assertEqual(self.pair(), self.pair(settings={"polymarket_us_volume_rebate": .5}))

    def test_minimum_size_and_fractional_display(self):
        k = quote("kalshi", "CHI", .4, size=4.9)
        p = quote("polymarket_us", "NYJ", .5, min_size=5)
        self.assertEqual(find_candidates(snapshots(k, p), now=NOW)["candidates"], [])
        p.meta["min_size"] = 2.5
        c = find_candidates(snapshots(k, p), now=NOW)["candidates"][0]
        self.assertEqual(c["contracts"], 4)

    def test_neighboring_dates_not_merged(self):
        p = quote("polymarket_us", "NYJ", .5)
        p.event_key = KEY.replace("01-15", "01-16")
        self.assertEqual(find_candidates(snapshots(quote("kalshi", "CHI", .4), p), now=NOW)["candidates"], [])

    def test_missing_receipt_provenance_and_approximate_rows_rejected(self):
        for field in ("refreshed", "req_ts", "obs_ts"):
            k = quote("kalshi", "CHI", .4)
            del k.meta[field]
            self.assertEqual(find_candidates(snapshots(k, quote("polymarket_us", "NYJ", .5)), now=NOW)["candidates"], [])
        self.assertEqual(find_candidates(snapshots(quote("kalshi", "CHI", .4, approx_time=True),
                         quote("polymarket_us", "NYJ", .5)), now=NOW)["candidates"], [])

    def test_same_outcome_and_wrongly_scoped_rows_not_complementary(self):
        data = snapshots(quote("kalshi", "CHI", .3), quote("polymarket_us", "CHI", .4))
        for snap in data:
            snap.events[KEY].outcomes = ["CHI", "CHI"]
        self.assertEqual(find_candidates(data, now=NOW)["candidates"], [])
        data = snapshots(quote("kalshi", "CHI", .4), quote("polymarket_us", "NYJ", .5))
        data[1].venue = "robinhood"
        report = find_candidates(data, now=NOW)
        self.assertEqual(report["candidates"], [])
        self.assertIn("wrongly scoped", report["errors"]["robinhood"][0])

    def test_in_play_missing_kickoff_and_non_moneyline_excluded(self):
        for field, value in (("in_play", True), ("start_time", None), ("market_type", "spread"),
                             ("start_time", datetime.fromtimestamp(NOW, timezone.utc))):
            data = snapshots(quote("kalshi", "CHI", .4), quote("polymarket_us", "NYJ", .5))
            for s in data:
                setattr(s.events[KEY], field, value)
            self.assertEqual(find_candidates(data, now=NOW)["candidates"], [])

    def test_inputs_are_not_mutated(self):
        data = snapshots(quote("kalshi", "CHI", .4), quote("polymarket_us", "NYJ", .5))
        saved = copy.deepcopy(data)
        find_candidates(data, now=NOW)
        self.assertEqual(data, saved)

    def test_invalid_run_limits(self):
        for params in ({"side_cap": float("nan")}, {"max_age_s": -1}, {"contracts": 0},
                       {"contracts": 1.5}, {"min_margin": -1}, {"now": float("inf")}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                find_candidates([], **params)

    def test_known_matched_terms_only_then_verified_payoff(self):
        rules = {"status": "verbatim", "tie": "half", "cancelled": "50-50",
                 "postponed": "open_until_complete", "ot_included": True}
        p = quote("polymarket_us", "NYJ", .5, tie_payout=.5)
        with mock.patch("arb_engine.quant.us_arbitrage.rule_for_quote", return_value=rules), \
             mock.patch("arb_engine.quant.us_arbitrage.pair_flags", return_value=[]):
            c = find_candidates(snapshots(quote("kalshi", "CHI", .4), p), now=NOW)["candidates"][0]
        self.assertEqual(c["classification"], "verified-payoff")
        self.assertEqual(c["tie_profit"], c["profit"])
        self.assertIn("not atomic", c["execution"])

    def test_unverified_rothera_or_losing_tie_remains_conditional(self):
        c = find_candidates(snapshots(quote("kalshi", "CHI", .4),
                            quote("robinhood", "NYJ", .5, tie_payout=0, exchange="rothera")), now=NOW)["candidates"][0]
        self.assertEqual(c["classification"], "conditional")
        self.assertLess(c["tie_profit"], 0)


class IntegrationTests(unittest.TestCase):
    def test_quote_fee_coefficient_frozen_for_replay(self):
        model = fee_model_for("polymarket_us", {"taker_theta": ".06"})
        self.assertEqual(model.fee(".5", 100), Decimal("1.50"))
        model = fee_model_for("polymarket_us", {"taker_theta": ".0695"})
        self.assertEqual(model.fee(".5", 1000), Decimal("17.38"))

    def test_fee_rebate_decimal_cast_and_validation(self):
        model = fee_model_for("polymarket_us", {}, settings={"polymarket_us_volume_rebate": .1})
        self.assertEqual(model.fee(".5", 100), Decimal("1.56"))
        for params in ({"taker_theta": "NaN"}, {"taker_theta": "-.1"}):
            with self.assertRaises(ValueError):
                fee_model_for("polymarket_us", params)
        with self.assertRaises(ValueError):
            fee_model_for("polymarket_us", {}, settings={"polymarket_us_volume_rebate": 2})

    def test_generic_scanner_cannot_default_unknown_us_tie_to_half(self):
        data = snapshots(quote("kalshi", "CHI", .4), quote("polymarket_us", "NYJ", .3))
        me = merge_snapshots(data)[KEY]
        report = analyze_event(me, {}, now=NOW, executable_venues={"kalshi", "polymarket_us"})
        self.assertIsNone(report.arb)
        row = report.outcomes[1].venues[0]
        self.assertIn("settlement", row.ineligible)
        self.assertIsNotNone(row.all_in)
        self.assertIsNone(row.max_buy_price)

    def test_unknown_us_quote_does_not_block_existing_other_pair(self):
        data = snapshots(quote("kalshi", "CHI", .4), quote("robinhood", "NYJ", .4, tie_payout=1),
                         quote("polymarket_us", "NYJ", .1))
        me = merge_snapshots(data)[KEY]
        report = analyze_event(me, {}, now=NOW, executable_venues={"kalshi", "robinhood", "polymarket_us"})
        self.assertIsNotNone(report.arb)
        self.assertEqual({l["venue"] for l in report.arb["legs"]}, {"kalshi", "robinhood"})


if __name__ == "__main__":
    unittest.main()

"""Adversarial checks for the offline paper-execution contract."""
from decimal import Decimal
import unittest

from arb_engine.fees.base import D, ZeroFees
from arb_engine.quant.paperexec import ioc_round_trip, two_leg_arb


def quote(ts, *, book="kalshi", market="K", ask=.40, ask_size=10,
          bid=.39, bid_size=10, refreshed=1):
    return {
        "obs_ts": float(ts), "refreshed": refreshed, "book_id": book,
        "venue_market_id": market, "side": "yes", "ask": ask,
        "ask_size": ask_size, "bid": bid, "bid_size": bid_size,
    }


class PennyPerContract:
    """Simple exact fee used to make every charged order visible."""

    def __init__(self):
        self.calls = []

    def fee(self, price, contracts, role="taker"):
        self.calls.append((D(price), contracts, role))
        return Decimal("0.01") * D(contracts)


class PaperExecutionAuditTests(unittest.TestCase):
    def test_decision_time_identity_rejects_kx_reseller_with_no_future_rows(self):
        result = two_leg_arb([], [], 100, .40, .50, 10, ZeroFees(), ZeroFees(),
                             book_id_a="kalshi", book_id_b="kalshi")
        self.assertEqual(result.excluded, "same-book")
        self.assertEqual(result.book_ids, ("kalshi", "kalshi"))
        self.assertEqual(tuple(leg.filled for leg in result.legs), (0, 0))

    def test_decision_time_identity_is_immutable_when_future_rows_disagree(self):
        # Future payload corruption/book remapping cannot turn two decision-time books into
        # one book (or vice versa).  The supplied decision identities are authoritative.
        a = [quote(1, book="future-shared", market="A")]
        b = [quote(1, book="future-shared", market="B", ask=.50)]
        result = two_leg_arb(a, b, 0, .40, .50, 10, ZeroFees(), ZeroFees(),
                             latency_b_s=1, book_id_a="kalshi", book_id_b="rothera",
                             settlement_compatible=True, tie_payouts=(.5, .5))
        self.assertEqual(result.excluded, "")
        self.assertEqual(result.book_ids, ("kalshi", "rothera"))
        self.assertTrue(result.guaranteed)

    def test_unknown_or_placeholder_book_identity_cannot_be_guaranteed(self):
        a = [quote(1, book=None, market="A")]
        b = [quote(1, book=None, market="B", ask=.50)]
        for bad in (None, "", "unknown", "?", " null "):
            result = two_leg_arb(a, b, 0, .40, .50, 10, ZeroFees(), ZeroFees(),
                                 latency_b_s=1, book_id_a=bad, book_id_b="rothera",
                                 settlement_compatible=True, tie_payouts=(.5, .5))
            self.assertFalse(result.guaranteed, bad)
            self.assertIsNone(result.book_ids)

    def test_arrival_windows_cover_one_three_and_manual_fifteen_seconds(self):
        fees = ZeroFees()
        one = ioc_round_trip([quote(1), quote(31, bid=.50)], 0, .40, 1, fees,
                             latency_s=1, horizon_s=30)
        three = ioc_round_trip([quote(3), quote(33, bid=.50)], 0, .40, 1, fees,
                               latency_s=3, horizon_s=30)
        manual = two_leg_arb([quote(1, market="A")],
                             [quote(15, book="rothera", market="B", ask=.50)],
                             0, .40, .50, 1, fees, fees)
        outside = ioc_round_trip([quote(3.001)], 0, .40, 1, fees,
                                 latency_s=1, entry_tol_s=2)
        self.assertEqual((one.filled, three.filled, manual.matched), (1, 1, 1))
        self.assertTrue(outside.missed)
        self.assertEqual(outside.reason, "no quote when the order arrives")

    def test_haircut_partial_ioc_cancels_remainder(self):
        trade = ioc_round_trip([quote(1, ask_size=5), quote(31, bid=.50, bid_size=4)],
                               0, .40, 5, ZeroFees(), haircut=.5, horizon_s=30)
        self.assertEqual((trade.filled, trade.cancelled, trade.partial), (2, 3, True))
        self.assertEqual([n for _, _, n, _ in trade.exits], [2])

    def test_fixed_exit_target_roll_bound_and_duplicate_liquidity(self):
        duplicate = quote(32, bid=.55, bid_size=1)
        rows = [quote(3, ask_size=4), quote(31, bid=.50, bid_size=0),
                duplicate, dict(duplicate), quote(33, bid=.56, bid_size=1),
                quote(34, bid=.90, bid_size=10)]
        trade = ioc_round_trip(rows, 0, .40, 4, ZeroFees(), latency_s=1,
                               entry_tol_s=2, horizon_s=30, max_rolls=2,
                               settlement=1)
        self.assertEqual([x[:3] for x in trade.exits], [(32.0, .55, 1), (33.0, .56, 1)])
        self.assertEqual((trade.settled, trade.unresolved), (2, 0))

    def test_settlement_has_no_exit_fee_and_fees_use_actual_fill_counts(self):
        fees = PennyPerContract()
        rows = [quote(1, ask=.40, ask_size=4), quote(31, bid=.60, bid_size=2)]
        trade = ioc_round_trip(rows, 0, .40, 4, fees, horizon_s=30, settlement=1)
        self.assertEqual(fees.calls, [(Decimal("0.4"), 4, "taker"),
                                     (Decimal("0.6"), 2, "taker")])
        self.assertEqual((trade.entry_fee, trade.exit_fee),
                         (Decimal("0.04"), Decimal("0.02")))
        self.assertEqual(trade.pnl, Decimal("1.54"))

    def test_partial_failed_leg_unwind_keeps_unresolved_inventory_and_both_fees(self):
        fee_a, fee_b = PennyPerContract(), PennyPerContract()
        a = [quote(1, ask=.40, ask_size=5), quote(16, bid=.30, bid_size=2)]
        b = [quote(15, book="rothera", market="R", ask=.50, ask_size=2)]
        result = two_leg_arb(a, b, 0, .40, .50, 5, fee_a, fee_b,
                             unwind_latency_s=1, book_id_a="kalshi",
                             book_id_b="rothera", settlement_compatible=True,
                             tie_payouts=(.5, .5))
        self.assertEqual((result.matched, result.unwound, result.unresolved), (2, 2, 1))
        self.assertEqual(fee_a.calls, [(Decimal("0.4"), 5, "taker"),
                                      (Decimal("0.3"), 2, "taker")])
        self.assertEqual(fee_b.calls, [(Decimal("0.5"), 2, "taker")])
        self.assertEqual(result.unwind_fee, Decimal("0.02"))
        self.assertIsNone(result.pnl)
        self.assertFalse(result.guaranteed)

    def test_tie_payout_decimal_sum_does_not_import_binary_float_noise(self):
        a = [quote(1, ask=.10, market="A")]
        b = [quote(1, ask=.10, book="rothera", market="B")]
        result = two_leg_arb(a, b, 0, .10, .10, 1, ZeroFees(), ZeroFees(),
                             latency_b_s=1, book_id_a="kalshi", book_id_b="rothera",
                             settlement_compatible=True, tie_payouts=(.1, .2))
        self.assertEqual(result.pnl_tie, Decimal("0.10"))


if __name__ == "__main__":
    unittest.main()

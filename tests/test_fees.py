"""Fee models against the venues' own published tables (see docs/VENUES.md)."""

import unittest
from decimal import Decimal

from arb_engine.fees import KalshiFees, PolymarketFees, PolymarketUSFees, RobinhoodFees, fee_model_for
from arb_engine.fees.polymarket import POLY_US_TAKER_THETA_AFTER, POLY_US_TAKER_THETA_BEFORE
from datetime import date

# Kalshi fee schedule PDF (July 7, 2026): "General Trading Fees Table", 100 contracts / 1 contract.
KALSHI_TABLE_100 = {0.01: "0.07", 0.05: "0.34", 0.10: "0.63", 0.15: "0.90", 0.20: "1.12", 0.25: "1.32", 0.30: "1.47", 0.35: "1.60", 0.40: "1.68", 0.45: "1.74", 0.50: "1.75", 0.55: "1.74", 0.60: "1.68", 0.65: "1.60", 0.70: "1.47", 0.75: "1.32", 0.80: "1.12", 0.85: "0.90", 0.90: "0.63", 0.95: "0.34", 0.99: "0.07"}
KALSHI_TABLE_1 = {0.01: "0.01", 0.05: "0.01", 0.10: "0.01", 0.15: "0.01", 0.20: "0.02", 0.50: "0.02", 0.80: "0.02", 0.85: "0.01", 0.99: "0.01"}
# Robinhood "Event contracts overview" examples, 100 contracts.
RH_TABLE = {0.01: ("0.05", "0.10"), 0.05: ("0.24", "0.48"), 0.25: ("0.94", "1.00"), 0.50: ("1.00", "1.00"), 0.75: ("0.94", "1.00"), 0.99: ("0.05", "0.10")}


class KalshiFeeTests(unittest.TestCase):
    def test_official_table_100_contracts(self):
        fm = KalshiFees()
        for p, expected in KALSHI_TABLE_100.items():
            self.assertEqual(fm.fee(p, 100), Decimal(expected), p)

    def test_official_table_1_contract(self):
        fm = KalshiFees()
        for p, expected in KALSHI_TABLE_1.items():
            self.assertEqual(fm.fee(p, 1), Decimal(expected), p)

    def test_maker_fee_only_on_maker_series(self):
        game = KalshiFees.from_series({"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1})
        prop = KalshiFees.from_series({"fee_type": "quadratic", "fee_multiplier": 1})
        self.assertEqual(game.fee(0.50, 100, "maker"), Decimal("0.44"))  # site shows "$0.02 - $0.44"
        self.assertEqual(game.fee(0.01, 100, "maker"), Decimal("0.02"))
        self.assertEqual(prop.fee(0.50, 100, "maker"), Decimal("0"))
        self.assertEqual(prop.fee(0.50, 100, "taker"), Decimal("1.75"))

    def test_multiplier_and_zero_fee_series(self):
        half = KalshiFees.from_series({"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 0.5})
        self.assertEqual(half.fee(0.50, 100), Decimal("0.88"))  # 0.875 rounds up
        free = KalshiFees.from_series({"fee_type": "quadratic", "fee_multiplier": 0})
        self.assertEqual(free.fee(0.50, 100), Decimal("0"))

    def test_centicent_rounding_option(self):
        fm = KalshiFees(rounding="centicent")
        self.assertEqual(fm.fee(0.05, 100), Decimal("0.3325"))
        self.assertEqual(KalshiFees(rounding="cent").fee(0.05, 100), Decimal("0.34"))

    def test_no_float_noise(self):
        # 0.07 * 100 * 0.10 * 0.90 = 0.6300000000000001 in binary floats -> would round to 0.64.
        self.assertEqual(KalshiFees().fee(0.10, 100), Decimal("0.63"))


class RobinhoodFeeTests(unittest.TestCase):
    def test_published_commission_examples(self):
        for p, (gold, no_gold) in RH_TABLE.items():
            self.assertEqual(RobinhoodFees(gold=True).commission(p, 100), Decimal(gold), p)
            self.assertEqual(RobinhoodFees(gold=False).commission(p, 100), Decimal(no_gold), p)

    def test_exchange_fee_added_per_contract(self):
        fm = RobinhoodFees(gold=False, exchange="rothera")
        self.assertEqual(fm.fee(0.50, 100), Decimal("2.00"))
        self.assertEqual(RobinhoodFees(exchange="forecastex").fee(0.50, 100), Decimal("1.00"))
        self.assertEqual(RobinhoodFees(exchange="kalshi").fee(0.99, 100), Decimal("1.10"))

    def test_exchange_inferred_from_symbol(self):
        self.assertEqual(RobinhoodFees.from_params({"symbol": "KXWTAMATCH-26SEP14YOUCHA-YOU"}).exchange, "kalshi")
        self.assertEqual(RobinhoodFees.from_params({"symbol": "NFLGAME-26SEP20PHITEN-PHI", "exchange_enum": "EXCHANGE_SOURCE_ROTHERA"}).exchange, "rothera")

    def test_single_contract_cap(self):
        self.assertEqual(RobinhoodFees(gold=True).commission(0.50, 1), Decimal("0.01"))


class PolymarketFeeTests(unittest.TestCase):
    def test_sports_peak(self):
        fm = PolymarketFees.from_market({"feeSchedule": {"exponent": 1, "rate": 0.05, "takerOnly": True, "rebateRate": 0.15}, "feesEnabled": True})
        self.assertEqual(fm.fee(0.50, 100), Decimal("1.25000"))
        self.assertEqual(fm.fee(0.50, 100, "maker"), Decimal("0"))
        self.assertEqual(fm.fee(0.34, 100), Decimal("1.12200"))

    def test_fees_disabled(self):
        self.assertEqual(PolymarketFees.from_market({"feesEnabled": False}).fee(0.5, 100), Decimal("0"))

    def test_us_theta_schedule(self):
        before = PolymarketUSFees.for_date(date(2026, 9, 15))
        after = PolymarketUSFees.for_date(date(2026, 9, 16))
        self.assertEqual(before.taker_theta, POLY_US_TAKER_THETA_BEFORE)
        self.assertEqual(after.taker_theta, POLY_US_TAKER_THETA_AFTER)
        self.assertEqual(before.fee(0.50, 100), Decimal("1.50"))
        self.assertEqual(before.fee(0.50, 100, "maker"), Decimal("-0.31"))
        self.assertEqual(after.fee(0.50, 100), Decimal("1.74"))  # 1.7375 -> banker's rounding


class RegistryTests(unittest.TestCase):
    def test_registry_picks_models(self):
        self.assertEqual(fee_model_for("kalshi", {"fee_type": "quadratic_with_maker_fees"}).name, "kalshi")
        self.assertEqual(fee_model_for("robinhood", {"symbol": "NFLGAME-X-Y"}, settings={"robinhood_gold": True}).gold, True)
        self.assertEqual(fee_model_for("polymarket", {"feeSchedule": {"rate": 0.05}}).rate, Decimal("0.05"))
        self.assertEqual(fee_model_for("sportsbook").fee(0.5, 100), Decimal("0"))


if __name__ == "__main__":
    unittest.main()

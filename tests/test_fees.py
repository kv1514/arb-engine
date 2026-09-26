"""Fee models against the venues' own published tables (see docs/VENUES.md).

Sources pinned here: Kalshi fee schedule PDF (2026-07-07), Robinhood "Event contracts
overview" (June 2026 pricing), Rothera Fee Schedule 20260520, RHD Fee Schedule 2026-05-28,
the Nadex/CDNA schedule effective 2026-08-01 (range only) and docs.polymarket.us/fees.
"""

import importlib.util
import json
import os
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from arb_engine.fees import KalshiFees, PolymarketFees, PolymarketUSFees, RobinhoodFees, fee_model_for
from arb_engine.fees.polymarket import POLY_US_TAKER_THETA_AFTER, POLY_US_TAKER_THETA_BEFORE, POLY_US_WORKED_EXAMPLE, check_polymarket_us_worked_example
from arb_engine.fees.registry import FEE_SETTINGS, fee_model_for_quote, fee_setting
from arb_engine.fees.robinhood import CDNA_FEE_MODELS, ROTHERA_FEE_MODELS, rothera_order_fee
from arb_engine.models import OutcomeQuote
from arb_engine.quant.arbitrage import Leg, evaluate

from .helpers import FIXTURES, load

ROOT = Path(__file__).resolve().parents[1]

# Kalshi fee schedule PDF (July 7, 2026): "General Trading Fees Table", 100 contracts / 1 contract.
KALSHI_TABLE_100 = {0.01: "0.07", 0.05: "0.34", 0.10: "0.63", 0.15: "0.90", 0.20: "1.12", 0.25: "1.32", 0.30: "1.47", 0.35: "1.60", 0.40: "1.68", 0.45: "1.74", 0.50: "1.75", 0.55: "1.74", 0.60: "1.68", 0.65: "1.60", 0.70: "1.47", 0.75: "1.32", 0.80: "1.12", 0.85: "0.90", 0.90: "0.63", 0.95: "0.34", 0.99: "0.07"}
KALSHI_TABLE_1 = {0.01: "0.01", 0.05: "0.01", 0.10: "0.01", 0.15: "0.01", 0.20: "0.02", 0.50: "0.02", 0.80: "0.02", 0.85: "0.01", 0.99: "0.01"}
# Robinhood "Event contracts overview" examples, 100 contracts.
RH_TABLE = {0.01: ("0.05", "0.10"), 0.05: ("0.24", "0.48"), 0.25: ("0.94", "1.00"), 0.50: ("1.00", "1.00"), 0.75: ("0.94", "1.00"), 0.99: ("0.05", "0.10")}
# Rothera Fee Schedule 20260520, retail k = 0.02, per ORDER: max(round_half_up(k P (1-P) C, 2), 0.01).
ROTHERA_TABLE_100 = {0.01: "0.02", 0.05: "0.10", 0.35: "0.46", 0.50: "0.50", 0.97: "0.06", 0.99: "0.02"}
ROTHERA_TABLE_10 = {0.01: "0.01", 0.05: "0.01", 0.35: "0.05", 0.50: "0.05", 0.97: "0.01", 0.99: "0.01"}
ROTHERA_TABLE_1 = {0.01: "0.01", 0.35: "0.01", 0.50: "0.01", 0.99: "0.01"}
# CDNA exchange part for 100 @ 0.50 under each model (commission adds $1.00 on top).
CDNA_TABLE_50 = {"flat_001": "1.00", "flat_002": "2.00", "weighted_007": "1.75"}

KALSHI_GAME = {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}


def model_from_vector(v: dict):
    """Rebuild the fee model a fee_vectors.json row describes through the registry, mapping the
    extension's camelCase settings back to the Python settings keys."""
    s = v.get("settings") or {}
    settings = {"robinhood_gold": bool(s.get("gold", False))}
    if s.get("rotheraFeeModel"):
        settings["rothera_fee_model"] = s["rotheraFeeModel"]
    if s.get("cdnaFeeModel"):
        settings["cdna_fee_model"] = s["cdnaFeeModel"]
    if v["venue"] == "polymarket_us":  # the vectors pin the post-change theta rather than today's date
        return PolymarketUSFees(taker_theta=Decimal(str(v["params"]["takerTheta"])))
    return fee_model_for(v["venue"], v["params"], settings=settings)


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
        game = KalshiFees.from_series(KALSHI_GAME)
        prop = KalshiFees.from_series({"fee_type": "quadratic", "fee_multiplier": 1})
        self.assertEqual(game.fee(0.50, 100, "maker"), Decimal("0.44"))  # site shows "$0.02 - $0.44"
        self.assertEqual(game.fee(0.01, 100, "maker"), Decimal("0.02"))
        self.assertEqual(prop.fee(0.50, 100, "maker"), Decimal("0"))
        self.assertEqual(prop.fee(0.50, 100, "taker"), Decimal("1.75"))

    def test_multiplier_and_zero_fee_series(self):
        half = KalshiFees.from_series({"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 0.5})
        self.assertEqual(half.fee(0.50, 100), Decimal("0.88"))  # 0.875 rounds up
        self.assertEqual(half.fee(0.50, 100, "maker"), Decimal("0.22"))  # 0.21875 rounds up
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

    def test_defaults_are_flat_001(self):
        fm = RobinhoodFees()
        self.assertEqual((fm.rothera_fee_model, fm.cdna_fee_model), ("flat_001", "flat_001"))
        self.assertEqual(RobinhoodFees(exchange="rothera").exchange_fee(0.99, 100), Decimal("1.00"))
        self.assertEqual(RobinhoodFees(exchange="cdna").exchange_fee(0.50, 100), Decimal("1.00"))
        with self.assertRaises(ValueError):
            RobinhoodFees(rothera_fee_model="cubic")
        with self.assertRaises(ValueError):
            RobinhoodFees(cdna_fee_model="flat_003")


class RotheraFeeTests(unittest.TestCase):
    """Rothera Fee Schedule 20260520: per-order quadratic fee with a $0.01 floor."""

    def test_schedule_retail_k_100_contracts(self):
        fm = RobinhoodFees(exchange="rothera", rothera_fee_model="quadratic")
        for p, expected in ROTHERA_TABLE_100.items():
            self.assertEqual(fm.exchange_fee(p, 100), Decimal(expected), p)
            self.assertEqual(rothera_order_fee(p, 100), Decimal(expected), p)

    def test_floor_is_per_order(self):
        fm = RobinhoodFees(exchange="rothera", rothera_fee_model="quadratic")
        for p, expected in ROTHERA_TABLE_10.items():
            self.assertEqual(fm.exchange_fee(p, 10), Decimal(expected), p)
        for p, expected in ROTHERA_TABLE_1.items():
            self.assertEqual(fm.exchange_fee(p, 1), Decimal(expected), p)
        self.assertEqual(fm.exchange_fee(0.50, 0), Decimal("0.00"))

    def test_schedule_worked_example_is_half_up(self):
        # The schedule's own example: k = 0.06, 100 @ $0.35 -> 0.06 x 0.35 x 0.65 x 100 = 1.365 -> $1.37.
        self.assertEqual(rothera_order_fee(0.35, 100, k=Decimal("0.06")), Decimal("1.37"))
        self.assertEqual(RobinhoodFees.from_params({"exchange": "rothera", "rothera_fee_model": "quadratic", "rothera_k": 0.06}).exchange_fee(0.35, 100), Decimal("1.37"))

    def test_all_in_at_the_tail_is_size_dependent(self):
        fm = RobinhoodFees(exchange="rothera", rothera_fee_model="quadratic")
        # 100 @ 0.99: commission ceil(0.10 x 0.99 x 0.01 x 100) = $0.10 plus $0.02 exchange -> $0.0002/contract.
        self.assertEqual(fm.fee(0.99, 100), Decimal("0.12"))
        self.assertAlmostEqual(fm.per_contract(0.99, 100), 0.001 + 0.0002, places=9)
        # 1 @ 0.99: the per-order floor makes the exchange part a full cent (+ $0.01 commission).
        self.assertEqual(fm.fee(0.99, 1), Decimal("0.02"))
        self.assertEqual(fm.exchange_fee(0.99, 1), Decimal("0.01"))
        # Under flat_001 the same 100-lot pays $1.00 exchange, 50x the schedule.
        self.assertEqual(RobinhoodFees(exchange="rothera").fee(0.99, 100), Decimal("1.10"))

    def test_kalshi_routed_and_forecastex_unaffected(self):
        self.assertEqual(RobinhoodFees(exchange="kalshi", rothera_fee_model="quadratic").fee(0.99, 100), Decimal("1.10"))
        self.assertEqual(RobinhoodFees(exchange="forecastex", rothera_fee_model="quadratic").fee(0.50, 100), Decimal("1.00"))

    def test_tail_arb_flips_from_no_arb_to_positive(self):
        # 100 contracts: Kalshi YES-A @ 0.05 ($0.34 fee) x Rothera YES-B @ 0.93 ($0.66 commission).
        k = Leg("A", "kalshi", 0.05, KalshiFees.from_series(KALSHI_GAME))
        flat = evaluate([k, Leg("B", "robinhood", 0.93, RobinhoodFees(exchange="rothera"))], 100)
        quad = evaluate([k, Leg("B", "robinhood", 0.93, RobinhoodFees(exchange="rothera", rothera_fee_model="quadratic"))], 100)
        self.assertFalse(flat.is_arb)
        self.assertAlmostEqual(flat.profit, 0.00, places=9)
        self.assertTrue(quad.is_arb)
        self.assertAlmostEqual(quad.profit, 0.87, places=9)  # $1.00 - $0.13 exchange fee
        # ... but a 1-lot pays the floor and is not an arb.
        one = evaluate([k, Leg("B", "robinhood", 0.93, RobinhoodFees(exchange="rothera", rothera_fee_model="quadratic"))], 1)
        self.assertFalse(one.is_arb)


class CdnaFeeTests(unittest.TestCase):
    def test_three_models_at_the_peak(self):
        for model, exch in CDNA_TABLE_50.items():
            fm = RobinhoodFees(exchange="cdna", cdna_fee_model=model)
            self.assertEqual(fm.exchange_fee(0.50, 100), Decimal(exch), model)
            self.assertEqual(fm.fee(0.50, 100), Decimal(exch) + Decimal("1.00"), model)
            # nadex is the same entity (North American Derivatives Exchange d/b/a CDNA).
            self.assertEqual(RobinhoodFees(exchange="nadex", cdna_fee_model=model).exchange_fee(0.50, 100), Decimal(exch), model)

    def test_weighted_rounds_up_and_flat_scales(self):
        w = RobinhoodFees(exchange="cdna", cdna_fee_model="weighted_007")
        self.assertEqual(w.exchange_fee(0.01, 100), Decimal("0.07"))  # 0.0693 -> up
        self.assertEqual(w.exchange_fee(0.99, 1), Decimal("0.01"))
        self.assertEqual(RobinhoodFees(exchange="cdna", cdna_fee_model="flat_002").exchange_fee(0.99, 7), Decimal("0.14"))
        # The CDNA model never touches Rothera and vice versa.
        self.assertEqual(RobinhoodFees(exchange="rothera", cdna_fee_model="flat_002").exchange_fee(0.50, 100), Decimal("1.00"))
        self.assertEqual(RobinhoodFees(exchange="cdna", rothera_fee_model="quadratic").exchange_fee(0.50, 100), Decimal("1.00"))


class PolymarketFeeTests(unittest.TestCase):
    def test_sports_peak(self):
        fm = PolymarketFees.from_market({"feeSchedule": {"exponent": 1, "rate": 0.05, "takerOnly": True, "rebateRate": 0.15}, "feesEnabled": True})
        self.assertEqual(fm.fee(0.50, 100), Decimal("1.25000"))
        self.assertEqual(fm.fee(0.50, 100, "maker"), Decimal("0"))
        self.assertEqual(fm.fee(0.34, 100), Decimal("1.12200"))

    def test_fees_disabled(self):
        self.assertEqual(PolymarketFees.from_market({"feesEnabled": False}).fee(0.5, 100), Decimal("0"))

    def test_us_theta_schedule(self):
        # docs.polymarket.us/fees, read 2026-09-26: 0.0695 from 12:00 AM ET on 2026-09-25.
        before = PolymarketUSFees.for_date(date(2026, 9, 24))
        after = PolymarketUSFees.for_date(date(2026, 9, 25))
        self.assertEqual(before.taker_theta, POLY_US_TAKER_THETA_BEFORE)
        self.assertEqual(after.taker_theta, POLY_US_TAKER_THETA_AFTER)
        self.assertEqual(PolymarketUSFees.for_date(date(2026, 9, 16)).taker_theta, POLY_US_TAKER_THETA_BEFORE)
        self.assertEqual(before.fee(0.50, 100), Decimal("1.50"))  # 0.06 * 100 * 0.25 = 1.50
        self.assertEqual(before.fee(0.50, 100, "maker"), Decimal("-0.31"))
        self.assertEqual(after.fee(0.50, 100), Decimal("1.74"))  # 1.7375 -> banker's rounding, the page's 100-lot row

    def test_us_fee_page_worked_example(self):
        # docs.polymarket.us/fees, read 2026-09-26. 1,000 @ $0.50 -> taker $17.38
        # (17.375 -> even), maker -$3.12 (-3.125 -> even). The other four examples
        # on that page are the same formula.
        fm = PolymarketUSFees(taker_theta=POLY_US_TAKER_THETA_AFTER)
        self.assertEqual(fm.fee(0.50, 1000), Decimal("17.38"))
        self.assertEqual(fm.fee(0.50, 1000, "maker"), Decimal("-3.12"))
        self.assertEqual(fm.fee(0.10, 1000), Decimal("6.26"))
        self.assertEqual(-fm.fee(0.10, 1000, "maker"), Decimal("1.12"))
        self.assertEqual(fm.fee(0.65, 1000), Decimal("15.81"))
        self.assertEqual(-fm.fee(0.65, 1000, "maker"), Decimal("2.84"))
        self.assertEqual(fm.fee(0.30, 1000), Decimal("14.60"))
        self.assertEqual(fm.fee(0.90, 1000), Decimal("6.26"))
        self.assertEqual(POLY_US_WORKED_EXAMPLE["taker"], Decimal("17.38"))
        self.assertTrue(check_polymarket_us_worked_example())
        self.assertTrue(check_polymarket_us_worked_example(PolymarketUSFees.for_date(date(2026, 9, 25))))
        self.assertTrue(check_polymarket_us_worked_example(PolymarketUSFees.for_date(date(2026, 9, 26))))
        # The day before the published effective date still uses 0.06, so the guard is not vacuous.
        self.assertFalse(check_polymarket_us_worked_example(PolymarketUSFees.for_date(date(2026, 9, 24))))
        self.assertFalse(check_polymarket_us_worked_example(PolymarketUSFees.for_date(date(2026, 9, 19))))


class RegistryTests(unittest.TestCase):
    def test_registry_picks_models(self):
        self.assertEqual(fee_model_for("kalshi", {"fee_type": "quadratic_with_maker_fees"}).name, "kalshi")
        self.assertEqual(fee_model_for("robinhood", {"symbol": "NFLGAME-X-Y"}, settings={"robinhood_gold": True}).gold, True)
        self.assertEqual(fee_model_for("polymarket", {"feeSchedule": {"rate": 0.05}}).rate, Decimal("0.05"))
        self.assertEqual(fee_model_for("sportsbook").fee(0.5, 100), Decimal("0"))

    def test_default_is_flat_001_without_settings(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ROBINHOOD_ROTHERA_FEE_MODEL", None)
            os.environ.pop("CDNA_FEE_MODEL", None)
            fm = fee_model_for("robinhood", {"symbol": "NFLGAME-26SEP20PHITEN-PHI"})
            self.assertEqual(fm.rothera_fee_model, "flat_001")
            self.assertEqual(fm.fee(0.99, 100), Decimal("1.10"))
            self.assertEqual(fee_model_for("robinhood", {"symbol": "NX.F.OPT.CFB-00027-260919-M.O.1.1.20270228"}, settings={}).cdna_fee_model, "flat_001")

    def test_model_from_settings_dict(self):
        fm = fee_model_for("robinhood", {"exchange": "rothera"}, settings={"rothera_fee_model": "quadratic"})
        self.assertEqual(fm.rothera_fee_model, "quadratic")
        self.assertEqual(fm.fee(0.99, 100), Decimal("0.12"))
        cd = fee_model_for("robinhood", {"exchange": "cdna"}, settings={"cdna_fee_model": "weighted_007"})
        self.assertEqual(cd.fee(0.50, 100), Decimal("2.75"))
        q = OutcomeQuote(venue="robinhood", venue_market_id="x", event_key="nfl:A|B:2026-09-20", outcome="A", outcome_label="A", ask=0.99, bid=0.98, fee_params={"exchange": "rothera", "symbol": "NFLGAME-26SEP20AB-A"})
        self.assertEqual(fee_model_for_quote(q, {"rothera_fee_model": "quadratic"}).fee(0.99, 100), Decimal("0.12"))
        self.assertEqual(fee_model_for_quote(q, {}).fee(0.99, 100), Decimal("1.10"))

    def test_model_from_env_var(self):
        with mock.patch.dict(os.environ, {"ROBINHOOD_ROTHERA_FEE_MODEL": "quadratic", "CDNA_FEE_MODEL": "flat_002"}):
            self.assertEqual(fee_setting(None, "rothera_fee_model"), "quadratic")
            self.assertEqual(fee_setting({}, "cdna_fee_model"), "flat_002")
            self.assertEqual(fee_model_for("robinhood", {"exchange": "rothera"}).fee(0.99, 100), Decimal("0.12"))
            self.assertEqual(fee_model_for("robinhood", {"exchange": "cdna"}).exchange_fee(0.50, 100), Decimal("2.00"))
            # An explicit settings dict beats the environment.
            self.assertEqual(fee_setting({"rothera_fee_model": "flat_001"}, "rothera_fee_model"), "flat_001")
        with mock.patch.dict(os.environ, {"ROBINHOOD_ROTHERA_FEE_MODEL": ""}):
            self.assertEqual(fee_setting(None, "rothera_fee_model"), "flat_001")

    def test_quote_fee_params_pin_the_model(self):
        fm = fee_model_for("robinhood", {"exchange": "rothera", "rothera_fee_model": "quadratic"}, settings={"rothera_fee_model": "flat_001"})
        self.assertEqual(fm.rothera_fee_model, "quadratic")

    def test_settings_declared_when_config_supports_it(self):
        # P01's declare_setting may or may not exist in this tree; when it does, both keys are registered.
        try:
            from arb_engine.config import KNOWN_SETTINGS
        except ImportError:
            self.skipTest("config.declare_setting not present")
        for key, (env, default, _) in FEE_SETTINGS.items():
            self.assertIn(key, KNOWN_SETTINGS)
            self.assertEqual((KNOWN_SETTINGS[key].env, KNOWN_SETTINGS[key].default), (env, default))


class FeeVectorTests(unittest.TestCase):
    """tests/fixtures/fee_vectors.json is what arb-core.js is checked against; every row must
    also come back out of the Python registry, so the two never drift."""

    @classmethod
    def setUpClass(cls):
        cls.vectors = load("fee_vectors.json")

    def test_every_vector_reproduces_through_the_registry(self):
        for v in self.vectors:
            fee = model_from_vector(v).fee(v["price"], v["contracts"], v["role"])
            self.assertEqual(float(fee), v["fee"], v)

    def test_vectors_cover_the_new_models_at_1_10_100(self):
        def rows(**match):
            return [v for v in self.vectors if v["venue"] == "robinhood" and all((v.get("settings") or {}).get(k) == val for k, val in match.items())]
        for model in ROTHERA_FEE_MODELS:
            sel = rows(rotheraFeeModel="quadratic") if model == "quadratic" else [v for v in rows() if not (v.get("settings") or {}).get("rotheraFeeModel") and v["params"].get("exchange") == "rothera"]
            self.assertEqual({1, 10, 100} - {v["contracts"] for v in sel}, set(), model)
        for model in CDNA_FEE_MODELS:
            self.assertEqual({1, 10, 100} - {v["contracts"] for v in rows(cdnaFeeModel=model)}, set(), model)
        us = [v for v in self.vectors if v["venue"] == "polymarket_us" and v["price"] == 0.5 and v["contracts"] == 1000]
        self.assertEqual({v["role"]: v["fee"] for v in us}, {"taker": 17.38, "maker": -3.12})
        half = [v for v in self.vectors if v["venue"] == "kalshi" and v["params"].get("fee_multiplier") == 0.5 and v["price"] == 0.5 and v["contracts"] == 100]
        self.assertEqual([v["fee"] for v in half], [0.88])

    def test_generator_is_in_sync_with_the_fixture(self):
        spec = importlib.util.spec_from_file_location("gen_fee_vectors", ROOT / "scripts" / "gen_fee_vectors.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.build_vectors(), self.vectors)


class FeeFlipFixtureTests(unittest.TestCase):
    """tests/fixtures/results/fee_flip_p10.json: metrics of the opt-in models on the recorded
    NFL / NCAAF fixture scans (scripts/fee_flip_p10.py). Proves the Rothera switch moves only
    Rothera all-ins and the CDNA switch only CDNA all-ins, and pins the sign-flip counts."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("fee_flip_p10", ROOT / "scripts" / "fee_flip_p10.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.metrics = mod.fee_flip_metrics()
        with open(FIXTURES / "results" / "fee_flip_p10.json", encoding="utf-8") as f:
            cls.recorded = json.load(f)

    def test_results_fixture_matches_recomputation(self):
        self.assertEqual(self.metrics, self.recorded)

    def test_only_the_switched_exchange_moves(self):
        for sport, per in self.metrics["fixtures"].items():
            for name, m in per.items():
                self.assertEqual(m["rows_moved_off_exchange"], 0, (sport, name))
                self.assertLessEqual(m["rows_moved"], m["rows_on_exchange"], (sport, name))
        nfl = self.metrics["fixtures"]["nfl"]["rothera:quadratic"]
        self.assertEqual(nfl["rows_moved"], nfl["rows_on_exchange"])  # every Rothera row re-priced
        self.assertGreater(nfl["margins_improved"], 0)
        self.assertEqual(self.metrics["fixtures"]["nfl"]["cdna:flat_002"]["rows_moved"], 0)  # no CDNA rows in the NFL fixture
        self.assertEqual(self.metrics["fixtures"]["ncaaf"]["rothera:quadratic"]["rows_moved"], 0)  # no Rothera rows in the NCAAF fixture


if __name__ == "__main__":
    unittest.main()

"""Per-leg dollars include fees and apply before any real submission."""
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from arb_engine.strategy.arbbutton import ArbButton
from tests.test_arbbutton import _button, _me, _sized, KEY, T0


class SideCapTests(unittest.TestCase):
    def test_both_legs_fit_twenty_five_dollars_even_with_split_fees(self):
        b, _ = _button(side_cap="25")
        me = _me()
        spec = b.register(KEY, "cap", _sized(me, budget=500), me.quotes_by_venue, now=T0)
        self.assertIsNotNone(spec)
        self.assertLess(spec["count"], _sized(me, budget=500)["contracts"])
        self.assertEqual(spec["ticket"]["contracts"], spec["count"])
        self.assertEqual(spec["ticket"]["payout"], spec["count"])
        self.assertTrue(all(leg["contracts"] == spec["count"] for leg in spec["ticket"]["legs"]))
        for leg, fee, field in zip((spec["kalshi"], spec["robinhood"]), spec["_fees"], ("limit", "max")):
            price = leg[field]
            cost = (Decimal(str(price)) + fee.fee(min(price, .5), 1, "taker")) * spec["count"]
            self.assertLessEqual(cost, Decimal("25"))

    def test_cap_too_small_for_one_contract_issues_no_ticket(self):
        b, _ = _button(side_cap=".01")
        me = _me()
        self.assertIsNone(b.register(KEY, "cap", _sized(me), me.quotes_by_venue, now=T0))

    def test_nonfinite_and_nonpositive_caps_rejected(self):
        for cap in ("NaN", "Infinity", "0", "-1"):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                ArbButton(side_cap=cap)

    def test_exchange_multiplier_increase_blocks_before_reservation(self):
        b, _ = _button(side_cap="25")
        me = _me()
        spec = b.register(KEY, "cap", _sized(me), me.quotes_by_venue, now=T0)
        b._fee_mults = SimpleNamespace(resolve=lambda *args: (Decimal("100"), None))
        executor = Mock(client=Mock())
        ledger = Mock()
        with patch.object(b, "executor", return_value=executor), patch.object(b, "ledger", return_value=ledger):
            result = b._send_real(spec, spec["count"], spec["_fees"][0], T0)
        self.assertEqual(result["status"], "skipped")
        ledger.reserve.assert_not_called()
        executor.execute.assert_not_called()

import os
import unittest
from unittest import mock

from arb_engine import config
from arb_engine.config import KNOWN_SETTINGS, as_bool, declare_setting, load_settings, setting, settings_from_env
from arb_engine.venues.http import HttpError
from tests.helpers import SequencedFakeHttp


class _Declared:
    """Declare scratch keys for one test and remove them afterwards."""

    def __init__(self, *specs):
        self.specs = specs

    def __enter__(self):
        return [declare_setting(*s[0], **s[1]) for s in self.specs]

    def __exit__(self, *exc):
        for s in self.specs:
            KNOWN_SETTINGS.pop(s[0][0], None)
        return False


class DeclareSettingTest(unittest.TestCase):
    def test_precedence_dict_env_default(self):
        with _Declared((("t_num",), dict(env="ARB_TEST_T_NUM", default=2.5, cast=float, doc="scratch"))), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARB_TEST_T_NUM", None)
            self.assertEqual(setting({}, "t_num"), 2.5)
            self.assertEqual(setting(None, "t_num"), 2.5)
            os.environ["ARB_TEST_T_NUM"] = "7"
            self.assertEqual(setting({}, "t_num"), 7.0)
            self.assertEqual(setting({"t_num": 1.0}, "t_num"), 1.0)
            self.assertEqual(setting({"t_num": None}, "t_num"), None)  # explicit None in the dict wins too
            self.assertEqual(load_settings()["t_num"], 7.0)
            os.environ["ARB_TEST_T_NUM"] = "   "
            self.assertEqual(setting({}, "t_num"), 2.5)  # empty typed env value means unset

    def test_cast_and_untyped_strings(self):
        with _Declared((("t_flag",), dict(env="ARB_TEST_T_FLAG", default=False, cast=as_bool)), (("t_str",), dict(env="ARB_TEST_T_STR", default="cent"))):
            with mock.patch.dict(os.environ, {"ARB_TEST_T_FLAG": "Yes", "ARB_TEST_T_STR": ""}):
                self.assertIs(setting({}, "t_flag"), True)
                self.assertEqual(setting({}, "t_str"), "")
            with mock.patch.dict(os.environ, {"ARB_TEST_T_FLAG": "0"}):
                self.assertIs(setting({}, "t_flag"), False)

    def test_undeclared_key(self):
        with self.assertRaises(KeyError):
            setting({}, "definitely_not_declared_zz")
        self.assertEqual(setting({}, "definitely_not_declared_zz", default=4), 4)
        self.assertEqual(setting({"definitely_not_declared_zz": 5}, "definitely_not_declared_zz", default=4), 5)

    def test_redeclaration_identical_ok_conflicting_raises(self):
        with _Declared((("t_dup",), dict(env="ARB_TEST_T_DUP", default=1, cast=int))):
            declare_setting("t_dup", env="ARB_TEST_T_DUP", default=1, cast=int, doc="same again")
            with self.assertRaises(ValueError):
                declare_setting("t_dup", env="ARB_TEST_T_DUP", default=2, cast=int)
            with self.assertRaises(ValueError):
                declare_setting("t_dup", env="OTHER", default=1, cast=int)
        self.assertNotIn("t_dup", KNOWN_SETTINGS)

    def test_known_settings_enumerates_the_historical_keys(self):
        expected = {
            "robinhood_gold": ("ROBINHOOD_GOLD", False),
            "kalshi_rounding": ("KALSHI_FEE_ROUNDING", "cent"),
            "polymarket_us_volume_rebate": ("POLYMARKET_US_VOLUME_REBATE", 0.0),
            "venue_weights": (None, None),
            "odds_api_key": ("ODDS_API_KEY", None),
        }
        for key, (env, default) in expected.items():
            spec = KNOWN_SETTINGS[key]
            self.assertEqual((spec.env, spec.default), (env, default), key)
            self.assertTrue(spec.doc, f"{key} needs a doc string")
        for spec in KNOWN_SETTINGS.values():
            self.assertTrue(spec.doc, f"{spec.key} needs a doc string")

    def test_load_settings_matches_the_old_settings_from_env(self):
        with mock.patch.dict(os.environ, {"ROBINHOOD_GOLD": "true", "KALSHI_FEE_ROUNDING": "centicent", "POLYMARKET_US_VOLUME_REBATE": "", "ODDS_API_KEY": "k"}):
            s = settings_from_env()
        self.assertEqual({k: s[k] for k in ("robinhood_gold", "kalshi_rounding", "polymarket_us_volume_rebate", "venue_weights", "odds_api_key")}, {"robinhood_gold": True, "kalshi_rounding": "centicent", "polymarket_us_volume_rebate": 0.0, "venue_weights": None, "odds_api_key": "k"})
        self.assertEqual(set(s), set(KNOWN_SETTINGS))
        self.assertIs(config.settings_from_env, settings_from_env)


class SequencedFakeHttpTest(unittest.TestCase):
    def test_scripted_order_and_recording(self):
        http = SequencedFakeHttp([
            (403, "forbidden"),
            ConnectionResetError("boom"),
            {"ok": 1},
            (200, '{"raw": true}'),
        ], base_headers={"User-Agent": "base"})
        with self.assertRaises(HttpError) as cm:
            http.get("https://site.api.espn.com/v2/a", params={"x": 1, "y": None}, headers={"User-Agent": "ua-1"})
        self.assertEqual(cm.exception.status, 403)
        with self.assertRaises(ConnectionResetError):
            http.get("https://sports.core.api.espn.com/v2/b", headers={"User-Agent": "ua-2"})
        self.assertEqual(http.post("https://api.elections.kalshi.com/trade-api/v2/orders", json_body={"n": 1}), {"ok": 1})
        self.assertEqual(http.get("https://demo-api.kalshi.co/v2/c", raw=True), '{"raw": true}')
        self.assertEqual(
            [(h, p, hd.get("User-Agent")) for h, p, hd in http.requests],
            [("site.api.espn.com", "/v2/a", "ua-1"), ("sports.core.api.espn.com", "/v2/b", "ua-2"), ("api.elections.kalshi.com", "/trade-api/v2/orders", "base"), ("demo-api.kalshi.co", "/v2/c", "base")],
        )
        self.assertEqual(http.calls[0], "https://site.api.espn.com/v2/a?x=1")
        self.assertEqual(http.methods, ["GET", "GET", "POST", "GET"])
        self.assertEqual(http.bodies[2], {"n": 1})
        self.assertEqual(http.remaining, 0)

    def test_running_past_the_script_fails_loudly(self):
        http = SequencedFakeHttp(['{"a": 1}'])
        self.assertEqual(http.get("https://x/y"), {"a": 1})
        with self.assertRaises(AssertionError):
            http.delete("https://x/y")

    def test_callable_item_and_json_body_for_error(self):
        http = SequencedFakeHttp([lambda: (500, {"error": "down"}), (200, {"v": 2})])
        with self.assertRaises(HttpError) as cm:
            http.get("https://x/y")
        self.assertIn('"down"', cm.exception.body)
        self.assertEqual(http.get("https://x/y", raw=True), '{"v": 2}')


if __name__ == "__main__":
    unittest.main()

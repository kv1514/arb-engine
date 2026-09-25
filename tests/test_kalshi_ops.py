"""Offline adversarial tests for manual Kalshi account and mutation controls."""

from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from arb_engine import cli
from arb_engine.cli_plugins.kalshi_ops import load_account_env, run_kalshi
from arb_engine.execution.kalshi import KalshiExecutor


class _Client:
    def __init__(self, env="demo"):
        self.env = env
        self.base_url = f"https://{env}.invalid"
        self.calls = []

    def cancel_order(self, order_id):
        self.calls.append(("cancel", order_id))
        return {"order_id": order_id, "reduced_by": "1.00"}

    def cancel_all_orders(self, subaccount=None):
        self.calls.append(("cancel-all", subaccount))


class ExecutorMutationGateTests(unittest.TestCase):
    def test_cancel_is_dry_run_without_network(self):
        client = _Client()
        result = KalshiExecutor(client).cancel("o-1")
        self.assertTrue(result["status"].startswith("DRY_RUN"))
        self.assertEqual(client.calls, [])

    def test_confirmed_demo_cancel_and_sweep(self):
        client = _Client()
        ex = KalshiExecutor(client)
        self.assertEqual(ex.cancel("o-1", confirm=True)["status"], "CANCELLED")
        self.assertEqual(ex.cancel_all(confirm=True, subaccount=2)["status"], "CANCELLED_ALL")
        self.assertEqual(client.calls, [("cancel", "o-1"), ("cancel-all", 2)])

    def test_production_cancel_requires_live_opt_in(self):
        client = _Client("prod")
        with mock.patch.dict(os.environ, {}, clear=True):
            result = KalshiExecutor(client).cancel_all(confirm=True)
        self.assertTrue(result["status"].startswith("BLOCKED"))
        self.assertEqual(client.calls, [])

    def test_blank_order_id_rejected_before_network(self):
        client = _Client()
        with self.assertRaises(ValueError):
            KalshiExecutor(client).cancel(" ", confirm=True)
        self.assertEqual(client.calls, [])

    def test_invalid_order_shapes_rejected_before_network(self):
        ex = KalshiExecutor(_Client())
        for kwargs in ({"ticker": "", "action": "buy", "side": "yes"},
                       {"ticker": "T", "action": "hold", "side": "yes"},
                       {"ticker": "T", "action": "buy", "side": "maybe"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ex.plan(count=1, price=.5, **kwargs)
        with self.assertRaisesRegex(ValueError, "post_only"):
            ex.plan("T", "buy", "yes", 1, .5, post_only=True, time_in_force="immediate_or_cancel")


class AccountOpsPluginTests(unittest.TestCase):
    def test_plugin_extends_existing_command(self):
        parser, overrides = cli.build_parser()
        self.assertIn("kalshi", overrides)
        args = parser.parse_args(["kalshi", "cancel", "--order-id", "o-1"])
        self.assertEqual((args.action, args.order_id, args.max_notional), ("cancel", "o-1", 25.0))

    def test_local_env_never_overrides_exported_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "env")
            path.write_text("export KALSHI_ENV=demo\nKALSHI_API_KEY=file-key\nIGNORED=x\n", encoding="utf-8")
            env = {"KALSHI_API_KEY": "shell-key"}
            loaded = load_account_env(path, env)
        self.assertEqual(loaded, ["KALSHI_ENV"])
        self.assertEqual(env, {"KALSHI_API_KEY": "shell-key", "KALSHI_ENV": "demo"})

    def test_manual_notional_cap_fails_before_client_construction(self):
        args = argparse.Namespace(action="order", no_account_env=True, ticker="T", price=.75,
                                  count=40, max_notional=25, side_action="buy", side="yes",
                                  post_only=False, exchange_index=None,
                                  time_in_force="immediate_or_cancel", confirm=True)
        with mock.patch("arb_engine.execution.kalshi.KalshiExecutor") as constructor:
            constructor.return_value.client = mock.Mock()
            with self.assertRaisesRegex(SystemExit, "exceeds --max-notional"):
                run_kalshi(args)
            constructor.return_value.plan.assert_not_called()

    def test_cancel_cli_remains_dry_run_by_default(self):
        fake = mock.Mock()
        fake.cancel.return_value = {"status": "DRY_RUN"}
        args = argparse.Namespace(action="cancel", no_account_env=True, order_id="o-9", confirm=False)
        out = io.StringIO()
        with mock.patch("arb_engine.execution.kalshi.KalshiExecutor", return_value=fake), redirect_stdout(out):
            self.assertEqual(run_kalshi(args), 0)
        fake.cancel.assert_called_once_with("o-9", confirm=False)
        self.assertEqual(json.loads(out.getvalue())["status"], "DRY_RUN")


if __name__ == "__main__":
    unittest.main()

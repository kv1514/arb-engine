"""Safe account/order lifecycle additions for the existing ``kalshi`` command.

Credentials remain outside the repository.  The handler reads ``~/.kalshi/env`` when it
exists, without overriding variables already exported by the operator.  Every mutation is
still dry-run by default and goes through :class:`KalshiExecutor`'s demo/production gates.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping


def _has_option(parser: argparse.ArgumentParser, option: str) -> bool:
    return any(option in action.option_strings for action in parser._actions)


def register(subparsers: Any, existing_parsers: Mapping[str, argparse.ArgumentParser]) -> dict[str, Callable[..., int]]:
    parser = existing_parsers.get("kalshi")
    if parser is None:
        return {}
    action = next((a for a in parser._actions if a.dest == "action"), None)
    if action is not None:
        action.choices = ["balance", "positions", "orders", "fills", "order", "cancel", "cancel-all"]
    additions = (
        ("--order-id", {"help": "order id for cancel"}),
        ("--status", {"default": "resting", "help": "order status filter (default resting; use all for no filter)"}),
        ("--time-in-force", {"choices": ["good_till_canceled", "immediate_or_cancel", "fill_or_kill"], "default": "good_till_canceled"}),
        ("--exchange-index", {"type": int}),
        ("--subaccount", {"type": int, "help": "optional subaccount for cancel-all"}),
        ("--max-notional", {"type": float, "default": 25.0, "help": "maximum loss/notional for a submitted manual order (default $25)"}),
        ("--no-account-env", {"action": "store_true", "help": "do not read ~/.kalshi/env"}),
    )
    for option, kwargs in additions:
        if not _has_option(parser, option):
            parser.add_argument(option, **kwargs)
    return {"kalshi": run_kalshi}


def load_account_env(path: Path | None = None, environ: dict[str, str] | None = None) -> list[str]:
    """Load only KALSHI_* assignments; exported process values always win."""
    path = path or Path("~/.kalshi/env").expanduser()
    environ = os.environ if environ is None else environ
    if not path.is_file():
        return []
    loaded: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        name, sep, value = line.partition("=")
        name = name.strip()
        if sep and name.startswith("KALSHI_") and name not in environ:
            environ[name] = os.path.expanduser(value.strip().strip('"').strip("'"))
            loaded.append(name)
    return loaded


def _order_max_loss(action: str, count: float, price: float) -> float:
    return float(count) * (float(price) if action == "buy" else 1.0 - float(price))


def run_kalshi(args: argparse.Namespace, settings: Mapping[str, Any] | None = None) -> int:
    if not getattr(args, "no_account_env", False):
        load_account_env()
    from ..execution.kalshi import KalshiExecutor

    ex = KalshiExecutor()
    action = args.action
    if action == "balance":
        result: Any = ex.client.balance()
    elif action == "positions":
        result = ex.client.positions()
    elif action == "orders":
        params = {} if args.status == "all" else {"status": args.status}
        result = ex.client.orders_v2(**params)
    elif action == "fills":
        result = ex.client.fills_v2()
    elif action == "order":
        if not args.ticker or args.price is None:
            raise SystemExit("kalshi order requires --ticker and --price")
        max_loss = _order_max_loss(args.side_action, args.count, args.price)
        if max_loss > args.max_notional:
            raise SystemExit(f"manual order maximum loss ${max_loss:.2f} exceeds --max-notional ${args.max_notional:.2f}")
        plan = ex.plan(args.ticker, args.side_action, args.side, args.count, args.price,
                       post_only=args.post_only, exchange_index=args.exchange_index,
                       time_in_force=args.time_in_force)
        result = ex.execute(plan, confirm=args.confirm)
    elif action == "cancel":
        if not args.order_id:
            raise SystemExit("kalshi cancel requires --order-id")
        result = ex.cancel(args.order_id, confirm=args.confirm)
    elif action == "cancel-all":
        result = ex.cancel_all(confirm=args.confirm, subaccount=args.subaccount)
    else:  # pragma: no cover - argparse owns the choices
        raise SystemExit(f"unknown Kalshi action: {action}")
    print(json.dumps(result, indent=1, default=str))
    return 0

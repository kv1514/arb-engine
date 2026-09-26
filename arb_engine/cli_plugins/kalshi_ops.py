"""Safe account/order lifecycle additions for the existing ``kalshi`` command.

Credentials remain outside the repository.  The handler reads ``~/.kalshi/env`` when it
exists, without overriding variables already exported by the operator.  Every mutation is
still dry-run by default and goes through :class:`KalshiExecutor`'s demo/production gates
(including the host/environment agreement check, so ``KALSHI_BASE_URL`` cannot point the
demo gates at a production host).

* ``order`` refuses non-finite numbers, and caps the order's maximum loss **fees included**
  (``count x price`` for a buy, ``count x (1 - price)`` for a sell, plus the most Kalshi's
  taker fee can be; Decimal) at ``--max-notional``. A confirmed order is recorded in the
  environment's order ledger (``execution/ledger.py``) before it is sent, carries the
  ledger's ``client_order_id``, and an answer that never came back is reported as UNKNOWN.
* Exit status: 0 done or dry-run; 3 blocked (a gate or the ledger refused); 4 the order's
  outcome is unknown (run ``kalshi reconcile``).
* ``orders`` / ``fills`` walk every page up to the client's limit and say whether the
  listing was truncated; ``positions`` says whether a further page exists.
* ``reconcile`` resolves the ledger's open orders against the exchange (reads only);
  ``ledger`` prints the ledger's state and its open orders; ``release --intent-id X --reason
  "..." --confirm`` releases one open intent by hand after you checked the exchange yourself
  (the escape hatch for an order the engine cannot prove either way; recorded as such).
"""

from __future__ import annotations

import argparse
import json
import math
import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

EXIT_BLOCKED, EXIT_UNKNOWN = 3, 4


def _has_option(parser: argparse.ArgumentParser, option: str) -> bool:
    return any(option in action.option_strings for action in parser._actions)


def register(subparsers: Any, existing_parsers: Mapping[str, argparse.ArgumentParser]) -> dict[str, Callable[..., int]]:
    parser = existing_parsers.get("kalshi")
    if parser is None:
        return {}
    action = next((a for a in parser._actions if a.dest == "action"), None)
    if action is not None:
        action.choices = ["balance", "positions", "orders", "fills", "order", "cancel", "cancel-all", "reconcile", "ledger", "release"]
    additions = (
        ("--order-id", {"help": "order id for cancel"}),
        ("--status", {"default": "resting", "help": "order status filter (default resting; use all for no filter)"}),
        ("--time-in-force", {"choices": ["good_till_canceled", "immediate_or_cancel", "fill_or_kill"], "default": "good_till_canceled"}),
        ("--exchange-index", {"type": int}),
        ("--subaccount", {"type": int, "help": "optional subaccount for cancel-all"}),
        ("--max-notional", {"type": float, "default": 25.0, "help": "maximum loss of a submitted manual order, Kalshi fees included (default $25)"}),
        ("--no-account-env", {"action": "store_true", "help": "do not read ~/.kalshi/env"}),
        ("--intent-id", {"help": "release: the order-ledger intent to release by hand"}),
        ("--reason", {"help": "release: why (what you checked on the exchange)"}),
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


def _finite(name: str, value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise SystemExit(f"{name} must be a number") from None
    if not math.isfinite(v):
        raise SystemExit(f"{name} must be finite (got {value!r})")
    return v


def _order_max_loss(action: str, count: float, price: float, multiplier: Any) -> Decimal:
    """The most a manual order can lose, fees included: a buy pays ``count x price``, a sell
    of the side is short ``count x (1 - price)``; either pays at most ``fee_bound`` in taker
    fees at the market's fee ``multiplier`` (a sell fills at or above its price, so its
    dearest-fee price mirrors to ``1 - p``)."""
    from ..execution.ledger import fee_bound
    from ..fees.base import D

    c, p = D(str(count)), D(str(price))
    exposed = p if action == "buy" else Decimal(1) - p
    return c * exposed + fee_bound(exposed, c, multiplier)


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=1, default=str))


def _status_code(result: Any) -> int:
    st = str((result or {}).get("status") or "") if isinstance(result, dict) else ""
    if st.startswith("BLOCKED"):
        return EXIT_BLOCKED
    if st == "UNKNOWN":
        return EXIT_UNKNOWN
    return 0


def run_kalshi(args: argparse.Namespace, settings: Mapping[str, Any] | None = None) -> int:
    if not getattr(args, "no_account_env", False):
        load_account_env()
    action = args.action
    if action == "order":
        # Validate before any client exists: nothing here needs the network to be refused.
        if not args.ticker or args.price is None:
            raise SystemExit("kalshi order requires --ticker and --price")
        count, price = _finite("--count", args.count), _finite("--price", args.price)
        max_notional = _finite("--max-notional", args.max_notional)
        if max_notional <= 0:
            raise SystemExit("--max-notional must be positive")
        if count <= 0 or not 0 < price < 1:
            raise SystemExit("--count must be positive and --price in (0, 1)")
        # At multiplier 1 first - a lower bound on the fee - so an order too big even then is
        # refused before any client exists; the market's own multiplier is checked below.
        max_loss = _order_max_loss(args.side_action, count, price, 1)
        if max_loss > Decimal(str(max_notional)):
            raise SystemExit(f"manual order maximum loss ${max_loss:.2f} (fees included) exceeds --max-notional ${max_notional:.2f}")
    from ..execution.kalshi import KalshiExecutor

    ex = KalshiExecutor()
    if action == "balance":
        result: Any = ex.client.balance()
    elif action == "positions":
        result = ex.client.positions()
        if isinstance(result, dict):
            result = {**result, "truncated": bool(result.get("cursor"))}
    elif action in ("orders", "fills"):
        params = {} if action == "fills" or args.status == "all" else {"status": args.status}
        path, key = ("/portfolio/orders", "orders") if action == "orders" else ("/portfolio/fills", "fills")
        rows, truncated = ex.client.paged(path, key, params)
        result = {key: rows, "count": len(rows), "truncated": truncated}
        if truncated:
            result["note"] = "more pages exist than were read: this is not the whole list"
    elif action == "order":
        from ..execution.ledger import FeeMultipliers

        mult, why = FeeMultipliers(ex.client).resolve(args.ticker)
        if mult is None:
            if args.confirm:
                _print({"status": f"BLOCKED: {why}; the fee - and so the maximum loss - cannot be bounded"})
                return EXIT_BLOCKED
        else:
            max_loss = _order_max_loss(args.side_action, count, price, mult)
            if max_loss > Decimal(str(max_notional)):
                raise SystemExit(f"manual order maximum loss ${max_loss:.2f} (fees included, fee multiplier {mult}) exceeds --max-notional ${max_notional:.2f}")
        return _manual_order(ex, args, max_loss, mult)
    elif action == "cancel":
        if not args.order_id:
            raise SystemExit("kalshi cancel requires --order-id")
        if not args.ticker and args.exchange_index is None:
            raise SystemExit("kalshi cancel requires --ticker for shard auto-routing or --exchange-index")
        result = ex.cancel(args.order_id, confirm=args.confirm, market_ticker=args.ticker,
                           exchange_index=args.exchange_index, subaccount=args.subaccount)
    elif action == "cancel-all":
        result = ex.cancel_all(confirm=args.confirm, subaccount=args.subaccount)
    elif action == "release":
        from ..execution.ledger import LedgerError, OrderLedger

        if not getattr(args, "intent_id", None) or not str(getattr(args, "reason", "") or "").strip():
            raise SystemExit("kalshi release requires --intent-id and --reason")
        try:
            led = OrderLedger.for_client(ex.client)
        except LedgerError as e:
            _print({"status": f"BLOCKED: {e}"})
            return EXIT_BLOCKED
        row = led.get(args.intent_id)
        if not args.confirm:
            _print({"status": "DRY_RUN (pass --confirm to release)", "intent": {k: (row or {}).get(k) for k in ("intent_id", "strategy", "ticker", "count", "state", "reason")}})
            return 0
        try:
            result = {"status": "RELEASED", "intent": led.release(args.intent_id, args.reason)}
        except LedgerError as e:
            _print({"status": f"BLOCKED: {e}"})
            return EXIT_BLOCKED
    elif action in ("reconcile", "ledger"):
        from ..execution.ledger import LedgerError, OrderLedger

        try:
            led = OrderLedger.for_client(ex.client)
        except LedgerError as e:
            _print({"status": f"BLOCKED: {e}"})
            return EXIT_BLOCKED
        result = {"reconciled": led.reconcile(ex.client)} if action == "reconcile" else {}
        result.update(led.status())
        result["open"] = [{k: r[k] for k in ("intent_id", "strategy", "ticker", "side", "count", "limit_price", "state", "order_id", "fill_count", "reason", "created_ts")}
                          for r in led.rows(("pending", "ambiguous", "accepted"))]
        _print(result)
        return EXIT_BLOCKED if result.get("blocked") else 0
    else:  # pragma: no cover - argparse owns the choices
        raise SystemExit(f"unknown Kalshi action: {action}")
    _print(result)
    return _status_code(result)


def _manual_order(ex: Any, args: argparse.Namespace, max_loss: Decimal, mult: Any = None) -> int:
    plan = ex.plan(args.ticker, args.side_action, args.side, args.count, args.price,
                   post_only=args.post_only, exchange_index=args.exchange_index,
                   time_in_force=args.time_in_force)
    if not args.confirm:
        result = ex.execute(plan, confirm=False)
        result["max_loss_fees_included"] = str(max_loss) if mult is not None else f"{max_loss} at fee multiplier 1 (the market's is unknown)"
        result["fee_multiplier"] = str(mult) if mult is not None else None
        _print(result)
        return 0
    problem = ex.gate_problem()
    if problem:
        _print({"status": f"BLOCKED: {problem}", "plan": plan.payload()})
        return EXIT_BLOCKED
    if float(args.count) != int(args.count):
        _print({"status": "BLOCKED: confirmed manual orders are whole contracts (the order ledger counts contracts)"})
        return EXIT_BLOCKED
    from ..execution.ledger import LedgerError, OrderLedger

    try:
        led = OrderLedger.for_client(ex.client)
        res = led.reserve(strategy="manual", ticker=args.ticker, side=args.side, action=args.side_action, count=int(args.count),
                          limit_price=args.price, tif=args.time_in_force, max_cost_per_contract=max_loss / int(args.count),
                          fee_multiplier=mult, detail={"source": "kalshi order --confirm"})
    except LedgerError as e:
        _print({"status": f"BLOCKED: order ledger: {e}"})
        return EXIT_BLOCKED
    if not res.ok:
        _print({"status": f"BLOCKED: {res.reason}"})
        return EXIT_BLOCKED
    plan.client_order_id = res.client_order_id
    req_ts = led.clock()
    try:
        result = ex.execute(plan, confirm=True)
    except Exception as e:  # noqa: BLE001 - the request may have reached the exchange
        hint = getattr(e, "status", None)
        led.ambiguous(res.intent_id, f"{type(e).__name__}: {e}"[:300], req_ts=req_ts, hint=hint if isinstance(hint, int) else None)
        _print({"status": "UNKNOWN", "intent_id": res.intent_id, "client_order_id": res.client_order_id, "error": f"{type(e).__name__}: {e}"[:300],
                "next": "python -m arb_engine kalshi reconcile  (finds the order by its client_order_id; new automatic orders are blocked until then)"})
        return EXIT_UNKNOWN
    if result.get("status") != "SUBMITTED":
        led.rejected(res.intent_id, f"executor returned {result.get('status')!r}")
        _print(result)
        return _status_code(result) or EXIT_BLOCKED
    state = led.accepted(res.intent_id, result.get("response") or {}, req_ts=req_ts)
    result.update(intent_id=res.intent_id, ledger_state=state, max_loss_fees_included=str(max_loss))
    _print(result)
    return 0 if state == "accepted" else EXIT_UNKNOWN

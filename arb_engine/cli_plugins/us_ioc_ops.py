"""Explicit manual IOC US purchases and restart recovery; never an automatic scanner."""
import json
import os
from pathlib import Path

from ..execution.polymarket_us_ioc import PolymarketUSExecutor, USOrderLedger, USOrderPlan
from ..venues.polymarket_us_trading import PolymarketUSTradingClient, load_credentials


def register(subparsers, existing_parsers):
    parser = subparsers.add_parser("us-ioc", help="gated US IOC purchase/recovery; dry-run by default")
    parser.add_argument("action", choices=("order", "ledger", "reconcile"))
    parser.add_argument("--market-slug")
    parser.add_argument("--side", choices=("yes", "no"), default="yes", help="contract side; --limit is what this side costs, not long price")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--limit", help="decision-time dollar price limit for the purchased side")
    parser.add_argument("--request-id", help="local deduplication ID; use the same ID for retries (never resends)")
    parser.add_argument("--intent-id", help="one existing ledger intent to reconcile; omitted = all")
    parser.add_argument("--confirm", action="store_true", help="enable gated mutation; reconcile may cancel remaining IOC quantity")
    parser.set_defaults(func=run)


def _client():
    # Secrets never enter args, JSON results, settings dicts or git.
    fields = {key: os.environ.get(key) for key in ("POLYMARKET_KEY_ID", "POLYMARKET_SECRET_KEY")}
    if any(value is not None for value in fields.values()):
        if not all(fields.values()):
            raise ValueError("both US credential variables are required")
        values = fields
    else:
        values = load_credentials(Path(__file__).resolve().parents[2] / "secrets" / "polymarket_us.env")
    return PolymarketUSTradingClient(values)


def run(args, settings=None):
    ledger = None
    try:
        if args.action == "order":
            if not args.market_slug or args.limit is None:
                raise ValueError("order requires --market-slug and --limit")
            extra = {"request_id": args.request_id} if args.request_id else {}
            plan = USOrderPlan(args.market_slug, args.side, args.count, args.limit, **extra)
            executor = PolymarketUSExecutor(_client() if args.confirm else None)
            try:
                result = executor.execute(plan, confirm=args.confirm)
            finally:
                if executor.ledger is not None:
                    executor.ledger.close()
        else:
            ledger = USOrderLedger()
            if args.action == "ledger":
                result = ledger.status()
            else:
                executor = PolymarketUSExecutor(_client(), ledger=ledger)
                rows = [ledger.get(args.intent_id)] if args.intent_id else ledger.status()["intents"]
                result = {"reconciled": [executor.recover(row["intent_id"], confirm=args.confirm) for row in rows], **ledger.status()}
        print(json.dumps(result, indent=2, default=str))
        status = result.get("status", "")
        unresolved = {"UNKNOWN", "OPEN", "CONTRADICTED", "PENDING"}
        if status in unresolved or any(r.get("status") in unresolved for r in result.get("reconciled", [])):
            return 4
        return 3 if status == "BLOCKED" else 0
    except Exception:
        # A credential, HTTP or ledger error may contain sensitive values. No repr.
        print(json.dumps({"status": "BLOCKED", "reason": "invalid input, credentials or unavailable ledger; inspect local setup"}))
        return 3
    finally:
        if ledger is not None:
            ledger.close()

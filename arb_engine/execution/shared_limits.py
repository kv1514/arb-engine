"""Hard production cash ceilings, shared by Kalshi and Polymarket US.

All callers use the same production SQLite file and BEGIN IMMEDIATE transaction.
Filled purchases keep counting: this conservative v1 does not infer that old
inventory settled, or that a hedge/external sale returned cash. Never reset at midnight.
"""
from decimal import Decimal

LEG_CAP = Decimal("25")
TOTAL_CAP = Decimal("50")


def exposure(connection):
    # Import only at call time: ledger.reserve uses this module too.
    from .ledger import OrderLedger
    total = Decimal("0")
    for row in connection.execute("SELECT * FROM intents WHERE state != 'rejected'"):
        total += OrderLedger.exposure_of(row) if row["state"] == "done" else Decimal(row["max_cost"])
    have = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pm_us_intents'").fetchone()
    if have:
        for row in connection.execute("SELECT charge FROM pm_us_intents"):
            total += Decimal(row["charge"])
    if not total.is_finite() or total < 0:
        raise ValueError("invalid shared exposure accounting")
    return total


def problem(connection, cost):
    cost = Decimal(cost)
    if not cost.is_finite() or cost <= 0 or cost > LEG_CAP:
        return "$25 production per-leg cap exceeded (fees included)"
    have = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pm_us_intents'").fetchone()
    if have and connection.execute("SELECT 1 FROM pm_us_intents WHERE state NOT IN ('done', 'missed') LIMIT 1").fetchone():
        return "Polymarket US order unresolved; new exposure blocked until reconciled"
    if exposure(connection) + cost > TOTAL_CAP:
        return "$50 shared production exposure cap exceeded (fees included)"
    return None

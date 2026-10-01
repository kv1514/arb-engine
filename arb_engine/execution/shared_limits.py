"""Hard production cash ceilings, shared by Kalshi and Polymarket US.

All callers use the same production SQLite file and BEGIN IMMEDIATE transaction.
Filled purchases keep counting: this conservative v1 does not infer that old
inventory settled, or that a hedge/external sale returned cash. Never reset at midnight.
"""
from decimal import Decimal, InvalidOperation

LEG_CAP = Decimal("25")
TOTAL_CAP = Decimal("50")


def _cash(raw):
    try:
        amount = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('invalid shared cash accounting') from None
    if not amount.is_finite() or amount < 0:
        raise ValueError('invalid shared cash accounting')
    return amount


def exposure(connection):
    # Import only at call time: ledger.reserve uses this module too.
    from .ledger import OrderLedger
    total = Decimal("0")
    for row in connection.execute("SELECT * FROM intents WHERE state != 'rejected'"):
        bound = _cash(row['max_cost'])
        amount = bound
        if row['state'] == 'done':
            # Older ledgers may mark done without explicit paid money. Missing
            # fees/costs are unknown, not zero: retain the original hold. Validate
            # each component independently so a negative one cannot offset cash.
            needed = ('fees', 'fill_count') if row['action'] == 'sell' else ('fees', 'fill_cost')
            for field in needed:
                if row[field] is not None:
                    _cash(row[field])
            if all(row[field] is not None for field in needed):
                amount = _cash(OrderLedger.exposure_of(row))
            if row['fill_state'] == 'contradicted':
                amount = max(amount, bound)
        total += amount
    have = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pm_us_intents'").fetchone()
    if have:
        for row in connection.execute("SELECT charge FROM pm_us_intents"):
            total += _cash(row["charge"])
    pairs = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pair_reservations'").fetchone()
    if pairs:
        recovery = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pair_recovery_orders'").fetchone()
        for row in connection.execute("SELECT pair_id,us_cash,kalshi_cash FROM pair_reservations"):
            us, kal = _cash(row['us_cash']), _cash(row['kalshi_cash'])
            if recovery:
                us_paid = kal_paid = Decimal(0)
                for order in connection.execute("SELECT role,cash_seen,fee_seen FROM pair_recovery_orders WHERE pair_id=?", (row['pair_id'],)):
                    cash, fee = _cash(order['cash_seen']), _cash(order['fee_seen'])
                    if order['role'] == 'hedge':
                        kal_paid += cash+fee
                    elif order['role'] in ('entry', 'unwind'):
                        # Sale proceeds are not new capacity. Count ALL exit
                        # commissions and preserve actual overrun lower bounds,
                        # including incomplete/contradicted order evidence.
                        us_paid += fee+(cash if order['role'] == 'entry' else 0)
                    else:
                        raise ValueError('invalid pair recovery accounting role')
                us, kal = max(us, us_paid), max(kal, kal_paid)
            total += us+kal
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
    pairs = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pair_reservations'").fetchone()
    if pairs and connection.execute("SELECT 1 FROM pair_reservations WHERE state NOT IN ('held','missed') LIMIT 1").fetchone():
        return 'production pair staged or unresolved; new exposure blocked'
    try:
        current = exposure(connection)
    except ValueError:
        return 'invalid shared cash accounting; new exposure blocked'
    if current + cost > TOTAL_CAP:
        return "$50 shared production exposure cap exceeded (fees included)"
    return None

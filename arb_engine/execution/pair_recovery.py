"""Durable pair recovery/outbox accounting. This module has NO order transport.

Claims are permanent before a future sender could use them. A restarted caller
may reconcile a known order ID, never obtain a second claim for the same order.
Parent cash remains held at its full bound, including two unwind commissions;
sale proceeds never restore room. These records are not exchange inventory.
Production dispatch integration and market-specific settlement proof remain gates.
"""
from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal, ROUND_FLOOR

from .kalshi import OrderPlan
from .ledger import LedgerError, _scope_problem, book_side, fills_evidence, order_evidence
from .pair_reservations import PairReservationLedger, _json
from .pairpaper import HAIRCUT, MAX_ROLLS, _bound
from .polymarket_us_ioc import MissingEvidence, USOrderLedger, decimal, timestamp
from .shared_limits import TOTAL_CAP, exposure
from .standing_approval import accounts, digest
from .us_pair_orders import InventoryEvidence, USPairOrder, order_evidence as us_evidence

D_MIN, D_MAX = Decimal('.01'), Decimal('.99')


class PairRecovery:
    """Non-sending coordinator using the SAME production transaction/cash holds.

    Entry claims need the standing-policy dispatch guard. Hedge/unwind claims
    only reduce independently verified existing exposure and never exceed the
    frozen ticket. Network I/O must occur outside all policy/ledger transactions.
    Returned commands explicitly do not authorize a production send yet.
    """
    def __init__(self, pairs):
        if not isinstance(pairs, PairReservationLedger):
            raise LedgerError('shared production pair reservations required')
        self.pairs, self.store = pairs, pairs.store
        with self.store._tx() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS pair_recovery_orders (
                id TEXT PRIMARY KEY, pair_id TEXT NOT NULL, role TEXT NOT NULL,
                attempt INTEGER NOT NULL, plan TEXT NOT NULL, payload TEXT NOT NULL,
                created REAL NOT NULL, deadline REAL NOT NULL, state TEXT NOT NULL,
                order_id TEXT, seen TEXT NOT NULL DEFAULT '0',
                cash_seen TEXT NOT NULL DEFAULT '0', fee_seen TEXT NOT NULL DEFAULT '0',
                cash TEXT, fees TEXT, verified_at REAL, reason TEXT NOT NULL DEFAULT '',
                UNIQUE(pair_id,role,attempt), UNIQUE(role,order_id),
                FOREIGN KEY(pair_id) REFERENCES pair_reservations(pair_id))''')
            c.execute('''CREATE TABLE IF NOT EXISTS pair_recovery_liquidity (
                resource TEXT PRIMARY KEY, used TEXT NOT NULL, capacity TEXT NOT NULL, price TEXT NOT NULL)''')

    def _parent(self, c, pair_id, binding):
        binding = self.pairs._binding(binding)
        row = c.execute('SELECT * FROM pair_reservations WHERE pair_id=?', (pair_id,)).fetchone()
        if row is None or json.loads(row['binding']) != accounts(binding):
            raise LedgerError('unknown pair or account binding changed')
        owner = c.execute("SELECT value FROM meta WHERE key='account_fp'").fetchone()
        key = c.execute('SELECT account_fp FROM keys WHERE key_fp=?', (binding['kalshi_key'],)).fetchone()
        us = c.execute("SELECT value FROM pm_us_meta WHERE key='key_fp'").fetchone()
        if (owner is None or key is None or us is None or owner['value'] != binding['kalshi_account'] or
                key['account_fp'] != owner['value'] or us['value'] != binding['polymarket_us_key']):
            raise LedgerError('persisted pair account binding changed')
        plan = json.loads(row['plan'])
        if digest(plan) != row['plan_digest']:
            raise LedgerError('frozen pair plan changed')
        now = timestamp(self.store.clock())
        events = c.execute('SELECT max(ts) FROM pair_reservation_events WHERE pair_id=?', (pair_id,)).fetchone()[0]
        if now < timestamp(row['created_ts']) or (events is not None and now < timestamp(events)):
            raise LedgerError('pair recovery clock regressed')
        if exposure(c) > TOTAL_CAP:
            raise LedgerError('shared pair cash ceiling exceeded')
        return row, plan, now

    @staticmethod
    def _orders(c, pair_id):
        return list(c.execute("SELECT * FROM pair_recovery_orders WHERE pair_id=? ORDER BY CASE role WHEN 'entry' THEN 0 WHEN 'hedge' THEN 1 ELSE 2 END,attempt,id", (pair_id,)))

    @staticmethod
    def _verified(rows, role):
        return [r for r in rows if r['role'] == role and r['state'] == 'verified']

    def _event(self, c, pair_id, now, kind):
        c.execute('INSERT INTO pair_reservation_events(pair_id,ts,kind) VALUES (?,?,?)', (pair_id, now, kind))

    def _insert(self, c, parent, role, attempt, plan, payload, now, deadline):
        final = timestamp(self.store.clock())
        if final < now or final >= deadline:
            raise LedgerError('order decision expired before durable claim')
        now = final
        iid = uuid.uuid4().hex
        c.execute('''INSERT INTO pair_recovery_orders
            (id,pair_id,role,attempt,plan,payload,created,deadline,state)
            VALUES (?,?,?,?,?,?,?,?,?)''',
                  (iid, parent['pair_id'], role, attempt, _json(plan), _json(payload), now, deadline, 'claimed'))
        if role in ('entry', 'hedge'):
            changed = c.execute("UPDATE pair_reserved_children SET state='claimed' WHERE pair_id=? AND role=? AND state='reserved'",
                                (parent['pair_id'], role)).rowcount
            if changed != 1:
                raise LedgerError('pair child already claimed or not reserved')
        c.execute("UPDATE pair_reservations SET state='unresolved',reason='durable claim needs exchange reconciliation' WHERE pair_id=?",
                  (parent['pair_id'],))
        self._event(c, parent['pair_id'], now, role+'-claim')
        return self._command(c.execute('SELECT * FROM pair_recovery_orders WHERE id=?', (iid,)).fetchone())

    @staticmethod
    def _command(row):
        return {**dict(row), 'plan': json.loads(row['plan']), 'payload': json.loads(row['payload']),
                'send_authorized': False, 'note': 'outbox accounting only; production transport integration remains blocked'}

    def claim_entry(self, pair_id, approval, binding):
        parent = self.pairs.get(pair_id)
        with approval.dispatch_guard(parent['permit_id'], binding, parent['plan_digest']) as proof:
            with self.store._tx() as c:
                parent, plan, now = self._parent(c, pair_id, binding)
                if (parent['state'] != 'staged' or self._orders(c, pair_id) or
                        proof['plan_digest'] != parent['plan_digest']):
                    raise LedgerError('entry already claimed; reconcile, never retry')
                leg = plan['legs'][0]
                terms = {'market_slug': leg['market'].split('#', 1)[0], 'side': leg['side'],
                         'count': int(decimal(plan['count'])), 'limit_price': leg['limit'], 'action': 'buy'}
                order = USPairOrder(**terms)
                if order.worst_cost(now) > decimal(parent['us_cash']):
                    raise LedgerError('entry fee bound no longer covered')
                result = self._insert(c, parent, 'entry', 0, terms, order.payload(), now, timestamp(proof['deadline']))
        return result

    def claim_hedge(self, pair_id, binding):
        with self.store._tx() as c:
            parent, plan, now = self._parent(c, pair_id, binding)
            rows = self._orders(c, pair_id)
            entry = self._verified(rows, 'entry')
            if len(entry) != 1 or any(r['role'] != 'entry' for r in rows):
                raise LedgerError('hedge requires one final verified entry and no previous hedge/unwind')
            qty = decimal(entry[0]['seen'])
            if qty <= 0 or qty != qty.to_integral_value():
                raise LedgerError('hedge requires positive whole verified fills')
            leg = plan['legs'][1]
            if qty < decimal(leg['minimum']) or qty % decimal(leg['increment']):
                raise LedgerError('verified partial entry is below hedge quantity grid')
            if qty*decimal(leg['limit'])+_bound(leg, qty) > decimal(parent['kalshi_cash']):
                raise LedgerError('hedge cash not covered')
            order = OrderPlan('kalshi', leg['market'], 'buy', leg['side'], int(qty), decimal(leg['limit']),
                              time_in_force='immediate_or_cancel', client_order_id=uuid.uuid4().hex)
            terms = {'ticker': order.ticker, 'side': order.side, 'count': int(qty), 'limit_price': leg['limit'],
                     'client_order_id': order.client_order_id, 'action': 'buy'}
            return self._insert(c, parent, 'hedge', 0, terms, order.payload(), now, timestamp(parent['deadline']))

    def accepted(self, command_id, order_id, binding):
        if not isinstance(order_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', order_id):
            raise LedgerError('missing exchange ID; claim stays held for investigation')
        with self.store._tx() as c:
            row = c.execute('SELECT * FROM pair_recovery_orders WHERE id=?', (command_id,)).fetchone()
            if row is None:
                raise LedgerError('unknown recovery command')
            _, _, now = self._parent(c, row['pair_id'], binding)
            if row['order_id'] == order_id:
                return  # repeated receipt cannot erase final/contradicted evidence
            if row['state'] != 'claimed' or row['order_id'] is not None:
                raise LedgerError('exchange ID cannot change after acceptance')
            same_venue = 'hedge' if row['role'] == 'hedge' else None
            others = c.execute('SELECT role FROM pair_recovery_orders WHERE order_id=? AND id!=?', (order_id, command_id)).fetchall()
            if any((r['role'] == 'hedge') == (same_venue == 'hedge') for r in others):
                raise LedgerError('exchange order ID already belongs to another command')
            c.execute("UPDATE pair_recovery_orders SET order_id=?,state='open' WHERE id=? AND state='claimed'",
                      (order_id, command_id))
            self._event(c, row['pair_id'], now, row['role']+'-accepted')

    def observe(self, command_id, raw, binding, *, final_read=True, fills=None, truncated=None, error=None):
        """Trusted account-scoped GET evidence only. Missing never means zero.

        Kalshi additionally requires a complete scoped fills listing agreeing
        with the terminal order and its explicit cost/fees, including zero fills.
        Contradictions are sticky; a later convenient row cannot repair them.
        """
        with self.store._tx() as c:
            row = c.execute('SELECT * FROM pair_recovery_orders WHERE id=?', (command_id,)).fetchone()
            if row is None or row['order_id'] is None:
                raise LedgerError('known exchange ID required; no absence-based release')
            parent, plan, now = self._parent(c, row['pair_id'], binding)
            terms = json.loads(row['plan'])
            # A scoped cumulative count remains a lower bound even when the
            # same response omits leaves/fees. A later zero cannot erase it.
            if isinstance(raw, dict):
                if row['role'] == 'hedge':
                    scoped = (raw.get('order_id') == row['order_id'] and raw.get('ticker') == terms['ticker'] and
                              raw.get('client_order_id') == terms['client_order_id'] and
                              raw.get('book_side') == book_side('buy', terms['side']) and raw.get('action') == 'buy')
                    observed_qty = raw.get('fill_count_fp', raw.get('fill_count'))
                else:
                    native = USPairOrder(**terms).payload()
                    scoped = raw.get('id') == row['order_id'] and all(raw.get(k) == native[k] for k in ('marketSlug', 'intent', 'type', 'tif'))
                    observed_qty = raw.get('cumQuantity')
                if scoped and observed_qty is not None:
                    try:
                        lower = decimal(observed_qty)
                        if lower < decimal(row['seen']) or lower > decimal(terms['count']) or lower < 0:
                            raise ValueError('quantity lower bound conflicts')
                    except ValueError:
                        c.execute("UPDATE pair_recovery_orders SET state='contradicted',reason='cumulative quantity conflict' WHERE id=?", (command_id,))
                        c.execute("UPDATE pair_reservations SET state='unresolved',reason='quantity conflict' WHERE pair_id=?", (row['pair_id'],))
                        self._event(c, row['pair_id'], now, row['role']+'-contradicted')
                        return False
                    c.execute('UPDATE pair_recovery_orders SET seen=? WHERE id=?', (str(lower), command_id))
                    try:
                        money = {}
                        if row['role'] == 'hedge':
                            for field, native_field, optional in (
                                    ('cash_seen', 'taker_fill_cost_dollars', 'maker_fill_cost_dollars'),
                                    ('fee_seen', 'taker_fees_dollars', 'maker_fees_dollars')):
                                if raw.get(native_field) is not None:
                                    money[field] = decimal(raw[native_field])+decimal(raw.get(optional, '0'))
                        else:
                            if lower and raw.get('avgPx') is not None:
                                px = USOrderLedger._money(raw['avgPx'])
                                if not D_MIN <= px <= D_MAX:
                                    raise ValueError('invalid average fill price')
                                side_price = px if terms['side'] == 'yes' else 1-px
                                limit = decimal(terms['limit_price'])
                                if (terms['action'] == 'buy' and side_price > limit) or (terms['action'] == 'sell' and side_price < limit):
                                    raise ValueError('fill price outside limit')
                                money['cash_seen'] = lower*side_price
                            if raw.get('commissionNotionalTotalCollected') is not None:
                                money['fee_seen'] = USOrderLedger._money(raw['commissionNotionalTotalCollected'])
                        for field, amount in money.items():
                            if amount < 0 or amount < decimal(row[field]) or (not lower and amount != 0):
                                raise ValueError('cumulative monetary lower bound conflicts')
                            c.execute('UPDATE pair_recovery_orders SET '+field+'=? WHERE id=?', (str(amount), command_id))
                    except (ValueError, ArithmeticError):
                        c.execute("UPDATE pair_recovery_orders SET state='contradicted',reason='cumulative money conflict' WHERE id=?", (command_id,))
                        c.execute("UPDATE pair_reservations SET state='unresolved',reason='money conflict' WHERE pair_id=?", (row['pair_id'],))
                        self._event(c, row['pair_id'], now, row['role']+'-contradicted')
                        return False
            try:
                if row['role'] == 'hedge':
                    qty, cash, fees, verified = self._kalshi(terms, row['order_id'], raw, final_read, fills, truncated, error)
                else:
                    ev = us_evidence(USPairOrder(**terms), raw, row['order_id'], final_read=final_read)
                    qty, cash, fees, verified = ev.quantity, ev.cash, ev.fees, ev.verified
                if qty < decimal(row['seen']):
                    raise ValueError('cumulative quantity regressed')
                for value, field in ((cash, 'cash_seen'), (fees, 'fee_seen')):
                    if value is not None and value < decimal(row[field]):
                        raise ValueError('cumulative money regressed')
                if row['state'] == 'verified' and (not verified or qty != decimal(row['seen']) or
                        cash != decimal(row['cash']) or fees != decimal(row['fees'])):
                    raise ValueError('final evidence changed')
                if row['state'] == 'contradicted':
                    return False
                leg_cash = decimal(parent['kalshi_cash'] if row['role'] == 'hedge' else parent['us_cash'])
                if cash is not None and fees is not None and row['role'] != 'unwind' and cash+fees > leg_cash:
                    raise ValueError('actual fill charges exceed reserved cash bound')
                c.execute('UPDATE pair_recovery_orders SET seen=?,cash_seen=?,fee_seen=?,cash=?,fees=?,state=?,verified_at=? WHERE id=?',
                          (str(qty), str(cash) if cash is not None else row['cash_seen'],
                           str(fees) if fees is not None else row['fee_seen'],
                           None if cash is None else str(cash), None if fees is None else str(fees),
                           'verified' if verified else 'open', now if verified else None, command_id))
                self._refresh(c, parent, plan)
                self._event(c, row['pair_id'], now, row['role']+'-evidence')
                return verified
            except MissingEvidence:
                return False
            except (ValueError, TypeError, ArithmeticError):
                c.execute("UPDATE pair_recovery_orders SET state='contradicted',reason='identity/count/money evidence conflict' WHERE id=?", (command_id,))
                c.execute("UPDATE pair_reservations SET state='unresolved',reason='sticky order evidence contradiction' WHERE pair_id=?", (row['pair_id'],))
                self._event(c, row['pair_id'], now, row['role']+'-contradicted')
                return False

    @staticmethod
    def _kalshi(terms, oid, raw, final_read, fills, truncated, error):
        if not isinstance(raw, dict):
            raise MissingEvidence('missing Kalshi order')
        scope = _scope_problem(raw, terms['ticker'], book_side('buy', terms['side']))
        if (raw.get('order_id') != oid or raw.get('client_order_id') != terms['client_order_id'] or scope or
                raw.get('ticker') != terms['ticker'] or raw.get('book_side') != book_side('buy', terms['side']) or
                raw.get('action') != 'buy'):
            raise ValueError('Kalshi order scope/identity conflict')
        ev = order_evidence(raw, terms['count'], True)
        if ev.filled is None:
            raise MissingEvidence('missing Kalshi fills')
        if not 0 <= ev.filled <= decimal(terms['count']):
            raise ValueError('Kalshi quantity outside intent')
        if ev.cost is not None and ev.cost > ev.filled*decimal(terms['limit_price']):
            raise ValueError('Kalshi fill exceeds limit')
        if not final_read or fills is None:
            return ev.filled, ev.cost, ev.fees, False
        if not isinstance(fills, list) or any(not isinstance(f, dict) for f in fills):
            raise MissingEvidence('malformed Kalshi fills listing')
        for fill in fills:
            if (fill.get('order_id') == oid and
                    (fill.get('ticker', fill.get('market_ticker')) != terms['ticker'] or fill.get('book_side') != book_side('buy', terms['side']))):
                raise ValueError('Kalshi fill missing or conflicting market/book scope')
            if fill.get('order_id') == oid and fill.get(terms['side']+'_price_dollars') is not None:
                if not 0 < decimal(fill[terms['side']+'_price_dollars']) <= decimal(terms['limit_price']):
                    raise ValueError('individual Kalshi fill violated decision limit')
        fe = fills_evidence(fills, truncated, error, oid, terms['ticker'], book_side('buy', terms['side']), terms['side']+'_price_dollars')
        if fe.contradiction(ev.filled, decimal(terms['count'])):
            raise ValueError('Kalshi fills contradict order')
        if fe.conclusive and (fe.count != ev.filled or
                (fe.cost is not None and ev.cost is not None and fe.cost != ev.cost) or
                (fe.fees is not None and ev.fees is not None and fe.fees != ev.fees)):
            raise ValueError('Kalshi final row and fills differ')
        verified = (ev.final and fe.conclusive and fe.count == ev.filled and
                    ev.cost is not None and ev.fees is not None and fe.cost == ev.cost and fe.fees == ev.fees)
        return max(ev.filled, fe.count), ev.cost, ev.fees, verified

    def _refresh(self, c, parent, plan):
        rows = self._orders(c, parent['pair_id'])
        us_paid = sum((decimal(r['fee_seen'])+(decimal(r['cash_seen']) if r['role'] == 'entry' else 0)
                       for r in rows if r['role'] != 'hedge'), Decimal(0))
        kal_paid = sum((decimal(r['cash_seen'])+decimal(r['fee_seen']) for r in rows if r['role'] == 'hedge'), Decimal(0))
        if us_paid > decimal(parent['us_cash']) or kal_paid > decimal(parent['kalshi_cash']):
            raise ValueError('observed purchase/exit charges exceed frozen parent cash')
        if any(r['state'] != 'verified' for r in rows):
            return
        entry, hedge = self._verified(rows, 'entry'), self._verified(rows, 'hedge')
        if not entry:
            return
        n = decimal(entry[0]['seen'])
        sold = sum((decimal(r['seen']) for r in self._verified(rows, 'unwind')), Decimal(0))
        h = decimal(hedge[0]['seen']) if hedge else Decimal(0)
        if h+sold > n:
            raise ValueError('hedges/sales exceed pair-owned entry')
        state = 'missed' if n == 0 else 'held' if h+sold == n else 'unresolved' if len(self._verified(rows, 'unwind')) >= MAX_ROLLS else 'unwind' if hedge or sold else 'entry-verified'
        # Keep both full parent holds even after sales. No automatic cash credit.
        c.execute("UPDATE pair_reservations SET state=?,reason='verified recovery state; full cash bound retained' WHERE pair_id=?",
                  (state, parent['pair_id']))

    def claim_unwind(self, pair_id, binding, inventory, quote):
        """Bounded sale of THIS pair's verified excess; no shorts or cash credit.

        Quote is a refreshed receipt-timed executable bid with displayed depth.
        Repeated receipt/resource cannot replenish that liquidity. Caller supplies
        account GET inventory evidence; it is necessary, never ownership proof.
        """
        with self.store._tx() as c:
            parent, plan, now = self._parent(c, pair_id, binding)
            rows = self._orders(c, pair_id)
            if not rows or any(r['state'] != 'verified' for r in rows):
                raise LedgerError('all attempted orders must be final verified before selling')
            entry = self._verified(rows, 'entry')
            hedge = self._verified(rows, 'hedge')
            exits = self._verified(rows, 'unwind')
            if len(entry) != 1 or len(exits) >= MAX_ROLLS:
                raise LedgerError('entry missing or bounded unwind attempts exhausted')
            # If the hedge was never claimed it must now be permanently abandoned.
            if not hedge:
                if now < timestamp(parent['deadline']):
                    raise LedgerError('unclaimed hedge still eligible; wait for its deadline')
                c.execute("UPDATE pair_reserved_children SET state='abandoned' WHERE pair_id=? AND role='hedge' AND state='reserved'", (pair_id,))
            leg = plan['legs'][0]
            slug = leg['market'].split('#', 1)[0]
            excess = decimal(entry[0]['seen'])-sum((decimal(r['seen']) for r in hedge+exits), Decimal(0))
            if not isinstance(inventory, InventoryEvidence) or (inventory.key_fp, inventory.market_slug, inventory.side) != (binding['polymarket_us_key'], slug, leg['side']):
                raise LedgerError('wrong account/market inventory')
            if (not timestamp(inventory.requested_at) <= timestamp(inventory.observed_at) <= now < timestamp(inventory.deadline) or
                    timestamp(inventory.deadline) > timestamp(inventory.requested_at)+6 or
                    inventory.requested_at < max(timestamp(r['verified_at']) for r in rows) or
                    decimal(inventory.available) < 0 or decimal(inventory.available) > abs(decimal(inventory.net)) or
                    (leg['side'] == 'yes' and inventory.net < 0) or (leg['side'] == 'no' and inventory.net > 0)):
                raise LedgerError('stale, opposite or inconsistent inventory evidence')
            if (quote.get('market') != leg['market'] or quote.get('book') != 'polymarket_us' or quote.get('side') != leg['side'] or
                    quote.get('refreshed') is not True or quote.get('approx_time') or
                    not timestamp(quote['req_ts']) <= timestamp(quote['obs_ts']) <= now < timestamp(quote['obs_ts'])+6 or
                    timestamp(quote['obs_ts']) < max(timestamp(r['verified_at']) for r in rows)):
                raise LedgerError('unwind needs exact fresh scoped bid receipt')
            bid, size = decimal(quote['bid']), decimal(quote['size'])
            if not Decimal('.01') <= bid <= Decimal('.99') or size <= 0 or (bid if leg['side'] == 'yes' else 1-bid) % decimal(leg['tick']):
                raise LedgerError('unwind bid/depth/grid invalid')
            # NO sales consume long offers; YES sales consume long bids.
            resource = _json([slug, 'bid' if leg['side'] == 'yes' else 'offer', timestamp(quote['obs_ts'])])
            previous = c.execute('SELECT * FROM pair_recovery_liquidity WHERE resource=?', (resource,)).fetchone()
            if previous and (decimal(previous['capacity']) != size*HAIRCUT or decimal(previous['price']) != bid):
                raise LedgerError('conflicting duplicate book receipt cannot create liquidity')
            used = decimal(previous['used']) if previous else Decimal(0)
            n = min(excess, decimal(inventory.available), size*HAIRCUT-used).to_integral_value(rounding=ROUND_FLOOR)
            if n <= 0 or n < decimal(leg['minimum']) or n % decimal(leg['increment']):
                raise LedgerError('no whole pair-owned executable excess remains')
            order = USPairOrder(slug, leg['side'], int(n), bid, action='sell')
            prior_loss = sum((max(Decimal(0), decimal(r['seen'])*decimal(leg['limit'])-decimal(r['cash']))+decimal(r['fees']) for r in exits), Decimal(0))
            # Charge ALL entry fees to recovery, not an optimistic allocation.
            loss = max(Decimal(0), n*(decimal(leg['limit'])-bid))+order.fee_bound(now)+decimal(entry[0]['fees'])+prior_loss
            if loss > decimal(plan['unwind_loss_cap']):
                raise LedgerError('fee-inclusive failed-leg loss bound exceeded; inventory unresolved')
            total_exit_fees = sum((decimal(r['fees']) for r in exits), Decimal(0))+order.fee_bound(now)
            if decimal(entry[0]['cash'])+decimal(entry[0]['fees'])+total_exit_fees > decimal(parent['us_cash']):
                raise LedgerError('unwind cash fees not covered by original parent')
            terms = {'market_slug': slug, 'side': leg['side'], 'count': int(n), 'limit_price': str(bid), 'action': 'sell'}
            result = self._insert(c, parent, 'unwind', len(exits), terms, order.payload(), now, timestamp(quote['obs_ts'])+6)
            c.execute('INSERT INTO pair_recovery_liquidity VALUES (?,?,?,?) ON CONFLICT(resource) DO UPDATE SET used=excluded.used',
                      (resource, str(used+n), str(size*HAIRCUT), str(bid)))
            return result

    def status(self, pair_id, binding):
        with self.store._tx() as c:
            parent, _, _ = self._parent(c, pair_id, binding)
            commands = [self._command(r) for r in self._orders(c, pair_id)]
            known = all(r['state'] == 'verified' for r in commands) and any(r['role'] == 'entry' for r in commands)
            excess = sum((decimal(r['seen'])*(1 if r['role'] == 'entry' else -1) for r in commands), Decimal(0)) if known else None
            return {'state': parent['state'], 'commands': commands, 'shared_exposure': str(exposure(c)),
                    'verified_unhedged_quantity': None if excess is None else str(excess),
                    'live_enabled': False, 'orders_submitted_by_coordinator': 0,
                    'reconciliation_required': [r['id'] for r in commands if r['state'] != 'verified']}

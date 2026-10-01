"""Atomic production pair/contingency reservations, NOT a paired sender.

Both child terms and cash holds commit in the existing production ledger before
either transport could be invoked. Existing executors deliberately cannot use
these staged children yet. No unresolved-order exemption is introduced here.
Reservations are conservative lifetime cash holds; no inferred settlement credit.
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal

from .ledger import LedgerError, env_host_problem, worst_cost
from .pairpaper import MAX_ROLLS, POLICY, _bound
from .polymarket_us_ioc import USOrderLedger, decimal, timestamp
from .shared_limits import LEG_CAP, TOTAL_CAP, exposure
from .standing_approval import accounts, digest
from .us_pair_orders import USPairOrder


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False, default=str)


class PairReservationLedger:
    """Wrap a bound USOrderLedger; use its production connection/transaction lock.

    Kalshi identity must already be authenticated/bound by OrderLedger.for_client
    or bind(client_identity(...)); no supplied raw account identifier is trusted.
    Public admission loads the exact stored plan INSIDE the standing policy guard,
    then locks production. Caller cannot replace the plan with an arbitrary ticket.
    """
    def __init__(self, ledger):
        if not isinstance(ledger, USOrderLedger):
            raise LedgerError('pair reservations require the shared US/Kalshi ledger')
        self.ledger, self.store = ledger, ledger.store
        if self.store.env != 'prod' or env_host_problem('prod', self.store.host):
            raise LedgerError('pair reservations require the production ledger host')
        with self.store._tx() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS pair_reservations (
                pair_id TEXT PRIMARY KEY, permit_id TEXT UNIQUE NOT NULL, event TEXT UNIQUE NOT NULL,
                binding TEXT NOT NULL, plan_digest TEXT NOT NULL, plan TEXT NOT NULL,
                created_ts REAL NOT NULL, deadline REAL NOT NULL, state TEXT NOT NULL,
                us_cash TEXT NOT NULL, kalshi_cash TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '')''')
            c.execute('''CREATE TABLE IF NOT EXISTS pair_reserved_children (
                child_id TEXT PRIMARY KEY, pair_id TEXT NOT NULL, venue TEXT NOT NULL,
                role TEXT NOT NULL, terms TEXT NOT NULL, state TEXT NOT NULL,
                UNIQUE(pair_id,venue,role), FOREIGN KEY(pair_id) REFERENCES pair_reservations(pair_id))''')
            c.execute('''CREATE TABLE IF NOT EXISTS pair_reservation_events (
                seq INTEGER PRIMARY KEY, pair_id TEXT NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL)''')

    def _binding(self, binding):
        binding = accounts(binding)
        if (self.store.identity.key_fp != binding['kalshi_key'] or
                self.store.identity.account_fp != binding['kalshi_account']):
            raise LedgerError('shared ledger is not bound to the approved Kalshi account/key')
        return binding

    @staticmethod
    def _costs(plan, now):
        if (not isinstance(plan, dict) or plan.get('policy') != POLICY or
                not isinstance(plan.get('legs'), list) or len(plan['legs']) != 2):
            raise LedgerError('invalid frozen pair policy/legs')
        us, kal = plan['legs']
        n = decimal(plan.get('count'))
        if n <= 0 or n != n.to_integral_value() or n > 10000:
            raise LedgerError('invalid pair count')
        if (us.get('venue') != 'polymarket_us' or kal.get('venue') != 'kalshi' or
                us.get('book') != 'polymarket_us' or kal.get('book') != 'kalshi' or
                us.get('event') != plan.get('event') or kal.get('event') != plan.get('event') or
                us.get('outcome') == kal.get('outcome') or decimal(plan.get('tie_payout')) < 1):
            raise LedgerError('pair books/outcomes/payoffs invalid; Robinhood automation unsupported')
        if timestamp(plan['decision']) > now or timestamp(plan['kickoff']) <= now:
            raise LedgerError('pair decision future or kickoff reached')
        for leg in (us, kal):
            if not timestamp(leg['requested_at']) <= timestamp(leg['observed_at']) <= timestamp(plan['decision']):
                raise LedgerError('pair receipt timing invalid')
            if not Decimal('.01') <= decimal(leg['limit']) <= Decimal('.99') or leg['side'] not in ('yes', 'no'):
                raise LedgerError('pair limit/side invalid')
        # Production Kalshi's split-fill cash bound intentionally floors the
        # multiplier at 1, even if a fee-free/discount series is observed. Preserve
        # that existing invariant; paper permission alone may reserve less.
        us_order = USPairOrder(us['market'].split('#', 1)[0], us['side'], int(n), decimal(us['limit']))
        exit_order = USPairOrder(us_order.market_slug, us['side'], int(n), Decimal('.01'), action='sell')
        us_cash = max(n*decimal(us['limit']) + _bound(us, n) + MAX_ROLLS*_bound(us, n, exit=True),
                      us_order.worst_cost(now) + MAX_ROLLS*exit_order.fee_bound(now))
        kal_cash = max(n*decimal(kal['limit']) + _bound(kal, n),
                       worst_cost(kal['limit'], n, kal['fee_params'].get('fee_multiplier')))
        if min(us_cash, kal_cash) <= 0 or max(us_cash, kal_cash) > LEG_CAP:
            raise LedgerError('$25 pair leg cap exceeded including contingency/split-fill fees')
        if us_cash+kal_cash > TOTAL_CAP:
            raise LedgerError('$50 pair cap exceeded')
        return us_cash, kal_cash

    def admit(self, approval, consumed, binding):
        """Stage an exact consumed permit and both children; never claim/send I/O.

        This does NOT consume a dispatch claim. Production sender integration
        must reuse this parent's unique permit and still fence actual dispatch.
        A crash after production commit but before policy commit cannot create a
        second parent because permit_id/event are unique. No reset frees its cash.
        """
        binding = self._binding(binding)
        if not isinstance(consumed, dict) or consumed.get('status') != 'PERMISSION_CONSUMED':
            raise LedgerError('consumed standing permission required')
        with approval.reservation_guard(consumed['permit_id'], binding, consumed['plan_digest']) as proof:
            with self.store._tx() as c:
                now = timestamp(self.store.clock())  # after BOTH transaction waits
                if now >= timestamp(proof['deadline']):
                    raise LedgerError('pair receipt/approval deadline expired while reserving')
                plan = proof['plan']
                if digest(plan) != proof['plan_digest']:
                    raise LedgerError('pair plan digest changed')
                self.ledger._bind(c, binding['polymarket_us_key'])
                # The account's stored binding must agree as well as this object.
                owner = c.execute("SELECT value FROM meta WHERE key='account_fp'").fetchone()
                key = c.execute('SELECT account_fp FROM keys WHERE key_fp=?', (binding['kalshi_key'],)).fetchone()
                if owner is None or key is None or owner['value'] != binding['kalshi_account'] or key['account_fp'] != owner['value']:
                    raise LedgerError('production account binding missing or changed')
                if c.execute("SELECT 1 FROM intents WHERE state IN ('pending','ambiguous','accepted') OR fill_state='contradicted' LIMIT 1").fetchone():
                    raise LedgerError('Kalshi order unresolved; no pair admission')
                if c.execute("SELECT 1 FROM pm_us_intents WHERE state NOT IN ('done','missed') LIMIT 1").fetchone():
                    raise LedgerError('US order unresolved; no pair admission')
                if c.execute("SELECT 1 FROM pair_reservations WHERE state NOT IN ('held','missed') LIMIT 1").fetchone():
                    raise LedgerError('an earlier pair is staged or unresolved; no new pair')
                if c.execute('SELECT 1 FROM pair_reservations WHERE permit_id=? OR event=?', (proof['permit_id'], plan['event'])).fetchone():
                    raise LedgerError('duplicate pair permit/event')
                us_cash, kal_cash = self._costs(plan, now)
                # Approval and real-cash stores must agree on bounds. Do not use
                # the weaker permission reservation to bypass production math.
                permit = approval.conn.execute('SELECT us_cash,kalshi_cash FROM permits WHERE id=?', (proof['permit_id'],)).fetchone()
                if permit is None or decimal(permit['us_cash']) < us_cash or decimal(permit['kalshi_cash']) < kal_cash:
                    raise LedgerError('permission cash does not cover production fee bounds')
                if exposure(c)+us_cash+kal_cash > TOTAL_CAP:
                    raise LedgerError('$50 aggregate shared production cap exceeded')
                iid = uuid.uuid4().hex
                terms = []
                for leg in plan['legs']:
                    terms.append({**leg, 'count': int(decimal(plan['count']))})
                us = terms[0]
                # Validate even before staging: #no identifies a displayed row,
                # not an API market slug. A future sender must verify adapter ID.
                USPairOrder(us['market'].split('#', 1)[0], us['side'], us['count'], decimal(us['limit'])).validate()
                final = timestamp(self.store.clock())
                if final < now or final >= timestamp(proof['deadline']):
                    raise LedgerError('pair expired during durable admission')
                later_us, later_kal = self._costs(plan, final)
                if later_us > us_cash or later_kal > kal_cash:
                    raise LedgerError('fee bound changed during pair admission')
                c.execute('INSERT INTO pair_reservations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                          (iid, proof['permit_id'], plan['event'], _json(binding), proof['plan_digest'], _json(plan),
                           final, proof['deadline'], 'staged', str(us_cash), str(kal_cash), 'sender integration not enabled'))
                for term, role in zip(terms, ('entry', 'hedge')):
                    c.execute('INSERT INTO pair_reserved_children VALUES (?,?,?,?,?,?)',
                              (uuid.uuid4().hex, iid, term['venue'], role, _json(term), 'reserved'))
                c.execute('INSERT INTO pair_reservation_events(pair_id,ts,kind) VALUES (?,?,?)', (iid, final, 'both-legs-reserved'))
        return self.get(iid)

    def abandon_staged(self, pair_id):
        """Release only expired local staging that never became sendable.

        No claimed, submitted, unknown or reconciled child may use this path.
        Keep parent/child IDs and the one-game/permit fence; never delete rows.
        A future child transfer must change these states under this SAME lock
        before any network attempt, including crash-before-request ambiguity.
        """
        with self.store._tx() as c:
            now = timestamp(self.store.clock())
            row = c.execute('SELECT * FROM pair_reservations WHERE pair_id=?', (pair_id,)).fetchone()
            children = c.execute('SELECT * FROM pair_reserved_children WHERE pair_id=?', (pair_id,)).fetchall()
            if (row is None or row['state'] != 'staged' or now < timestamp(row['created_ts']) or
                    now < timestamp(row['deadline']) or len(children) != 2 or
                    {(r['venue'], r['role']) for r in children} != {('polymarket_us', 'entry'), ('kalshi', 'hedge')} or
                    any(r['state'] != 'reserved' for r in children)):
                raise LedgerError('only expired, wholly unclaimed local staging may be abandoned')
            c.execute("UPDATE pair_reservations SET state='missed',us_cash='0',kalshi_cash='0',reason='staged children never sendable' WHERE pair_id=? AND state='staged'", (pair_id,))
            c.execute("UPDATE pair_reserved_children SET state='abandoned' WHERE pair_id=? AND state='reserved'", (pair_id,))
            c.execute('INSERT INTO pair_reservation_events(pair_id,ts,kind) VALUES (?,?,?)', (pair_id, now, 'never-sent-staging-expired'))
        return self.get(pair_id)

    def get(self, pair_id):
        with self.store._lock:
            row = self.store.conn.execute('SELECT * FROM pair_reservations WHERE pair_id=?', (pair_id,)).fetchone()
            if row is None:
                raise LedgerError('unknown production pair reservation')
            result = dict(row)
            result['children'] = [dict(r) for r in self.store.conn.execute('SELECT * FROM pair_reserved_children WHERE pair_id=? ORDER BY role', (pair_id,))]
            return result

    def status(self):
        with self.store._lock:
            ids = [row['pair_id'] for row in self.store.conn.execute('SELECT pair_id FROM pair_reservations ORDER BY created_ts,pair_id')]
            cash = exposure(self.store.conn)
        return {'status': 'ACCOUNTING_ONLY', 'orders_submitted': 0, 'shared_exposure': str(cash),
                'pairs': [self.get(iid) for iid in ids],
                'note': 'staged children are not sendable; no same-pair executor exemption exists'}

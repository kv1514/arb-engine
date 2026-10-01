"""Standing, revocable permission for verified pairs, NOT an order transport.

The operator arms once. A trusted coordinator may approve/consume an exact fresh
pair without another human prompt. No LLM judges permission and no error becomes
"allow". Executors still need their live gates, reservations and fill evidence.
An approval is not a reservation in the production order ledger or proof of fills.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from ..compliance import executable_venues, load_rules
from ..matching.settlement_rules import load as load_settlements, verify as verify_settlements
from .pairpaper import MAX_ROLLS, _bound, plans
from .polymarket_us_ioc import decimal, timestamp

LEG_CAP, TOTAL_CAP = Decimal(25), Decimal(50)
PROFILE = {'version': 1, 'venues': ['kalshi', 'polymarket_us'], 'environment': 'prod',
           'sport': 'nfl', 'market_type': 'moneyline', 'pre_game_only': True,
           'settlement': 'verified-compatible', 'leg_cap_fees_included': '25',
           'total_cap_fees_included': '50', 'entry': 'ioc', 'expiry_required': True}
ROOT = Path(__file__).resolve().parents[1]
SPEC_FILES = ['execution/standing_approval.py', 'execution/pairpaper.py',
              'execution/us_pair_orders.py', 'execution/pair_reservations.py', 'execution/pair_recovery.py', 'execution/kalshi_once.py',
              'execution/polymarket_us_ioc.py', 'execution/shared_limits.py', 'execution/ledger.py', 'execution/kalshi.py',
              'venues/polymarket_us.py', 'venues/polymarket_us_trading.py', 'venues/kalshi.py',
              'venues/http.py', 'cli_plugins/us_arbs.py', 'cli_plugins/trade_approval.py',
              'quant/us_arbitrage.py', 'matching/settlement_rules.py',
              'matching/matcher.py', 'matching/normalize.py', 'matching/teams.py',
              'compliance.py', 'models/__init__.py', 'config.py',
              'fees/base.py', 'fees/kalshi.py', 'fees/polymarket.py', 'fees/registry.py',
              'data/settlement_rules.json', 'data/venue_rules.json', 'data/nfl_teams.json']


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False, default=str)


def digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def spec_hash():
    h = hashlib.sha256(_json(PROFILE).encode())
    for name in SPEC_FILES:
        h.update(name.encode()+b'\0'+(ROOT/name).read_bytes()+b'\0')
    # Pin the evidence text as well as its registry claim. Only local leaf files
    # under the known rules-fixture directory may supply that evidence.
    fixtures = ROOT.parent/'tests'/'fixtures'/'rules'
    registry = json.loads((ROOT/'data/settlement_rules.json').read_bytes())
    names = set()
    for row in registry['rules']:
        name = row.get('source', {}).get('fixture')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+\.txt', name):
            raise ValueError('invalid local settlement evidence path')
        names.add(name)
    for name in sorted(names):
        path = fixtures/name
        if path.is_symlink() or not path.is_file() or path.resolve().parent != fixtures.resolve():
            raise ValueError('settlement evidence must be a local regular file')
        h.update(name.encode()+b'\0'+path.read_bytes()+b'\0')
    return h.hexdigest()


# Cached registry loaders must never evaluate permission using old in-memory rules
# while a new disk hash is being approved. A code/rule update requires a restart.
PROCESS_SPEC_HASH = spec_hash()


def _current_spec():
    current = spec_hash()
    if current != PROCESS_SPEC_HASH:
        raise ValueError('approval code/rules changed in this process; restart required')
    return current


def _refresh_rules():
    before = _current_spec()
    load_rules.cache_clear()
    load_settlements.cache_clear()
    load_rules()
    load_settlements()
    if verify_settlements():
        raise ValueError('settlement evidence failed registry verification')
    if _current_spec() != before:
        raise ValueError('rules changed while loading')


def accounts(value):
    """Fingerprints supplied by a trusted authenticated caller; never raw identifiers."""
    if not isinstance(value, dict) or set(value) != {'kalshi_key', 'kalshi_account', 'polymarket_us_key'}:
        raise ValueError('complete account binding required')
    patterns = {'kalshi_key': r'key:[a-f0-9]{32}', 'kalshi_account': r'account:[a-f0-9]{32}',
                'polymarket_us_key': r'[a-f0-9]{64}'}
    if any(not isinstance(value[k], str) or not re.fullmatch(patterns[k], value[k]) for k in patterns):
        raise ValueError('invalid account binding')
    return dict(value)


def _safe_file(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('approval symlink refused')
    if path.exists():
        metadata = path.stat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or
                metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) & 0o077):
            raise ValueError('approval file must be private and owned by operator')
    for suffix in ('-journal', '-wal', '-shm'):
        aux = Path(str(path)+suffix)
        if aux.is_symlink():
            raise ValueError('approval journal symlink refused')
        if aux.exists():
            meta = aux.stat()
            if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid() or
                    meta.st_nlink != 1 or stat.S_IMODE(meta.st_mode) & 0o077):
                raise ValueError('approval journal must be private and operator-owned')
    return path


def read_status(path, *, now=None):
    """Read-only status: never creates directories, files or account clients."""
    now = timestamp(time.time() if now is None else now)
    path = _safe_file(path)
    if not path.exists():
        return {'status': 'UNARMED', 'approval_active': False, 'execution_enabled': False}
    conn = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return ApprovalStore._status(conn, now)
    finally:
        conn.close()


class ApprovalStore:
    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = _safe_file(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        parent = self.path.parent.stat()
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o022:
            raise ValueError('approval directory must be operator-owned and not publicly writable')
        # Create privately BEFORE SQLite opens it; never chmod a user's existing file.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(fd)
        _safe_file(self.path)
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        # DELETE mode keeps read-only status from creating WAL/SHM sidecars.
        self.conn.execute('PRAGMA journal_mode=DELETE')
        self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.execute('CREATE TABLE IF NOT EXISTS approval (id INTEGER PRIMARY KEY CHECK(id=1),active INTEGER NOT NULL,generation TEXT NOT NULL,last_ts REAL NOT NULL,body TEXT NOT NULL)')
        self.conn.execute('CREATE TABLE IF NOT EXISTS permits (id TEXT PRIMARY KEY,event TEXT UNIQUE NOT NULL,generation TEXT NOT NULL,deadline REAL NOT NULL,claimed INTEGER NOT NULL DEFAULT 0,us_cash TEXT NOT NULL,kalshi_cash TEXT NOT NULL,digest TEXT NOT NULL,body TEXT NOT NULL)')
        columns = {row[1] for row in self.conn.execute('PRAGMA table_info(permits)')}
        if 'dispatch_claimed' not in columns:
            self.conn.execute('ALTER TABLE permits ADD COLUMN dispatch_claimed INTEGER NOT NULL DEFAULT 0')

    def close(self):
        self.conn.close()

    @contextmanager
    def _tx(self):
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.conn.execute('COMMIT')
        except BaseException:
            self.conn.execute('ROLLBACK')
            raise

    @staticmethod
    def _status(conn, now):
        row = conn.execute('SELECT * FROM approval WHERE id=1').fetchone()
        if row is None:
            return {'status': 'UNARMED', 'approval_active': False, 'execution_enabled': False}
        body = json.loads(row['body'])
        reason = 'ARMED'
        if not row['active']:
            reason = 'REVOKED'
        elif not timestamp(row['last_ts']) <= now < timestamp(body['expires']):
            reason = 'EXPIRED_OR_CLOCK_REGRESSED'
        elif (body['profile'] != PROFILE or body['spec_hash'] != spec_hash() or
              body['spec_hash'] != PROCESS_SPEC_HASH):
            reason = 'SPEC_CHANGED'
        us, kal = ApprovalStore._held(conn)
        return {'status': reason, 'approval_active': reason == 'ARMED', 'execution_enabled': False,
                'profile': body['profile'], 'created': body['created'], 'expires': body['expires'],
                'permission_cash_held': {'polymarket_us': str(us), 'kalshi': str(kal), 'total': str(us+kal)},
                'account_binding': 'Kalshi account+key fingerprints; US sending-key fingerprint',
                'note': 'permission only; production pair executor and live gates are still required'}

    @staticmethod
    def _held(conn):
        us, kal = Decimal(0), Decimal(0)
        for row in conn.execute('SELECT us_cash,kalshi_cash FROM permits'):
            u, k = decimal(row['us_cash']), decimal(row['kalshi_cash'])
            if u < 0 or k < 0:
                raise ValueError('invalid permission accounting')
            us, kal = us+u, kal+k
        return us, kal

    def arm(self, binding, *, hours=6, settings=None):
        binding = accounts(binding)
        duration = decimal(hours)
        if not 0 < duration <= 24:
            raise ValueError('approval duration must be in (0,24] hours')
        selected = {'kalshi_rounding': (settings or {}).get('kalshi_rounding', 'cent'),
                    'executable_venues': (settings or {}).get('executable_venues'),
                    'polymarket_us_volume_rebate': 0}
        if selected['kalshi_rounding'] not in ('cent', 'centicent'):
            raise ValueError('unknown fee rounding')
        with self._tx():
            now = timestamp(self.clock())
            _refresh_rules()
            if not {'kalshi', 'polymarket_us'} <= executable_venues(selected, with_adapter_only=False):
                raise ValueError('both approved venues must be eligible when arming')
            # Freeze the resolved scope, never let None fall through to future env.
            selected['executable_venues'] = ['kalshi', 'polymarket_us']
            body = {'accounts': binding, 'created': now, 'expires': now+float(duration*3600),
                    'profile': dict(PROFILE), 'spec_hash': _current_spec(), 'settings': selected}
            old = self.conn.execute('SELECT * FROM approval WHERE id=1').fetchone()
            if old and now < timestamp(old['last_ts']):
                raise ValueError('clock regressed')
            if old and json.loads(old['body'])['accounts'] != binding:
                raise ValueError('approval belongs to another account/key; no automatic rotation')
            after = timestamp(self.clock())
            if after < now or after >= body['expires'] or _current_spec() != body['spec_hash']:
                raise ValueError('approval clock changed or expired before arming')
            self.conn.execute('INSERT INTO approval VALUES (1,1,?,?,?) ON CONFLICT(id) DO UPDATE SET active=1,generation=excluded.generation,last_ts=excluded.last_ts,body=excluded.body',
                              (uuid.uuid4().hex, after, _json(body)))
            # Re-arming does not delete previous permits or restore cash capacity.
        return read_status(self.path, now=timestamp(self.clock()))

    def revoke(self):
        with self._tx():
            self.conn.execute('UPDATE approval SET active=0 WHERE id=1')
        return {'status': 'REVOKED', 'approval_active': False, 'execution_enabled': False,
                'note': 'new permission stops; this is not cancellation of sent orders or a dispatch fence for permits already consumed'}

    def _check(self, binding, now):
        if self._status(self.conn, now)['status'] != 'ARMED':
            raise ValueError('standing approval inactive, expired or changed')
        row = self.conn.execute('SELECT * FROM approval WHERE id=1').fetchone()
        body = json.loads(row['body'])
        if body['accounts'] != accounts(binding):
            raise ValueError('account/key mismatch')
        self.conn.execute('UPDATE approval SET last_ts=? WHERE id=1', (now,))
        return row, body

    def approve(self, snapshots, binding, *, contracts=100):
        """Exact pair permission from causal raw quotes, without a human/LLM prompt.

        This creates NO real order intent. The eventual coordinator must reserve
        and reconcile both legs in the production ledger before submitting.
        """
        with self._tx():
            now = timestamp(self.clock())
            row, policy = self._check(binding, now)
            _refresh_rules()
            candidates = plans(snapshots, now=now, contracts=contracts, settings=policy['settings'])
            us_held, kal_held = self._held(self.conn)
            for plan in candidates:
                if self.conn.execute('SELECT 1 FROM permits WHERE event=?', (plan['event'],)).fetchone():
                    continue
                n = plan['count']
                us = decimal(plan['legs'][0]['limit'])*n+_bound(plan['legs'][0], n)+MAX_ROLLS*_bound(plan['legs'][0], n, exit=True)
                kal = decimal(plan['legs'][1]['limit'])*n+_bound(plan['legs'][1], n)
                if (us <= 0 or kal <= 0 or us+us_held > LEG_CAP or kal+kal_held > LEG_CAP or
                        us+kal+us_held+kal_held > TOTAL_CAP):
                    continue
                deadline = min(policy['expires'], plan['kickoff'],
                               *(timestamp(leg['observed_at'])+6 for leg in plan['legs']))
                after = timestamp(self.clock())
                if after < now:
                    raise ValueError('approval clock regressed during planning')
                if deadline <= after:
                    continue
                iid, hashed = uuid.uuid4().hex, digest(plan)
                stored = _json(plan)
                after = timestamp(self.clock())
                if after >= deadline or after < now:
                    raise ValueError('permission expired during approval')
                self._check(binding, after)
                final = timestamp(self.clock())
                if final < after or final >= deadline:
                    raise ValueError('permission expired during final checks')
                self.conn.execute('INSERT INTO permits(id,event,generation,deadline,us_cash,kalshi_cash,digest,body) VALUES (?,?,?,?,?,?,?,?)',
                                  (iid, plan['event'], row['generation'], deadline, str(us), str(kal), hashed, stored))
                self.conn.execute('UPDATE approval SET last_ts=? WHERE id=1', (final,))
                return {'status': 'APPROVED', 'permit_id': iid, 'plan_digest': hashed, 'deadline': deadline,
                        'plan': plan, 'execution_enabled': False, 'orders_submitted': 0}
            return {'status': 'BLOCKED', 'reason': 'no fresh verified pair within remaining permission cash; nothing sent',
                    'execution_enabled': False, 'orders_submitted': 0}

    def consume(self, iid, binding, plan_digest):
        """Single use: caller must possess the exact plan, account and active policy."""
        with self._tx():
            now = timestamp(self.clock())  # sample AFTER transaction lock acquisition
            grant, policy = self._check(binding, now)
            row = self.conn.execute('SELECT * FROM permits WHERE id=?', (iid,)).fetchone()
            if (row is None or row['claimed'] or row['generation'] != grant['generation'] or
                    now >= timestamp(row['deadline']) or row['digest'] != plan_digest):
                raise ValueError('unknown, expired, changed or already consumed permission')
            plan = json.loads(row['body'])
            if digest(plan) != row['digest']:
                raise ValueError('permission plan changed')
            # Hashing/spec checks may themselves outlive a six-second quote. Never
            # use the pre-verification clock value to authorize a late consumer.
            checked = timestamp(self.clock())
            self._check(binding, checked)
            final = timestamp(self.clock())
            if final < checked or final >= min(timestamp(row['deadline']), timestamp(policy['expires'])):
                raise ValueError('permission expired or clock regressed during final checks')
            self.conn.execute('UPDATE permits SET claimed=1 WHERE id=? AND claimed=0', (iid,))
            self.conn.execute('UPDATE approval SET last_ts=? WHERE id=1', (final,))
            return {'status': 'PERMISSION_CONSUMED', 'permit_id': iid, 'generation': grant['generation'],
                    'plan_digest': row['digest'], 'deadline': row['deadline'], 'plan': plan, 'orders_submitted': 0,
                    'note': 'permission is not an order reservation or proof that both legs can execute'}

    def dispatch_guard(self, iid, binding, plan_digest):
        """Fence revocation/re-arming at a NEW-entry durable dispatch claim.

        Trusted coordinator lock order MUST be policy -> production ledger. Hold
        this guard while atomically reserving both legs and claiming the US send;
        NEVER across network I/O. A committed claim precedes a later revoke; revoke
        does not cancel an already claimed order. Transport must separately enforce
        the returned deadline after signing. Permission and production databases
        are NOT one transaction: the production parent must uniquely bind permit_id,
        and claimed sends must never be retried, including on guard failure/crash.
        """
        return self._permit_guard(iid, binding, plan_digest, dispatch=True)

    def reservation_guard(self, iid, binding, plan_digest):
        """Serialize parent accounting admission, without claiming a transport.

        The parent must bind this unique permit durably. A later actual dispatch
        still needs dispatch_guard: staging never bypasses a subsequent revoke.
        """
        return self._permit_guard(iid, binding, plan_digest, dispatch=False)

    @contextmanager
    def _permit_guard(self, iid, binding, plan_digest, *, dispatch):
        with self._tx():
            now = timestamp(self.clock())
            grant, policy = self._check(binding, now)
            row = self.conn.execute('SELECT * FROM permits WHERE id=?', (iid,)).fetchone()
            if (row is None or row['claimed'] != 1 or row['dispatch_claimed'] or
                    row['generation'] != grant['generation'] or row['digest'] != plan_digest or
                    now >= timestamp(row['deadline'])):
                raise ValueError('dispatch permission unconsumed, reused, revoked or expired')
            plan = json.loads(row['body'])
            if digest(plan) != plan_digest:
                raise ValueError('dispatch plan changed')
            self._check(binding, timestamp(self.clock()))
            deadline = min(timestamp(row['deadline']), timestamp(policy['expires']))
            checked = timestamp(self.clock())
            if checked < now or checked >= deadline:
                raise ValueError('dispatch permission expired during verification')
            if dispatch:
                self.conn.execute('UPDATE permits SET dispatch_claimed=1 WHERE id=? AND dispatch_claimed=0', (iid,))
            yield {'permit_id': iid, 'generation': grant['generation'], 'plan_digest': plan_digest,
                   'deadline': deadline, 'plan': plan, 'accounts': accounts(binding)}
            final = timestamp(self.clock())
            if final < checked or final >= deadline:
                raise ValueError('dispatch claim expired; any production claim stays held, never retry')
            self.conn.execute('UPDATE approval SET last_ts=? WHERE id=1', (final,))

"""Durable automatic *paper* pairs. No account client or order transport exists here.

Frozen rule: US IOC first, then Kalshi IOC for only the observed US fill, each
after 3s in a 2s arrival window. Haircut .5. Cancel entry remainders. Sell excess
US inventory after another 3s; at most two 2s unwind windows, loss bounded at $1
including fees. Missing/unsafe exits are unresolved, never valued at settlement.
Reservations and simulated liquidity consumption survive restarts. Paper cash
is separate from the real order ledger; even completed cash is held conservatively.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal, ROUND_FLOOR
from itertools import product
from pathlib import Path

from ..compliance import executable_venues
from ..fees.registry import fee_model_for
from ..matching.matcher import merge_snapshots
from ..quant.us_arbitrage import _quote_ok, _settlement_gates
from .polymarket_us_ioc import decimal, timestamp

ZERO = Decimal(0)
LEG_CAP, TOTAL_CAP = Decimal(25), Decimal(50)
LATENCY, WINDOW, HAIRCUT, MAX_ROLLS = 3., 2., Decimal('.5'), 2
ACTIVE = {'entry', 'hedge', 'unwind', 'unresolved'}
POLICY = {'version': 1, 'latency': LATENCY, 'window': WINDOW,
          'haircut': str(HAIRCUT), 'max_unwind_windows': MAX_ROLLS}


def live_readiness():
    """Capability gates, not flags that imply a production paired executor exists."""
    return {'status': 'BLOCKED', 'live_enabled': False, 'orders_submitted': 0,
            'blockers': ['market-specific compatible US settlement proof is missing',
                         'production pair child transfer/sends and reconciliation are not implemented',
                         'production US inventory exits/failed-leg recovery are not implemented'],
            'available_mode': 'paper'}


def _model(leg):
    return fee_model_for(leg['venue'], leg['fee_params'], settings=leg['settings'])


def _bound(leg, n, *, exit=False):
    model = _model(leg)
    p = Decimal('.5') if exit else min(decimal(leg['limit']), Decimal('.5'))
    fee = decimal(model.fee(p, n, 'taker'))
    if leg['venue'] == 'kalshi':
        fee = max(fee, n * decimal(model.fee(p, 1, 'taker')))
    if fee < 0:
        raise ValueError('negative fee bound')
    return fee


def _leg(q, settings):
    if q.book_id != q.venue:
        raise ValueError('unknown or routed book identity')
    side = q.meta.get('side', 'yes')
    if side not in {'yes', 'no'}:
        raise ValueError('unknown contract side')
    tick = decimal(q.meta.get('tick_size', q.meta.get('tick', '.01')))
    minimum = decimal(q.meta.get('min_size', 1))
    increment = decimal(q.meta.get('quantity_increment', 1))
    price = decimal(q.ask)
    # US limit is expressed in long price by the API, even for buying NO.
    grid_price = 1-price if q.venue == 'polymarket_us' and side == 'no' else price
    if tick <= 0 or tick >= 1 or grid_price % tick or minimum <= 0 or increment <= 0:
        raise ValueError('invalid price/quantity grid')
    params = deepcopy(q.fee_params)
    if q.venue == 'kalshi':
        multiplier = decimal(params.get('fee_multiplier'))
        if multiplier < 0 or params.get('fee_multiplier_assumed'):
            raise ValueError('unverified Kalshi fee multiplier')
    elif decimal(params.get('taker_theta')) < 0:
        raise ValueError('unverified US fee schedule')
    return {'venue': q.venue, 'market': q.venue_market_id, 'book': q.book_id,
            'event': q.event_key, 'outcome': q.outcome, 'side': side, 'limit': str(price),
            'observed_at': q.ts, 'requested_at': q.meta['req_ts'],
            'tick': str(tick), 'minimum': str(minimum), 'increment': str(increment),
            'fee_params': params, 'settings': {'kalshi_rounding': settings.get('kalshi_rounding', 'cent'),
                                             'polymarket_us_volume_rebate': 0}}


def _quantity(leg, available):
    n = int(decimal(available).to_integral_value(rounding=ROUND_FLOOR))
    while n > 0 and (Decimal(n) < decimal(leg['minimum']) or Decimal(n) % decimal(leg['increment'])):
        n -= 1
    return n


def _usable(q, now):
    try:
        return _quote_ok(q, now, 6) and decimal(q.meta['req_ts']) >= 0
    except (ValueError, TypeError, ArithmeticError):
        return False


def _current(quotes, now):
    """Latest causal row per contract; invalid/conflicting latest rows invalidate it."""
    grouped = {}
    for q in quotes:
        try:
            at = timestamp(q.ts)
        except (ValueError, TypeError, OverflowError):
            continue
        if at > now:
            continue
        key = (q.venue, q.venue_market_id, q.book_id, q.event_key, q.outcome, q.meta.get('side', 'yes'))
        grouped.setdefault(key, []).append(q)
    result = []
    for qs in grouped.values():
        latest = max(q.ts for q in qs)
        rows = [q for q in qs if q.ts == latest]
        if all(q == rows[0] for q in rows) and _usable(rows[0], now):
            result.append(rows[0])
    return sorted(result, key=lambda q: (q.ts, q.venue, q.venue_market_id, q.outcome))


def _observations(quotes, now):
    grouped = {}
    for q in quotes:
        if not _usable(q, now):
            continue
        key = (q.venue, q.venue_market_id, q.book_id, q.event_key, q.outcome, q.meta.get('side', 'yes'), q.ts)
        grouped.setdefault(key, []).append(q)
    return sorted([qs[0] for qs in grouped.values() if all(q == qs[0] for q in qs)],
                  key=lambda q: (q.ts, q.venue, q.venue_market_id, q.outcome))


def plans(snapshots, *, now, contracts=100, min_margin='0.01', settings=None):
    """Recompute Decimal plans from raw books; never trust a candidate's float math."""
    now = timestamp(now)
    if isinstance(contracts, bool) or int(contracts) != contracts or not 1 <= contracts <= 10000:
        raise ValueError('invalid contract limit')
    floor = decimal(min_margin)
    if floor < 0:
        raise ValueError('negative margin floor')
    settings = {**(settings or {}), 'polymarket_us_volume_rebate': 0}
    allowed = executable_venues(settings, with_adapter_only=False)
    if not {'kalshi', 'polymarket_us'} <= allowed:
        return []
    # An incomplete snapshot cannot establish the game/identity universe.
    if any(s.errors for s in snapshots):
        return []
    scoped = deepcopy(snapshots)
    identities = {}
    for s in scoped:
        for key, info in s.events.items():
            identities.setdefault(key, set()).add((info.sport, info.market_type, tuple(sorted(info.outcomes)),
                                                  info.start_time, info.in_play is True))
        s.quotes = [q for q in s.quotes if q.venue == s.venue and q.event_key in s.events
                    and q.outcome in s.events[q.event_key].outcomes]
    out = []
    for event in merge_snapshots(scoped, date_tolerance_days=0).values():
        info = event.info
        if (len(identities.get(event.event_key, set())) != 1 or
                info.sport != 'nfl' or info.market_type != 'moneyline' or info.in_play or
                len(set(info.outcomes)) != 2 or len(info.outcomes) != 2 or not info.start_time or
                info.start_time.timestamp() <= now + 2*(LATENCY+WINDOW)):
            continue
        quotes = _current([q for qs in event.quotes_by_venue.values() for q in qs
                           if q.venue in {'kalshi', 'polymarket_us'}], now)
        for a, b in product(quotes, quotes):
            if a.venue != 'polymarket_us' or b.venue != 'kalshi' or a.outcome == b.outcome or a.book_id == b.book_id:
                continue
            gates, tie = _settlement_gates(a, b)
            if gates or tie is None or tie < 1:
                continue
            try:
                legs = [_leg(q, settings) for q in (a, b)]
                maximum = min(contracts, int(min(decimal(a.ask_size), decimal(b.ask_size))*HAIRCUT))
                for n in range(maximum, 0, -1):
                    if any(_quantity(leg, n) != n for leg in legs):
                        continue
                    costs = [decimal(leg['limit'])*n + _bound(leg, n) for leg in legs]
                    unwind_fee = MAX_ROLLS * _bound(legs[0], n, exit=True)
                    # Exit commission needs cash too, even if the second leg fails.
                    if costs[0]+unwind_fee > LEG_CAP or costs[1] > LEG_CAP or sum(costs)+unwind_fee > TOTAL_CAP:
                        continue
                    if (n-sum(costs))/n <= floor:
                        continue
                    out.append({'event': event.event_key, 'decision': now, 'kickoff': info.start_time.timestamp(),
                                'count': n, 'legs': legs, 'reservation': str(sum(costs)+unwind_fee),
                                'unwind_loss_cap': '1', 'tie_payout': str(tie), 'policy': dict(POLICY)})
                    break
            except (ValueError, TypeError, ArithmeticError):
                continue
    # Stable selection independent of input/name ordering; same game tried once.
    return sorted(out, key=lambda p: (p['event'], p['reservation'], json.dumps(p['legs'], sort_keys=True, default=str)))


class PaperPairs:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.execute('CREATE TABLE IF NOT EXISTS pairs (id TEXT PRIMARY KEY,event TEXT UNIQUE,charge TEXT NOT NULL,data TEXT NOT NULL)')
        self.conn.execute('CREATE TABLE IF NOT EXISTS liquidity (key TEXT PRIMARY KEY,used INTEGER NOT NULL)')

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

    def _rows(self):
        return [json.loads(r['data']) for r in self.conn.execute('SELECT data FROM pairs ORDER BY event,id')]

    def status(self):
        return {'mode': 'paper', 'live_enabled': False, 'orders_submitted': 0,
                'held_paper_cash': str(sum((decimal(r['charge']) for r in self.conn.execute('SELECT charge FROM pairs')), ZERO)),
                'cash_release': 'no inferred settlement or credit for sale proceeds', 'pairs': self._rows()}

    def admit(self, snapshots, *, now, contracts=100, settings=None):
        now = timestamp(now)
        candidates = plans(snapshots, now=now, contracts=contracts, settings=settings)
        with self._tx():
            existing = self._rows()
            if any(p['phase'] in ACTIVE for p in existing):
                return None
            charge = sum((decimal(r['charge']) for r in self.conn.execute('SELECT charge FROM pairs')), ZERO)
            seen = {p['event'] for p in existing}
            for plan in candidates:
                if plan['event'] in seen or charge+decimal(plan['reservation']) > TOTAL_CAP:
                    continue
                plan.update(id=uuid.uuid4().hex, phase='entry', due=now+LATENCY, deadline=now+LATENCY+WINDOW,
                            first=0, hedge=0, unwound=0, rolls=0, fills=[], reason='')
                blob = json.dumps(plan, sort_keys=True, default=str)
                self.conn.execute('INSERT INTO pairs VALUES (?,?,?,?)', (plan['id'], plan['event'], plan['reservation'], blob))
                return plan['id']
        return None

    @staticmethod
    def _identity(q, leg):
        return (q.venue, q.venue_market_id, q.book_id, q.event_key, q.outcome, q.meta.get('side', 'yes')) == (
            leg['venue'], leg['market'], leg['book'], leg['event'], leg['outcome'], leg['side'])

    def _fill(self, p, q, leg, requested, *, sell=False):
        if json.dumps(q.fee_params, sort_keys=True, default=str) != json.dumps(leg['fee_params'], sort_keys=True, default=str):
            p['reason'] = 'fee parameters changed; no safe frozen-price fill'
            return 0
        price = decimal(q.bid if sell else q.ask)
        limit = decimal(leg['limit'])
        grid_price = 1-price if leg['venue'] == 'polymarket_us' and leg['side'] == 'no' else price
        if grid_price % decimal(leg['tick']) or (not sell and price > limit):
            return 0
        # Buying NO and selling YES consume the SAME underlying US long bid.
        level = 'bid' if (leg['side'] == 'no') != sell else 'ask'
        key = json.dumps([leg['book'], leg['market'].split('#')[0], level, q.ts])
        used = self.conn.execute('SELECT used FROM liquidity WHERE key=?', (key,)).fetchone()
        size = decimal(q.bid_size if sell else q.ask_size)
        available = max(0, int(size*HAIRCUT)-(used['used'] if used else 0))
        n = _quantity(leg, min(requested, available))
        if not n:
            return 0
        fee = decimal(_model(leg).fee(price, n, 'taker'))
        if fee < 0:
            return 0
        if sell:
            first_fill = next(f for f in p['fills'] if f['action'] == 'buy' and f['venue'] == 'polymarket_us')
            # Conservative fee allocation: include all first-entry fees and prior exit fees.
            fees = decimal(first_fill['fee'])+fee+sum((decimal(f['fee']) for f in p['fills'] if f['action'] == 'sell'), ZERO)
            prior_loss = sum((max(ZERO, decimal(first_fill['price'])-decimal(f['price']))*f['count']
                              for f in p['fills'] if f['action'] == 'sell'), ZERO)
            remaining = p['first']-p['hedge']-p['unwound']
            loss = prior_loss + max(ZERO, decimal(first_fill['price'])-price)*remaining + fees
            if loss > decimal(p['unwind_loss_cap']):
                p['reason'] = 'unwind loss cap exceeded'
                return 0
        self.conn.execute('INSERT INTO liquidity VALUES (?,?) ON CONFLICT(key) DO UPDATE SET used=excluded.used',
                          (key, n+(used['used'] if used else 0)))
        p['fills'].append({'venue': leg['venue'], 'action': 'sell' if sell else 'buy', 'count': n,
                           'price': str(price), 'fee': str(fee), 'obs_ts': q.ts})
        return n

    def advance(self, snapshots, *, now):
        now = timestamp(now)
        valid = []
        unsafe_entries = set()
        for snap in snapshots:
            for key, info in snap.events.items():
                if info.in_play or info.start_time is None or info.start_time.timestamp() <= now:
                    unsafe_entries.add(key)
            if snap.errors:
                continue
            for q in snap.quotes:
                try:
                    if (q.venue == snap.venue and q.event_key in snap.events and
                            q.outcome in snap.events[q.event_key].outcomes):
                        valid.append(q)
                except (TypeError, ValueError, ArithmeticError):
                    continue
        quotes = _observations(valid, now)
        with self._tx():
            for p in self._rows():
                # One phase per poll: a hedge cannot use observations already available
                # when the first fill was processed. It arrives at a later receipt.
                phase = p['phase']
                if phase not in {'entry', 'hedge', 'unwind'}:
                    continue
                if p.get('policy') != POLICY:
                    p['phase'], p['reason'] = 'unresolved', 'paper policy changed; operator review required'
                    self.conn.execute('UPDATE pairs SET data=? WHERE id=?',
                                      (json.dumps(p, sort_keys=True, default=str), p['id']))
                    continue
                leg = p['legs'][1 if phase == 'hedge' else 0]
                mark = next((q for q in quotes if self._identity(q, leg) and p['due'] <= q.ts <= p['deadline']), None)
                if mark is None and now <= p['deadline']:
                    continue
                n = 0
                if mark is not None and (phase == 'unwind' or now < p['kickoff'] and p['event'] not in unsafe_entries):
                    requested = p['count'] if phase == 'entry' else p['first'] if phase == 'hedge' else p['first']-p['hedge']-p['unwound']
                    try:
                        n = self._fill(p, mark, leg, requested, sell=phase == 'unwind')
                    except (ValueError, TypeError, ArithmeticError):
                        p['reason'] = 'invalid price/depth/fee evidence'
                at = now  # sequential dispatch depends on processing time, not an old quote
                if phase == 'entry':
                    p['first'], p['phase'] = n, 'hedge' if n else 'missed'
                elif phase == 'hedge':
                    p['hedge'], p['phase'] = n, 'locked' if n == p['first'] else 'unwind'
                else:
                    p['unwound'] += n
                    p['rolls'] += 1
                    if p['first'] == p['hedge']+p['unwound']:
                        p['phase'] = 'recovered'
                    elif p['rolls'] >= MAX_ROLLS:
                        p['phase'], p['reason'] = 'unresolved', p['reason'] or 'excess inventory remains'
                p['due'], p['deadline'] = at+LATENCY, at+LATENCY+WINDOW
                # Known simulated purchases and all fees stay held; proceeds are not
                # credited. Active/unresolved pairs retain their entire reservation.
                charge = decimal(p['reservation'])
                if p['phase'] not in ACTIVE:
                    charge = sum((decimal(f['price'])*f['count'] for f in p['fills'] if f['action'] == 'buy'), ZERO)
                    charge += sum((decimal(f['fee']) for f in p['fills']), ZERO)
                self.conn.execute('UPDATE pairs SET data=?,charge=? WHERE id=?',
                                  (json.dumps(p, sort_keys=True, default=str), str(charge), p['id']))
        return self.status()

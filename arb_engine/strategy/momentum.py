"""Causal price-pressure diagnostics. Experimental, never an execution signal.

Regression uses elapsed seconds, not poll indices. Projections shrink by path efficiency
(net change / total movement), and never extrapolate beyond the observed time span.
Book imbalance is displayed as context only: displayed liquidity can be cancelled.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from ..models import OutcomeQuote
from .leadlag import _fresh, _mid


def slope(rows, column=1):
    """Least-squares price change per second, centred to avoid epoch cancellation."""
    x = [r[0] - rows[0][0] for r in rows]
    mx = sum(x) / len(x)
    my = sum(r[column] for r in rows) / len(rows)
    var = sum((t - mx) ** 2 for t in x)
    return sum((t - mx) * (r[column] - my) for t, r in zip(x, rows)) / var if var else 0.0


@dataclass
class MomentumTracker:
    window_s: float = 60.0
    short_s: float = 20.0
    horizon_s: float = 10.0
    fresh_s: float = 15.0
    min_samples: int = 5
    min_span_s: float = 20.0
    max_spread: float = 0.08
    _history: dict = field(default_factory=dict)

    def observe(self, event_key: str, quotes_by_venue: dict, now: float) -> list[dict]:
        # Bounded memory across expired games and noisy polling clients.
        self._history = {k: h for k, h in self._history.items() if h and now - h[-1][0] <= self.window_s}
        result, seen = [], set()
        for venue, quotes in sorted(quotes_by_venue.items()):
            for q in sorted(quotes, key=lambda q: q.ts, reverse=True):
                if q.event_key != event_key or q.venue != venue or (q.meta or {}).get('side') == 'no':
                    continue
                # Same underlying book counts once, even if displayed by two brokers.
                key = (event_key, q.book_id, q.outcome)
                if key in seen:
                    continue
                mid = _mid(q)
                if mid is None or not _fresh(q, now, self.fresh_s) or q.ask - q.bid > self.max_spread:
                    continue
                seen.add(key)
                h = self._history.setdefault(key, deque(maxlen=512))
                if h and q.ts < h[-1][0]:
                    continue
                if h and q.ts - h[-1][0] > self.fresh_s:
                    h.clear()
                if not h or q.ts > h[-1][0]:
                    h.append((q.ts, mid, q.bid, q.ask))
                while h and q.ts - h[0][0] > self.window_s:
                    h.popleft()
                # A cached snapshot may render the diagnostic but never adds evidence.
                rows = list(h)
                if len(rows) < self.min_samples or rows[-1][0] - rows[0][0] < self.min_span_s:
                    continue
                recent = [r for r in rows if q.ts - r[0] <= self.short_s]
                if len(recent) < 3 or recent[-1][0] - recent[0][0] < self.short_s / 2:
                    continue
                speed = slope(recent)
                path = sum(abs(b[1] - a[1]) for a, b in zip(recent, recent[1:]))
                efficiency = abs(recent[-1][1] - recent[0][1]) / path if path else 0.0
                horizon = min(self.horizon_s, recent[-1][0] - recent[0][0])
                delta = speed * horizon * efficiency
                # A moving ask with a stationary bid is a spread change, not confirmation.
                confirmed = slope(recent, 2) * speed > 0 and slope(recent, 3) * speed > 0
                direction = 'flat'
                if confirmed and efficiency >= 0.6 and abs(delta) >= 0.005:
                    direction = 'dip-watch' if delta < 0 else 'rising'
                if not confirmed:
                    delta = 0.0
                trough = min(r[1] for r in rows)
                if direction == 'rising' and rows[0][1] - trough >= 0.02 and mid - trough >= 0.01:
                    direction = 'rebound-watch'
                imbalance = None
                if all(v is not None and math.isfinite(v) and v >= 0 for v in (q.bid_size, q.ask_size)) and q.bid_size + q.ask_size > 0:
                    imbalance = (q.bid_size - q.ask_size) / (q.bid_size + q.ask_size)
                result.append(dict(venue=venue, book_id=q.book_id, outcome=q.outcome,
                    ts=q.ts, status=direction, mid=mid, spread=q.ask-q.bid,
                    short_slope=speed, long_slope=slope(rows), efficiency=efficiency,
                    imbalance=imbalance, projected_mid=max(0.0, min(1.0, mid+delta)),
                    horizon_s=horizon, samples=len(rows), signal_only=True,
                    caveat='Experimental trend projection; not fair value or a buy signal'))
        return result


def replay(fixture: dict) -> dict:
    """Score all warmed-up projections against a no-change baseline, in causal order.

    Input is the recorded-tick fixture schema. Labels use the first available snapshot
    within five seconds AFTER the horizon. Missing/stale marks are excluded, not filled
    forward. This is a price forecast test, not a profit/fill simulation.
    """
    tracker = MomentumTracker()
    forecasts, observations = [], {}
    event_key = fixture['event_key']
    previous = -math.inf
    for tick in fixture['ticks']:
        now = tick['ts']
        if now <= previous:
            raise ValueError('Replay timestamps must be strictly increasing')
        previous = now
        quotes = {}
        for venue, items in tick['quotes'].items():
            for item in items:
                data = dict(item)
                outcome = data.pop('outcome')
                market = data.pop('venue_market_id', f'{venue}-{outcome}')
                data.setdefault('ts', now)
                q = OutcomeQuote(venue, market, event_key, outcome, **data)
                quotes.setdefault(venue, []).append(q)
                if _mid(q) is not None and _fresh(q, now, tracker.fresh_s) and (q.meta or {}).get('side') != 'no':
                    observations.setdefault((q.book_id, q.outcome), {})[q.ts] = _mid(q)
        forecasts.extend(tracker.observe(event_key, quotes, now))
    errors, baseline, statuses = [], [], {}
    for f in forecasts:
        statuses[f['status']] = statuses.get(f['status'], 0) + 1
        target = f['ts'] + f['horizon_s']
        marks = observations.get((f['book_id'], f['outcome']), {})
        eligible = [t for t in marks if target <= t <= target + 5]
        if not eligible:
            continue
        actual = marks[min(eligible)]
        errors.append(abs(actual - f['projected_mid']))
        baseline.append(abs(actual - f['mid']))
    return dict(forecasts=len(forecasts), scored=len(errors), missing_marks=len(forecasts)-len(errors),
        statuses=statuses, mean_absolute_error=sum(errors)/len(errors) if errors else None,
        persistence_mean_absolute_error=sum(baseline)/len(baseline) if baseline else None,
        interpretation='Diagnostic replay only; no fills, fees, profit or calibrated probabilities')

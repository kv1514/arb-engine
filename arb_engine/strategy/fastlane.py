"""Fast lane: one-second quote refreshes for the games that are live, between full ticks.

The full ``live`` tick re-fetches whole catalogues (Robinhood's category page, every Kalshi
series, Polymarket's tag) and the ESPN state — 3-5 s of work, so it runs every 5-10 s. The
LAG edge decays in the ~25 s it takes Kalshi to follow a Rothera repricing (docs/MODEL.md,
"The first live Sunday"), so a 5 s poll sees it at least one poll late. The fast lane keeps
the last full snapshot's quotes for each live event and, every second, refreshes only the
two executable venues' top of book with the cheapest calls each venue offers:

* Kalshi: ``GET /markets?tickers=a,b,…`` (one call for every live game's tickers) — bid /
  ask in dollars and the fp sizes.
* Robinhood: ``GET /marketdata/event/contract/quotes/v1/?ids=…`` (20 ids per call) — yes /
  no bid, ask, sizes and the venue's own ``ask_venue_timestamp`` (the freshness the LAG
  rule checks).

Polymarket is left as last seen (it trails by minutes and the account cannot trade there).
Refreshed quotes are copies of the snapshot's ``OutcomeQuote`` objects with new prices, so
fee params, book ids, tie payouts and urls stay exactly what the adapters produced; a venue
whose refresh fails keeps its previous quotes and the failure is reported once per lane step.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable, Optional

from ..models import OutcomeQuote


def _f(x: Any) -> Optional[float]:
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def _epoch(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x) / (1000.0 if x > 1e11 else 1.0)
    try:
        from datetime import datetime

        s = str(x).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def _clock_for(now: Optional[float], clock: Optional[Callable[[], float]]) -> Callable[[], float]:
    """An explicit ``now`` pins every timestamp (tests, replays); otherwise the real clock is
    read before the request (``req_ts``) and again when the answer is in (``obs_ts``)."""
    if clock is not None:
        return clock
    return (lambda: float(now)) if now is not None else time.time


def carried(q: OutcomeQuote) -> OutcomeQuote:
    """A quote this step did not refresh: it keeps the time it was *actually* observed (a
    carried quote stamped "now" would look fresh and could fake a move) and is flagged."""
    meta = dict(q.meta or {})
    if meta.get("obs_ts") is None:
        meta["obs_ts"] = q.ts
        meta["approx_time"] = True
    meta["refreshed"] = False
    return dataclasses.replace(q, meta=meta)


def refresh_kalshi(client: Any, quotes: list[OutcomeQuote], now: Optional[float] = None, clock: Optional[Callable[[], float]] = None) -> list[OutcomeQuote]:
    """Fresh top of book for Kalshi ``quotes`` via one batched ``/markets?tickers=`` call
    (chunks of 50). Quotes whose ticker is missing from the answer are carried unchanged."""
    tick = _clock_for(now, clock)
    tickers = sorted({q.meta.get("ticker") or q.venue_market_id.split("#")[0] for q in quotes})
    if not tickers:
        return list(quotes)
    req_ts = tick()
    fresh: dict[str, dict] = {}
    for i in range(0, len(tickers), 50):
        chunk = tickers[i : i + 50]
        data = client.get("/markets", {"tickers": ",".join(chunk), "limit": 100})
        for m in data.get("markets", []) or []:
            fresh[m.get("ticker", "")] = m
    now = tick()   # obs_ts: when the answer was in hand
    out: list[OutcomeQuote] = []
    for q in quotes:
        m = fresh.get(q.meta.get("ticker") or q.venue_market_id.split("#")[0])
        if not m:
            out.append(carried(q))
            continue
        yes_ask = _f(m.get("yes_ask_dollars")) or ((_f(m.get("yes_ask")) or 0) / 100.0 or None)
        yes_bid = _f(m.get("yes_bid_dollars")) or ((_f(m.get("yes_bid")) or 0) / 100.0 or None)
        if (q.meta or {}).get("side") == "no":
            # A NO row is the complement of the YES book.
            ask = round(1.0 - yes_bid, 4) if yes_bid is not None else None
            bid = round(1.0 - yes_ask, 4) if yes_ask is not None else None
            ask_size, bid_size = _f(m.get("yes_bid_size_fp")), _f(m.get("yes_ask_size_fp"))
        else:
            ask = yes_ask if yes_ask and yes_ask < 1.0 else None
            bid = yes_bid if yes_bid and yes_bid > 0.0 else None
            ask_size, bid_size = _f(m.get("yes_ask_size_fp")), _f(m.get("yes_bid_size_fp"))
        meta = dict(q.meta or {})
        meta.update({"last": _f(m.get("last_price_dollars")) or meta.get("last"), "volume": _f(m.get("volume_fp")) or meta.get("volume"), "req_ts": req_ts, "obs_ts": now, "approx_time": False, "refreshed": True})
        out.append(dataclasses.replace(q, ask=ask, bid=bid, ask_size=ask_size if ask_size is not None else q.ask_size, bid_size=bid_size if bid_size is not None else q.bid_size, ts=now, meta=meta))
    return out


def refresh_robinhood(adapter: Any, quotes: list[OutcomeQuote], now: Optional[float] = None, clock: Optional[Callable[[], float]] = None) -> list[OutcomeQuote]:
    """Fresh top of book for Robinhood ``quotes`` via the quotes API (20 ids per call)."""
    tick = _clock_for(now, clock)
    ids = sorted({(q.meta or {}).get("contract_id") or q.venue_market_id.split("#")[0] for q in quotes})
    if not ids:
        return list(quotes)
    req_ts = tick()
    fresh = adapter.quotes(ids)
    now = tick()   # obs_ts: when the answer was in hand
    out: list[OutcomeQuote] = []
    for q in quotes:
        cid = (q.meta or {}).get("contract_id") or q.venue_market_id.split("#")[0]
        qd = fresh.get(cid)
        if not qd:
            out.append(carried(q))
            continue
        qt = _epoch(qd.get("ask_venue_timestamp") or qd.get("updated_at"))
        meta = dict(q.meta or {})
        meta.update({"state": qd.get("state", meta.get("state")), "last": _f(qd.get("last_trade_price")) if qd.get("last_trade_price") is not None else meta.get("last"), "updated_at": qd.get("updated_at", meta.get("updated_at")), "no_ask": _f(qd.get("no_ask_price")), "no_bid": _f(qd.get("no_bid_price")), "req_ts": req_ts, "obs_ts": now, "approx_time": False, "refreshed": True})
        if meta.get("side") == "no":
            out.append(dataclasses.replace(q, ask=_f(qd.get("no_ask_price")), bid=_f(qd.get("no_bid_price")), ask_size=_f(qd.get("bid_size_fractional") or qd.get("bid_size")), bid_size=_f(qd.get("ask_size_fractional") or qd.get("ask_size")), ts=now, quote_time=qt, meta=meta))
        else:
            out.append(dataclasses.replace(q, ask=_f(qd.get("yes_ask_price")), bid=_f(qd.get("yes_bid_price")), ask_size=_f(qd.get("ask_size_fractional") or qd.get("ask_size")), bid_size=_f(qd.get("bid_size_fractional") or qd.get("bid_size")), ts=now, quote_time=qt, meta=meta))
    return out


class FastLane:
    """Refreshes the executable venues' quotes for a set of events once per step.

    ``kalshi_client`` is the ``KalshiClient`` (``.get``), ``robinhood`` the ``RobinhoodAdapter``
    (``.quotes``); either may be None to skip that venue. ``step`` returns
    ``{event_key: quotes_by_venue}`` with the refreshed lists merged over the last known ones,
    plus the per-venue errors of this step."""

    def __init__(self, kalshi_client: Any = None, robinhood: Any = None, timeout: float = 2.5, clock: Callable[[], float] = time.time) -> None:
        self.kalshi, self.robinhood = kalshi_client, robinhood
        self.timeout = timeout
        self.clock = clock
        self.last: dict[str, dict[str, list[OutcomeQuote]]] = {}
        self.steps = 0
        self.errors: list[str] = []
        self.trade_cursor: dict[str, int] = {}
        self.trade_last_poll: dict[str, float] = {}
        self.trade_poll_gaps: dict[str, float] = {}
        self.trade_errors: list[str] = []
        self._trade_page_cursor: dict[str, str] = {}
        self._trade_pending: dict[str, list[dict]] = {}
        self._trade_min_ts: dict[str, Optional[int]] = {}
        self._trade_thread: Optional[threading.Thread] = None
        self._trade_lock = threading.Lock()
        self._trade_rr = 0

    def seed(self, events: dict[str, dict[str, list[OutcomeQuote]]]) -> None:
        """Adopt the latest full snapshot's quotes (called after every full tick)."""
        self.last = {}
        for key, by in events.items():
            self.last[key] = {}
            for venue, quotes in by.items():
                seeded = []
                for q in quotes:
                    meta = dict(q.meta or {})
                    meta["refreshed"] = False
                    seeded.append(dataclasses.replace(q, meta=meta))
                self.last[key][venue] = seeded

    def step(self, keys: Optional[Iterable[str]] = None, now: Optional[float] = None) -> tuple[dict[str, dict[str, list[OutcomeQuote]]], list[str]]:
        clock = (lambda: float(now)) if now is not None else self.clock
        keys = list(keys) if keys is not None else list(self.last)
        errors: list[str] = []
        k_quotes = [q for k in keys for q in self.last.get(k, {}).get("kalshi", [])]
        r_quotes = [q for k in keys for q in self.last.get(k, {}).get("robinhood", [])]
        jobs: dict[str, Callable[[], list[OutcomeQuote]]] = {}
        if self.kalshi is not None and k_quotes:
            jobs["kalshi"] = lambda: refresh_kalshi(self.kalshi, k_quotes, clock=clock)
        if self.robinhood is not None and r_quotes:
            jobs["robinhood"] = lambda: refresh_robinhood(self.robinhood, r_quotes, clock=clock)
        results: dict[str, list[OutcomeQuote]] = {}
        if jobs:
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                futs = {v: pool.submit(fn) for v, fn in jobs.items()}
                for v, fut in futs.items():
                    try:
                        results[v] = fut.result(timeout=self.timeout)
                    except Exception as e:  # a slow or failing venue keeps its previous quotes
                        errors.append(f"fastlane {v}: {e!r}")
        for k in keys:
            for v, qs in self.last.get(k, {}).items():
                if v not in results:
                    self.last[k][v] = [carried(q) for q in qs]
        for v, qs in results.items():
            by_key: dict[str, list[OutcomeQuote]] = {}
            for q in qs:
                by_key.setdefault(q.event_key, []).append(q)
            for k, lst in by_key.items():
                self.last.setdefault(k, {})[v] = lst
        self.steps += 1
        self.errors = errors
        return {k: self.last[k] for k in keys if k in self.last}, errors

    def _poll_one_ticker(self, store: Any, ticker: str, now: Optional[float], max_pages: int) -> int:
        clock = (lambda: float(now)) if now is not None else self.clock
        if ticker not in self.trade_cursor and ticker not in self._trade_min_ts and hasattr(store, "latest_trade_ts"):
            self.trade_cursor[ticker] = store.latest_trade_ts(ticker)
        minimum = self._trade_min_ts.setdefault(ticker, self.trade_cursor.get(ticker))
        cursor = self._trade_page_cursor.get(ticker)
        trades = list(self._trade_pending.get(ticker, []))
        req_ts = clock()
        complete = False
        for _ in range(max_pages):
            params = {"ticker": ticker, "limit": 1000, "min_ts": minimum, "cursor": cursor}
            page = self.kalshi.get("/markets/trades", {k: v for k, v in params.items() if v is not None}) or {}
            trades.extend(page.get("trades") or [])
            cursor = page.get("cursor")
            if not cursor:
                complete = True
                break
        obs_ts = clock()
        previous = self.trade_last_poll.get(ticker)
        if previous is not None:
            self.trade_poll_gaps[ticker] = obs_ts - previous
        self.trade_last_poll[ticker] = obs_ts
        if not complete:
            # Do not advance the durable watermark over an unfinished newest-first backlog.
            # A restart sees the old DB watermark and safely re-fetches these buffered pages.
            self._trade_pending[ticker] = trades
            self._trade_page_cursor[ticker] = str(cursor)
            return 0
        inserted = store.record_trade_prints(trades, ticker, req_ts=req_ts, obs_ts=obs_ts)
        stamps = [t for t in (_epoch(tr.get("created_time") or tr.get("ts")) for tr in trades) if t is not None]
        if stamps:
            self.trade_cursor[ticker] = int(max(stamps))  # inclusive: same-second late prints survive
        self._trade_pending.pop(ticker, None)
        self._trade_page_cursor.pop(ticker, None)
        self._trade_min_ts.pop(ticker, None)
        return inserted

    def _poll_trades_sync(self, store: Any, tickers: list[str], now: Optional[float], max_pages: int) -> int:
        inserted = 0
        for ticker in tickers:
            try:
                inserted += self._poll_one_ticker(store, ticker, now, max_pages)
            except Exception as exc:
                self.trade_errors.append(f"trade prints {ticker}: {exc!r}")
        return inserted

    def poll_trades(self, store: Any, now: Optional[float] = None, per_step: Optional[int] = None,
                    max_pages: int = 5, cadence_s: float = 5.0, background: bool = False) -> int:
        """Record Kalshi's public trade prints for the live tickers (L1 changes are never read
        as trades). By default every ticker whose five-second cadence is due is polled; an
        explicit ``per_step`` provides the older round-robin mode. ``background=True`` keeps
        that HTTP pagination off the L1 loop. Each ticker is paged back to its cursor
        (newest-first pages; a busy second can exceed one page) and the cursor is the newest
        print's second, not one past it: prints stamped in that same second after the poll
        still arrive, and the trade_id primary key drops the repeats."""
        if self.kalshi is None or store is None:
            return 0
        tickers = sorted({(q.meta or {}).get("ticker") or q.venue_market_id.split("#")[0]
                          for by in self.last.values() for q in by.get("kalshi", [])})
        if not tickers:
            return 0
        decision_now = float(now) if now is not None else self.clock()
        due = [t for t in tickers if decision_now - self.trade_last_poll.get(t, -float("inf")) >= cadence_s
               or t in self._trade_page_cursor]
        if per_step is not None and due:
            ordered = [tickers[(self._trade_rr + i) % len(tickers)] for i in range(len(tickers))]
            due_set = set(due)
            due = [t for t in ordered if t in due_set][:per_step]
            self._trade_rr = (self._trade_rr + per_step) % len(tickers)
        if not due:
            return 0
        if background:
            with self._trade_lock:
                if self._trade_thread is not None and self._trade_thread.is_alive():
                    return 0
                self._trade_thread = threading.Thread(target=self._poll_trades_sync,
                    args=(store, due, None, max_pages), name="kalshi-trade-prints", daemon=True)
                self._trade_thread.start()
            return 0
        return self._poll_trades_sync(store, due, now, max_pages)

    def wait_for_trade_polls(self, timeout: Optional[float] = None) -> bool:
        """Wait for the current background page walk before its Store can be closed.

        Returns false only when the bounded wait expires. No new poll can replace the
        captured thread while it is alive because ``poll_trades`` uses ``_trade_lock``.
        """
        with self._trade_lock:
            thread = self._trade_thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def trade_poll_status(self, now: Optional[float] = None, cadence_s: float = 5.0) -> dict[str, dict[str, Any]]:
        """Observable cadence health for the recorder and post-run diagnostics."""
        at = self.clock() if now is None else float(now)
        tickers = sorted({(q.meta or {}).get("ticker") or q.venue_market_id.split("#")[0]
                          for by in self.last.values() for q in by.get("kalshi", [])})
        return {ticker: {"last_poll_ts": self.trade_last_poll.get(ticker),
                         "last_gap_s": self.trade_poll_gaps.get(ticker),
                         "overdue": at - self.trade_last_poll.get(ticker, -float("inf")) > cadence_s,
                         "backlog": ticker in self._trade_page_cursor}
                for ticker in tickers}

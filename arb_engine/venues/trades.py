"""Public trade tapes (Kalshi, Polymarket) with a read-through cache, for event studies.

Both venues publish every print with a sub-minute timestamp, which is what an event study
around a model move needs (candles blur the first 30 s of repricing):

* Kalshi ``GET /trade-api/v2/markets/trades?ticker=&min_ts=&max_ts=&limit=1000&cursor=``
  → ``{"trades": [{trade_id, ticker, count | count_fp, yes_price (cents) | yes_price_dollars,
  no_price, taker_side, created_time (ISO, µs)}], "cursor": "..."}``, newest first, cursor
  paginated (empty cursor = last page). Prices are the YES side of the ticker.
* Polymarket data API ``GET https://data-api.polymarket.com/trades?market=<conditionId>&
  limit=&offset=`` → a JSON list ``[{asset (token id), conditionId, side (BUY|SELL), size,
  price, timestamp (epoch s), outcome, outcomeIndex, transactionHash}]``, newest first,
  offset paginated. The API filters on the condition id; the token id is filtered here.

This is the *historical* tape for research. The live recorder's print poller
(``strategy/fastlane.py``) is a different client and does not use this cache.

**Completeness is explicit.** Every cached tape (``out/cache/trades/<venue>-<market>-<digest>.json``,
schema ``TAPE_SCHEMA``) says whether it is ``complete``: the venue's pagination ran to its end
(Kalshi: an empty cursor; Polymarket: a short page or a page older than ``min_ts``), no row was
unreadable, and no trade id came back with two different payloads. Only a complete tape is
ever returned. A page budget (``max_pages``) that runs out, a failed page request, a cursor the
venue repeats, or a restart mid-fetch leaves an *incomplete* tape on disk with where to resume
(``next_cursor``, the cursors already seen, the trades so far) and raises
:class:`IncompleteTape`. The next call for the same query resumes from there with its own page
budget instead of starting over; a saved cursor the venue no longer accepts (HTTP 4xx) restarts
the pagination from the top and merges by trade id. ``offline=True`` never fetches: it returns a
complete cached tape or raises.

**Kalshi prints are deduplicated by ``trade_id``.** Overlapping pages (a resumed cursor, a
restart, prints sharing a second at a page boundary) keep one copy; the same id with a
different payload raises :class:`ConflictingTrades` and nothing is cached as complete. A print
without a trade id, or with an unreadable time, price or size, is counted as rejected and keeps
the tape from being complete. (Polymarket prints are not deduplicated: one transaction hash
can carry several prints.)

**Windows.** ``min_ts`` / ``max_ts`` are exact float seconds and must be finite. Kalshi's query
takes whole seconds, so the request is widened by a second on each side and trimmed locally
to the exact window: a print in the window's first or last second is never lost to rounding.

**Cache keys** hash the exact query (venue, market, the float window, the schema), so two
windows that round to the same whole seconds, or two market names that sanitise alike, never
share a file; the stored query is checked again on every read. Files are written atomically.

**Old cache files** (``<venue>-<market>-<int min>-<int max>.json``, no schema) cannot say whether
their fetch ran out of pages, so they are never served: online they are ignored and the tape is
fetched again under the new key; offline the error names the old file. See
docs/CLAUDE_HANDOFF_2026-09-26.md ("Historical trade tapes") for the migration.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

from ..matching.normalize import parse_iso
from .http import HttpClient, HttpError

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
POLY_DATA = "https://data-api.polymarket.com"
KALSHI_PAGE = 1000
POLY_PAGE = 500
TAPE_SCHEMA = 2


@dataclass
class Trade:
    venue: str
    market: str          # Kalshi ticker or Polymarket token id
    ts: float            # epoch seconds, sub-second precision preserved
    price: float         # dollars per $1 payout of the market's YES / the token's outcome
    size: float          # contracts / shares
    side: str = ""       # Kalshi taker_side ('yes'|'no'); Polymarket 'BUY'|'SELL'
    trade_id: str = ""

    @property
    def ts_ms(self) -> int:
        return int(round(self.ts * 1000))


class TapeError(RuntimeError):
    """A tape that must not be used as a research input."""


class IncompleteTape(TapeError):
    """The tape is not known to be complete: the page budget ran out, a page failed, the
    venue repeated a cursor, rows were unreadable, or (offline) only a partial or old-format
    cache exists. ``tape`` is what is known so far (never to be used as the whole tape)."""

    def __init__(self, message: str, tape: Optional["Tape"] = None):
        super().__init__(message)
        self.tape = tape


class ConflictingTrades(TapeError):
    """One trade id came back with two different payloads."""


@dataclass
class Tape:
    venue: str
    market: str
    min_ts: Optional[float]
    max_ts: Optional[float]
    trades: list[Trade] = field(default_factory=list)
    complete: bool = False
    reason: str = ""                    # why not complete ("" when complete)
    pages: int = 0                      # pages fetched for this tape, over every call
    next_cursor: Optional[str] = None   # Kalshi: where an incomplete fetch resumes
    seen_cursors: list[str] = field(default_factory=list)
    duplicates: int = 0                 # overlapping copies of a trade id dropped
    rejected: int = 0                   # rows without a trade id or with an unreadable time / price / size
    restarts: int = 0                   # times the pagination restarted from the top
    fetched_at: float = 0.0


def _f(x: Any) -> Optional[float]:
    """A finite float, or None (``float('nan')`` and ``'inf'`` are not numbers here)."""
    try:
        v = float(x) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None
    return v if v is not None and math.isfinite(v) else None


def parse_kalshi_trade(t: dict[str, Any]) -> Optional[Trade]:
    """One Kalshi print -> Trade (YES price in dollars; cents field as the fallback)."""
    price = _f(t.get("yes_price_dollars"))
    if price is None:
        cents = _f(t.get("yes_price"))
        price = cents / 100.0 if cents is not None else None
    dt = parse_iso(t.get("created_time"))
    if price is None or dt is None:
        return None
    size = _f(t.get("count_fp"))
    if size is None:
        size = _f(t.get("count"))
    if size is None:
        return None
    return Trade(venue="kalshi", market=str(t.get("ticker") or ""), ts=dt.timestamp(), price=price, size=size, side=str(t.get("taker_side") or ""), trade_id=str(t.get("trade_id") or ""))


def parse_polymarket_trade(t: dict[str, Any]) -> Optional[Trade]:
    price, ts = _f(t.get("price")), _f(t.get("timestamp"))
    if price is None or ts is None:
        return None
    if ts > 1e12:  # defensive: a millisecond stamp
        ts = ts / 1000.0
    return Trade(venue="polymarket", market=str(t.get("asset") or ""), ts=ts, price=price, size=_f(t.get("size")) or 0.0, side=str(t.get("side") or ""), trade_id=str(t.get("transactionHash") or t.get("id") or ""))


def _window(min_ts: Optional[float], max_ts: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    out = []
    for name, v in (("min_ts", min_ts), ("max_ts", max_ts)):
        if v is None:
            out.append(None)
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{name} {v!r} is not a number") from None
        if not math.isfinite(f):
            raise ValueError(f"{name} {v!r} is not finite")
        out.append(f)
    if out[0] is not None and out[1] is not None and out[0] > out[1]:
        raise ValueError(f"min_ts {out[0]} is after max_ts {out[1]}")
    return out[0], out[1]


def _query(venue: str, market: str, min_ts: Optional[float], max_ts: Optional[float]) -> dict[str, Any]:
    return {"schema": TAPE_SCHEMA, "venue": venue, "market": str(market), "min_ts": min_ts, "max_ts": max_ts}


def _cache_key(venue: str, market: str, min_ts: Optional[float], max_ts: Optional[float]) -> str:
    """``<venue>-<market>-<digest of the exact query>``: the readable part is sanitised, the
    digest is not, so neither rounded windows nor look-alike market names can collide."""
    digest = hashlib.sha256(json.dumps(_query(venue, market, min_ts, max_ts), sort_keys=True).encode()).hexdigest()[:24]
    return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{venue}-{market}")[:80] + f"-{digest}"


def _legacy_cache_key(venue: str, market: str, min_ts: Optional[float], max_ts: Optional[float]) -> str:
    """The pre-completeness key (whole seconds, truncated): only used to name an old file."""
    raw = f"{venue}-{market}-{'none' if min_ts is None else int(min_ts)}-{'none' if max_ts is None else int(max_ts)}"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw)


class TradesClient:
    def __init__(self, http: Optional[HttpClient] = None, cache_dir: str = "out/cache/trades", offline: bool = False):
        self.http = http or HttpClient(rate_limit=6)
        self.cache_dir = cache_dir
        self.offline = offline

    # ---- cache -----------------------------------------------------------------------
    def _cache_path(self, key: str) -> str:
        return os.path.join(self.cache_dir, key + ".json")

    def _load(self, venue: str, market: str, min_ts: Optional[float], max_ts: Optional[float]) -> Optional[Tape]:
        """The cached tape for exactly this query, complete or not; None when there is none
        (or the file is unreadable, of another schema, or of another query)."""
        p = self._cache_path(_cache_key(venue, market, min_ts, max_ts))
        try:
            with open(p, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(doc, dict) or doc.get("schema") != TAPE_SCHEMA or doc.get("query") != _query(venue, market, min_ts, max_ts):
            return None
        try:
            trades = [Trade(**t) for t in doc.get("trades") or []]
        except TypeError:
            return None
        return Tape(venue=venue, market=str(market), min_ts=min_ts, max_ts=max_ts, trades=trades, complete=bool(doc.get("complete")),
                    reason=str(doc.get("reason") or ""), pages=int(doc.get("pages") or 0), next_cursor=doc.get("next_cursor") or None,
                    seen_cursors=[str(c) for c in doc.get("seen_cursors") or []], duplicates=int(doc.get("duplicates") or 0),
                    rejected=int(doc.get("rejected") or 0), restarts=int(doc.get("restarts") or 0), fetched_at=float(doc.get("fetched_at") or 0.0))

    def _store(self, tape: Tape) -> None:
        """Write the tape atomically (a crash leaves the previous file, never half of one)."""
        doc = {"query": _query(tape.venue, tape.market, tape.min_ts, tape.max_ts), "schema": TAPE_SCHEMA, **{k: v for k, v in asdict(tape).items() if k != "trades"},
               "trades": [asdict(t) for t in tape.trades]}
        os.makedirs(self.cache_dir, exist_ok=True)
        path = self._cache_path(_cache_key(tape.venue, tape.market, tape.min_ts, tape.max_ts))
        fd, tmp = tempfile.mkstemp(dir=self.cache_dir, prefix=".tape-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _try_store(self, tape: Tape) -> None:
        """Cache the tape if the directory is writable; a fetch is not lost to a full disk."""
        try:
            self._store(tape)
        except OSError:
            pass

    def store_complete(self, venue: str, market: str, min_ts: Optional[float], max_ts: Optional[float], trades: Iterable[Trade]) -> Tape:
        """Record a tape known to be complete (a fixture, or one checked by hand) for offline use."""
        min_ts, max_ts = _window(min_ts, max_ts)
        tape = Tape(venue=venue, market=str(market), min_ts=min_ts, max_ts=max_ts, trades=sorted(trades, key=lambda t: (t.ts, t.trade_id)),
                    complete=True, fetched_at=time.time())
        self._store(tape)
        return tape

    def _legacy_note(self, venue: str, market: str, min_ts: Optional[float], max_ts: Optional[float]) -> str:
        p = self._cache_path(_legacy_cache_key(venue, market, min_ts, max_ts))
        return (f"; an old-format cache file exists at {p} but it cannot show whether its fetch ran out of pages, so it is "
                "never used - fetch online once to replace it") if os.path.exists(p) else ""

    def _cached_or_offline(self, venue: str, market: str, min_ts: Optional[float], max_ts: Optional[float]) -> tuple[Optional[Tape], Optional[Tape]]:
        """(complete tape to return, partial tape to resume from)."""
        tape = self._load(venue, market, min_ts, max_ts)
        if tape is not None and tape.complete:
            return tape, None
        if self.offline:
            where = self._cache_path(_cache_key(venue, market, min_ts, max_ts))
            if tape is not None:
                raise IncompleteTape(f"offline: the cached {venue} tape of {market} is incomplete ({tape.reason}; {len(tape.trades)} prints over "
                                     f"{tape.pages} pages) at {where}; fetch online to finish it", tape)
            raise FileNotFoundError(f"offline: no cached trades at {where}" + self._legacy_note(venue, market, min_ts, max_ts))
        return None, tape

    # ---- venues ----------------------------------------------------------------------
    def kalshi_trades(self, ticker: str, min_ts: Optional[float] = None, max_ts: Optional[float] = None, max_pages: int = 50) -> list[Trade]:
        """Every print of ``ticker`` in [min_ts, max_ts], oldest first, deduplicated by trade id.
        Raises :class:`IncompleteTape` rather than return a tape not known to be complete."""
        return self.kalshi_tape(ticker, min_ts, max_ts, max_pages=max_pages).trades

    def kalshi_tape(self, ticker: str, min_ts: Optional[float] = None, max_ts: Optional[float] = None, max_pages: int = 50,
                    checkpoint_every: int = 10) -> Tape:
        """The complete Kalshi tape with its record (see the module docstring), or
        :class:`IncompleteTape` / :class:`ConflictingTrades`. Progress is kept on disk every
        ``checkpoint_every`` pages and whenever the fetch stops, so the next call - a larger
        ``max_pages``, a restarted process - resumes where it stopped."""
        min_ts, max_ts = _window(min_ts, max_ts)
        if int(max_pages) < 1:
            raise ValueError("max_pages must be at least 1")
        done, tape = self._cached_or_offline("kalshi", ticker, min_ts, max_ts)
        if done is not None:
            return done
        tape = tape or Tape(venue="kalshi", market=str(ticker), min_ts=min_ts, max_ts=max_ts)
        by_id = {t.trade_id: t for t in tape.trades}
        cursor = tape.next_cursor
        resumed = cursor is not None
        # Cursors already followed belong to one pass: a resumed pass keeps them (a cursor coming
        # back is a loop), a pass from the top starts clean and counts its own unreadable rows.
        seen = list(tape.seen_cursors) if resumed else []
        if not resumed:
            tape.rejected = 0
        # Kalshi's bounds are whole seconds (and their inclusivity is not documented): ask for
        # a second more on each side and trim to the exact window here.
        lo = math.floor(min_ts) - 1 if min_ts is not None else None
        hi = math.ceil(max_ts) + 1 if max_ts is not None else None

        def keep(reason: str, next_cursor: Optional[str]) -> Tape:
            tape.trades = sorted(by_id.values(), key=lambda t: (t.ts, t.trade_id))
            tape.complete, tape.reason, tape.next_cursor, tape.seen_cursors = False, reason, next_cursor, list(seen)
            tape.fetched_at = time.time()
            self._try_store(tape)
            return tape

        fetched = 0
        while True:
            if fetched >= int(max_pages):
                raise IncompleteTape(f"kalshi tape of {ticker}: page budget {max_pages} used up with more pages left ({len(by_id)} prints "
                                     "so far); call again - with the same or a larger budget - to resume from the saved cursor",
                                     keep(f"page budget {max_pages} used up", cursor))
            params: dict[str, Any] = {"ticker": ticker, "limit": KALSHI_PAGE, "min_ts": lo, "max_ts": hi, "cursor": cursor}
            try:
                data = self.http.get(f"{KALSHI}/markets/trades", params) or {}
            except Exception as e:  # noqa: BLE001 - every failure leaves a resumable, incomplete tape
                status = int(getattr(e, "status", 0) or 0)
                if resumed and cursor is not None and isinstance(e, HttpError) and 400 <= status < 500:
                    # The saved cursor is no longer accepted: start again from the newest page;
                    # prints already kept merge by trade id.
                    cursor, seen, resumed = None, [], False
                    tape.restarts += 1
                    tape.rejected = 0
                    fetched += 1
                    continue
                raise IncompleteTape(f"kalshi tape of {ticker}: page request failed ({e}); call again to resume",
                                     keep(f"page request failed: {type(e).__name__}", cursor)) from e
            fetched += 1
            tape.pages += 1
            resumed = False
            for raw in (data.get("trades") or []) if isinstance(data, dict) else []:
                tr = parse_kalshi_trade(raw) if isinstance(raw, dict) else None
                if tr is None or not tr.trade_id or (tr.market and tr.market != ticker):
                    tape.rejected += 1
                    continue
                if not tr.market:
                    tr = dataclasses.replace(tr, market=str(ticker))
                if (min_ts is not None and tr.ts < min_ts) or (max_ts is not None and tr.ts > max_ts):
                    continue
                prev = by_id.get(tr.trade_id)
                if prev is None:
                    by_id[tr.trade_id] = tr
                elif prev == tr:
                    tape.duplicates += 1
                else:
                    keep(f"trade {tr.trade_id} came back with two payloads", None)
                    raise ConflictingTrades(f"kalshi tape of {ticker}: trade {tr.trade_id} came back with two different payloads "
                                            f"({prev} vs {tr}); the tape is kept incomplete")
            nxt = (data.get("cursor") if isinstance(data, dict) else None) or None
            if nxt is None:
                break
            if nxt == cursor or nxt in seen:
                # A cursor the venue already gave: following it would loop. Nothing proves the
                # tape is whole, so it stays incomplete and the next call starts from the top.
                raise IncompleteTape(f"kalshi tape of {ticker}: the venue repeated cursor {nxt!r}; not complete",
                                     keep("the venue repeated a cursor", None))
            seen.append(nxt)
            cursor = nxt
            if checkpoint_every and tape.pages % int(checkpoint_every) == 0:
                keep("fetch in progress", cursor)          # a crash resumes from here
        tape.trades = sorted(by_id.values(), key=lambda t: (t.ts, t.trade_id))
        tape.next_cursor, tape.seen_cursors, tape.fetched_at = None, [], time.time()
        if tape.rejected:
            tape.complete, tape.reason = False, f"{tape.rejected} unreadable row(s)"
            self._try_store(tape)
            raise IncompleteTape(f"kalshi tape of {ticker}: {tape.rejected} print(s) could not be read (no trade id, another ticker, "
                                 "or no time, price or size); it is not a complete research input", tape)
        tape.complete, tape.reason = True, ""
        self._try_store(tape)
        return tape

    def polymarket_trades(self, token_id: str, min_ts: Optional[float] = None, max_ts: Optional[float] = None, condition_id: Optional[str] = None, max_pages: int = 40) -> list[Trade]:
        """Every print of ``token_id`` in [min_ts, max_ts], oldest first (offset paginated on
        the data API, which filters by condition id; pass it to avoid paging the whole tape).
        Complete when a short page or a page older than ``min_ts`` ends the tape; a page budget
        that runs out raises :class:`IncompleteTape` (offsets shift as prints arrive, so there is
        no resume: the next call starts again)."""
        min_ts, max_ts = _window(min_ts, max_ts)
        done, _ = self._cached_or_offline("polymarket", str(token_id), min_ts, max_ts)
        if done is not None:
            return done.trades
        out: list[Trade] = []
        offset = 0
        complete = False
        pages = 0
        for _ in range(max_pages):
            pages += 1
            params: dict[str, Any] = {"limit": POLY_PAGE, "offset": offset, "takerOnly": "true"}
            if condition_id:
                params["market"] = condition_id
            else:
                params["asset"] = token_id
            data = self.http.get(f"{POLY_DATA}/trades", params) or []
            batch = data if isinstance(data, list) else (data.get("trades") or data.get("data") or [])
            oldest = None
            for t in batch:
                tr = parse_polymarket_trade(t)
                if tr is None:
                    continue
                oldest = tr.ts if oldest is None else min(oldest, tr.ts)
                if tr.market != str(token_id):
                    continue
                if (min_ts is None or tr.ts >= min_ts) and (max_ts is None or tr.ts <= max_ts):
                    out.append(tr)
            if len(batch) < POLY_PAGE or (min_ts is not None and oldest is not None and oldest < min_ts):
                complete = True
                break
            offset += len(batch)
        out.sort(key=lambda t: (t.ts, t.trade_id))
        tape = Tape(venue="polymarket", market=str(token_id), min_ts=min_ts, max_ts=max_ts, trades=out, complete=complete,
                    reason="" if complete else f"page budget {max_pages} used up", pages=pages, fetched_at=time.time())
        self._try_store(tape)
        if not complete:
            raise IncompleteTape(f"polymarket tape of {token_id}: page budget {max_pages} used up with more pages left; "
                                 "call again with a larger max_pages", tape)
        return out


def as_home_prices(trades: Iterable[Trade], is_home: bool = True) -> list[tuple[float, float, float]]:
    """``[(ts, P(home), size)]`` from a market's prints: the away market's YES is 1 - P(home)."""
    return [(t.ts, t.price if is_home else 1.0 - t.price, t.size) for t in trades]

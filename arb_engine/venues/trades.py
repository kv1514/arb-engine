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

**Only a complete tape is returned**, online or offline; anything else raises
:class:`IncompleteTape` (carrying what is known so far as ``.tape``). A Kalshi tape is complete
when, in one *pass* (a pagination from the newest page to an empty cursor):

* the window was **closed** when the pass began: ``max_ts`` is set and is at least
  ``settle_s`` (15 min) before the pass's first page was fetched. A window still open, or an
  open-ended one (``max_ts=None``), can gain prints, so it is refused before any request (and,
  because the query's ``max_ts`` bounds what the venue returns, a pass over a closed window reads
  a set that can no longer change, which is what makes a resumed pass sound). The margin covers
  a print the venue publishes late and a local clock running ahead of the venue's; the rule
  trusts the local clock to within it;
* every page was a well-formed answer (a JSON object with a ``trades`` list and a string
  ``cursor``; anything else - an empty body, an error object - is a failed page, never the end);
* no row was unreadable (no trade id, another ticker, a non-finite or missing time, a price
  outside [0, 1], a size that is not positive) and no trade id came back with two payloads
  (:class:`ConflictingTrades`).

The page budget (``max_pages``) running out, a failed page or a restart mid-fetch leave an
incomplete tape on disk and raise :class:`IncompleteTape`; the next call continues the same pass
from the saved cursor with its own budget. A saved pass is not resumed - the next call starts a
fresh one - when the venue rejects its cursor as unknown (HTTP 400/404/410/422), when it began
before the window closed (by this client's ``settle_s``) or at a time still ahead of the clock,
or when it ended on a repeated cursor, a conflict or unreadable rows. A fresh pass starts empty
and is judged alone: prints of an earlier pass are neither carried into it nor compared with it
(the venue's current answer is the tape, so a print it no longer reports, or has revised, does
not survive). Progress is checkpointed after the first page and every ``checkpoint_every``
pages; writes are atomic, and an incomplete state never overwrites a complete file (checked
under a per-tape ``fcntl`` lock; without ``fcntl``, e.g. on Windows, that check is best effort).
A complete tape that cannot be cached is still returned, with a logged warning.

**Kalshi prints are deduplicated by ``trade_id``** within a pass (overlapping pages, a resumed
cursor, prints sharing a second at a page boundary); the same id with a different payload in one
pass, inside or just outside the window, raises :class:`ConflictingTrades`, and the next call
starts a fresh pass. Polymarket prints are not deduplicated (one transaction hash can carry
several prints), and, as before, a Polymarket row that cannot be read is skipped; its pass
cannot resume (offsets shift as prints arrive).

**Windows.** ``min_ts`` / ``max_ts`` are exact finite seconds (not bools). Kalshi's query takes
whole seconds, so the request is widened by a second each side and trimmed to the exact window.

**Cache files** (schema 3) are keyed by a digest of the exact query (venue, market, window,
Polymarket ``condition_id``, schema); the file name keeps a sanitised prefix of
``<venue>-<market>`` for humans (possibly shortened). Everything stored is checked again on read
(:func:`tape_from_doc`): the query, the type and range of every field and print, and, for a
complete tape, the closed-window rule against its ``pass_started_at`` with the reader's
``settle_s``. A file that fails any check is not used.

**Older cache files** are never served: the pre-schema format cannot say whether its fetch ran
out of pages, and schema 2 files (the first two rounds of this client) were never re-checked
against the closed-window rule, and the first round's can be cut short. Online the tape is
fetched again under the new key; offline the error names the old file.
``scripts/migrate_trade_cache.py`` moves them, and any file that fails its checks, aside.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Optional

from ..matching.normalize import parse_iso
from .http import HttpClient, HttpError

try:  # POSIX advisory locks (stdlib); without them the check-then-write below is best effort
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

log = logging.getLogger(__name__)

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
POLY_DATA = "https://data-api.polymarket.com"
KALSHI_PAGE = 1000
POLY_PAGE = 500
TAPE_SCHEMA = 3                         # 2: files not re-checked against the closed-window rule on read
SETTLE_S = 900.0                        # a window is closed once its end is this far behind the pass's start
LOCK_TIMEOUT_S = 30.0                   # how long a writer waits for another writer of the same tape
STALE_CURSOR_STATUS = (400, 404, 410, 422)
VENUES = ("kalshi", "polymarket")


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
    """The tape is not known to be complete (see the module docstring). ``tape`` is what is
    known so far - never to be used as the whole tape."""

    def __init__(self, message: str, tape: Optional["Tape"] = None):
        super().__init__(message)
        self.tape = tape


class ConflictingTrades(TapeError):
    """One trade id came back with two different payloads in one pass."""


@dataclass
class Tape:
    venue: str
    market: str
    min_ts: Optional[float]
    max_ts: Optional[float]
    trades: list[Trade] = field(default_factory=list)
    complete: bool = False
    reason: str = ""                    # why not complete ("" when complete)
    pages: int = 0                      # pages fetched in the current pass, over every call
    next_cursor: Optional[str] = None   # Kalshi: where the current pass resumes (None: start a fresh pass)
    seen_cursors: list[str] = field(default_factory=list)
    pass_started_at: float = 0.0        # when the current pass fetched its first page
    duplicates: int = 0                 # overlapping copies of a trade id dropped
    rejected: int = 0                   # rows without a trade id, of another ticker, or with an unreadable time / price / size
    restarts: int = 0                   # passes started over because the venue no longer knew the saved cursor
    fetched_at: float = 0.0
    condition_id: Optional[str] = None  # Polymarket: the market filter the query used


def _f(x: Any) -> Optional[float]:
    """A finite float parsed from an API field, or None (``float('nan')``, ``'inf'`` and bools
    are not numbers here)."""
    if isinstance(x, bool):
        return None
    try:
        v = float(x) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None
    return v if v is not None and math.isfinite(v) else None


def _num(x: Any) -> Optional[float]:
    """A finite int or float value (not a bool, not text) as a float, or None: what a stored
    or handed-in number must be."""
    if type(x) not in (int, float) or not math.isfinite(x):
        return None
    return float(x)


def _seconds(name: str, v: Any) -> float:
    s = _num(v)
    if s is None or s < 0:
        raise ValueError(f"{name} must be a finite number of seconds, at least 0 (got {v!r})")
    return s


def parse_kalshi_trade(t: dict[str, Any]) -> Optional[Trade]:
    """One Kalshi print -> Trade (YES price in dollars; cents field as the fallback); None when
    a field is missing or out of range (a price outside [0, 1], a size that is not positive)."""
    price = _f(t.get("yes_price_dollars"))
    if price is None:
        cents = _f(t.get("yes_price"))
        price = cents / 100.0 if cents is not None else None
    dt = parse_iso(t.get("created_time"))
    if price is None or dt is None or not 0.0 <= price <= 1.0:
        return None
    size = _f(t.get("count_fp"))
    if size is None:
        size = _f(t.get("count"))
    if size is None or size <= 0:
        return None
    return Trade(venue="kalshi", market=str(t.get("ticker") or ""), ts=dt.timestamp(), price=price, size=size, side=str(t.get("taker_side") or ""), trade_id=str(t.get("trade_id") or ""))


def parse_polymarket_trade(t: dict[str, Any]) -> Optional[Trade]:
    price, ts = _f(t.get("price")), _f(t.get("timestamp"))
    if price is None or ts is None or not 0.0 <= price <= 1.0:
        return None
    if ts > 1e12:  # defensive: a millisecond stamp
        ts = ts / 1000.0
    size = _f(t.get("size")) or 0.0
    if size < 0:
        return None
    return Trade(venue="polymarket", market=str(t.get("asset") or ""), ts=ts, price=price, size=size, side=str(t.get("side") or ""), trade_id=str(t.get("transactionHash") or t.get("id") or ""))


def _window(min_ts: Any, max_ts: Any) -> tuple[Optional[float], Optional[float]]:
    out: list[Optional[float]] = []
    for name, v in (("min_ts", min_ts), ("max_ts", max_ts)):
        if v is None:
            out.append(None)
            continue
        if isinstance(v, bool):
            raise ValueError(f"{name} {v!r} is not a timestamp")
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{name} {v!r} is not a number") from None
        if not math.isfinite(f):
            raise ValueError(f"{name} {v!r} is not finite")
        out.append(f + 0.0)            # -0.0 and 0.0 are one window
    if out[0] is not None and out[1] is not None and out[0] > out[1]:
        raise ValueError(f"min_ts {out[0]} is after max_ts {out[1]}")
    return out[0], out[1]


def _pages(max_pages: Any) -> int:
    if isinstance(max_pages, bool):
        raise ValueError(f"max_pages {max_pages!r} is not a page count")
    try:
        n = int(max_pages)
    except (TypeError, ValueError):
        raise ValueError(f"max_pages {max_pages!r} is not a page count") from None
    if n != max_pages or n < 1:
        raise ValueError(f"max_pages must be a whole number of pages, at least 1 (got {max_pages!r})")
    return n


def _open_reason(max_ts: Optional[float], started: float, settle_s: float) -> Optional[str]:
    """None when a window ending at ``max_ts`` was closed at ``started``; else why it was not."""
    if max_ts is None:
        return "the window has no end (max_ts=None): more prints can arrive"
    if max_ts > started - settle_s:
        return (f"the window was still open at {started:.0f} (it ends at {max_ts:.0f} and counts as closed "
                f"{settle_s:g} s later, at {max_ts + settle_s:.0f})")
    return None


def _query(venue: str, market: str, min_ts: Optional[float], max_ts: Optional[float], condition_id: Optional[str] = None,
           schema: int = TAPE_SCHEMA) -> dict[str, Any]:
    q: dict[str, Any] = {"schema": schema, "venue": venue, "market": str(market), "min_ts": min_ts, "max_ts": max_ts}
    if condition_id is not None:
        q["condition_id"] = str(condition_id)
    return q


def _cache_key(venue: str, market: str, min_ts: Optional[float], max_ts: Optional[float], condition_id: Optional[str] = None,
               schema: int = TAPE_SCHEMA) -> str:
    """``<sanitised venue-market prefix>-<digest of the exact query>``: the prefix is for humans
    (and may be shortened); the digest, not the prefix, tells tapes apart."""
    digest = hashlib.sha256(json.dumps(_query(venue, market, min_ts, max_ts, condition_id, schema), sort_keys=True).encode()).hexdigest()[:24]
    return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{venue}-{market}")[:80] + f"-{digest}"


def _legacy_cache_key(venue: str, market: str, min_ts: Optional[float], max_ts: Optional[float]) -> str:
    """The pre-completeness key (whole seconds, truncated): only used to name an old file."""
    raw = f"{venue}-{market}-{'none' if min_ts is None else int(min_ts)}-{'none' if max_ts is None else int(max_ts)}"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw)


def _check_trades(venue: str, market: str, min_ts: Optional[float], max_ts: Optional[float], trades: Iterable[Any]) -> list[Trade]:
    """The trades of one tape, validated: right venue and market; a time, price and size that
    are finite numbers (not text); a time inside the window, a price in [0, 1], a size above 0
    (Kalshi) or not below it (Polymarket); a text side and trade id; and (Kalshi) one print per
    trade id. Returns them oldest first with float values; raises ValueError on the first
    violation."""
    out: list[Trade] = []
    ids: set[str] = set()
    for t in trades:
        if not isinstance(t, Trade):
            raise ValueError(f"not a Trade: {t!r}")
        if t.venue != venue or t.market != str(market):
            raise ValueError(f"trade {t.trade_id!r} is of {t.venue} {t.market!r}, not {venue} {market!r}")
        ts, price, size = _num(t.ts), _num(t.price), _num(t.size)
        if ts is None or price is None or size is None:
            raise ValueError(f"trade {t.trade_id!r} has a time, price or size that is not a finite number")
        if not 0.0 <= price <= 1.0 or size < 0 or (venue == "kalshi" and size == 0):
            raise ValueError(f"trade {t.trade_id!r} has a price outside [0, 1] or a size out of range (price {price}, size {size})")
        if not isinstance(t.side, str) or not isinstance(t.trade_id, str):
            raise ValueError(f"trade {t.trade_id!r} has a side or trade id that is not text")
        if (min_ts is not None and ts < min_ts) or (max_ts is not None and ts > max_ts):
            raise ValueError(f"trade {t.trade_id!r} at {ts} is outside the window [{min_ts}, {max_ts}]")
        if venue == "kalshi":
            if not t.trade_id or t.trade_id in ids:
                raise ValueError(f"kalshi trade id {t.trade_id!r} is missing or repeated")
            ids.add(t.trade_id)
        out.append(dataclasses.replace(t, ts=ts, price=price, size=size))
    return sorted(out, key=lambda t: (t.ts, t.trade_id))


def tape_from_doc(doc: Any, settle_s: float = SETTLE_S, query: Optional[dict[str, Any]] = None) -> Tape:
    """The tape a cache document holds, after every check a reader makes; ValueError naming the
    first that fails. Checked: the schema; the stored query (it must equal ``query`` when given,
    and be well formed); counts, cursors and times of the right types; every print
    (:func:`_check_trades`); and, for a complete tape, no cursor left, no rejected rows and a
    window that was closed when its pass began (by ``settle_s``)."""
    if not isinstance(doc, dict):
        raise ValueError("not a JSON object")
    if doc.get("schema") != TAPE_SCHEMA:
        raise ValueError(f"schema {doc.get('schema')!r}, not {TAPE_SCHEMA}")
    q = doc.get("query")
    if query is not None and q != query:
        raise ValueError("the stored query is another tape's")
    if not isinstance(q, dict) or q.get("venue") not in VENUES or not isinstance(q.get("market"), str) \
            or not isinstance(q.get("condition_id", ""), str):
        raise ValueError("the stored query is not well formed")
    venue, market, cond = q["venue"], q["market"], q.get("condition_id")
    min_ts, max_ts = _window(q.get("min_ts"), q.get("max_ts"))
    if q != _query(venue, market, min_ts, max_ts, cond):
        raise ValueError("the stored query is not well formed")
    complete = doc.get("complete")
    if type(complete) is not bool:
        raise ValueError(f"complete is {complete!r}, not true or false")
    counts = {k: doc.get(k, 0) for k in ("pages", "duplicates", "rejected", "restarts")}
    for k, v in counts.items():
        if type(v) is not int or v < 0:
            raise ValueError(f"{k} is {v!r}, not a count")
    nxt, seen = doc.get("next_cursor"), doc.get("seen_cursors") or []
    if (nxt is not None and not isinstance(nxt, str)) or not isinstance(seen, list) or not all(isinstance(c, str) for c in seen):
        raise ValueError("next_cursor / seen_cursors are not text")
    started, fetched = _num(doc.get("pass_started_at", 0.0)), _num(doc.get("fetched_at", 0.0))
    if started is None or fetched is None:
        raise ValueError("pass_started_at / fetched_at is not a finite number")
    rows = doc.get("trades")
    if not isinstance(rows, list) or not all(isinstance(t, dict) for t in rows):
        raise ValueError("trades is not a list of records")
    try:
        parsed = [Trade(**t) for t in rows]
    except TypeError:
        raise ValueError("a print is not a trade record") from None
    trades = _check_trades(venue, market, min_ts, max_ts, parsed)
    if complete:
        if nxt is not None or counts["rejected"]:
            raise ValueError("marked complete with a cursor left or rejected rows")
        why = _open_reason(max_ts, started, settle_s)
        if why:
            raise ValueError(f"marked complete, but {why} when its pass began")
    return Tape(venue=venue, market=market, min_ts=min_ts, max_ts=max_ts, trades=trades, complete=complete,
                reason=str(doc.get("reason") or ""), pages=counts["pages"], next_cursor=nxt, seen_cursors=list(seen),
                pass_started_at=started, duplicates=counts["duplicates"], rejected=counts["rejected"], restarts=counts["restarts"],
                fetched_at=fetched, condition_id=cond)


class TradesClient:
    def __init__(self, http: Optional[HttpClient] = None, cache_dir: str = "out/cache/trades", offline: bool = False,
                 clock: Callable[[], float] = time.time, settle_s: float = SETTLE_S, lock_timeout_s: float = LOCK_TIMEOUT_S):
        self.settle_s = _seconds("settle_s", settle_s)
        self.lock_timeout_s = _seconds("lock_timeout_s", lock_timeout_s)
        self.http = http or HttpClient(rate_limit=6)
        self.cache_dir = cache_dir
        self.offline = offline
        self.clock = clock
        self.last_store_error: Optional[str] = None

    def _now(self) -> float:
        now = _num(self.clock())
        if now is None:
            raise ValueError(f"the clock gave {self.clock()!r}, not a finite time")
        return now

    # ---- cache -----------------------------------------------------------------------
    def _cache_path(self, key: str) -> str:
        return os.path.join(self.cache_dir, key + ".json")

    def _path_for(self, tape: Tape) -> str:
        return self._cache_path(_cache_key(tape.venue, tape.market, tape.min_ts, tape.max_ts, tape.condition_id))

    def _read_why(self, path: str, venue: str, market: str, min_ts: Optional[float], max_ts: Optional[float],
                  condition_id: Optional[str]) -> tuple[Optional[Tape], str]:
        """(the tape stored at ``path`` for exactly this query, "") or (None, why it is not used)."""
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except FileNotFoundError:
            return None, ""
        except (OSError, ValueError) as e:
            return None, f"unreadable ({type(e).__name__})"
        try:
            return tape_from_doc(doc, self.settle_s, _query(venue, market, min_ts, max_ts, condition_id)), ""
        except (TypeError, ValueError) as e:
            return None, str(e)

    def _read(self, path: str, venue: str, market: str, min_ts: Optional[float], max_ts: Optional[float],
              condition_id: Optional[str]) -> Optional[Tape]:
        return self._read_why(path, venue, market, min_ts, max_ts, condition_id)[0]

    def _load(self, venue: str, market: str, min_ts: Optional[float], max_ts: Optional[float], condition_id: Optional[str] = None) -> Optional[Tape]:
        return self._read(self._cache_path(_cache_key(venue, market, min_ts, max_ts, condition_id)), venue, market, min_ts, max_ts, condition_id)

    @contextlib.contextmanager
    def _locked(self, path: str):
        """One writer at a time per tape, across threads and processes: an advisory lock on a
        dot-file beside it, opened read-only (so a lock file this user cannot write does not
        block saves) and waited for at most ``lock_timeout_s`` (then TimeoutError). Without
        ``fcntl`` there is no lock."""
        if fcntl is None:
            yield
            return
        lock = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".lock")
        fd = os.open(lock, os.O_RDONLY | os.O_CREAT, 0o644)
        try:
            deadline = time.monotonic() + self.lock_timeout_s
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"another writer has held {lock} for over {self.lock_timeout_s:g} s") from None
                    time.sleep(0.01)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _store(self, tape: Tape) -> None:
        """Write the tape atomically (a crash leaves the previous file, never half of one). An
        incomplete state never replaces a complete file of the same query (a late writer): the
        check and the write happen under the tape's lock."""
        path = self._path_for(tape)
        os.makedirs(self.cache_dir, exist_ok=True)
        with self._locked(path):
            if not tape.complete:
                have = self._read(path, tape.venue, tape.market, tape.min_ts, tape.max_ts, tape.condition_id)
                if have is not None and have.complete:
                    return
            self._write(path, tape)

    def _write(self, path: str, tape: Tape) -> None:
        doc = {"query": _query(tape.venue, tape.market, tape.min_ts, tape.max_ts, tape.condition_id), "schema": TAPE_SCHEMA,
               **{k: v for k, v in asdict(tape).items() if k not in ("trades", "condition_id")}, "trades": [asdict(t) for t in tape.trades]}
        fd, tmp = tempfile.mkstemp(dir=self.cache_dir, prefix=".tape-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, allow_nan=False)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _try_store(self, tape: Tape) -> bool:
        """Cache the tape if possible; a fetch is not lost to an unwritable directory or a stuck
        lock, but the failure is kept (``last_store_error``) and named in any resume advice."""
        try:
            self._store(tape)
            self.last_store_error = None
            return True
        except (OSError, ValueError) as e:
            self.last_store_error = f"{type(e).__name__}: {e}"
            return False

    def _store_finished(self, tape: Tape) -> None:
        if not self._try_store(tape):
            log.warning("the complete %s tape of %s was not cached (%s); the next call fetches it again",
                        tape.venue, tape.market, self.last_store_error)

    def _resume_advice(self, saved: bool) -> str:
        return ("call again to resume from the saved state" if saved else
                f"progress could NOT be saved ({self.last_store_error}): the next call starts again - fix the cache directory {self.cache_dir}")

    def store_complete(self, venue: str, market: str, min_ts: Optional[float], max_ts: Optional[float], trades: Iterable[Trade],
                       condition_id: Optional[str] = None) -> Tape:
        """Record a tape known to be complete (a fixture, or one checked by hand) for offline
        use, as if its pass began now: the window must be closed now (by ``settle_s``), and the
        trades are validated like a cached tape's. ValueError otherwise."""
        if venue not in VENUES:
            raise ValueError(f"venue {venue!r} is not one of {VENUES}")
        min_ts, max_ts = _window(min_ts, max_ts)
        now = self._now()
        why = _open_reason(max_ts, now, self.settle_s)
        if why:
            raise ValueError(f"{venue} tape of {market} not stored as complete: {why}")
        tape = Tape(venue=venue, market=str(market), min_ts=min_ts, max_ts=max_ts, trades=_check_trades(venue, market, min_ts, max_ts, trades),
                    complete=True, pass_started_at=now, fetched_at=now, condition_id=None if condition_id is None else str(condition_id))
        self._store(tape)
        return tape

    def _older_files_note(self, venue: str, market: str, min_ts: Optional[float], max_ts: Optional[float],
                          condition_id: Optional[str] = None) -> str:
        """Names a cache file of this query written before schema 3, if one exists."""
        names = [_legacy_cache_key(venue, market, min_ts, max_ts), _cache_key(venue, market, min_ts, max_ts, condition_id, schema=2)]
        if condition_id is not None:                   # the first round keyed Polymarket tapes without it
            names.append(_cache_key(venue, market, min_ts, max_ts, None, schema=2))
        old = [p for p in map(self._cache_path, names) if os.path.exists(p)]
        return (f"; an older cache file exists at {old[0]} but it is never used (written before the current checks, it can "
                "be cut short or fetched while its window was open) - fetch online once to replace it "
                "(scripts/migrate_trade_cache.py moves old files aside)") if old else ""

    def _cached_or_offline(self, venue: str, market: str, min_ts: Optional[float], max_ts: Optional[float],
                           condition_id: Optional[str] = None) -> tuple[Optional[Tape], Optional[Tape]]:
        """(complete tape to return, partial tape of the current pass)."""
        where = self._cache_path(_cache_key(venue, market, min_ts, max_ts, condition_id))
        tape, why = self._read_why(where, venue, market, min_ts, max_ts, condition_id)
        if tape is not None and tape.complete:
            return tape, None
        if self.offline:
            if tape is not None:
                raise IncompleteTape(f"offline: the cached {venue} tape of {market} is incomplete ({tape.reason}; {len(tape.trades)} prints) "
                                     f"at {where}; fetch online to finish it", tape)
            note = f" (a file exists there but fails its checks: {why})" if why else ""
            raise FileNotFoundError(f"offline: no usable cached trades at {where}{note}" + self._older_files_note(venue, market, min_ts, max_ts, condition_id))
        return None, tape

    def _refuse_open(self, venue: str, market: str, max_ts: Optional[float], now: float) -> None:
        why = _open_reason(max_ts, now, self.settle_s)
        if why:
            raise IncompleteTape(f"{venue} tape of {market}: {why}; a tape of an open window is never complete, so nothing was fetched")

    # ---- venues ----------------------------------------------------------------------
    def kalshi_trades(self, ticker: str, min_ts: Optional[float] = None, max_ts: Optional[float] = None, max_pages: int = 50) -> list[Trade]:
        """Every print of ``ticker`` in [min_ts, max_ts], oldest first, deduplicated by trade id.
        Raises :class:`IncompleteTape` rather than return a tape not known to be complete."""
        return self.kalshi_tape(ticker, min_ts, max_ts, max_pages=max_pages).trades

    def kalshi_tape(self, ticker: str, min_ts: Optional[float] = None, max_ts: Optional[float] = None, max_pages: int = 50,
                    checkpoint_every: int = 10) -> Tape:
        """The complete Kalshi tape with its record (see the module docstring), or
        :class:`IncompleteTape` / :class:`ConflictingTrades`."""
        min_ts, max_ts = _window(min_ts, max_ts)
        budget = _pages(max_pages)
        now = self._now()
        self._refuse_open("kalshi", ticker, max_ts, now)
        done, prior = self._cached_or_offline("kalshi", ticker, min_ts, max_ts)
        if done is not None:
            return done
        # Resume the saved pass only while it can still end complete: it has a cursor, and it
        # began after the window closed (by this client's margin) and not after now.
        resumed = (prior is not None and prior.next_cursor is not None and prior.pass_started_at <= now
                   and _open_reason(max_ts, prior.pass_started_at, self.settle_s) is None)
        if resumed:
            tape = prior
            by_id = {t.trade_id: t for t in tape.trades}
        else:                                    # a fresh pass: starts empty and is judged alone
            tape = Tape(venue="kalshi", market=str(ticker), min_ts=min_ts, max_ts=max_ts, restarts=prior.restarts if prior else 0)
            by_id = {}
        seen_all: dict[str, Trade] = dict(by_id)   # every row of this pass, window margin included
        seen = list(tape.seen_cursors) if resumed else []
        cursor = tape.next_cursor if resumed else None
        # Kalshi's bounds are whole seconds (and their inclusivity is not documented): ask for
        # a second more on each side and trim to the exact window here.
        lo = math.floor(min_ts) - 1 if min_ts is not None else None
        hi = math.ceil(max_ts) + 1 if max_ts is not None else None

        def keep(reason: str, next_cursor: Optional[str]) -> tuple[Tape, bool]:
            tape.trades = sorted(by_id.values(), key=lambda t: (t.ts, t.trade_id))
            tape.complete, tape.reason, tape.next_cursor = False, reason, next_cursor
            tape.seen_cursors = list(seen) if next_cursor is not None else []
            tape.fetched_at = self._now()
            return tape, self._try_store(tape)

        def stop(reason: str, next_cursor: Optional[str], message: str) -> IncompleteTape:
            t, saved = keep(reason, next_cursor)
            advice = self._resume_advice(saved) if next_cursor is not None or not saved else "the next call starts a fresh pass"
            return IncompleteTape(f"kalshi tape of {ticker}: {message}; {advice}", t)

        fetched = 0
        while True:
            if fetched >= budget:
                raise stop(f"page budget {budget} used up", cursor, f"page budget {budget} used up with more pages left ({len(by_id)} prints so far)")
            params: dict[str, Any] = {"ticker": ticker, "limit": KALSHI_PAGE, "min_ts": lo, "max_ts": hi, "cursor": cursor}
            if cursor is None and not tape.pages:
                tape.pass_started_at = self._now()
            try:
                data = self.http.get(f"{KALSHI}/markets/trades", params)
            except Exception as e:  # noqa: BLE001 - every failure leaves a resumable, incomplete tape
                status = int(getattr(e, "status", 0) or 0)
                if resumed and cursor is not None and isinstance(e, HttpError) and status in STALE_CURSOR_STATUS:
                    # The venue no longer knows the saved cursor: a fresh pass.
                    by_id, seen_all, seen, cursor, resumed = {}, {}, [], None, False
                    tape.trades, tape.pages, tape.rejected, tape.duplicates = [], 0, 0, 0
                    tape.restarts += 1
                    fetched += 1
                    continue
                raise stop(f"page request failed: {type(e).__name__}", cursor, f"page request failed ({e})") from e
            if not isinstance(data, dict) or not isinstance(data.get("trades"), list) or not isinstance(data.get("cursor"), str):
                # An empty body, an error object or a page without its cursor is not the last
                # page: it is a failed one.
                shape = type(data).__name__ if not isinstance(data, dict) else f"keys {sorted(data)[:6]}"
                raise stop("malformed page", cursor, f"malformed page ({shape}: needs a trades list and a string cursor)")
            fetched += 1
            tape.pages += 1
            resumed = False
            for raw in data["trades"]:
                tr = parse_kalshi_trade(raw) if isinstance(raw, dict) else None
                if tr is None or not tr.trade_id or (tr.market and tr.market != ticker):
                    tape.rejected += 1
                    continue
                if not tr.market:
                    tr = dataclasses.replace(tr, market=str(ticker))
                prev = seen_all.get(tr.trade_id)
                if prev is not None and prev != tr:
                    keep(f"trade {tr.trade_id} came back with two payloads", None)
                    raise ConflictingTrades(f"kalshi tape of {ticker}: trade {tr.trade_id} came back with two different payloads in one pass "
                                            f"({prev} vs {tr}); the tape is kept incomplete and the next call starts a fresh pass "
                                            f"(the cached file is {self._path_for(tape)})")
                if prev is not None:
                    tape.duplicates += 1
                    continue
                seen_all[tr.trade_id] = tr
                if (min_ts is not None and tr.ts < min_ts) or (max_ts is not None and tr.ts > max_ts):
                    continue
                by_id[tr.trade_id] = tr
            nxt = data["cursor"] or None
            if nxt is None:
                break
            if nxt == cursor or nxt in seen:
                # A cursor the venue already gave: following it would loop.
                raise stop("the venue repeated a cursor", None, f"the venue repeated cursor {nxt!r}")
            seen.append(nxt)
            cursor = nxt
            if tape.pages == 1 or (checkpoint_every and tape.pages % int(checkpoint_every) == 0):
                keep("fetch in progress", cursor)          # a crash resumes from here
        if tape.rejected:
            raise stop(f"{tape.rejected} unreadable row(s)", None,
                       f"{tape.rejected} print(s) could not be read (no trade id, another ticker, or no valid time, price or size)")
        still_open = _open_reason(max_ts, tape.pass_started_at, self.settle_s)
        if still_open:                                     # only if the clock went backwards mid-pass
            raise stop("window still open", None, f"{still_open} when the pass began")
        tape.trades = sorted(by_id.values(), key=lambda t: (t.ts, t.trade_id))
        tape.complete, tape.reason, tape.next_cursor, tape.seen_cursors, tape.fetched_at = True, "", None, [], self._now()
        self._store_finished(tape)
        return tape

    def polymarket_trades(self, token_id: str, min_ts: Optional[float] = None, max_ts: Optional[float] = None, condition_id: Optional[str] = None, max_pages: int = 40) -> list[Trade]:
        """Every print of ``token_id`` in [min_ts, max_ts], oldest first (offset paginated on
        the data API, which filters by condition id; pass it to avoid paging the whole tape).
        Complete when a short page or a page older than ``min_ts`` ends the tape, every page was
        well formed, and the window was closed when the fetch began (an open one is refused
        before any request); otherwise :class:`IncompleteTape` (no resume: the next call starts
        again)."""
        min_ts, max_ts = _window(min_ts, max_ts)
        budget = _pages(max_pages)
        cond = str(condition_id) if condition_id is not None else None
        started = self._now()
        self._refuse_open("polymarket", str(token_id), max_ts, started)
        done, _ = self._cached_or_offline("polymarket", str(token_id), min_ts, max_ts, cond)
        if done is not None:
            return done.trades
        out: list[Trade] = []
        offset, pages, reason = 0, 0, ""
        for _ in range(budget):
            params: dict[str, Any] = {"limit": POLY_PAGE, "offset": offset, "takerOnly": "true"}
            if cond:
                params["market"] = cond
            else:
                params["asset"] = token_id
            try:
                data = self.http.get(f"{POLY_DATA}/trades", params)
            except Exception as e:  # noqa: BLE001
                reason = f"page request failed: {type(e).__name__}: {e}"
                break
            batch = data if isinstance(data, list) else (data.get("trades") if isinstance(data, dict) and isinstance(data.get("trades"), list) else
                                                         data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), list) else None)
            if batch is None:
                reason = f"malformed page ({type(data).__name__})"
                break
            pages += 1
            oldest = None
            for t in batch:
                tr = parse_polymarket_trade(t) if isinstance(t, dict) else None
                if tr is None:
                    continue
                oldest = tr.ts if oldest is None else min(oldest, tr.ts)
                if tr.market != str(token_id):
                    continue
                if (min_ts is None or tr.ts >= min_ts) and (max_ts is None or tr.ts <= max_ts):
                    out.append(tr)
            if len(batch) < POLY_PAGE or (min_ts is not None and oldest is not None and oldest < min_ts):
                break
            offset += len(batch)
        else:
            reason = f"page budget {budget} used up"
        out.sort(key=lambda t: (t.ts, t.trade_id))
        tape = Tape(venue="polymarket", market=str(token_id), min_ts=min_ts, max_ts=max_ts, trades=out, complete=not reason,
                    reason=reason, pages=pages, pass_started_at=started, fetched_at=self._now(), condition_id=cond)
        if not reason:
            self._store_finished(tape)
            return out
        saved = self._try_store(tape)
        advice = "call again with a larger max_pages" if "budget" in reason else "call again"
        if not saved:
            advice += f" (progress could not be saved: {self.last_store_error})"
        raise IncompleteTape(f"polymarket tape of {token_id}: {reason}; {advice}", tape)


def as_home_prices(trades: Iterable[Trade], is_home: bool = True) -> list[tuple[float, float, float]]:
    """``[(ts, P(home), size)]`` from a market's prints: the away market's YES is 1 - P(home)."""
    return [(t.ts, t.price if is_home else 1.0 - t.price, t.size) for t in trades]

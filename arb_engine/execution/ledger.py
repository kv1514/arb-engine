"""Durable order intents, exposure budgets and reconciliation for every Kalshi order the
engine sends by itself (LAG entries and lock legs, the "Robinhood done" button) and for
confirmed manual orders.

Why a ledger: an order request has an outcome the sending process cannot always observe.
The gateway answers with an order id (accepted), or refuses it - or the request went out
and no answer came back (a timeout, a 5xx after ``HttpClient`` re-sent it, a crash
between send and record). Kalshi may well have accepted that order. Budgets kept in
process memory forget it, and every fill, at the next restart, so the same day's budget
can be spent twice; two processes (the NFL and college slates) each had their own.

One SQLite file per Kalshi environment (``out/orders/kalshi_<env>_ledger.sqlite3``),
shared by every process that trades it:

1. **Intent before request.** :meth:`OrderLedger.reserve` writes the intent - ticker,
   side, count, limit, a fresh ``client_order_id`` and the worst-case cost *fees included*
   - and reserves that worst case against the strategy's daily and per-game budgets in one
   ``BEGIN IMMEDIATE`` transaction, so two processes cannot both take the last dollars.
   If the write fails the caller gets :class:`LedgerError` and sends nothing.
2. **Every answer recorded**: :meth:`accepted` (order id, the exchange's fill count),
   :meth:`rejected` (nothing was sent, or the exchange provably never took it),
   :meth:`ambiguous` (anything else, including a response without an order id).
3. **Unknown blocks new exposure.** While an intent is ambiguous - or pending longer than
   ``pending_stale_s`` (no request lives that long), or pending in a process that no longer
   exists - :meth:`reserve` refuses everything except an exempt lock leg of a *known* fill.
4. **Reconciliation** (:meth:`reconcile`) finds each unresolved intent on the exchange by
   its ``client_order_id`` (``GET /portfolio/orders?ticker=&min_ts=``, every page; Kalshi
   has no client_order_id filter) and reads each accepted order's final fills and fees
   (``GET /portfolio/orders/{id}``: ``fill_count_fp``, ``taker|maker_fill_cost_dollars``,
   ``taker|maker_fees_dollars``; ``GET /portfolio/fills?order_id=`` cross-checks), replacing
   the reserved worst case with what was paid. An intent that complete listings still do not
   show ``not_found_s`` after it was sent was never accepted: its reservation is released.
   An incomplete (truncated) listing proves nothing and never releases anything.

Budgets are dollars **including fees**. An open intent counts at its worst case
(``count x limit`` + :func:`fee_bound`), an accepted immediate-or-cancel order at its
reported fills at the limit plus that bound, a reconciled one at its actual fill cost plus
actual fees. A day is the local calendar date at reservation.
"""

from __future__ import annotations

import json
import math
import os
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Iterator, Optional

from ..fees.base import D
from ..fees.kalshi import KalshiFees

try:  # settings registry; keep importable without it
    from ..config import declare_setting as _declare_setting, setting as _setting
except ImportError:  # pragma: no cover
    _declare_setting = _setting = None  # type: ignore[assignment]
if _declare_setting is not None:
    try:
        _declare_setting("order_ledger_dir", env="ARB_ORDER_LEDGER_DIR", default=os.path.join("out", "orders"), cast=str,
                         doc="Directory of the durable Kalshi order ledgers (execution/ledger.py): one SQLite file per environment, shared by every process that sends orders.")
    except Exception:  # pragma: no cover - conflicting re-declaration is a test failure elsewhere
        pass

PENDING, AMBIGUOUS, ACCEPTED, DONE, REJECTED = "pending", "ambiguous", "accepted", "done", "rejected"
OPEN = (PENDING, AMBIGUOUS, ACCEPTED)
ZERO = Decimal("0")
HALF = Decimal("0.5")
IOC = ("immediate_or_cancel", "fill_or_kill", "ioc", "fok")

# Kalshi REST hosts per environment (venues/kalshi.ENV_REST_BASE + LEGACY_REST_BASE, and the
# pre-2025 trading host). Orders only go to a host whose environment is the client's.
KNOWN_HOSTS = {
    "prod": ("external-api.kalshi.com", "api.elections.kalshi.com", "trading-api.kalshi.com"),
    "demo": ("external-api.demo.kalshi.co", "demo-api.kalshi.co"),
}

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)""",
    """CREATE TABLE IF NOT EXISTS intents (
  intent_id TEXT PRIMARY KEY, client_order_id TEXT NOT NULL UNIQUE, dedupe_key TEXT UNIQUE,
  strategy TEXT NOT NULL, parent_id TEXT, env TEXT NOT NULL, host TEXT NOT NULL, owner TEXT NOT NULL,
  created_ts REAL NOT NULL, updated_ts REAL NOT NULL, day TEXT NOT NULL,
  event_key TEXT, game_key TEXT, ticker TEXT NOT NULL, action TEXT NOT NULL, side TEXT NOT NULL,
  tif TEXT NOT NULL, count INTEGER NOT NULL, limit_price TEXT NOT NULL, max_cost TEXT NOT NULL,
  state TEXT NOT NULL, order_id TEXT, fill_count TEXT, fill_cost TEXT, fees TEXT,
  req_ts REAL, resp_ts REAL, reconciled_ts REAL, checks INTEGER NOT NULL DEFAULT 0,
  misses INTEGER NOT NULL DEFAULT 0, hint INTEGER, reason TEXT, detail TEXT)""",
    """CREATE INDEX IF NOT EXISTS intents_open ON intents (state)""",
    """CREATE INDEX IF NOT EXISTS intents_day ON intents (strategy, day)""",
    """CREATE INDEX IF NOT EXISTS intents_game ON intents (strategy, game_key)""",
    """CREATE INDEX IF NOT EXISTS intents_parent ON intents (parent_id)""",
    """CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
  intent_id TEXT, kind TEXT NOT NULL, detail TEXT)""",
)


class LedgerError(RuntimeError):
    """The ledger could not record (or refuses this environment): send nothing."""


API_PATH = "/trade-api/v2"


def endpoint_problem(base_url: str) -> Optional[str]:
    """Why ``base_url`` is not a well-formed Kalshi REST endpoint, or None.

    The signed headers (key id, timestamp, signature) and every order ride on this URL, so
    it must be exactly ``https://<host>/trade-api/v2``: HTTPS (never cleartext), no
    user-info, the default port, that path and nothing after it (no query, no fragment), and
    no whitespace or control characters that different parsers could read differently."""
    from urllib.parse import urlsplit

    raw = str(base_url or "")
    if not raw or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in raw) or "\\" in raw:
        return "empty, or contains whitespace, control characters or backslashes"
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as e:
        return f"unparseable ({e})"
    if parts.scheme.lower() != "https":
        return f"scheme {parts.scheme or '(none)'!r}: only https"
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        return "carries user-info (credentials) in the URL"
    if port not in (None, 443):
        return f"port {port}: only the default HTTPS port"
    if not parts.hostname:
        return "no host"
    if parts.path.rstrip("/") != API_PATH:
        return f"path {parts.path or '/'!r}: must be {API_PATH}"
    if parts.query or parts.fragment or raw.rstrip("/").endswith(("?", "#")):
        return "query or fragment after the API path"
    return None


def host_env(base_url: str) -> Optional[str]:
    """``prod`` / ``demo`` for a well-formed endpoint on a known Kalshi host, else None."""
    from urllib.parse import urlsplit

    if endpoint_problem(base_url) is not None:
        return None
    host = (urlsplit(str(base_url or "")).hostname or "").lower()
    for env, hosts in KNOWN_HOSTS.items():
        if host in hosts:
            return env
    return None


def env_host_problem(env: str, base_url: str) -> Optional[str]:
    """Why an order must not go to ``base_url`` for ``KALSHI_ENV=env``, or None.

    ``KALSHI_BASE_URL`` overrides the host without changing ``KALSHI_ENV``; the demo gates
    (no ``ARB_LIVE_TRADING`` needed) must never reach a production host that way, and an
    unrecognised host (a proxy, a typo) could be either. The whole endpoint is checked
    (``endpoint_problem``): a recognised host over plain HTTP, on another port or path is
    refused too."""
    from urllib.parse import urlsplit

    structure = endpoint_problem(base_url)
    if structure is not None:
        return f"{base_url!r} is not a Kalshi API endpoint: {structure}"
    host = (urlsplit(str(base_url or "")).hostname or "").lower()
    where = host_env(base_url)
    env = str(env or "").lower()
    if where is None:
        return (f"{host} is not a known Kalshi host ({', '.join(KNOWN_HOSTS.get(env, ()) or sum(KNOWN_HOSTS.values(), ()))}); "
                "orders only go to a recognised demo or production host")
    if where != env:
        return f"KALSHI_ENV={env} but {host} is Kalshi's {where} host (KALSHI_BASE_URL?)"
    return None


def fee_bound(limit: Any, count: Any, multiplier: Any = 1) -> Decimal:
    """The most Kalshi can charge in taker fees for ``count`` contracts bought at prices
    no higher than ``limit``: ``count`` x the one-contract fee at the dearest-fee price
    ``min(limit, 0.5)`` (``p(1-p)`` peaks at 0.5), each rounded up to the cent. Rounding per
    one contract bounds any split into fills: ``ceil(n x f) <= n x ceil(f)`` on the cent grid.
    ``multiplier`` below 1 (a half-fee series) is ignored: the bound stays at the full rate."""
    p = min(D(limit), HALF)
    m = max(D(multiplier), Decimal(1))
    return KalshiFees(multiplier=m).fee(p, 1, "taker") * D(count)


def worst_cost(limit: Any, count: Any) -> Decimal:
    """``count x limit`` plus :func:`fee_bound`: the most a buy of ``count`` at ``limit`` costs."""
    return D(limit) * D(count) + fee_bound(limit, count)


def default_path(env: str, directory: Optional[str] = None) -> str:
    base = directory
    if base is None and _setting is not None:
        try:
            base = _setting(None, "order_ledger_dir")
        except KeyError:  # pragma: no cover
            base = None
    return os.path.join(base or os.path.join("out", "orders"), f"kalshi_{env}_ledger.sqlite3")


def _dec(x: Any) -> Optional[Decimal]:
    """Kalshi fixed-point strings and numbers -> Decimal; None/''/non-finite -> None."""
    if x is None or x == "":
        return None
    try:
        v = D(x) if not isinstance(x, (bool,)) else None
    except (InvalidOperation, ValueError, TypeError):
        return None
    if v is None or not v.is_finite():
        return None
    return v


def _s(x: Optional[Decimal]) -> Optional[str]:
    return None if x is None else str(x)


@dataclass
class Budget:
    """Dollars (fees included) a strategy may commit per local day and per game."""
    daily: Optional[Decimal] = None
    per_game: Optional[Decimal] = None


@dataclass
class Reservation:
    ok: bool
    reason: str = ""
    intent_id: Optional[str] = None
    client_order_id: Optional[str] = None
    count: int = 0
    max_cost: Decimal = ZERO
    room: Optional[Decimal] = None


class OrderLedger:
    def __init__(self, path: str, env: str, host: str, clock: Any = time.time, owner: Optional[str] = None,
                 pending_stale_s: float = 300.0, not_found_s: float = 30.0, settle_s: float = 2.0, max_checks: int = 120) -> None:
        self.path, self.env, self.host, self.clock = path, str(env).lower(), str(host), clock
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}"
        # HttpClient re-sends a POST up to 3 times with 20 s timeouts (+curl slack): a pending
        # intent older than this is not in flight any more, whoever owns it.
        self.pending_stale_s, self.not_found_s, self.settle_s = float(pending_stale_s), float(not_found_s), float(settle_s)
        # An accepted order (known to the exchange, exposure bounded) stops being polled after
        # this many failed reads; an unknown one is polled for as long as it blocks.
        self.max_checks = int(max_checks)
        self._lock = threading.RLock()
        try:
            if path != ":memory:":
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self.conn = sqlite3.connect(path, timeout=10.0, isolation_level=None, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            if path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.execute("PRAGMA busy_timeout=10000")
            with self._tx() as c:
                for stmt in _SCHEMA:
                    c.execute(stmt)
                row = c.execute("SELECT value FROM meta WHERE key='env'").fetchone()
                if row is None:
                    c.execute("INSERT INTO meta VALUES ('env', ?)", (self.env,))
                elif row["value"] != self.env:
                    raise LedgerError(f"{path} is the Kalshi {row['value']} ledger; this client is {self.env}")
        except LedgerError:
            raise
        except (sqlite3.Error, OSError) as e:
            raise LedgerError(f"order ledger {path} unavailable: {e!r}") from e

    @classmethod
    def for_client(cls, client: Any, path: Optional[str] = None, **kw: Any) -> "OrderLedger":
        """The ledger of ``client``'s environment; refuses a host of another environment."""
        env = str(getattr(client, "env", "") or "").lower()
        base = str(getattr(client, "base_url", "") or "")
        problem = env_host_problem(env, base)
        if problem:
            raise LedgerError(problem)
        return cls(path or default_path(env), env, base, **kw)

    def close(self) -> None:
        try:
            self.conn.close()
        except (sqlite3.Error, AttributeError):  # pragma: no cover
            pass

    def __del__(self) -> None:  # the connection is the only resource; close it with the object
        self.close()

    # ---- plumbing ------------------------------------------------------------------------
    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` transaction (the write lock across processes)."""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def _write(self, fn: Any) -> Any:
        try:
            with self._tx() as c:
                return fn(c)
        except sqlite3.Error as e:
            raise LedgerError(f"order ledger write failed: {e!r}") from e

    @staticmethod
    def _event(c: sqlite3.Connection, ts: float, intent_id: Optional[str], kind: str, **detail: Any) -> None:
        c.execute("INSERT INTO events (ts, intent_id, kind, detail) VALUES (?,?,?,?)",
                  (ts, intent_id, kind, json.dumps(detail, default=str, sort_keys=True) if detail else None))

    def _check_client(self, client: Any) -> None:
        env = str(getattr(client, "env", "") or "").lower()
        if env and env != self.env:
            raise LedgerError(f"the {self.env} ledger cannot be reconciled with a {env} client")

    # ---- exposure ------------------------------------------------------------------------
    @staticmethod
    def exposure_of(row: Any) -> Decimal:
        """What one intent commits, fees included (see the module docstring)."""
        st = row["state"]
        if st == REJECTED:
            return ZERO
        if st == DONE:
            return (_dec(row["fill_cost"]) or ZERO) + (_dec(row["fees"]) or ZERO)
        if st == ACCEPTED and str(row["tif"]).lower() in IOC:
            n = _dec(row["fill_count"])
            if n is not None:
                return min(worst_cost(row["limit_price"], n), _dec(row["max_cost"]) or ZERO)
        return _dec(row["max_cost"]) or ZERO

    def _sum(self, c: sqlite3.Connection, where: str, args: tuple) -> Decimal:
        rows = c.execute(f"SELECT state, tif, fill_count, fill_cost, fees, limit_price, max_cost FROM intents WHERE state != 'rejected' AND {where}", args).fetchall()
        return sum((self.exposure_of(r) for r in rows), ZERO)

    def exposure(self, strategy: Optional[str] = None, day: Optional[str] = None, game_key: Optional[str] = None) -> Decimal:
        where, args = ["1=1"], []
        for col, val in (("strategy", strategy), ("day", day), ("game_key", game_key)):
            if val is not None:
                where.append(f"{col} = ?")
                args.append(val)
        with self._lock:
            return self._sum(self.conn, " AND ".join(where), tuple(args))

    def day_of(self, ts: float) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(ts))

    # ---- blocking ------------------------------------------------------------------------
    def _owner_gone(self, owner: str) -> bool:
        host, _, pid = str(owner).rpartition(":")
        if owner == self.owner or host != socket.gethostname():
            return False
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return True
        except (PermissionError, ValueError, OSError):
            return False
        return False

    def _unknown(self, c: sqlite3.Connection, now: float) -> list[Any]:
        """Intents whose exchange outcome is unknown: ambiguous, or pending but no longer in
        flight (older than ``pending_stale_s`` or owned by a process that is gone)."""
        out = []
        for r in c.execute("SELECT * FROM intents WHERE state IN ('pending', 'ambiguous') ORDER BY created_ts").fetchall():
            if r["state"] == AMBIGUOUS or now - float(r["created_ts"]) > self.pending_stale_s or self._owner_gone(r["owner"]):
                out.append(r)
        return out

    def blocked(self, now: Optional[float] = None) -> Optional[str]:
        """Why no new exposure may be reserved right now, or None."""
        now = self.clock() if now is None else float(now)
        with self._lock:
            rows = self._unknown(self.conn, now)
        return self._block_reason(rows)

    @staticmethod
    def _block_reason(rows: list[Any]) -> Optional[str]:
        if not rows:
            return None
        head = ", ".join(f"{r['strategy']} {r['ticker']} x{r['count']} ({r['reason'] or r['state']})" for r in rows[:3])
        return f"{len(rows)} order(s) with unknown outcome - {head}{' ...' if len(rows) > 3 else ''}; new exposure blocked until reconciled"

    # ---- 1. intent before request --------------------------------------------------------
    def reserve(self, *, strategy: str, ticker: str, side: str, count: int, limit_price: Any, action: str = "buy",
                tif: str = "immediate_or_cancel", event_key: Optional[str] = None, game_key: Optional[str] = None,
                dedupe_key: Optional[str] = None, parent_id: Optional[str] = None, budget: Optional[Budget] = None,
                max_cost_per_contract: Any = None, max_lock_attempts: int = 3, now: Optional[float] = None,
                detail: Optional[dict] = None) -> Reservation:
        """Record an intent and reserve its worst case, atomically; a refusal records nothing
        but an event. ``count`` shrinks to what the budget has room for (at least 1).

        A lock leg (``parent_id`` = the entry's intent) is exempt from the budgets and from
        the unknown-outcome block of *other* intents - it cuts exposure - but only up to the
        entry's verified fill minus the lock contracts already bought or still unresolved,
        and at most ``max_lock_attempts`` times, so it can never over-hedge."""
        now = self.clock() if now is None else float(now)
        try:
            count = int(count)
            limit = D(limit_price)
        except (TypeError, ValueError, InvalidOperation):
            return Reservation(False, "count/limit not numeric")
        if count <= 0:
            return Reservation(False, "count must be positive")
        if not limit.is_finite() or not ZERO < limit < 1:
            return Reservation(False, f"limit {limit_price!r} outside (0, 1)")
        per = D(max_cost_per_contract) if max_cost_per_contract is not None else None
        if per is not None and (not per.is_finite() or per <= 0):
            return Reservation(False, "max_cost_per_contract must be a positive finite number")
        day = self.day_of(now)

        def txn(c: sqlite3.Connection) -> Reservation:
            n = count
            if parent_id is None:
                unknown = self._unknown(c, now)
                if unknown:
                    why = self._block_reason(unknown) or "blocked"
                    self._event(c, now, None, "refused", strategy=strategy, ticker=ticker, count=count, reason=why)
                    return Reservation(False, why)
            else:
                parent = c.execute("SELECT * FROM intents WHERE intent_id = ?", (parent_id,)).fetchone()
                inv = _dec(parent["fill_count"]) if parent is not None and parent["state"] in (ACCEPTED, DONE) else None
                if inv is None:
                    why = "lock leg: the entry's fill is not verified" if parent is not None else "lock leg: unknown entry intent"
                    self._event(c, now, parent_id, "refused", strategy=strategy, ticker=ticker, count=count, reason=why)
                    return Reservation(False, why)
                locks = c.execute("SELECT * FROM intents WHERE parent_id = ?", (parent_id,)).fetchall()
                if len(locks) >= max_lock_attempts:
                    why = f"lock leg: {len(locks)} attempts already (max {max_lock_attempts})"
                    self._event(c, now, parent_id, "refused", strategy=strategy, ticker=ticker, count=count, reason=why)
                    return Reservation(False, why)
                committed = sum((self._contracts_committed(r) for r in locks), ZERO)
                room_ct = int((inv - committed).to_integral_value(ROUND_FLOOR))
                if room_ct <= 0:
                    why = f"lock leg: {committed} of {inv} contracts already hedged or unresolved"
                    self._event(c, now, parent_id, "refused", strategy=strategy, ticker=ticker, count=count, reason=why)
                    return Reservation(False, why)
                n = min(n, room_ct)
            if dedupe_key and c.execute("SELECT 1 FROM intents WHERE dedupe_key = ?", (dedupe_key,)).fetchone():
                self._event(c, now, None, "refused", strategy=strategy, ticker=ticker, dedupe_key=dedupe_key, reason="duplicate")
                return Reservation(False, f"duplicate signal ({dedupe_key}) already has an order")
            unit = per if per is not None else None
            room = None
            if budget is not None and parent_id is None:
                rooms = []
                if budget.daily is not None:
                    rooms.append(D(budget.daily) - self._sum(c, "strategy = ? AND day = ?", (strategy, day)))
                if budget.per_game is not None and game_key:
                    rooms.append(D(budget.per_game) - self._sum(c, "strategy = ? AND game_key = ?", (strategy, game_key)))
                if rooms:
                    room = min(rooms)
                    fit = n
                    while fit > 0 and (unit * fit if unit is not None else worst_cost(limit, fit)) > room:
                        fit -= 1
                    if fit <= 0:
                        why = f"budget: ${max(room, ZERO):.2f} left of the {strategy} cap (fees included)"
                        self._event(c, now, None, "refused", strategy=strategy, ticker=ticker, count=count, reason=why)
                        return Reservation(False, why, room=room)
                    n = fit
            max_cost = unit * n if unit is not None else worst_cost(limit, n)
            iid, coid = uuid.uuid4().hex, str(uuid.uuid4())
            c.execute("""INSERT INTO intents (intent_id, client_order_id, dedupe_key, strategy, parent_id, env, host, owner,
                created_ts, updated_ts, day, event_key, game_key, ticker, action, side, tif, count, limit_price, max_cost, state, detail)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (iid, coid, dedupe_key, strategy, parent_id, self.env, self.host, self.owner, now, now, day, event_key,
                       game_key, ticker, action, side, tif, n, str(limit), str(max_cost), PENDING,
                       json.dumps(detail or {}, default=str, sort_keys=True)))
            self._event(c, now, iid, "reserved", count=n, requested=count, limit=str(limit), max_cost=str(max_cost), room=_s(room))
            return Reservation(True, intent_id=iid, client_order_id=coid, count=n, max_cost=max_cost, room=room)

        return self._write(txn)

    @staticmethod
    def _contracts_committed(r: Any) -> Decimal:
        """Contracts a lock intent bought or may have bought."""
        if r["state"] == REJECTED:
            return ZERO
        if r["state"] in (ACCEPTED, DONE) and str(r["tif"]).lower() in IOC:
            n = _dec(r["fill_count"])
            if n is not None:
                return n
        return D(r["count"])

    # ---- 2. every answer recorded --------------------------------------------------------
    def accepted(self, intent_id: str, response: Any, now: Optional[float] = None, req_ts: Optional[float] = None) -> str:
        """The create-order answer: an order id makes it ``accepted`` (fills provisional
        until reconciled); a response without one is ``ambiguous``. Returns the new state."""
        now = self.clock() if now is None else float(now)
        od = (response or {}).get("order") if isinstance(response, dict) and isinstance(response.get("order"), dict) else (response or {})
        if not isinstance(od, dict) or not (od.get("order_id") or od.get("id")):
            self.ambiguous(intent_id, "create response carried no order id", now=now, req_ts=req_ts, response=response)
            return AMBIGUOUS
        oid = str(od.get("order_id") or od.get("id"))
        fill = _dec(od.get("fill_count") if od.get("fill_count") is not None else od.get("fill_count_fp"))
        raw = {k: od.get(k) for k in ("order_id", "client_order_id", "fill_count", "remaining_count", "average_fill_price",
                                      "average_fee_paid", "ts_ms", "status") if od.get(k) is not None}

        def txn(c: sqlite3.Connection) -> str:
            c.execute("UPDATE intents SET state=?, order_id=?, fill_count=?, req_ts=COALESCE(?, req_ts), resp_ts=?, updated_ts=?, reason=NULL WHERE intent_id=?",
                      (ACCEPTED, oid, _s(fill), req_ts, now, now, intent_id))
            self._event(c, now, intent_id, "accepted", **raw)
            return ACCEPTED

        return self._write(txn)

    def rejected(self, intent_id: str, reason: str, now: Optional[float] = None, **detail: Any) -> None:
        now = self.clock() if now is None else float(now)

        def txn(c: sqlite3.Connection) -> None:
            c.execute("UPDATE intents SET state=?, reason=?, updated_ts=?, fill_count=COALESCE(fill_count, '0') WHERE intent_id=?",
                      (REJECTED, str(reason)[:500], now, intent_id))
            self._event(c, now, intent_id, "rejected", reason=str(reason)[:500], **detail)

        self._write(txn)

    def ambiguous(self, intent_id: str, reason: str, now: Optional[float] = None, req_ts: Optional[float] = None,
                  hint: Optional[int] = None, **detail: Any) -> None:
        """The request may or may not have reached the exchange (timeout, 5xx, 409, a
        response without an order id). ``hint`` = the HTTP status, if any."""
        now = self.clock() if now is None else float(now)

        def txn(c: sqlite3.Connection) -> None:
            c.execute("UPDATE intents SET state=?, reason=?, hint=?, req_ts=COALESCE(?, req_ts), resp_ts=?, updated_ts=? WHERE intent_id=?",
                      (AMBIGUOUS, str(reason)[:500], hint, req_ts, now, now, intent_id))
            self._event(c, now, intent_id, "ambiguous", reason=str(reason)[:500], hint=hint, **detail)

        self._write(txn)

    def done(self, intent_id: str, fill_count: Decimal, fill_cost: Decimal, fees: Decimal, now: Optional[float] = None, **detail: Any) -> None:
        now = self.clock() if now is None else float(now)

        def txn(c: sqlite3.Connection) -> None:
            c.execute("UPDATE intents SET state=?, fill_count=?, fill_cost=?, fees=?, reconciled_ts=?, updated_ts=?, reason=NULL WHERE intent_id=?",
                      (DONE, str(fill_count), str(fill_cost), str(fees), now, now, intent_id))
            self._event(c, now, intent_id, "done", fill_count=str(fill_count), fill_cost=str(fill_cost), fees=str(fees), **detail)

        self._write(txn)

    def note(self, intent_id: Optional[str], kind: str, now: Optional[float] = None, **detail: Any) -> None:
        now = self.clock() if now is None else float(now)
        self._write(lambda c: self._event(c, now, intent_id, kind, **detail))

    # ---- reads ---------------------------------------------------------------------------
    def get(self, intent_id: str) -> Optional[dict]:
        with self._lock:
            r = self.conn.execute("SELECT * FROM intents WHERE intent_id = ?", (intent_id,)).fetchone()
        return dict(r) if r is not None else None

    def rows(self, states: Optional[tuple[str, ...]] = None, limit: int = 200) -> list[dict]:
        with self._lock:
            if states:
                q = f"SELECT * FROM intents WHERE state IN ({','.join('?' * len(states))}) ORDER BY created_ts DESC LIMIT ?"
                rows = self.conn.execute(q, (*states, limit)).fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM intents ORDER BY created_ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def events(self, intent_id: Optional[str] = None) -> list[dict]:
        with self._lock:
            if intent_id is None:
                rows = self.conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM events WHERE intent_id = ? ORDER BY seq", (intent_id,)).fetchall()
        return [dict(r) for r in rows]

    def status(self, now: Optional[float] = None) -> dict[str, Any]:
        """Counts by state, today's committed dollars per strategy, and the block, if any."""
        now = self.clock() if now is None else float(now)
        day = self.day_of(now)
        with self._lock:
            by_state = {r[0]: r[1] for r in self.conn.execute("SELECT state, COUNT(*) FROM intents GROUP BY state")}
            strategies = [r[0] for r in self.conn.execute("SELECT DISTINCT strategy FROM intents")]
            today = {s: str(self._sum(self.conn, "strategy = ? AND day = ?", (s, day))) for s in strategies}
        return {"env": self.env, "path": self.path, "day": day, "by_state": by_state, "committed_today": today,
                "blocked": self.blocked(now)}

    # ---- 4. reconciliation ---------------------------------------------------------------
    def reconcile(self, client: Any, now: Optional[float] = None) -> list[dict]:
        """Resolve every open intent against the exchange. Returns one record per intent
        looked at: ``{intent_id, before, after, note}``. Errors leave the intent as it was
        (an ambiguous one stays blocking) and are reported, never raised."""
        self._check_client(client)
        now = self.clock() if now is None else float(now)
        with self._lock:
            rows = self.conn.execute("SELECT * FROM intents WHERE state IN ('pending', 'ambiguous', 'accepted') ORDER BY created_ts").fetchall()
            unknown = {r["intent_id"] for r in self._unknown(self.conn, now)}
        out = []
        for r in rows:
            if r["state"] == PENDING and r["intent_id"] not in unknown:
                continue                           # still in flight in a live process
            if r["state"] == ACCEPTED and int(r["checks"] or 0) >= self.max_checks:
                continue                           # known order, exposure bounded: stop polling it
            since = float(r["resp_ts"] or r["updated_ts"] or r["created_ts"])
            if now - since < self.settle_s:
                continue                           # Kalshi's read side trails its writes
            try:
                note = self._reconcile_one(client, dict(r), now)
            except Exception as e:  # noqa: BLE001 - reported, the intent keeps blocking
                note = f"error: {e!r}"[:300]
                self._write(lambda c, iid=r["intent_id"], n=note: (
                    c.execute("UPDATE intents SET checks = checks + 1 WHERE intent_id = ?", (iid,)),
                    self._event(c, now, iid, "reconcile-error", error=n)))
            after = self.get(r["intent_id"]) or {}
            out.append({"intent_id": r["intent_id"], "strategy": r["strategy"], "ticker": r["ticker"], "before": r["state"],
                        "after": after.get("state"), "note": note})
        return out

    def _reconcile_one(self, client: Any, r: dict, now: float) -> str:
        iid = r["intent_id"]
        order: Optional[dict] = None
        if r["order_id"]:
            try:
                order = client.order(r["order_id"]) or None
            except Exception as e:  # noqa: BLE001
                if getattr(e, "status", None) == 404:
                    self._bump(iid, now, "order not visible yet (404)")
                    return "order not visible yet"
                raise
        else:
            rows, truncated = list_orders(client, ticker=r["ticker"], min_ts=int(float(r["created_ts"])) - 60)
            order = next((o for o in rows if str(o.get("client_order_id") or "") == r["client_order_id"]), None)
            if order is None:
                if truncated:
                    self._bump(iid, now, "listing truncated: not conclusive")
                    return "listing truncated"
                misses = int(r["misses"] or 0) + 1
                sent = float(r["req_ts"] or r["created_ts"])
                # A definitive client error (4xx other than 409 conflict / 429) is very likely a
                # refusal: one complete listing after the settle time proves it.
                definitive = r["hint"] is not None and 400 <= int(r["hint"]) < 500 and int(r["hint"]) not in (409, 429)
                window = self.settle_s if definitive else self.not_found_s
                if now - sent >= window and (misses >= 2 or definitive):
                    self._write(lambda c: c.execute("UPDATE intents SET misses=? WHERE intent_id=?", (misses, iid)))
                    self.rejected(iid, f"not on the exchange {now - sent:.0f}s after sending ({misses} complete listings): never accepted", now=now)
                    return "released: never accepted"
                self._write(lambda c: (c.execute("UPDATE intents SET misses=?, checks=checks+1 WHERE intent_id=?", (misses, iid)),
                                       self._event(c, now, iid, "not-found", misses=misses)))
                return f"not found yet ({misses})"
            oid = str(order.get("order_id") or order.get("id") or "")
            self._write(lambda c: (c.execute("UPDATE intents SET order_id=?, state=?, updated_ts=? WHERE intent_id=?", (oid, ACCEPTED, now, iid)),
                                   self._event(c, now, iid, "found", order_id=oid, status=order.get("status"))))
        if not order:
            self._bump(iid, now, "empty order read")
            return "empty order read"
        status = str(order.get("status") or "").lower()
        filled = _dec(order.get("fill_count_fp") if order.get("fill_count_fp") is not None else order.get("fill_count"))
        remaining = _dec(order.get("remaining_count_fp") if order.get("remaining_count_fp") is not None else order.get("remaining_count"))
        if status == "resting" or (status not in ("executed", "canceled", "cancelled", "expired") and (remaining or ZERO) > 0):
            self._write(lambda c: (c.execute("UPDATE intents SET fill_count=?, checks=checks+1 WHERE intent_id=?", (_s(filled), iid)),
                                   self._event(c, now, iid, "still-resting", status=status, remaining=_s(remaining))))
            return f"order {status or 'open'}: {remaining} resting"
        if filled is None:
            self._bump(iid, now, f"order row without a fill count (status {status!r})")
            return "no fill count"
        cost = sum((_dec(order.get(k)) or ZERO for k in ("taker_fill_cost_dollars", "maker_fill_cost_dollars")), ZERO)
        fees = sum((_dec(order.get(k)) or ZERO for k in ("taker_fees_dollars", "maker_fees_dollars")), ZERO)
        detail: dict[str, Any] = {"status": status, "source": "order"}
        if filled > 0 and cost == 0:
            # No cost fields: price the fills at the limit (the most an IOC buy can pay) and
            # the fee at its bound, so the budget errs high.
            cost, fees = D(r["limit_price"]) * filled, fee_bound(r["limit_price"], filled)
            detail["source"] = "order-without-cost-fields: limit x fills + fee bound"
        if filled > 0 and hasattr(client, "fills_v2"):
            try:
                fills = [f for f in (client.fills_v2(order_id=order.get("order_id") or r["order_id"]) or [])
                         if str(f.get("order_id") or "") in ("", str(order.get("order_id") or r["order_id"]))]
            except Exception as e:  # noqa: BLE001 - the order row stands on its own
                fills, detail["fills_error"] = [], repr(e)[:200]
            if fills:
                f_count = sum((_dec(f.get("count_fp") if f.get("count_fp") is not None else f.get("count")) or ZERO for f in fills), ZERO)
                f_fees = sum((_dec(f.get("fee_cost")) or ZERO for f in fills), ZERO)
                detail.update(fills=len(fills), fills_count=str(f_count), fills_fees=str(f_fees))
                if f_count != filled or f_fees != fees:
                    detail["fills_mismatch"] = True
                    fees = max(fees, f_fees)       # the budget keeps the larger of the two
            else:
                detail["fills"] = 0                # fills trail the order row; nothing to compare yet
        self.done(iid, filled, cost, fees, now=now, **detail)
        return f"done: {filled} filled, ${cost} + ${fees} fees"

    def _bump(self, iid: str, now: float, note: str) -> None:
        self._write(lambda c: (c.execute("UPDATE intents SET checks = checks + 1 WHERE intent_id = ?", (iid,)),
                               self._event(c, now, iid, "check", note=note)))


def list_orders(client: Any, **params: Any) -> tuple[list[dict], bool]:
    """(rows, truncated) of ``GET /portfolio/orders`` for ``params``, every page the client
    will walk. A client without :meth:`paged` (test fakes) is taken as complete."""
    params = {k: v for k, v in params.items() if v is not None}
    if hasattr(client, "paged"):
        return client.paged("/portfolio/orders", "orders", params)
    return list(client.orders_v2(**params) or []), False


def finite_positive(x: Any) -> bool:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v > 0

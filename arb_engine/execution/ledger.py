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
4. **One account per ledger.** Each intent records a fingerprint of the API key that sent
   it and of the account behind it (SHA-256 of the key id and of the account's
   ``GET /communications/id`` - never the identifiers themselves, and never a secret). A
   ledger is bound to the first account it sees and refuses a client of another one; a key
   rotated on the same account is recognised by its account and mapped (``keys`` table).
   Reconciliation leaves another account's intents alone, and releases a never-seen order
   only when the reconciling client is provably the account that sent it - an order absent
   from *another* account's listing proves nothing.
5. **Lock legs are hedges, not exemptions.** A lock leg (``reserve(parent_id=...)``) skips
   the budgets and the block of *unrelated* unknown orders, so the exemption is granted only
   to a proven hedge. The two layers check different things:

   * the caller (``strategy/lagexec.LagExecutor.buy_lock``) checks what only market data and
     the settlement registry can show: the quote is Kalshi's, for the entry's market, fresh,
     priced at or under its ask; the lock contract's settlement identity (outcome paid on,
     side, tie payout, whether the market can tie) comes from the registry; and the exchange
     still shows the entry's contracts in the account (``GET /portfolio/positions``);
   * the ledger checks, atomically with the reservation, everything its records can prove:
     the entry is this account's verified purchase with a recorded settlement identity; the
     lock buys the same market (event key and Kalshi event ticker), pays on the *other*
     outcome (never the entry's own contract or outcome), and the pair's payoffs are
     complementary (tie payouts known and summing to at least $1 where the market can tie);
     the quote the caller priced it on is within ``lock_quote_max_age_s``; no *related*
     order (the entry, its locks, anything on either ticker) has an unknown outcome; and the
     remaining inventory - the entry's fill minus exits (sells of its contract and buys of
     the opposite side on its market since, by any strategy) minus every earlier lock leg
     (unresolved ones at full count) - bounds the count.

6. **Reconciliation** (:meth:`reconcile`) finds each unresolved intent on the exchange by
   its ``client_order_id`` (``GET /portfolio/orders?ticker=&min_ts=``, every page; Kalshi
   has no client_order_id filter) and reads each accepted order's final fills and fees
   (``GET /portfolio/orders/{id}``: ``fill_count_fp``, ``taker|maker_fill_cost_dollars``,
   ``taker|maker_fees_dollars``; ``GET /portfolio/fills?order_id=`` cross-checks), replacing
   the reserved worst case with what was paid. An intent that complete listings still do not
   show ``not_found_s`` after it was sent was never accepted: its reservation is released.
   An incomplete (truncated) listing proves nothing and never releases anything.
6. **Only evidence releases a reservation** (:func:`order_evidence`, :func:`fills_evidence`).
   An order is final only when its row shows a terminal status (executed / canceled), an
   explicit remaining quantity of zero and a fill count within what was ordered; anything
   less - no status, an unknown one, no remaining quantity - leaves the intent open with its
   reservation. An absent fee field is *unknown*, never zero (an explicit ``"0.0000"`` is
   zero): until the order row or a complete, de-duplicated fills listing states the fees, an
   IOC whose fills are final keeps ``fill_count x limit`` plus :func:`fee_bound` reserved and
   is reconciled again later. A create answer is an IOC's final fill count only with an
   explicit zero remaining quantity, or when every contract ordered filled.

   **Fill evidence only grows.** Filled contracts are cumulative on the exchange, so every
   count an answer shows - the create answer, an order row, a fills listing (a lower bound
   even when truncated) - is kept (``fill_seen``, and where it came from) and survives
   restarts. No read may book less than an earlier answer showed: a row reporting fewer
   fills (none, say, after a create answer reported one) is a **contradiction**, like an
   order row and fills listing that disagree (more fills listed than the row reports, one
   fill id with two contents, a fill or row of this order on another market or book side). A
   contradicted intent (``fill_state = contradicted``) releases nothing: an IOC counts at
   its whole worst case, no lock leg is sized on it, it is read again, and the executor
   alerts on it.

   **Finishing needs the fills listing.** An intent is ``done`` - its reservation replaced by
   what was paid - only when a final order row and a *complete, correctly scoped* fills
   listing (``GET /portfolio/fills?order_id=``: every page read, no error, only this order's
   rows, each on the intent's market and book side - bid / ask of the YES leg, as the order
   was sent; a YES sell's fills say ``side: no`` - every row with a fill id and a count,
   duplicates identical) agree on the fill count, and that count is no lower than any
   earlier answer showed. A listing that is missing (a client that cannot read fills),
   failed, truncated or scoped wrongly proves nothing: the intent stays open with what its
   evidence supports - an IOC's fills counted at the limit plus the fee bound only when an
   exchange answer already stated them final, else its whole worst case - and is read again.
   That holds for an order that filled nothing too: a zero-fill cancellation is done once
   the complete listing is empty. A count confirmed this way is ``verified``; only a verified
   entry (or an operator-``corrected`` one) can be hedged by a lock leg.
7. **Exchange corrections are explicit.** The exchange can bust a trade, and then an order
   truly holds fewer contracts than it once reported. The ledger never infers that from a
   lower count: the intent stays contradicted until a person checks the exchange and calls
   :meth:`OrderLedger.accept_correction` (``kalshi correct --intent-id X --reason ...
   --confirm``). That re-reads the order and its fills and books them only when both are
   final, complete and agree, recording the earlier evidence it overrides and the reason. A
   manual :meth:`OrderLedger.release` (never accepted) refuses an intent that showed fills.

Budgets are dollars **including fees**. An open intent counts at its worst case
(``count x limit`` + :func:`fee_bound` at the market's own fee multiplier), an accepted
immediate-or-cancel order whose fills an exchange answer stated final at those fills (never
fewer than any answer showed) at the limit plus that bound, a contradicted one at its whole
worst case again, a finished one at its actual fill cost plus actual fees. A day is the
local calendar date at reservation. Every time the ledger is handed (a decision time, a
request time, a quote time) must be a finite number: a NaN compares false with everything
and would pass every age and staleness check it meets.

The fee multiplier is the series' ``fee_multiplier`` as the exchange the order goes to
reports it (:class:`FeeMultipliers`), never an assumed 1: every Kalshi series today has 1,
0.5 or 0 (14,394 series on 2026-09-26), but a series with more would otherwise pay more
than was reserved. An intent whose multiplier is unknown is not reserved (so not sent).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Iterable, Iterator, Optional

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
# What an intent's fill count rests on (``fill_state``): an exchange answer not yet confirmed
# by a final order row and a complete fills listing; confirmed by both; answers that disagree
# (a lower count than an earlier answer showed, or row and fills apart); or an operator's
# accepted exchange correction. Only verified and corrected fills can be hedged.
PROVISIONAL, VERIFIED, CONTRADICTED, CORRECTED = "provisional", "verified", "contradicted", "corrected"
HEDGEABLE = (VERIFIED, CORRECTED)
ZERO = Decimal("0")
HALF = Decimal("0.5")
IOC = ("immediate_or_cancel", "fill_or_kill", "ioc", "fok")
# Order statuses after which nothing more can fill (Kalshi: resting | canceled | executed).
TERMINAL_STATUSES = ("executed", "canceled", "cancelled", "expired")
_KEEP = object()   # OrderLedger._hold: leave the booked fill count as it is

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
  misses INTEGER NOT NULL DEFAULT 0, hint INTEGER, reason TEXT, detail TEXT, fee_mult TEXT, key_fp TEXT, account_fp TEXT,
  settlement TEXT, fill_seen TEXT, fill_source TEXT, fill_state TEXT)""",
    """CREATE INDEX IF NOT EXISTS intents_open ON intents (state)""",
    """CREATE INDEX IF NOT EXISTS intents_day ON intents (strategy, day)""",
    """CREATE INDEX IF NOT EXISTS intents_game ON intents (strategy, game_key)""",
    """CREATE INDEX IF NOT EXISTS intents_parent ON intents (parent_id)""",
    """CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
  intent_id TEXT, kind TEXT NOT NULL, detail TEXT)""",
    """CREATE TABLE IF NOT EXISTS keys (key_fp TEXT PRIMARY KEY, account_fp TEXT NOT NULL, first_ts REAL, last_ts REAL)""",
)


# Columns added after the first ledgers were written (added in place on open).
_MIGRATIONS = (("fee_mult", "TEXT"), ("key_fp", "TEXT"), ("account_fp", "TEXT"), ("settlement", "TEXT"),
               ("fill_seen", "TEXT"), ("fill_source", "TEXT"), ("fill_state", "TEXT"))


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


def fee_bound(limit: Any, count: Any, multiplier: Any) -> Decimal:
    """The most Kalshi can charge in taker fees for ``count`` contracts bought at prices
    no higher than ``limit`` on a series with fee multiplier ``multiplier``: ``count`` x the
    one-contract fee at the dearest-fee price ``min(limit, 0.5)`` (``p(1-p)`` peaks at 0.5),
    each rounded up to the cent. Rounding per one contract bounds any split into fills and
    either rounding rule: ``ceil(n x f) <= n x ceil(f)`` on the cent grid, and a centicent
    rounding is never above a cent one. A multiplier below 1 (a half-fee or fee-free
    series) still counts as 1; one above 1 counts in full. There is no default: the caller
    must know the market's multiplier (:class:`FeeMultipliers`)."""
    m = D(multiplier)
    if not m.is_finite() or m < 0:
        raise ValueError(f"fee multiplier {multiplier!r} must be a finite number >= 0")
    p = min(D(limit), HALF)
    return KalshiFees(multiplier=max(m, Decimal(1))).fee(p, 1, "taker") * D(count)


def worst_cost(limit: Any, count: Any, multiplier: Any) -> Decimal:
    """``count x limit`` plus :func:`fee_bound`: the most a buy of ``count`` at ``limit`` costs."""
    return D(limit) * D(count) + fee_bound(limit, count, multiplier)


def series_ticker(market_ticker: str) -> str:
    """The series of a Kalshi market ticker (``KXNFLGAME-26OCT01PITCLE-PIT`` -> ``KXNFLGAME``)."""
    return str(market_ticker or "").split("-", 1)[0].split("#", 1)[0].upper()


def quote_fee_multiplier(fee_params: Any) -> Optional[Decimal]:
    """The multiplier a quote's fee parameters *state*, or None when they do not (missing,
    or the adapter only assumed one because the series could not be read)."""
    fp = dict(fee_params or {}) if isinstance(fee_params, dict) else {}
    if not fp or fp.get("fee_multiplier_assumed") or ("fee_multiplier" not in fp and "fee_type" not in fp):
        return None
    if str(fp.get("fee_type") or "").lower() in ("none", "no_fees", "flat_zero"):
        return Decimal(0)
    if "fee_multiplier" not in fp:
        return None
    v = _dec(fp.get("fee_multiplier"))
    return v if v is not None and v >= 0 else None


class FeeMultipliers:
    """Each series' fee multiplier as the exchange the orders go to reports it
    (``GET /series/{ticker}``), cached for ``ttl_s``. ``resolve`` combines it with what a
    quote states and keeps the larger: the reservation must not come in under the fee."""

    def __init__(self, client: Any, ttl_s: float = 3600.0, clock: Any = time.time) -> None:
        self.client, self.ttl_s, self.clock = client, float(ttl_s), clock
        self._cache: dict[str, tuple[float, Optional[Decimal], Optional[str]]] = {}

    def lookup(self, ticker: str) -> tuple[Optional[Decimal], Optional[str]]:
        """(multiplier, None) or (None, why it is unknown)."""
        series = series_ticker(ticker)
        now = self.clock()
        hit = self._cache.get(series)
        if hit is not None and now - hit[0] < self.ttl_s and hit[1] is not None:
            return hit[1], None
        fn = getattr(self.client, "series", None)
        if not callable(fn):
            return None, "this client cannot read series"
        try:
            ser = fn(series) or {}
        except Exception as e:  # noqa: BLE001
            return None, f"GET /series/{series} failed ({e!r})"[:200]
        mult = quote_fee_multiplier({k: ser.get(k) for k in ("fee_type", "fee_multiplier") if k in ser})
        if mult is None:
            return None, f"series {series} states no fee_multiplier"
        self._cache[series] = (now, mult, None)
        return mult, None

    def resolve(self, ticker: str, stated: Any = None) -> tuple[Optional[Decimal], Optional[str]]:
        """The larger of the exchange's multiplier and the one a quote states; (None, why)
        when neither is known."""
        looked, why = self.lookup(ticker)
        stated_d = quote_fee_multiplier({"fee_multiplier": stated}) if stated is not None else None
        known = [x for x in (looked, stated_d) if x is not None]
        if not known:
            return None, f"fee multiplier of {series_ticker(ticker)} unknown: {why}"
        return max(known), None


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


def _finite_time(x: Any) -> Optional[float]:
    """A timestamp (seconds) as a finite float; None for anything else - a NaN, an infinity,
    a bool, a string, None. A NaN compares false with everything, so a NaN time would pass
    every age check it meets (``now - nan > max_age`` is False)."""
    if isinstance(x, bool) or not isinstance(x, (int, float, Decimal)):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


def book_side(action: Any, side: Any) -> str:
    """The V2 book side an order was sent on (``bid`` / ``ask`` of the YES leg): buy YES and
    sell NO bid, sell YES and buy NO ask - the mapping of ``venues.kalshi.build_order_payload``.
    Order rows and fills carry it as ``book_side``; their legacy ``side`` / ``outcome_side``
    do not name the contract (a YES sell's fills say ``side: no``, seen on demo 2026-09-27)."""
    return "bid" if (str(action).lower(), str(side).lower()) in {("buy", "yes"), ("sell", "no")} else "ask"


def _scope_problem(row: dict, ticker: Any, bside: Optional[str]) -> Optional[str]:
    """Why an order row or fill that names this order is not on the intent's market / book
    side, or None. Fields a row leaves out are not held against it."""
    t = row.get("ticker") or row.get("market_ticker")
    if ticker and t and str(t) != str(ticker):
        return f"on {t}, not {ticker}"
    b = str(row.get("book_side") or "").lower()
    if bside and b and b != bside:
        return f"book side {b}, not {bside}"
    return None


def fingerprint(kind: str, env: str, value: str) -> str:
    """A one-way fingerprint of an identifier (an API key id, a communications id): what the
    ledger stores instead of the identifier itself."""
    return f"{kind}:" + hashlib.sha256(f"kalshi|{env}|{kind}|{value}".encode()).hexdigest()[:32]


@dataclass(frozen=True)
class Identity:
    """Who a client trades as: fingerprints of its API key and of its account, and why the
    account is unknown when it is."""
    key_fp: Optional[str] = None
    account_fp: Optional[str] = None
    error: Optional[str] = None


def client_identity(client: Any, env: Optional[str] = None) -> Identity:
    """The client's key and account fingerprints. The account comes from ``GET
    /communications/id`` (one id per account, whatever key signs); when that read fails the
    account is unknown and only the key identifies the sender."""
    env = str(env or getattr(client, "env", "") or "").lower()
    api_key = getattr(client, "api_key", None)
    key_fp = fingerprint("key", env, str(api_key)) if api_key else None
    fn = getattr(client, "communications_id", None)
    if not callable(fn):
        return Identity(key_fp, None, "this client cannot read its account id")
    try:
        cid = fn()
    except Exception as e:  # noqa: BLE001
        return Identity(key_fp, None, f"GET /communications/id failed ({type(e).__name__})")
    if not cid:
        return Identity(key_fp, None, "GET /communications/id returned no id")
    return Identity(key_fp, fingerprint("account", env, str(cid)), None)


def kalshi_event_ticker(ticker: Any) -> Optional[str]:
    """The Kalshi event a market ticker belongs to (``KXNFLGAME-26SEP21DENKC-KC`` ->
    ``KXNFLGAME-26SEP21DENKC``); None for anything that is not ``SERIES-EVENT-MARKET``."""
    parts = str(ticker or "").split("#", 1)[0].upper().split("-")
    if len(parts) < 3 or not all(parts):
        return None
    return "-".join(parts[:-1])


def _named_outcomes(event_key: str) -> Optional[set[str]]:
    """The two outcomes a moneyline key names (``nfl:DEN|KC:2026-09-21``); None for lines."""
    ek = str(event_key or "")
    if ":spread:" in ek or ":total:" in ek:
        return None
    parts = ek.split(":")
    if len(parts) < 3 or "|" not in parts[1]:
        return None
    a, _, b = parts[1].partition("|")
    return {a, b} if a and b and a != b else None


def settlement_identity(venue: str, event_key: str, outcome: str, side: str, tie_payout: Any, can_tie: bool) -> dict:
    """What a contract pays, as the order ledger records it: the book (``venue``), the market
    (``event_key``), the outcome it pays $1 on (normalized: a NO row names the team it pays
    on), its ``side``, what it pays on a tie / push (``tie_payout``, None when unknown) and
    whether the market can end that way at all (``can_tie``)."""
    return {"venue": str(venue).lower(), "event_key": str(event_key), "outcome": str(outcome), "side": str(side).lower(),
            "tie_payout": None if tie_payout is None else str(tie_payout), "can_tie": bool(can_tie)}


def _settlement(sd: Any) -> tuple[Optional[dict], Optional[str]]:
    """A validated settlement identity (tie payout as Decimal), or (None, why)."""
    if not isinstance(sd, dict):
        return None, "no settlement identity"
    venue, ek, oc, side = (str(sd.get(k) or "") for k in ("venue", "event_key", "outcome", "side"))
    if not venue or not ek or not oc or side.lower() not in ("yes", "no"):
        return None, "incomplete settlement identity (venue, event_key, outcome, side)"
    if not isinstance(sd.get("can_tie"), bool):
        return None, "the settlement identity does not say whether the market can tie"
    tie = sd.get("tie_payout")
    t = _dec(tie) if tie is not None else None
    if tie is not None and (t is None or t < 0 or t > 1):
        return None, f"tie payout {tie!r} outside [0, 1]"
    return {"venue": venue.lower(), "event_key": ek, "outcome": oc, "side": side.lower(), "tie_payout": t, "can_tie": sd["can_tie"]}, None


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
                 pending_stale_s: float = 300.0, not_found_s: float = 30.0, settle_s: float = 2.0, max_checks: int = 120,
                 owner_alive: Any = None, lock_quote_max_age_s: float = 10.0) -> None:
        self.path, self.env, self.host, self.clock = path, str(env).lower(), str(host), clock
        self.identity = Identity(error="not bound to a client")   # set by bind() / for_client()
        # A lock leg must be priced on a quote no older than this (the lock book's own fresh_s).
        self.lock_quote_max_age_s = float(lock_quote_max_age_s)
        self._owner_alive = owner_alive                            # owner -> bool | None; tests and other hosts
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
                have = {r[1] for r in c.execute("PRAGMA table_info(intents)")}
                for col, typ in _MIGRATIONS:
                    if col not in have:            # a ledger written by an older version of this module
                        c.execute(f"ALTER TABLE intents ADD COLUMN {col} {typ}")
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
        """The ledger of ``client``'s environment, bound to its account; refuses a host of
        another environment and a client of another account."""
        env = str(getattr(client, "env", "") or "").lower()
        base = str(getattr(client, "base_url", "") or "")
        problem = env_host_problem(env, base)
        if problem:
            raise LedgerError(problem)
        led = cls(path or default_path(env), env, base, **kw)
        try:
            led.bind(client_identity(client, env))
        except LedgerError:
            led.close()
            raise
        return led

    # ---- identity ------------------------------------------------------------------------
    def _mapped(self, c: sqlite3.Connection, key_fp: Optional[str]) -> Optional[str]:
        if not key_fp:
            return None
        row = c.execute("SELECT account_fp FROM keys WHERE key_fp = ?", (key_fp,)).fetchone()
        return row["account_fp"] if row is not None else None

    def bind(self, identity: Identity, now: Optional[float] = None) -> Identity:
        """Bind the ledger to ``identity``'s account (the first account seen owns the file).
        A different account raises ``LedgerError``; a new key of the owning account is mapped
        to it (key rotation). Returns the effective identity (the account filled in from the
        key map when the account read failed)."""
        now = self._time(now, "now")

        def txn(c: sqlite3.Connection) -> Identity:
            owner_row = c.execute("SELECT value FROM meta WHERE key='account_fp'").fetchone()
            owner = owner_row["value"] if owner_row is not None else None
            mapped = self._mapped(c, identity.key_fp)
            if identity.account_fp and mapped and mapped != identity.account_fp:
                raise LedgerError("this API key was recorded for another Kalshi account in this ledger")
            acct = identity.account_fp or mapped
            if acct and owner and acct != owner:
                raise LedgerError(f"{self.path} belongs to another Kalshi account; point ARB_ORDER_LEDGER_DIR at a separate "
                                  "directory for this one (budgets and reconciliation never mix accounts)")
            if acct and not owner:
                c.execute("INSERT INTO meta VALUES ('account_fp', ?)", (acct,))
            if acct and identity.key_fp:
                c.execute("INSERT INTO keys VALUES (?,?,?,?) ON CONFLICT(key_fp) DO UPDATE SET last_ts = excluded.last_ts",
                          (identity.key_fp, acct, now, now))
            eff = Identity(identity.key_fp, acct, None if acct else identity.error)
            self._event(c, now, None, "bind", key=identity.key_fp and identity.key_fp[:12], account=acct and acct[:12],
                        account_known=bool(acct), note=None if acct else identity.error)
            return eff

        eff = self._write(txn)
        self.identity = eff
        return eff

    def _identity_for(self, client: Any) -> Identity:
        """The reconciling client's identity: the bound one when it is the same key, else read
        (and completed from the key map), never bound."""
        env_key = getattr(client, "api_key", None)
        if self.identity.key_fp and env_key and fingerprint("key", self.env, str(env_key)) == self.identity.key_fp:
            return self.identity
        ident = client_identity(client, self.env)
        with self._lock:
            mapped = self._mapped(self.conn, ident.key_fp)
        return Identity(ident.key_fp, ident.account_fp or mapped, ident.error)

    def _relation(self, row: Any, ident: Identity) -> str:
        """``same`` (provably the account that sent it), ``other`` (provably not) or
        ``unknown``."""
        with self._lock:
            r_acct = row["account_fp"] or self._mapped(self.conn, row["key_fp"])
        if r_acct and ident.account_fp:
            return "same" if r_acct == ident.account_fp else "other"
        if row["key_fp"] and ident.key_fp and row["key_fp"] == ident.key_fp:
            return "same"                                  # one key never spans two accounts
        return "unknown"

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

    def _time(self, t: Any, what: str = "time") -> float:
        """``t`` (the ledger's clock when None) as a finite timestamp; ``LedgerError``
        otherwise - the ledger's boundary for every time it is handed."""
        raw = self.clock() if t is None else t
        v = _finite_time(raw)
        if v is None:
            raise LedgerError(f"{what} {raw!r} is not a finite timestamp")
        return v

    # ---- exposure ------------------------------------------------------------------------
    @staticmethod
    def exposure_of(row: Any) -> Decimal:
        """What one intent commits, fees included (see the module docstring)."""
        st = row["state"]
        if st == REJECTED:
            return ZERO
        if st == DONE:
            return (_dec(row["fill_cost"]) or ZERO) + (_dec(row["fees"]) or ZERO)
        keys = row.keys()
        if "fill_state" in keys and row["fill_state"] == CONTRADICTED:
            return _dec(row["max_cost"]) or ZERO               # answers disagree: the whole worst case
        if st == ACCEPTED and str(row["tif"]).lower() in IOC:
            n = _dec(row["fill_count"])
            m = _dec(row["fee_mult"]) if "fee_mult" in keys else None
            seen = _dec(row["fill_seen"]) if "fill_seen" in keys else None
            if n is not None and m is not None:
                if seen is not None and seen > n:
                    n = seen                                   # never fewer than an answer showed
                return min(worst_cost(row["limit_price"], n, m), _dec(row["max_cost"]) or ZERO)
        return _dec(row["max_cost"]) or ZERO

    def _sum(self, c: sqlite3.Connection, where: str, args: tuple) -> Decimal:
        rows = c.execute("SELECT state, tif, fill_count, fill_cost, fees, limit_price, max_cost, fee_mult, fill_seen, fill_state "
                         f"FROM intents WHERE state != 'rejected' AND {where}", args).fetchall()
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
        if self._owner_alive is not None:
            alive = self._owner_alive(owner)
            if alive is not None:
                return not alive
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
        now = self._time(now, "now")
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
                max_cost_per_contract: Any = None, fee_multiplier: Any = None, max_lock_attempts: int = 3,
                now: Optional[float] = None, detail: Optional[dict] = None, settlement: Optional[dict] = None,
                quote_ts: Optional[float] = None) -> Reservation:
        """Record an intent and reserve its worst case, atomically; a refusal records nothing
        but an event. ``count`` shrinks to what the budget has room for (at least 1).
        ``settlement`` (:func:`settlement_identity`) records what the contract pays; an entry
        without one can never be hedged under the lock exemption.

        A lock leg (``parent_id`` = the entry's intent) is exempt from the budgets and from
        the unknown-outcome block of *unrelated* intents - it cuts exposure - only once
        :meth:`_lock_check` has proven it a hedge of that entry (see the module docstring:
        same market, the other outcome, complementary payoffs, a fresh quote ``quote_ts``, no
        related unknown order), and only for the entry's remaining inventory (fill minus
        exits minus earlier lock legs), at most ``max_lock_attempts`` times.

        Times are checked here, at the boundary: ``now`` and ``quote_ts`` must be finite
        numbers (a NaN quote time used to pass the freshness check)."""
        try:
            now = self._time(now, "decision time")
        except LedgerError as e:
            return Reservation(False, str(e))
        qts: Optional[float] = None
        if quote_ts is not None:
            qts = _finite_time(quote_ts)
            if qts is None:
                return Reservation(False, f"quote time {quote_ts!r} is not a finite timestamp")
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
        mult = _dec(fee_multiplier) if fee_multiplier is not None else None
        if fee_multiplier is not None and (mult is None or mult < 0):
            return Reservation(False, f"fee multiplier {fee_multiplier!r} must be a finite number >= 0")
        if per is None and mult is None:
            # Without the market's multiplier the fee - and so the worst case - is not bounded.
            return Reservation(False, "fee multiplier of the market unknown: the fee cannot be bounded")
        sd = None
        if settlement is not None:
            sd, why = _settlement(settlement)
            if sd is None:
                return Reservation(False, f"settlement identity rejected: {why}")
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
                room_ct, why, inventory = self._lock_check(c, parent_id, ticker, side, action, settlement, qts, now, max_lock_attempts)
                if room_ct is None:
                    self._event(c, now, parent_id, "refused", strategy=strategy, ticker=ticker, count=count, reason=why, **inventory)
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
                    while fit > 0 and (unit * fit if unit is not None else worst_cost(limit, fit, mult)) > room:
                        fit -= 1
                    if fit <= 0:
                        why = f"budget: ${max(room, ZERO):.2f} left of the {strategy} cap (fees included)"
                        self._event(c, now, None, "refused", strategy=strategy, ticker=ticker, count=count, reason=why)
                        return Reservation(False, why, room=room)
                    n = fit
            max_cost = unit * n if unit is not None else worst_cost(limit, n, mult)
            iid, coid = uuid.uuid4().hex, str(uuid.uuid4())
            c.execute("""INSERT INTO intents (intent_id, client_order_id, dedupe_key, strategy, parent_id, env, host, owner,
                created_ts, updated_ts, day, event_key, game_key, ticker, action, side, tif, count, limit_price, max_cost, state, detail,
                fee_mult, key_fp, account_fp, settlement)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (iid, coid, dedupe_key, strategy, parent_id, self.env, self.host, self.owner, now, now, day, event_key,
                       game_key, ticker, action, side, tif, n, str(limit), str(max_cost), PENDING,
                       json.dumps(detail or {}, default=str, sort_keys=True), _s(mult), self.identity.key_fp, self.identity.account_fp,
                       json.dumps({**sd, "tie_payout": _s(sd["tie_payout"])}, sort_keys=True) if sd is not None else None))
            self._event(c, now, iid, "reserved", count=n, requested=count, limit=str(limit), max_cost=str(max_cost), room=_s(room),
                        fee_multiplier=_s(mult))
            return Reservation(True, intent_id=iid, client_order_id=coid, count=n, max_cost=max_cost, room=room)

        return self._write(txn)

    def _lock_check(self, c: sqlite3.Connection, parent_id: str, ticker: str, side: str, action: str, settlement: Any,
                    quote_ts: Optional[float], now: float, max_attempts: int) -> tuple[Optional[int], Optional[str], dict]:
        """(contracts the lock leg may buy, None, inventory) when it is a proven hedge of the
        entry ``parent_id``; (None, why, inventory) otherwise. Runs inside the reservation's
        transaction, so the inventory it counts cannot change before the intent is written."""
        def no(why: str, **info: Any) -> tuple[None, str, dict]:
            return None, f"lock leg: {why}", info

        parent = c.execute("SELECT * FROM intents WHERE intent_id = ?", (parent_id,)).fetchone()
        if parent is None:
            return no("unknown entry intent")
        if parent["parent_id"]:
            return no("the entry is itself a lock leg")
        if str(parent["action"]).lower() != "buy":
            return no("the entry is not a purchase")
        if str(action).lower() != "buy":
            return no("a lock leg buys the other outcome; selling is an exit, not a hedge")
        # Only a fill that reconciliation confirmed (a final order row and a complete fills
        # listing that agree, never below an earlier answer) - or that an operator corrected -
        # can be hedged: a create answer alone, or answers that disagree, are not inventory.
        fstate = parent["fill_state"]
        if fstate == CONTRADICTED:
            return no(f"the entry's fill evidence is contradicted ({parent['reason'] or 'the exchange answers disagree'}): "
                      "nothing is hedged on it until it is resolved")
        fill = _dec(parent["fill_count"]) if parent["state"] in (ACCEPTED, DONE) and fstate in HEDGEABLE else None
        if fill is None:
            said = parent["fill_count"] if parent["fill_count"] is not None else parent["fill_seen"]
            return no("the entry's fill is not verified" + (f" (an answer said {said}; its order row and a complete fills listing have not confirmed it yet)"
                                                           if said is not None else ""))
        if self._relation(parent, self.identity) != "same":
            return no("the entry was not sent by this account (or its account cannot be verified): its contracts are not this client's to hedge")
        psd, why = _settlement(json.loads(parent["settlement"]) if parent["settlement"] else None)
        if psd is None:
            return no(f"the entry's settlement identity is unknown ({why}): it cannot be hedged under the exemption")
        lsd, why = _settlement(settlement)
        if lsd is None:
            return no(f"unknown settlement identity of the lock contract ({why})")
        # The same market: same book, same event key, same Kalshi event.
        if lsd["venue"] != psd["venue"]:
            return no(f"unrelated book: {lsd['venue']} is not the entry's {psd['venue']}")
        if lsd["event_key"] != psd["event_key"] or (parent["event_key"] and parent["event_key"] != psd["event_key"]):
            return no(f"unrelated market: {lsd['event_key']} is not the entry's {psd['event_key']}")
        pev, lev = kalshi_event_ticker(parent["ticker"]), kalshi_event_ticker(ticker)
        if pev is None or lev is None or pev != lev:
            return no(f"unrelated market: {ticker} is not in the entry's Kalshi event {pev or parent['ticker']}")
        # The other outcome, never more of the entry's own.
        if ticker == parent["ticker"] and str(side).lower() == str(parent["side"]).lower():
            return no("same-side addition: the lock buys the entry's own contract")
        if lsd["outcome"] == psd["outcome"]:
            return no(f"same-side addition: the lock pays on the entry's own outcome {psd['outcome']}")
        named = _named_outcomes(psd["event_key"])
        if named is not None and not {psd["outcome"], lsd["outcome"]} <= named:
            return no(f"outcome not of this market: {sorted({psd['outcome'], lsd['outcome']} - named)}")
        # Complementary payoffs: $1 when either outcome wins; on a tie / push, the two tie payouts.
        if psd["can_tie"] or lsd["can_tie"]:
            if psd["tie_payout"] is None or lsd["tie_payout"] is None:
                return no("unknown settlement identity: what the pair pays on a tie is unknown")
            pays = psd["tie_payout"] + lsd["tie_payout"]
            if pays < Decimal(1) - Decimal("1e-9"):
                return no(f"not complementary: the pair pays ${pays} on a tie, less than the $1 it pays otherwise")
        # A fresh quote (a finite time: a NaN would pass both comparisons below).
        if quote_ts is None:
            return no("no quote time: the price it was decided on cannot be shown fresh")
        qt = _finite_time(quote_ts)
        if qt is None:
            return no(f"quote time {quote_ts!r} is not a finite timestamp")
        age = now - qt
        if age > self.lock_quote_max_age_s:
            return no(f"stale quote: {age:.1f}s old (max {self.lock_quote_max_age_s:g}s)")
        if age < -2.0:
            return no(f"quote time {-age:.1f}s in the future")
        # No related order whose outcome is unknown (an unrelated one does not stop a hedge).
        related = [r for r in self._unknown(c, now)
                   if r["intent_id"] == parent_id or r["parent_id"] == parent_id or r["ticker"] in (parent["ticker"], ticker)]
        if related:
            return no(f"{len(related)} related order(s) with unknown outcome ({related[0]['strategy']} {related[0]['ticker']}): the inventory cannot be verified")
        locks = c.execute("SELECT * FROM intents WHERE parent_id = ?", (parent_id,)).fetchall()
        if len(locks) >= max_attempts:
            return no(f"{len(locks)} attempts already (max {max_attempts})")
        # Remaining inventory: fill - exits since the entry (any strategy) - every lock leg.
        exits_rows = c.execute(
            """SELECT * FROM intents WHERE ticker = ? AND intent_id != ? AND state != 'rejected' AND created_ts >= ?
               AND ((action = 'sell' AND side = ?) OR (action = 'buy' AND side != ? AND COALESCE(parent_id, '') != ?))""",
            (parent["ticker"], parent_id, parent["created_ts"], parent["side"], parent["side"], parent_id)).fetchall()
        exits = sum((self._contracts_committed(r) for r in exits_rows), ZERO)
        hedged = sum((self._contracts_committed(r) for r in locks), ZERO)
        info = {"fill": str(fill), "exits": str(exits), "hedged": str(hedged)}
        room = int((fill - exits - hedged).to_integral_value(ROUND_FLOOR))
        if room <= 0:
            unsettled = [r for r in list(exits_rows) + list(locks) if r["state"] != REJECTED and not self._settled(r)]
            if unsettled:
                # Not final: legs whose fills are not verified yet count in full, so there may be
                # room once they are reconciled (the lock book keeps watching).
                return None, (f"lock leg: waiting - {len(unsettled)} earlier lock leg(s) or exit(s) not reconciled yet count in full "
                              f"(filled {fill}, exited {exits}, hedged or unresolved {hedged})"), info
            return None, f"lock leg: nothing left to hedge (filled {fill}, exited {exits}, hedged or unresolved {hedged})", info
        return room, None, info

    @staticmethod
    def _settled(r: Any) -> bool:
        """Is this lock leg's or exit's fill count established - verified by reconciliation
        (or corrected by an operator) on a finished order or an IOC that cannot fill more?"""
        return (r["fill_state"] in HEDGEABLE and _dec(r["fill_count"]) is not None
                and (r["state"] == DONE or (r["state"] == ACCEPTED and str(r["tif"]).lower() in IOC)))

    @classmethod
    def _contracts_committed(cls, r: Any) -> Decimal:
        """Contracts a lock leg or exit bought or may have bought: its fill count once it is
        settled (:meth:`_settled`), else everything it could have filled - a create answer
        alone, answers that disagree or an order still resting prove nothing."""
        if r["state"] == REJECTED:
            return ZERO
        if cls._settled(r):
            return D(r["fill_count"])
        return D(r["count"])

    # ---- 2. every answer recorded --------------------------------------------------------
    def accepted(self, intent_id: str, response: Any, now: Optional[float] = None, req_ts: Optional[float] = None) -> str:
        """The create-order answer: an order id makes it ``accepted`` (fills provisional
        until reconciled); a response without one is ``ambiguous``. Returns the new state.

        An IOC's reported fill count is booked - and its reservation shrinks to those fills
        at the limit plus the fee bound - only when the answer is final: a fill count within
        the order, no non-terminal status, and nothing left - an explicit zero remaining
        quantity, or every contract ordered filled (booking a full fill releases nothing: its
        worst case is the whole reservation). Any other answer keeps the whole worst case
        until reconciliation reads the order.

        Every fill count the answer shows within the order - final or not - is kept as
        evidence (``fill_seen``): no later read may book fewer (see the module docstring, 6.).
        The fills stay ``provisional`` until an order row and a complete fills listing confirm
        them; no lock leg is sized on them before that."""
        now = self._time(now, "now")
        req_ts = None if req_ts is None else self._time(req_ts, "request time")
        od = (response or {}).get("order") if isinstance(response, dict) and isinstance(response.get("order"), dict) else (response or {})
        if not isinstance(od, dict) or not (od.get("order_id") or od.get("id")):
            self.ambiguous(intent_id, "create response carried no order id", now=now, req_ts=req_ts, response=response)
            return AMBIGUOUS
        oid = str(od.get("order_id") or od.get("id"))
        fill = _count_field(od, "fill_count")[1]
        remaining = _count_field(od, "remaining_count")[1]
        status = str(od.get("status") or "").strip().lower()
        raw = {k: od.get(k) for k in ("order_id", "client_order_id", "fill_count", "remaining_count", "average_fill_price",
                                      "average_fee_paid", "ts_ms", "status") if od.get(k) is not None}

        def txn(c: sqlite3.Connection) -> str:
            row = c.execute("SELECT state, tif, count, fill_seen, fill_source, fill_state FROM intents WHERE intent_id = ?", (intent_id,)).fetchone()
            if row is not None and row["state"] in (ACCEPTED, DONE):
                # A late or repeated answer for an intent reconciliation already found: its state
                # is not reset; a count above everything seen is kept as evidence.
                cnt = _dec(row["count"])
                prior_seen = _dec(row["fill_seen"])
                if fill is not None and cnt is not None and ZERO <= fill <= cnt and (prior_seen is None or fill > prior_seen):
                    c.execute("UPDATE intents SET fill_seen=?, fill_source=? WHERE intent_id=?", (_s(fill), "the create answer", intent_id))
                self._event(c, now, intent_id, "late-answer", state=row["state"], **raw)
                return row["state"]
            reopened = row is not None and row["state"] == REJECTED     # released, but the exchange has the order
            ioc = row is not None and str(row["tif"]).lower() in IOC
            ordered = D(row["count"]) if row is not None else None
            within = fill is not None and ordered is not None and ZERO <= fill <= ordered
            nothing_left = (remaining is not None and remaining == 0) or (remaining is None and within and fill == ordered)
            final = within and nothing_left and (not status or status in TERMINAL_STATUSES)
            book = fill if (final or not ioc) else None
            why = None if book is not None or not ioc else (
                "create answer not final (" + ", ".join(p for p in (
                    "no fill count" if fill is None else None,
                    f"fill count {fill} outside the order" if fill is not None and not within else None,
                    f"{remaining} remaining" if remaining is not None and remaining != 0 else None,
                    "no remaining quantity for a partial fill" if remaining is None and within and fill != ordered else None,
                    f"status {status!r}" if status and status not in TERMINAL_STATUSES else None)
                    if p) + "): the whole worst case stays reserved until reconciled")
            # Every count the answer shows is evidence of what filled (a lower bound), final or not.
            prior = _dec(row["fill_seen"]) if row is not None else None
            seen, source = (_s(prior), row["fill_source"]) if row is not None else (None, None)
            fstate = row["fill_state"] if row is not None else None
            if within:
                if prior is not None and fill < prior:      # fewer than an earlier answer showed
                    book, fstate = (None if ioc else book), CONTRADICTED
                    why = f"the create answer says {fill} filled, fewer than the {prior} {source or 'an earlier answer'} showed: the whole worst case stays reserved"
                else:
                    seen, source = _s(fill), "the create answer"
                    fstate = fstate if fstate == CONTRADICTED else PROVISIONAL
            if reopened:
                why = "; ".join(w for w in ("a create answer arrived after the intent was released: the exchange has the order", why) if w)
            c.execute("UPDATE intents SET state=?, order_id=?, fill_count=?, req_ts=COALESCE(?, req_ts), resp_ts=?, updated_ts=?, reason=?, "
                      "fill_seen=?, fill_source=?, fill_state=? WHERE intent_id=?",
                      (ACCEPTED, oid, _s(book), req_ts, now, now, why, seen, source, fstate, intent_id))
            self._event(c, now, intent_id, "accepted", final=final, **({"reopened": True} if reopened else {}), **raw)
            return ACCEPTED

        return self._write(txn)

    def _stale(self, c: sqlite3.Connection, now: float, intent_id: str, write: str, state: Optional[str], **detail: Any) -> bool:
        """Record a write that no longer applies (the intent changed meanwhile - another
        process, a later answer) instead of making it: prior evidence is never overwritten."""
        self._event(c, now, intent_id, "stale-write-ignored", write=write, state=state, **detail)
        return False

    def rejected(self, intent_id: str, reason: str, now: Optional[float] = None, from_states: tuple = (PENDING, AMBIGUOUS),
                 **detail: Any) -> bool:
        """Nothing was sent, or the exchange provably never took it: the reservation is
        released. Only from ``from_states`` (a manual release also from ``accepted``): a stale
        decision never overwrites an intent found or finished meanwhile. Returns whether it
        was applied."""
        now = self._time(now, "now")

        def txn(c: sqlite3.Connection) -> bool:
            cur = c.execute("SELECT state FROM intents WHERE intent_id = ?", (intent_id,)).fetchone()
            if cur is None or cur["state"] not in from_states:
                return self._stale(c, now, intent_id, "rejected", cur["state"] if cur else None, reason=str(reason)[:500])
            c.execute("UPDATE intents SET state=?, reason=?, updated_ts=?, fill_count=COALESCE(fill_count, '0') WHERE intent_id=?",
                      (REJECTED, str(reason)[:500], now, intent_id))
            self._event(c, now, intent_id, "rejected", reason=str(reason)[:500], **detail)
            return True

        return self._write(txn)

    def ambiguous(self, intent_id: str, reason: str, now: Optional[float] = None, req_ts: Optional[float] = None,
                  hint: Optional[int] = None, **detail: Any) -> None:
        """The request may or may not have reached the exchange (timeout, 5xx, 409, a
        response without an order id). ``hint`` = the HTTP status of a single-attempt
        refusal (:func:`refusal_hint`), if any. Never regresses an intent found meanwhile."""
        now = self._time(now, "now")
        req_ts = None if req_ts is None else self._time(req_ts, "request time")

        def txn(c: sqlite3.Connection) -> None:
            cur = c.execute("SELECT state FROM intents WHERE intent_id = ?", (intent_id,)).fetchone()
            if cur is None or cur["state"] not in (PENDING, AMBIGUOUS):
                self._stale(c, now, intent_id, "ambiguous", cur["state"] if cur else None, reason=str(reason)[:500], hint=hint)
                return
            c.execute("UPDATE intents SET state=?, reason=?, hint=?, req_ts=COALESCE(?, req_ts), resp_ts=?, updated_ts=? WHERE intent_id=?",
                      (AMBIGUOUS, str(reason)[:500], hint, req_ts, now, now, intent_id))
            self._event(c, now, intent_id, "ambiguous", reason=str(reason)[:500], hint=hint, **detail)

        self._write(txn)

    def done(self, intent_id: str, fill_count: Decimal, fill_cost: Decimal, fees: Decimal, now: Optional[float] = None,
             fill_state: str = VERIFIED, **detail: Any) -> bool:
        """Finish an intent at what was paid. ``fill_state``: ``verified`` (a final row and a
        complete fills listing agreed) or ``corrected`` (an operator accepted an exchange
        correction); the highest count ever seen stays on record in ``fill_seen``.

        Checked again inside the write, against what is stored *now*: only an open intent is
        finished, and - unless it is an operator's correction - never below a count an answer
        showed (another reader may have recorded more fills since this decision was read); that
        makes it contradicted instead. Returns whether it was finished."""
        now = self._time(now, "now")

        def txn(c: sqlite3.Connection) -> bool:
            row = c.execute("SELECT state, tif, fill_seen, fill_source FROM intents WHERE intent_id = ?", (intent_id,)).fetchone()
            if row is None or row["state"] not in OPEN:
                return self._stale(c, now, intent_id, "done", row["state"] if row else None, fill_count=str(fill_count))
            prior = _dec(row["fill_seen"])
            if fill_state != CORRECTED and prior is not None and D(fill_count) < prior:
                note = (f"contradicted: {row['fill_source'] or 'an earlier answer'} showed {prior} filled, the answers read now {fill_count}"
                        " - the whole worst case stays reserved")
                c.execute("UPDATE intents SET fill_state=?, fill_count=CASE WHEN ? THEN NULL ELSE fill_count END, reason=?, updated_ts=?, "
                          "checks=checks+1 WHERE intent_id=?", (CONTRADICTED, str(row["tif"]).lower() in IOC, note[:500], now, intent_id))
                self._event(c, now, intent_id, "contradicted", note=note[:500], fill_count=str(fill_count))
                return False
            seen, source = ((_s(prior), row["fill_source"]) if prior is not None and prior >= D(fill_count)
                            else (str(fill_count), "a final order row and its fills listing"))
            c.execute("UPDATE intents SET state=?, fill_count=?, fill_cost=?, fees=?, reconciled_ts=?, updated_ts=?, reason=NULL, "
                      "fill_state=?, fill_seen=?, fill_source=? WHERE intent_id=?",
                      (DONE, str(fill_count), str(fill_cost), str(fees), now, now, fill_state, seen, source, intent_id))
            self._event(c, now, intent_id, "done", fill_count=str(fill_count), fill_cost=str(fill_cost), fees=str(fees), fill_state=fill_state, **detail)
            return True

        return self._write(txn)

    def release(self, intent_id: str, reason: str, now: Optional[float] = None) -> dict:
        """An operator's decision, after checking the exchange by hand: this open intent was
        never accepted (or is dealt with) - release its reservation. Recorded as such.

        Refused for an intent that an exchange answer showed filled: releasing it would book
        those contracts as never bought. An exchange correction goes through
        :meth:`accept_correction` (``kalshi correct``), which books what the exchange shows."""
        if not str(reason or "").strip():
            raise LedgerError("a manual release needs a reason")
        row = self.get(intent_id)
        if row is None or row["state"] not in (PENDING, AMBIGUOUS, ACCEPTED):
            raise LedgerError(f"no open intent {intent_id}")
        seen = _dec(row.get("fill_seen"))
        if seen is not None and seen > 0:
            raise LedgerError(f"intent {intent_id}: {row.get('fill_source') or 'an exchange answer'} showed {seen} filled - a release "
                              "would book them as never bought. If the exchange corrected the fills, use `kalshi correct` (it books "
                              "what the exchange shows now)")
        if not self.rejected(intent_id, f"released by hand: {reason}", now=now, from_states=OPEN, manual=True, was=row["state"]):
            raise LedgerError(f"intent {intent_id} changed meanwhile: nothing released")
        return self.get(intent_id) or {}

    def accept_correction(self, intent_id: str, client: Any, reason: str, now: Optional[float] = None, apply: bool = True) -> dict:
        """An operator's decision, after checking the exchange: this contradicted intent shows
        fewer fills than an earlier answer did because the exchange corrected them (a busted
        trade, say). Book what the exchange shows *now* - re-read here: the order row must be
        final, its fills listing complete and correctly scoped, and the two must agree on a
        count below the one seen before. The earlier evidence it overrides and ``reason`` go on
        record (``fill_state = corrected``). ``apply=False`` only reports what would be booked
        (the CLI's dry run). Raises ``LedgerError`` saying what is missing otherwise."""
        if not str(reason or "").strip():
            raise LedgerError("an exchange correction needs a reason (what you checked on the exchange)")
        now = self._time(now, "now")
        self._check_client(client)
        row = self.get(intent_id)
        if row is None or row["state"] not in OPEN:
            raise LedgerError(f"no open intent {intent_id}")
        if row.get("fill_state") != CONTRADICTED:
            raise LedgerError(f"intent {intent_id} is not contradicted ({row.get('fill_state') or 'no fill evidence'}): "
                              "`kalshi reconcile` finishes it from the exchange's answers")
        oid = row.get("order_id")
        if not oid:
            raise LedgerError(f"intent {intent_id} has no order id to read")
        if self._relation(row, self._identity_for(client)) != "same":
            raise LedgerError("this client is not provably the account that sent the order: its reads cannot correct it")
        try:
            order = client.order(oid) or {}
        except Exception as e:  # noqa: BLE001
            raise LedgerError(f"GET /portfolio/orders/{oid} failed ({e!r}): nothing corrected") from e
        ordered = D(row["count"])
        ev = order_evidence(order, row["count"], str(row["tif"]).lower() in IOC)
        fills = self._read_fills(client, row, str(oid))
        problems = list(ev.problems)
        scope = _scope_problem(order, row["ticker"], book_side(row["action"], row["side"]))
        if scope:
            problems.append(f"the order row is {scope}")
        if not fills.conclusive:
            problems.append(f"the fills listing is not complete ({fills.why()})")
        why = fills.contradiction(ev.filled if ev.filled is not None else ordered, ordered)
        if why:
            problems.append(why)
        if ev.filled is not None and fills.conclusive and fills.count != ev.filled:
            problems.append(f"the fills listing shows {fills.count}, the order row {ev.filled}")
        prior = _dec(row.get("fill_seen"))
        if ev.filled is not None and prior is not None and ev.filled >= prior:
            problems.append(f"the exchange shows {ev.filled} filled, not fewer than the {prior} seen: `kalshi reconcile` finishes it")
        cost, fees = ev.cost, ev.fees
        if fills.conclusive and fills.fees is not None:
            fees = fills.fees if fees is None else max(fees, fills.fees)
        if not problems and (cost is None or fees is None):
            problems.append("; ".join(ev.missing or ["cost or fees not reported"]))
        if problems:
            raise LedgerError("not corrected: " + "; ".join(problems))
        out = {"intent_id": intent_id, "ticker": row["ticker"], "order_id": oid, "seen_before": _s(prior), "seen_source": row.get("fill_source"),
               "filled_now": str(ev.filled), "fill_cost": str(cost), "fees": str(fees), "applied": bool(apply)}
        if apply:
            self.done(intent_id, ev.filled, cost, fees, now=now, fill_state=CORRECTED, correction=True, operator_reason=str(reason)[:500],
                      overrides=_s(prior), overrides_source=row.get("fill_source"), **fills.notes)
        return out

    def note(self, intent_id: Optional[str], kind: str, now: Optional[float] = None, **detail: Any) -> None:
        now = self._time(now, "now")
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
        """Counts by state, today's committed dollars per strategy, the block, if any, and the
        open intents whose exchange answers contradict each other (``contradicted``: each
        needs `kalshi reconcile`, or `kalshi correct` after checking the exchange)."""
        now = self._time(now, "now")
        day = self.day_of(now)
        with self._lock:
            by_state = {r[0]: r[1] for r in self.conn.execute("SELECT state, COUNT(*) FROM intents GROUP BY state")}
            strategies = [r[0] for r in self.conn.execute("SELECT DISTINCT strategy FROM intents")]
            today = {s: str(self._sum(self.conn, "strategy = ? AND day = ?", (s, day))) for s in strategies}
            contradicted = [{k: r[k] for k in ("intent_id", "strategy", "ticker", "count", "fill_seen", "fill_source", "reason")}
                            for r in self.conn.execute("SELECT * FROM intents WHERE state IN ('pending', 'ambiguous', 'accepted') "
                                                       "AND fill_state = ? ORDER BY created_ts", (CONTRADICTED,))]
        with self._lock:
            owner = self.conn.execute("SELECT value FROM meta WHERE key='account_fp'").fetchone()
            nkeys = self.conn.execute("SELECT COUNT(*) FROM keys").fetchone()[0]
        return {"env": self.env, "path": self.path, "day": day, "by_state": by_state, "committed_today": today,
                "blocked": self.blocked(now), "contradicted": contradicted,
                "account": (owner["value"][:12] + "…") if owner is not None else None, "keys_seen": nkeys,
                "this_key_account_known": bool(self.identity.account_fp)}

    # ---- 4. reconciliation ---------------------------------------------------------------
    def owner_gone(self, owner: str) -> bool:
        """True when ``owner`` (``host:pid``) is a process on this host that no longer runs."""
        return self._owner_gone(owner)

    def reconcile(self, client: Any, now: Optional[float] = None, include_resting: bool = False,
                  skip: Iterable[str] = (), recheck_exhausted: bool = False) -> list[dict]:
        """Resolve every open intent against the exchange. Returns one record per intent
        looked at: ``{intent_id, before, after, note}``. Errors leave the intent as it was
        (an ambiguous one stays blocking) and are reported, never raised.

        An accepted *resting* order (good-till-cancelled: the maker's) belongs to the process
        that placed it, which reads it every poll and hands the rows to :meth:`apply_row`;
        it is only looked at with ``include_resting`` - by that process, or by its successor
        once the owner is gone - and never when its owner is another live process. ``skip``
        names intents the caller is reading itself. An accepted order read ``max_checks``
        times without final evidence (fees still unreported, say) keeps its bounded
        reservation and is no longer polled automatically; ``recheck_exhausted`` (the
        ``kalshi reconcile`` command) reads it again, so late fills and fees still land.
        Each record also says the intent's ``fill_state`` after the read (``contradicted``
        when the exchange's answers disagree: the caller alerts on it)."""
        self._check_client(client)
        now = self._time(now, "now")
        ident = self._identity_for(client)
        with self._lock:
            owner_row = self.conn.execute("SELECT value FROM meta WHERE key='account_fp'").fetchone()
        if ident.account_fp and owner_row is not None and owner_row["value"] != ident.account_fp:
            raise LedgerError(f"{self.path} belongs to another Kalshi account: not reconciled with this client")
        with self._lock:
            rows = self.conn.execute("SELECT * FROM intents WHERE state IN ('pending', 'ambiguous', 'accepted') ORDER BY created_ts").fetchall()
            unknown = {r["intent_id"] for r in self._unknown(self.conn, now)}
        skip = set(skip or ())
        out = []
        for r in rows:
            if r["state"] == PENDING and r["intent_id"] not in unknown:
                continue                           # still in flight in a live process
            if r["intent_id"] in skip:
                continue
            if r["state"] == ACCEPTED and str(r["tif"]).lower() not in IOC:
                if not include_resting or (r["owner"] != self.owner and not self._owner_gone(r["owner"])):
                    continue                       # a resting order is its (live) owner's to read
            rel = self._relation(r, ident)
            if rel == "other":
                out.append({"intent_id": r["intent_id"], "strategy": r["strategy"], "ticker": r["ticker"], "before": r["state"],
                            "after": r["state"], "note": "sent by another Kalshi account: not reconciled with this client"})
                continue
            if r["state"] == ACCEPTED and int(r["checks"] or 0) >= self.max_checks and not recheck_exhausted:
                continue                           # known order, exposure bounded: stop polling it
            since = float(r["resp_ts"] or r["updated_ts"] or r["created_ts"])
            if now - since < self.settle_s:
                continue                           # Kalshi's read side trails its writes
            try:
                note = self._reconcile_one(client, dict(r), now, release_ok=(rel == "same"))
            except Exception as e:  # noqa: BLE001 - reported, the intent keeps blocking
                note = f"error: {e!r}"[:300]
                self._write(lambda c, iid=r["intent_id"], n=note: (
                    c.execute("UPDATE intents SET checks = checks + 1 WHERE intent_id = ?", (iid,)),
                    self._event(c, now, iid, "reconcile-error", error=n)))
            after = self.get(r["intent_id"]) or {}
            out.append({"intent_id": r["intent_id"], "strategy": r["strategy"], "ticker": r["ticker"], "before": r["state"],
                        "after": after.get("state"), "note": note, "fill_state": after.get("fill_state")})
        return out

    def _reconcile_one(self, client: Any, r: dict, now: float, release_ok: bool = True) -> str:
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
                if not release_ok:
                    # Absent from a listing of an account that may not be the sender's: that
                    # proves nothing, so the reservation stays (and keeps blocking).
                    self._bump(iid, now, "absent from this client's listing, but its account is not the sender's provably: not released")
                    return "not released: sender's account not verified"
                misses = int(r["misses"] or 0) + 1
                sent = float(r["req_ts"] or r["created_ts"])
                # A definitive client error (4xx other than 409 conflict / 429) is very likely a
                # refusal: one complete listing after the settle time proves it.
                definitive = r["hint"] is not None and 400 <= int(r["hint"]) < 500 and int(r["hint"]) not in (409, 429)
                window = self.settle_s if definitive else self.not_found_s
                if now - sent >= window and (misses >= 2 or definitive):
                    self._write(lambda c: c.execute("UPDATE intents SET misses=? WHERE intent_id=?", (misses, iid)))
                    if not self.rejected(iid, f"not on the exchange {now - sent:.0f}s after sending ({misses} complete listings): never accepted", now=now):
                        return "not released: the intent changed meanwhile"
                    return "released: never accepted"
                self._write(lambda c: (c.execute("UPDATE intents SET misses=?, checks=checks+1 WHERE intent_id=?", (misses, iid)),
                                       self._event(c, now, iid, "not-found", misses=misses)))
                return f"not found yet ({misses})"
            oid = str(order.get("order_id") or order.get("id") or "")

            def found(c: sqlite3.Connection) -> bool:
                cur = c.execute("UPDATE intents SET order_id=?, state=?, updated_ts=? WHERE intent_id=? AND state IN (?, ?)",
                                (oid, ACCEPTED, now, iid, PENDING, AMBIGUOUS))
                self._event(c, now, iid, "found" if cur.rowcount else "stale-write-ignored", order_id=oid, status=order.get("status"))
                return bool(cur.rowcount)

            self._write(found)
        # What is stored now (another process may have read this intent since the snapshot).
        fresh = self.get(iid)
        if fresh is None or fresh["state"] not in OPEN:
            return f"changed meanwhile ({(fresh or {}).get('state')})"
        return self._apply_order(client, fresh, order, now)

    def apply_row(self, intent_id: str, order: dict, client: Any = None, now: Optional[float] = None) -> str:
        """Book an order row the caller read itself (``GET /portfolio/orders/{id}``): fills of
        a resting order so far, or the final fills and fees of a finished one."""
        now = self._time(now, "now")
        row = self.get(intent_id)
        if row is None:
            raise LedgerError(f"no intent {intent_id}")
        if row["state"] in (DONE, REJECTED):
            return f"already {row['state']}"
        oid = str((order or {}).get("order_id") or (order or {}).get("id") or "")
        if row["order_id"] and oid and oid != row["order_id"]:
            raise LedgerError(f"order row {oid} is not intent {intent_id}'s order {row['order_id']}")
        return self._apply_order(client, row, order, now)

    def _read_fills(self, client: Any, r: dict, oid: str) -> "FillsEvidence":
        """The fills listing of order ``oid``, scoped to the intent's market and book side; a
        missing client proves nothing (like a failed read)."""
        if client is None:
            return FillsEvidence(ZERO, None, False, notes={"fills_error": "no client to read the fills listing with"})
        return fills_evidence(*list_fills(client, oid), order_id=oid, ticker=r["ticker"], bside=book_side(r["action"], r["side"]))

    def _apply_order(self, client: Any, r: dict, order: Optional[dict], now: float) -> str:
        """Book one order row (the module docstring, 6.). Fill evidence only grows: a row or
        listing showing fewer fills than an earlier answer did - or row and listing apart, or
        either on another market - is a contradiction and releases nothing. ``done`` - the
        reservation replaced by what was paid - needs a final row (:func:`order_evidence`)
        whose count no earlier answer exceeded, a complete, correctly scoped fills listing
        (:func:`fills_evidence`) agreeing with it, the row's fill cost, and fees from the row
        or the listing; anything less keeps the intent open (``_hold``)."""
        iid = r["intent_id"]
        if not order:
            self._bump(iid, now, "empty order read")
            return "empty order read"
        ioc = str(r["tif"]).lower() in IOC
        ordered = D(r["count"])
        ev = order_evidence(order, r["count"], ioc)
        oid = str(order.get("order_id") or order.get("id") or r["order_id"] or "")
        prior = _dec(r.get("fill_seen"))                     # the most any earlier answer showed filled
        prior_src = r.get("fill_source") or "an earlier answer"
        booked = _dec(r.get("fill_count"))
        count = ev.filled if ev.filled is not None and ZERO <= ev.filled <= ordered else None
        detail: dict[str, Any] = {"status": ev.status or None, "source": "order"}
        problems: list[str] = []
        scope = _scope_problem(order, r["ticker"], book_side(r["action"], r["side"]))
        if scope:
            problems.append(f"the order row is {scope}")
        if order.get("client_order_id") and r.get("client_order_id") and str(order["client_order_id"]) != str(r["client_order_id"]):
            problems.append("the order row carries another client_order_id")
        if count is not None and prior is not None and count < prior:
            problems.append(f"cumulative fills went down: {prior_src} showed {prior} filled, the order row now says {count}")
        # A final row, or one that contradicts an earlier answer, is checked against the fills.
        fills = self._read_fills(client, r, oid) if (ev.final or problems) else None
        if fills is not None:
            detail.update(fills.notes)
            why = fills.contradiction(count if count is not None else ordered, ordered)
            if why:
                problems.append("order and fills disagree: " + why)
        seen, seen_src = prior, prior_src
        for n, src in ((count, "an order row"), (fills.count if fills is not None and fills.count <= ordered else None, "a fills listing")):
            if n is not None and (seen is None or n > seen):
                seen, seen_src = n, src
        evidence: dict[str, Any] = {"fill_seen": seen, "fill_source": seen_src} if seen is not None else {}
        if problems:
            # Nothing is released: an IOC's fill count is unverified again (the whole worst case
            # counts, no lock leg is sized on it), every count seen stays on record, and it is
            # read again. A true exchange correction is accepted only by hand (accept_correction).
            return self._hold(iid, now, "contradicted: " + "; ".join(problems) + (" - the whole worst case stays reserved" if ioc else ""),
                              fill=None if ioc else _KEEP, fill_state=CONTRADICTED, kind="contradicted",
                              filled=_s(ev.filled), remaining=_s(ev.remaining), **evidence, **detail)
        # Consistent again: a contradiction clears to provisional (a count must be verified anew).
        consistent = PROVISIONAL if (r.get("fill_state") == CONTRADICTED or (r.get("fill_state") is None and seen is not None)) else _KEEP
        # Book more fills than the booked count (never fewer); a count nobody booked stays unbooked.
        raise_to = count if (count is not None and booked is not None and count > booked) else _KEEP
        if not ev.final:
            if not ioc and ev.status == "resting" and count is not None:
                # A resting order's fills so far: booked for the record, nothing released (a
                # resting order counts at its whole worst case until a final row).
                return self._hold(iid, now, f"order resting: {ev.remaining if ev.remaining is not None else '?'} resting",
                                  fill=count if booked is None or count > booked else _KEEP, fill_state=PROVISIONAL, kind="still-resting",
                                  keep_reason=True, status=ev.status, remaining=_s(ev.remaining), **evidence)
            # A row that says contracts may still fill (no terminal status, and a remaining
            # quantity above zero or none at all) puts an IOC back at its whole worst case,
            # whatever its create answer said: it can buy up to its full count yet.
            may_fill = ev.status not in TERMINAL_STATUSES and (ev.remaining is None or ev.remaining > 0)
            return self._hold(iid, now, "order not final: " + "; ".join(ev.problems) + (" - it may still fill: the whole worst case stays reserved"
                                                                                      if ioc and may_fill else ""),
                              fill=None if (ioc and may_fill) else raise_to, fill_state=PROVISIONAL if (ioc and may_fill) else consistent,
                              status=ev.status or None, filled=_s(ev.filled), remaining=_s(ev.remaining), **evidence)
        if not fills.conclusive:
            # Nothing proves the row's count is all that filled: nothing is released beyond what
            # an exchange answer already stated final, and it is read again.
            return self._hold(iid, now, f"{count} filled per the order row, but the fills listing is not complete ({fills.why()}): "
                              "nothing released until it is", fill=raise_to, fill_state=PROVISIONAL, **evidence, **detail)
        if fills.count < count:
            detail["fills_trail"] = True
            return self._hold(iid, now, f"the fills listing shows {fills.count} of the {count} the order row reports (it trails the row): read again",
                              fill=raise_to, fill_state=PROVISIONAL, **evidence, **detail)
        # Verified: a final row and a complete listing agree, and no earlier answer showed more.
        cost, fees = ev.cost, ev.fees
        if fills.fees is not None:
            if fees is None:
                fees = fills.fees
                detail["source"] = "order + fills (fees)"
            elif fills.fees != fees:
                detail["fills_mismatch"] = True
                fees = max(fees, fills.fees)       # both are the exchange's: the budget keeps the larger
        if cost is None or fees is None:
            # The fills are final; what they cost (or their fees) is not established yet: book
            # the fills - an IOC then counts them at the limit plus the fee bound, any other
            # order keeps its whole worst case - and read again (late fees land then).
            keep = "fills kept at the limit plus the fee bound" if ioc else "the whole worst case stays reserved"
            return self._hold(iid, now, f"{count} filled, " + "; ".join(ev.missing or ["fees not reported"]) + f": {keep} until the exchange reports it",
                              fill=count, fill_state=VERIFIED, **evidence, **detail)
        self.done(iid, count, cost, fees, now=now, **detail)
        return f"done: {count} filled, ${cost} + ${fees} fees"

    def _hold(self, iid: str, now: float, note: str, fill: Any = _KEEP, fill_state: Any = _KEEP, fill_seen: Any = _KEEP,
              fill_source: Any = _KEEP, kind: str = "held", keep_reason: bool = False, **detail: Any) -> str:
        """Keep an intent open - nothing is released - and say why (``reason``, and a
        ``held`` / ``contradicted`` / ``still-resting`` event). ``fill``: a fill count to book,
        ``None`` to unverify the booked one; ``fill_state`` / ``fill_seen`` / ``fill_source``:
        the fill evidence's new state, highest count seen and where it came from. The
        ``_KEEP`` default leaves a field as it is; ``keep_reason`` leaves ``reason`` alone.

        Inside the write, against what is stored *now*: only an open intent is touched, the
        highest count seen only grows (another reader may have recorded more meanwhile), and
        a count booked below it makes the intent contradicted instead."""
        def txn(c: sqlite3.Connection) -> str:
            row = c.execute("SELECT state, tif, fill_seen, fill_source FROM intents WHERE intent_id = ?", (iid,)).fetchone()
            if row is None or row["state"] not in OPEN:
                self._stale(c, now, iid, kind, row["state"] if row else None, note=note[:500])
                return note
            f, st, seen, src, text = fill, fill_state, fill_seen, fill_source, note
            stored = _dec(row["fill_seen"])
            if seen is not _KEEP and stored is not None and (seen is None or stored > seen):
                seen, src = _KEEP, _KEEP                          # keep the higher count already on record
            top = stored if seen is _KEEP else seen
            if isinstance(f, Decimal) and top is not None and f < top and st != CONTRADICTED:
                st, f = CONTRADICTED, (None if str(row["tif"]).lower() in IOC else _KEEP)
                text = f"contradicted: {row['fill_source'] or 'an earlier answer'} showed {top} filled, this read {fill} - " + note
            sets, args = ["checks = checks + 1"], []
            if not keep_reason or st == CONTRADICTED:
                sets.append("reason = ?")
                args.append(text[:500])
            for col, val in (("fill_count", f), ("fill_state", st), ("fill_seen", seen), ("fill_source", src)):
                if val is not _KEEP:
                    sets.append(f"{col} = ?")
                    args.append(_s(val) if isinstance(val, Decimal) else val)
            if f is not _KEEP or st is not _KEEP:
                sets.append("updated_ts = ?")
                args.append(now)
            c.execute(f"UPDATE intents SET {', '.join(sets)} WHERE intent_id = ?", (*args, iid))
            self._event(c, now, iid, "contradicted" if st == CONTRADICTED else kind, note=text[:500], **detail)
            return text

        return self._write(txn)

    def _bump(self, iid: str, now: float, note: str) -> None:
        self._write(lambda c: (c.execute("UPDATE intents SET checks = checks + 1 WHERE intent_id = ?", (iid,)),
                               self._event(c, now, iid, "check", note=note)))


def refusal_hint(e: BaseException) -> Optional[int]:
    """The HTTP status of a failed create as the ledger's ``hint`` (a definitive 4xx lets one
    complete listing release the intent) - only when it answered the one and only attempt.
    ``HttpClient`` re-sends a POST after a 429, a 5xx or a transport error, so a 4xx on a later
    attempt may answer a re-send of an order the exchange already accepted: it proves nothing
    about that order, and the full not-found window applies (``venues.http`` sets ``attempts``)."""
    st = getattr(e, "status", None)
    if not isinstance(st, int) or isinstance(st, bool):
        return None
    return st if int(getattr(e, "attempts", 1) or 1) <= 1 else None


def list_orders(client: Any, **params: Any) -> tuple[list[dict], bool]:
    """(rows, truncated) of ``GET /portfolio/orders`` for ``params``, every page the client
    will walk. A client without :meth:`paged` (test fakes) is taken as complete."""
    params = {k: v for k, v in params.items() if v is not None}
    if hasattr(client, "paged"):
        return client.paged("/portfolio/orders", "orders", params)
    return list(client.orders_v2(**params) or []), False


def list_fills(client: Any, order_id: str) -> tuple[list[dict], Optional[bool], Optional[str]]:
    """(rows, truncated, error) of ``GET /portfolio/fills?order_id=``, every page the client
    will walk. A client without ``paged`` (test fakes) is taken as complete, like
    :func:`list_orders`; a read that fails returns the error and proves nothing."""
    try:
        if callable(getattr(client, "paged", None)):
            rows, truncated = client.paged("/portfolio/fills", "fills", {"order_id": order_id})
            return list(rows or []), bool(truncated), None
        if callable(getattr(client, "fills_v2", None)):
            return list(client.fills_v2(order_id=order_id) or []), False, None
    except Exception as e:  # noqa: BLE001 - reported with the evidence, never raised
        return [], None, repr(e)[:200]
    return [], None, "this client cannot read fills"


# ---- what an exchange answer proves -------------------------------------------------------
def _count_field(od: dict, name: str) -> tuple[bool, Optional[Decimal]]:
    """(present, value) of a Kalshi count: the fixed-point ``<name>_fp`` spelling first, then
    ``<name>``. A present value that is not a number reads ``(True, None)``."""
    for k in (f"{name}_fp", name):
        v = od.get(k)
        if v is not None and v != "":
            return True, _dec(v)
    return False, None


def _money(od: dict, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> tuple[Optional[Decimal], Optional[str]]:
    """The sum of dollar fields, or (None, why): a required field that is absent is
    *unknown*, never zero (an explicit ``"0.0000"`` is zero), and a present field that is
    not a non-negative number makes the whole sum unknown."""
    total = ZERO
    for k in required + optional:
        raw = od.get(k)
        if raw is None or raw == "":
            if k in required:
                return None, f"no {k}"
            continue
        v = _dec(raw)
        if v is None or v < 0:
            return None, f"{k}={raw!r} is not a dollar amount"
        total += v
    return total, None


@dataclass
class OrderEvidence:
    """What one order row proves. ``final``: a terminal status, an explicit zero remaining
    quantity and a fill count within the order, all present and consistent (``problems``
    says what is missing or contradictory). ``cost`` / ``fees``: None when not established
    (``missing`` says why)."""
    status: str
    filled: Optional[Decimal]
    remaining: Optional[Decimal]
    final: bool
    problems: list = field(default_factory=list)
    cost: Optional[Decimal] = None
    fees: Optional[Decimal] = None
    missing: list = field(default_factory=list)


def order_evidence(order: dict, count: Any, ioc: bool) -> OrderEvidence:
    """Read ``GET /portfolio/orders/{id}`` (or a listing row) for an order of ``count``.

    Cost and fees are taken per liquidity side: an IOC never rests, so it needs the taker
    fields (maker ones are added when present); any other order needs both sides. Recorded
    on demo 2026-09-26, every terminal row (300 canceled, 31 executed) carried the status,
    ``remaining_count_fp: "0.00"`` and all four dollar fields."""
    status = str(order.get("status") or "").strip().lower()
    has_f, filled = _count_field(order, "fill_count")
    has_r, remaining = _count_field(order, "remaining_count")
    has_i, initial = _count_field(order, "initial_count")
    ordered = D(count)
    problems: list[str] = []
    if not status:
        problems.append("no status")
    elif status not in TERMINAL_STATUSES:
        problems.append(f"status {status!r} is not terminal")
    if filled is None:
        problems.append("fill count is not a number" if has_f else "no fill count")
    elif not ZERO <= filled <= ordered:
        problems.append(f"fill count {filled} outside 0..{ordered}")
    if remaining is None:
        problems.append("remaining quantity is not a number" if has_r else "no remaining quantity")
    elif remaining != 0:
        problems.append(f"{remaining} still open")
    if has_i and initial != ordered:
        problems.append(f"initial count {initial} but {ordered} ordered")
    if status == "executed" and filled is not None and filled != ordered:
        problems.append(f"executed with {filled} of {ordered} filled")
    ev = OrderEvidence(status, filled, remaining, False, problems)
    cost_req = ("taker_fill_cost_dollars",) if ioc else ("taker_fill_cost_dollars", "maker_fill_cost_dollars")
    fee_req = ("taker_fees_dollars",) if ioc else ("taker_fees_dollars", "maker_fees_dollars")
    cost_opt = ("maker_fill_cost_dollars",) if ioc else ()
    fee_opt = ("maker_fees_dollars",) if ioc else ()
    if filled is not None and filled == 0:
        # Nothing filled: nothing paid. A row that reports cost or fees anyway contradicts itself.
        cost, _ = _money(order, (), cost_req + cost_opt)
        fees, _ = _money(order, (), fee_req + fee_opt)
        if cost is None or fees is None or cost > 0 or fees > 0:
            problems.append("cost or fees reported for an order with no fills")
        ev.cost, ev.fees = ZERO, ZERO
    elif filled is not None:
        cost, why_c = _money(order, cost_req, cost_opt)
        if cost is not None and cost <= 0:
            cost, why_c = None, f"fill cost {cost} for {filled} contracts"
        fees, why_f = _money(order, fee_req, fee_opt)
        ev.cost, ev.fees = cost, fees
        ev.missing = [w for w in (why_c, why_f) if w]
    ev.final = not problems
    return ev


@dataclass
class FillsEvidence:
    """What a fills listing for one order proves. ``count`` / ``fees`` are de-duplicated by
    fill id over this order's rows on the intent's market and book side; ``fees`` is None
    when a counted fill carries no ``fee_cost``. ``conclusive``: every page read without an
    error, every row this order's and in scope, every row with a fill id and a positive
    count, no fill id listed twice with different contents - only then can the listing
    confirm a count, establish fees or show that fills trail. ``count`` is a lower bound of
    what filled even when the listing is not conclusive."""
    count: Decimal
    fees: Optional[Decimal]
    conclusive: bool
    conflicts: int = 0
    out_of_scope: int = 0
    notes: dict = field(default_factory=dict)

    def contradiction(self, filled: Decimal, ordered: Optional[Decimal] = None) -> Optional[str]:
        """Why the listing contradicts an order row reporting ``filled`` (of ``ordered``), or
        None. More fills than the row counts contradict it even when the listing is
        truncated: they are a lower bound."""
        if ordered is not None and self.count > ordered:
            return f"the fills listing shows {self.count} contracts, more than the {ordered} ordered"
        if self.count > filled:
            return f"the fills listing shows {self.count} contracts, the order row {filled}"
        if self.conflicts:
            return f"{self.conflicts} fill id(s) listed twice with different contents"
        if self.out_of_scope:
            return f"{self.out_of_scope} fill(s) of this order on another market or book side ({self.notes.get('fills_scope')})"
        return None

    def why(self) -> str:
        """Why the listing is not conclusive (for the record)."""
        n = self.notes
        parts = [f"read failed: {n['fills_error']}" if n.get("fills_error") else None,
                 "truncated" if n.get("fills_truncated") else None,
                 f"{n['fills_foreign']} row(s) of other orders" if n.get("fills_foreign") else None,
                 f"{n['fills_unattributed']} row(s) without an order id" if n.get("fills_unattributed") else None,
                 f"{n['fills_without_id']} fill(s) without a fill id" if n.get("fills_without_id") else None,
                 f"{n['fills_malformed']} malformed fill(s)" if n.get("fills_malformed") else None,
                 f"{n['fills_conflicting']} conflicting duplicate(s)" if n.get("fills_conflicting") else None,
                 f"{n['fills_out_of_scope']} fill(s) out of scope" if n.get("fills_out_of_scope") else None]
        return ", ".join(p for p in parts if p) or "not conclusive"


def _fill_content(f: dict) -> tuple:
    return tuple(str(f.get(k)) for k in ("count_fp", "count", "fee_cost", "yes_price_dollars", "no_price_dollars", "outcome_side", "side",
                                          "book_side", "action", "is_taker", "ticker", "market_ticker"))


def fills_evidence(rows: list[dict], truncated: Optional[bool], error: Optional[str], order_id: str,
                   ticker: Optional[str] = None, bside: Optional[str] = None) -> FillsEvidence:
    """Summarise ``GET /portfolio/fills?order_id=`` rows (see :class:`FillsEvidence`). A row of
    another order, or one without an order id, means the listing is not the one asked for;
    a fill of this order on another market (``ticker``) or book side (``bside``) contradicts
    the order; a fill without a fill id cannot be de-duplicated. None of those is counted,
    and each makes the listing inconclusive."""
    mine, unattributed, foreign = [], 0, 0
    for f in rows or []:
        o = str(f.get("order_id") or "")
        if o == str(order_id):
            mine.append(f)
        elif not o:
            unattributed += 1
        else:
            foreign += 1
    scoped, out_of_scope, scope_note = [], 0, None
    for f in mine:
        why = _scope_problem(f, ticker, bside)
        if why:
            out_of_scope += 1
            scope_note = scope_note or why
        else:
            scoped.append(f)
    seen: dict[str, dict] = {}
    unique, duplicates, conflicts, without_id = [], 0, 0, 0
    for f in scoped:
        key = str(f.get("fill_id") or f.get("trade_id") or "")
        if not key:
            without_id += 1                        # cannot be told from a repeat: not counted
        elif key in seen:
            duplicates += 1
            conflicts += _fill_content(seen[key]) != _fill_content(f)
        else:
            seen[key] = f
            unique.append(f)
    count, fees, fee_missing, malformed = ZERO, ZERO, 0, 0
    for f in unique:
        n = _count_field(f, "count")[1]
        if n is None or n <= 0:
            malformed += 1
            continue
        count += n
        fee = _dec(f.get("fee_cost")) if f.get("fee_cost") not in (None, "") else None
        if fee is None or fee < 0:
            fee_missing += 1
        else:
            fees += fee
    notes: dict[str, Any] = {"fills": len(unique), "fills_count": str(count), "fills_fees": None if fee_missing else str(fees)}
    for k, v in (("fills_truncated", truncated is not False and error is None), ("fills_error", error), ("fills_duplicates", duplicates),
                 ("fills_conflicting", conflicts), ("fills_without_id", without_id), ("fills_unattributed", unattributed),
                 ("fills_foreign", foreign), ("fills_malformed", malformed), ("fills_without_fee", fee_missing),
                 ("fills_out_of_scope", out_of_scope), ("fills_scope", scope_note)):
        if v:
            notes[k] = v
    conclusive = error is None and truncated is False and not (unattributed or foreign or conflicts or malformed or without_id or out_of_scope)
    return FillsEvidence(count, None if fee_missing else fees, conclusive, conflicts, out_of_scope, notes)


def finite_positive(x: Any) -> bool:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v > 0

"""The "Robinhood done" button: you buy the Robinhood leg of an arb, tap, the bot buys the Kalshi leg.

Robinhood has no API for prediction markets, so its leg is always bought by hand; Kalshi's
can be bought by the engine. The button turns an ARB push into a two-step trade that never
leaves the *bot's* half hanging: you buy Robinhood first (at most ``rh_max``), tap
"Robinhood done", and within a couple of seconds the engine re-reads both venues' live
prices, checks they still make the arb it pushed, and buys the Kalshi leg immediate-or-cancel
at no more than ``kalshi_limit`` - the most it may pay and still lock the set given what you
paid on Robinhood. The result comes back as an ARB FILL push, including what is left unhedged
if Kalshi moved.

How the tap reaches the engine: the push carries an ntfy ``http`` action that POSTs
``arb <token>`` to the command topic (the alert topic + ``-cmd``); every live process keeps
one streaming subscription to that topic open (a single long-lived request - polling every
second would run into ntfy.sh's per-visitor request limit, which the alert pushes share) and
acts only on tokens it issued itself. A token is 64 random
bits, single use, and expires after ``ttl_s`` (3 minutes).

Modes (``arb_button_mode``), from safest up:

* ``off``   - no button.
* ``paper`` - the default: every step runs against *live* prices (Kalshi's production order
              book, Robinhood's live quote) but the Kalshi buy is only simulated, walking the
              real book up to the limit. Nothing is sent anywhere. This is the practice mode.
* ``demo``  - the Kalshi order goes to Kalshi's demo exchange (its prices are not the real
              ones: this checks the order plumbing, not the price).
* ``live``  - a real immediate-or-cancel order on your Kalshi account. Needs the production
              key (``KALSHI_ENV=prod``) *and* ``ARB_LIVE_TRADING=1``.

Every step is journalled to ``out/orders/arb_button.jsonl``. Demo / live Kalshi orders
also go through the environment's durable order ledger (``execution/ledger.py``, shared with
the LAG executor): the order is reserved against ``daily_notional`` (fees included) before
it is sent, carries the ledger's ``client_order_id``, a tap is sent at most once per token
even across processes, and an order whose outcome is unknown (timeout, 5xx) is reported as
UNKNOWN - check Kalshi before touching the Robinhood leg - and blocks every new order until
the ledger finds it on the exchange (reconciled a few seconds after each real order).
"""
from __future__ import annotations

import json
import math
import os
import secrets
import threading
import time
from decimal import Decimal
from typing import Any, Callable, Optional

from ..config import declare_setting, setting

declare_setting("arb_button_side_cap", env="ARB_BUTTON_SIDE_CAP", default="25", cast=str,
                doc="Maximum dollars per leg of an arbitrage-button ticket, including conservative fill fees; Robinhood remains manual.")

MODES = ("off", "paper", "demo", "live")
TICK = 0.01


def _f(x: Any) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _c(p: Optional[float]) -> str:
    return "?" if p is None else f"{p * 100:g}¢"


def _money(x: float) -> str:
    return ("-$" if x < 0 else "$") + f"{abs(x):,.2f}"


def all_in(fee_model: Any, price: float, n: int) -> float:
    """Price plus fee per contract, at the real order size (fees round per order)."""
    return price + float(fee_model.fee(price, n, "taker")) / n


def max_price(fee_model: Any, other_all_in: float, n: int, floor: float, cap: float = 0.99) -> Optional[float]:
    """The highest 1c price p >= ``floor`` at which this leg plus a leg costing
    ``other_all_in`` (all-in, per contract) still pays out at least what it costs."""
    best = None
    p = round(floor, 2)
    while p <= cap + 1e-9:
        if all_in(fee_model, p, n) + other_all_in <= 1.0 + 1e-12:
            best = p
        else:
            break
        p = round(p + TICK, 2)
    return best


def _stated_multiplier(quote: Any) -> Optional[str]:
    """The fee multiplier the Kalshi quote's fee params state (None when only assumed)."""
    from ..execution.ledger import quote_fee_multiplier

    m = quote_fee_multiplier(getattr(quote, "fee_params", None))
    return str(m) if m is not None else None


def _find_quote(quotes_by_venue: dict, venue: str, market_id: str) -> Any:
    for q in (quotes_by_venue or {}).get(venue, []) or []:
        if getattr(q, "venue_market_id", None) == market_id:
            return q
    return None


class ArbButton:
    def __init__(self, mode: str = "paper", alerts: Any = None, cmd_url: Optional[str] = None, fee_for: Optional[Callable[[Any], Any]] = None,
                 executor: Any = None, data_client: Any = None, robinhood: Any = None, ttl_s: float = 180.0,
                 max_contracts: int = 2000, daily_notional: float = 500.0, journal_path: str = "out/orders/arb_button.jsonl",
                 http_get: Optional[Callable[[str], str]] = None, clock: Callable[[], float] = time.time,
                 stream: Optional[Callable[[str], Any]] = None, auto_practice_s: Optional[float] = None,
                 side_cap: Any = None) -> None:
        self.side_cap = Decimal(str(setting(None, "arb_button_side_cap") if side_cap is None else side_cap))
        if not self.side_cap.is_finite() or self.side_cap <= 0:
            raise ValueError("arb_button_side_cap must be finite and positive")
        if mode not in MODES:
            raise ValueError(f"arb_button_mode must be one of {MODES}")
        if mode == "live" and os.environ.get("ARB_LIVE_TRADING") != "1":
            raise RuntimeError("live button orders need ARB_LIVE_TRADING=1 (and the production Kalshi key)")
        self.mode, self.alerts, self.cmd_url = mode, alerts, (cmd_url or "").rstrip("/") or None
        self.fee_for = fee_for
        self._executor, self._data, self._rh = executor, data_client, robinhood
        self.ttl_s, self.max_contracts, self.daily_notional = ttl_s, max_contracts, daily_notional
        self.journal_path, self.http_get, self.clock, self.stream = journal_path, http_get, clock, stream
        self.pending: dict[str, dict[str, Any]] = {}
        self.results: dict[str, dict[str, Any]] = {}      # token -> the tap's result record
        self.auto_results: dict[str, dict[str, Any]] = {}
        self.auto_practice_s = auto_practice_s
        self.spent_today, self.day = 0.0, None
        self.ledger_path: Optional[str] = None     # None: execution/ledger.default_path(env)
        self._ledger: Any = None
        self._fee_mults: Any = None                # execution/ledger.FeeMultipliers, built with the executor
        self.journal_errors = 0
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ---- lazily built venue access (no network until a tap) -----------------------------
    def data_client(self) -> Any:
        if self._data is None:
            from ..venues.kalshi import KalshiClient

            self._data = KalshiClient(env=os.environ.get("KALSHI_DATA_ENV", "prod"))
        return self._data

    def robinhood(self) -> Any:
        if self._rh is None:
            from ..venues.robinhood import RobinhoodAdapter

            self._rh = RobinhoodAdapter()
        return self._rh

    def executor(self) -> Any:
        if self._executor is None and self.mode in ("demo", "live"):
            from ..execution.kalshi import KalshiExecutor
            from ..venues.kalshi import KalshiClient

            self._executor = KalshiExecutor(KalshiClient(env="prod" if self.mode == "live" else "demo"))
        return self._executor

    def ledger(self) -> Any:
        """The order ledger of the executor's environment (demo / live only); raises
        ``LedgerError`` when it cannot be opened (then nothing is sent)."""
        if self._ledger is None:
            from ..execution.ledger import OrderLedger

            self._ledger = OrderLedger.for_client(self.executor().client, path=self.ledger_path, clock=self.clock)
        return self._ledger

    def reconcile(self) -> list:
        """Resolve this environment's open orders against the exchange (never raises)."""
        if self.mode not in ("demo", "live"):
            return []
        try:
            res = self.ledger().reconcile(self.executor().client, self.clock())
        except Exception as e:  # noqa: BLE001
            res = [{"error": repr(e)[:300]}]
        changed = [r for r in res if r.get("before") != r.get("after") or r.get("error")]
        if changed:
            self._journal({"event": "reconcile", "results": changed})
        return res

    def _reconcile_later(self, *delays: float) -> None:
        for d in delays:
            t = threading.Timer(float(d), self.reconcile)
            t.daemon = True
            t.start()

    # ---- 1. an arb is pushed: issue a button ---------------------------------------------
    def register(self, event_key: str, title: str, sized: dict, quotes_by_venue: dict, now: Optional[float] = None,
                 practice: bool = False) -> Optional[dict[str, Any]]:
        """A button for a Kalshi + Robinhood (non-Kalshi book) arb, or None. ``sized`` is the
        alert's sized ArbResult (dict); prices are the asks it was sized at."""
        if self.mode == "off" or not self.cmd_url:
            return None
        legs = list(sized.get("legs") or [])
        ki = [i for i, l in enumerate(legs) if l.get("venue") == "kalshi"]
        ri = [i for i, l in enumerate(legs) if l.get("venue") == "robinhood"]
        if len(legs) != 2 or len(ki) != 1 or len(ri) != 1:
            return None
        kl, rl = legs[ki[0]], legs[ri[0]]
        kq, rq = _find_quote(quotes_by_venue, "kalshi", kl.get("market_id")), _find_quote(quotes_by_venue, "robinhood", rl.get("market_id"))
        if kq is None or rq is None or str(getattr(rq, "book_id", "")) == "kalshi":
            return None                     # a Robinhood KX quote is Kalshi's own book: not an arb pair
        n = int(min(float(sized.get("contracts") or 0), self.max_contracts))
        if n <= 0:
            return None
        kfee, rfee = self.fee_for(kq), self.fee_for(rq)
        k_ask, r_ask = float(kl["price"]), float(rl["price"])
        # Share the room between the legs: Robinhood may cost up to half the slack above its
        # ask; the Kalshi limit is then whatever still locks the set given that price.
        r_top = max_price(rfee, all_in(kfee, k_ask, n), n, r_ask) or r_ask
        rh_max = round(r_ask + math.floor(round((r_top - r_ask) / TICK, 6) / 2) * TICK, 2)
        k_limit = max_price(kfee, all_in(rfee, rh_max, n), n, 0.01)
        if k_limit is None or k_limit < k_ask - 1e-9:
            if not practice:
                return None                 # not a lock at the alert's own prices
            # A practice pair need not lock: rehearse buying at exactly the prices it was issued at.
            k_limit, rh_max = k_ask, r_ask
        # Bound split-fill rounding and price-improvement fees conservatively.
        k_unit = Decimal(str(k_limit)) + Decimal(str(kfee.fee(min(k_limit, 0.5), 1, "taker")))
        r_unit = Decimal(str(rh_max)) + Decimal(str(rfee.fee(min(rh_max, 0.5), 1, "taker")))
        n = min(n, int(self.side_cap // k_unit), int(self.side_cap // r_unit))
        if n <= 0:
            return None
        if not practice and all_in(kfee, k_limit, n) + all_in(rfee, rh_max, n) > 1.0 + 1e-12:
            return None
        now = self.clock() if now is None else now
        token = secrets.token_hex(8)
        meta_k = getattr(kq, "meta", None) or {}
        meta_r = getattr(rq, "meta", None) or {}

        def label(leg: dict, side: str) -> str:   # Robinhood labels a NO leg "NO <team>"; the side says it once
            t = str(leg.get("label") or leg.get("outcome") or "?")
            return t[3:] if side == "no" and t.upper().startswith("NO ") else t
        spec = {
            "token": token, "created": now, "expires": now + self.ttl_s, "event_key": event_key, "title": title, "count": n,
            "practice": practice, "alert_margin": sized.get("margin"), "alert_cost": sized.get("total_cost"),
            "side_cap": str(self.side_cap),
            "kalshi": {"fee_multiplier_stated": _stated_multiplier(kq),
                       "ticker": meta_k.get("ticker") or str(kl.get("market_id")).split("#")[0], "side": kl.get("side") or meta_k.get("side") or "yes",
                       "label": label(kl, str(kl.get("side") or meta_k.get("side") or "yes").lower()), "outcome": kl.get("outcome"), "alert_ask": k_ask, "limit": k_limit,
                       "exchange_index": meta_k.get("exchange_index"), "url": kl.get("url")},
            "robinhood": {"contract_id": meta_r.get("contract_id") or str(rl.get("market_id")).split("#")[0], "side": rl.get("side") or meta_r.get("side") or "yes",
                          "label": label(rl, str(rl.get("side") or meta_r.get("side") or "yes").lower()), "outcome": rl.get("outcome"), "alert_ask": r_ask, "max": rh_max,
                          "exchange": meta_r.get("exchange"), "url": rl.get("url")},
            "action": {"action": "http", "label": "Robinhood done - buy Kalshi", "url": self.cmd_url, "method": "POST", "body": f"arb {token}", "clear": True},
        }
        spec["_fees"] = (kfee, rfee)
        # Reprice the displayed ticket at its actual capped quantity (rounding is
        # per order, so scaling the original dollars would be incorrect).
        if n != sized.get("contracts"):
            capped_legs = []
            total = Decimal("0")
            for leg in legs:
                fee = kfee if leg["venue"] == "kalshi" else rfee
                px = Decimal(str(leg["price"]))
                charge = Decimal(str(fee.fee(px, n, "taker")))
                cost = px * n + charge
                capped_legs.append(dict(leg, contracts=n, fee=float(charge), cost=float(cost),
                                        fee_detail=fee.breakdown(px, n, "taker"), vwap=None))
                total += cost
            spec["ticket"] = dict(sized, contracts=n, legs=capped_legs, total_cost=float(total),
                                  payout=n, profit=float(Decimal(n) - total),
                                  margin=float((Decimal(n) - total) / n))
            if sized.get("tie_payout_total") is not None:
                spec["ticket"]["tie_margin"] = float(Decimal(str(sized["tie_payout_total"])) - total / n)
        with self._lock:
            self.pending[token] = spec
            self._expire(now)
        self._journal({"event": "issued", **{k: v for k, v in spec.items() if not k.startswith("_")}})
        self.start()
        if self.auto_practice_s and not practice:
            t = threading.Timer(float(self.auto_practice_s), self._auto_safe, args=(token,))
            t.daemon = True
            t.start()
        return spec

    def _auto_safe(self, token: str) -> None:
        try:
            self.auto_practice(token)
        except Exception as e:   # a practice tap must never take the process down
            self._journal({"event": "auto-practice-error", "token": token, "error": repr(e)[:300]})

    def _expire(self, now: float) -> None:
        for t in [t for t, p in self.pending.items() if now > p["expires"] + 600]:
            del self.pending[t]

    # ---- 2. the tap arrives ----------------------------------------------------------------
    def start(self) -> None:
        """Poll the command topic once a second on a daemon thread (idempotent)."""
        if self.mode == "off" or not self.cmd_url or self.http_get is False:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="arb-button", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _get(self, url: str) -> str:
        if self.http_get:
            return self.http_get(url)
        from ..venues.http import HttpClient

        return HttpClient(timeout=15).get(url, raw=True)

    def poll_once(self, since: str) -> tuple[str, list[dict[str, Any]]]:
        """(new ``since``, results): read the command topic after ``since`` and act on it."""
        text = self._get(f"{self.cmd_url}/json?poll=1&since={since}")
        results = []
        for line in (text or "").splitlines():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("event") != "message":
                continue
            since = msg.get("id") or since
            r = self.handle_message(msg)
            if r is not None:
                results.append(r)
        return since, results

    def handle_message(self, msg: dict[str, Any]) -> Optional[dict[str, Any]]:
        """One ntfy JSON message: act on ``arb <token>``."""
        if msg.get("event") != "message":
            return None
        body = str(msg.get("message") or "").strip().split()
        if len(body) == 2 and body[0] == "arb":
            return self.fire(body[1])
        return None

    def _stream(self, since: str):
        """Messages from one long-lived subscription (ntfy sends a keepalive every ~45 s)."""
        import urllib.request

        req = urllib.request.Request(f"{self.cmd_url}/json?since={since}", headers={"User-Agent": "arb-engine"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw in resp:
                if self._stop.is_set():
                    return
                try:
                    yield json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue

    def _loop(self) -> None:
        since, backoff = str(int(self.clock())), 5.0
        while not self._stop.is_set():
            try:
                for msg in (self.stream(since) if self.stream else self._stream(since)):
                    since = msg.get("id") or since
                    self.handle_message(msg)
                    backoff = 5.0
            except Exception as e:   # a dropped connection or a 429 must not end the listener
                self._journal({"event": "listen-error", "error": repr(e)[:300]})
                backoff = min(backoff * 2, 120.0)
            self._stop.wait(backoff)

    # ---- 3. verify live prices and buy the Kalshi leg -------------------------------------
    def live_prices(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Kalshi's live order book for the leg (asks, best first) and Robinhood's live quote."""
        from ..venues.kalshi import parse_orderbook

        k, r = spec["kalshi"], spec["robinhood"]
        out: dict[str, Any] = {}
        try:
            yes_book, no_book = parse_orderbook(self.data_client().orderbook(k["ticker"], depth=20))
            book = no_book if str(k["side"]).lower() == "no" else yes_book
            out["kalshi_asks"] = [(float(l.price), float(l.size)) for l in book.asks]
        except Exception as e:
            out["kalshi_error"] = repr(e)[:200]
            out["kalshi_asks"] = []
        try:
            qd = self.robinhood().quotes([r["contract_id"]]).get(r["contract_id"]) or {}
            no = str(r["side"]).lower() == "no"
            out["rh_ask"] = _f(qd.get("no_ask_price" if no else "yes_ask_price"))
            out["rh_bid"] = _f(qd.get("no_bid_price" if no else "yes_bid_price"))
            out["rh_state"] = qd.get("state")
        except Exception as e:
            out["rh_error"] = repr(e)[:200]
        out["at"] = self.clock()          # both prices in hand
        return out

    def fire(self, token: str, now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """The tap: check live prices against the alert and buy (or simulate) the Kalshi leg.
        Returns the result record; None for a token this process did not issue."""
        with self._lock:
            spec = self.pending.get(token)
            if spec is None:
                return None
            now = self.clock() if now is None else now
            if spec.get("used"):
                return None
            spec["used"] = True
        k, r, n = spec["kalshi"], spec["robinhood"], spec["count"]
        rec: dict[str, Any] = {"event": "tap", "token": token, "mode": self.mode, "event_key": spec["event_key"], "title": spec["title"],
                               "count": n, "practice": spec.get("practice", False), "tap_after_s": round(now - spec["created"], 2)}
        if now > spec["expires"]:
            rec.update(status="expired", filled=0, unhedged=n)
            return self._finish(spec, rec)
        return self._finish(spec, self._evaluate(spec, rec, now, simulate=self.mode == "paper"))

    def auto_practice(self, token: str, now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """A practice tap ``auto_practice_s`` after a real arb's button was issued - about the
        time it takes to buy the Robinhood leg - always simulated (never an order, whatever the
        mode), never pushed, and it does not use the token up: your own tap still works. It
        answers, for every arb pushed: at that moment, did both live prices still match, could
        you have bought Robinhood at or under its max, and would the Kalshi leg have filled?"""
        spec = self.pending.get(token)
        if spec is None:
            return None
        now = self.clock() if now is None else now
        rec: dict[str, Any] = {"event": "auto-practice", "token": token, "mode": self.mode, "event_key": spec["event_key"], "title": spec["title"],
                               "count": spec["count"], "tap_after_s": round(now - spec["created"], 2)}
        rec = self._evaluate(spec, rec, now, simulate=True, capped=False)
        self.auto_results[token] = rec
        self._journal(rec)
        return rec

    def confirm(self, token: str, now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """The tap's own check, run at the moment of the alert: Kalshi's live order book and
        Robinhood's live quote, the Kalshi buy walked against the book, nothing sent. Kalshi's
        ``/markets`` price - what the sweep and the fast lane read - trails its order book by
        5-10 s while a game moves (2026-09-26: 34 % of busy college reads disagreed), so an arb
        seen there can already be gone from the book. Journalled as ``confirm``."""
        spec = self.pending.get(token)
        if spec is None:
            return None
        now = self.clock() if now is None else now
        rec: dict[str, Any] = {"event": "confirm", "token": token, "mode": self.mode, "event_key": spec["event_key"], "title": spec["title"],
                               "count": spec["count"], "tap_after_s": round(now - spec["created"], 2)}
        rec = self._evaluate(spec, rec, now, simulate=True, capped=False)
        self._journal(rec)
        return rec

    def withdraw(self, token: str, reason: str = "") -> None:
        """Forget a button whose push was not sent: no tap can use it and no practice tap runs."""
        with self._lock:
            spec = self.pending.pop(token, None)
        if spec is not None:
            self._journal({"event": "withdrawn", "token": token, "event_key": spec["event_key"], "reason": reason})

    def _evaluate(self, spec: dict[str, Any], rec: dict[str, Any], now: float, simulate: bool, capped: bool = True) -> dict[str, Any]:
        """Read both live prices, compare them with the alert, and buy - or, ``simulate``,
        walk Kalshi's live book up to the limit."""
        k, r, n = spec["kalshi"], spec["robinhood"], spec["count"]
        token = spec["token"]
        live = self.live_prices(spec)
        asks = live.get("kalshi_asks") or []
        rec.update(kalshi_live_ask=asks[0][0] if asks else None, kalshi_live_depth=asks[0][1] if asks else None,
                   rh_live_ask=live.get("rh_ask"), rh_live_bid=live.get("rh_bid"), rh_state=live.get("rh_state"),
                   kalshi_alert_ask=k["alert_ask"], kalshi_limit=k["limit"], rh_alert_ask=r["alert_ask"], rh_max=r["max"],
                   check_s=round(live["at"] - now, 2))
        for e in ("kalshi_error", "rh_error"):
            if live.get(e):
                rec[e] = live[e]
        kfee, rfee = spec["_fees"]
        # Does the set still lock at live prices (Robinhood at its live ask, Kalshi at its)?
        if rec["kalshi_live_ask"] is not None and rec["rh_live_ask"] is not None:
            rec["live_set_cost"] = round(all_in(kfee, rec["kalshi_live_ask"], n) + all_in(rfee, rec["rh_live_ask"], n), 4)
            rec["still_locks_live"] = rec["live_set_cost"] <= 1.0 + 1e-12
        # Could Robinhood still be bought at or under the push's max?
        if rec["rh_live_ask"] is not None:
            rec["rh_within_max"] = rec["rh_live_ask"] <= r["max"] + 1e-9
        # Caps: a real order is reserved against the ledger's daily budget (fees included,
        # shared across processes and restarts) in _send_real; practice has no exposure.
        self._roll_day(now)
        room = self.daily_notional - self.spent_today if (capped and simulate) else float("inf")
        count = n if room >= n * k["limit"] else int(room // k["limit"])
        if count <= 0:
            rec.update(status="skipped", reason="daily notional cap reached", filled=0, unhedged=n)
            return rec
        if simulate:
            filled, cost = 0, Decimal("0")
            levels = []
            for px, size in asks:
                if px > k["limit"] + 1e-9 or filled >= count:
                    break
                take = int(min(size, count - filled))
                if take <= 0:
                    continue
                levels.append((px, take))
                filled += take
                cost += Decimal(str(px)) * take + Decimal(str(kfee.fee(px, take, "taker")))
            rec.update(status="simulated", filled=filled, levels=levels, kalshi_cost=float(cost))
        else:
            rec.update(self._send_real(spec, count, kfee, now))
            if rec.get("status") == "UNKNOWN":
                rec["unhedged"] = None      # the Kalshi leg may or may not be bought: say so, not a number
                rec["would_lock"] = False
                return rec
        rec["unhedged"] = n - int(rec.get("filled") or 0)
        spent = float(rec.get("kalshi_cost") or 0.0)
        if rec.get("filled"):
            f = int(rec["filled"])
            rh_cost = float(Decimal(str(r["alert_ask"])) * f + Decimal(str(rfee.fee(r["alert_ask"], f, "taker"))))
            rh_cost_max = float(Decimal(str(r["max"])) * f + Decimal(str(rfee.fee(r["max"], f, "taker"))))
            rec["locked_sets"] = f
            rec["profit_at_alert_rh_price"] = round(f - rh_cost - spent, 2)
            rec["profit_at_rh_max"] = round(f - rh_cost_max - spent, 2)
        rec["would_lock"] = bool(rec.get("rh_within_max")) and rec["unhedged"] == 0
        return rec

    def _send_real(self, spec: dict[str, Any], count: int, kfee: Any, now: float) -> dict[str, Any]:
        """Demo / live: reserve in the ledger, send the IOC with the ledger's id, record the
        answer. Returns the fields for the tap's record."""
        from ..execution.ledger import ACCEPTED, Budget, FeeMultipliers, LedgerError, refusal_hint, worst_cost
        from ..matching.normalize import game_event_key

        k, token = spec["kalshi"], spec["token"]
        try:
            ex, led = self.executor(), self.ledger()
            if self._fee_mults is None:
                self._fee_mults = FeeMultipliers(ex.client, clock=self.clock)
            mult, why = self._fee_mults.resolve(k["ticker"], k.get("fee_multiplier_stated"))
            if mult is None:
                return {"status": "skipped", "reason": why, "filled": 0}
            if worst_cost(k["limit"], count, mult) > self.side_cap:
                return {"status": "skipped", "reason": "per-side cap including fees exceeded", "filled": 0}
            res = led.reserve(strategy="button", ticker=k["ticker"], side=str(k["side"]).lower(), count=count, limit_price=k["limit"],
                              event_key=spec["event_key"], game_key=game_event_key(spec["event_key"]), dedupe_key=f"button:{token}",
                              budget=Budget(daily=Decimal(str(self.daily_notional))), fee_multiplier=mult, now=now,
                              detail={"token": token, "title": spec.get("title"), "rh": spec.get("robinhood", {}).get("contract_id")})
        except LedgerError as e:
            return {"status": "blocked", "reason": f"order ledger: {e}", "filled": 0}
        except Exception as e:  # noqa: BLE001 - e.g. no credentials: nothing sent
            return {"status": "error", "reason": repr(e)[:300], "filled": 0}
        if not res.ok:
            blocked = "unknown outcome" in res.reason
            return {"status": "blocked" if blocked else "skipped", "reason": res.reason, "filled": 0}
        out: dict[str, Any] = {"intent_id": res.intent_id, "client_order_id": res.client_order_id, "sent_count": res.count}
        req_ts = self.clock()
        try:
            plan = ex.plan(k["ticker"], "buy", str(k["side"]).lower(), res.count, float(k["limit"]), post_only=False, exchange_index=k.get("exchange_index"),
                           note=f"arb button {token}", time_in_force="immediate_or_cancel", client_order_id=res.client_order_id)
        except Exception as e:  # noqa: BLE001 - refused before sending
            led.rejected(res.intent_id, f"plan refused: {e!r}")
            return {**out, "status": "error", "reason": f"plan refused: {e!r}"[:300], "filled": 0}
        try:
            result = ex.execute(plan, confirm=True)
        except Exception as e:  # noqa: BLE001 - the request may have reached the exchange
            try:
                led.ambiguous(res.intent_id, f"{type(e).__name__}: {e}"[:300], req_ts=req_ts, hint=refusal_hint(e))
            except LedgerError:
                pass
            self._reconcile_later(3.0, 15.0, 45.0)
            return {**out, "status": "UNKNOWN", "reason": f"{type(e).__name__}: {e}"[:300], "filled": None, "req_ts": req_ts, "resp_ts": self.clock()}
        resp_ts = self.clock()
        if result.get("status") != "SUBMITTED":     # a gate refused it: nothing was sent
            led.rejected(res.intent_id, f"executor returned {result.get('status')!r}")
            return {**out, "status": "error", "reason": f"executor returned {result.get('status')!r}", "filled": 0}
        resp = result.get("response") or {}
        try:
            state = led.accepted(res.intent_id, resp, now=resp_ts, req_ts=req_ts)
        except LedgerError as e:
            state = f"unrecorded: {e}"
        self._reconcile_later(3.0, 15.0)
        od = resp.get("order") if isinstance(resp.get("order"), dict) else resp
        if state != ACCEPTED:
            return {**out, "status": "UNKNOWN", "reason": "create response without an order id" if not str(state).startswith("unrecorded") else state,
                    "filled": None, "req_ts": req_ts, "resp_ts": resp_ts}
        filled = int(_f(od.get("fill_count")) or 0)
        avg = _f(od.get("average_fill_price"))
        # Provisional until the ledger reads the order back: fills at the reported average
        # price (else the limit) plus the fee model's fee; the ledger keeps the exchange's.
        px = avg if avg is not None else float(k["limit"])
        cost = filled * px + (float(kfee.fee(px, filled, "taker")) if filled > 0 else 0.0)
        return {**out, "status": "SUBMITTED", "order_id": od.get("order_id") or od.get("id"), "filled": filled, "kalshi_cost": round(cost, 4),
                "kalshi_cost_provisional": True, "average_fill_price": od.get("average_fill_price"), "average_fee_paid": od.get("average_fee_paid"),
                "req_ts": req_ts, "resp_ts": resp_ts, "latency_ms": round((resp_ts - req_ts) * 1000, 1)}

    def _roll_day(self, now: float) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if day != self.day:
            self.day, self.spent_today = day, 0.0

    # ---- 4. report ------------------------------------------------------------------------
    def describe(self, spec: dict[str, Any], rec: dict[str, Any]) -> tuple[str, str]:
        """(title, body) of the ARB FILL push."""
        k, r, n = spec["kalshi"], spec["robinhood"], spec["count"]
        tag = {"paper": "PRACTICE", "demo": "DEMO", "live": "LIVE"}.get(self.mode, self.mode.upper())
        lines = []
        st = rec.get("status")
        filled = int(rec.get("filled") or 0)
        verb = "would buy" if self.mode == "paper" else "bought"
        if st == "expired":
            title = f"{tag} button expired - {spec['title']}"
            lines.append(f"Tapped {rec['tap_after_s']:.0f}s after the alert (limit {self.ttl_s:.0f}s): nothing bought on Kalshi.")
        elif st == "UNKNOWN":
            title = f"{tag} UNKNOWN - {spec['title']}"
            lines.append(f"Kalshi: the order for {rec.get('sent_count') or n} {k['label']} {str(k['side']).upper()} (limit {_c(k['limit'])}) was sent but its result is unknown "
                         f"({rec.get('reason')}). Check Kalshi's orders before selling the Robinhood leg; no new order is sent until the engine finds it.")
        elif st == "blocked":
            title = f"{tag} BLOCKED - {spec['title']}"
            lines.append(f"Kalshi: nothing sent - {rec.get('reason')}")
        elif filled >= n:
            title = f"{tag} OK - {spec['title']}"
            lines.append(f"Kalshi: {verb} {filled} {k['label']} {str(k['side']).upper()} {self._at(rec, k)}")
        elif filled > 0:
            title = f"{tag} PARTIAL - {spec['title']}"
            lines.append(f"Kalshi: {verb} only {filled} of {n} {k['label']} {str(k['side']).upper()} {self._at(rec, k)} (limit {_c(k['limit'])})")
        else:
            title = f"{tag} MISSED - {spec['title']}"
            why = rec.get("reason") or (f"Kalshi ask is {_c(rec.get('kalshi_live_ask'))} now, above the {_c(k['limit'])} limit" if rec.get("kalshi_live_ask") is not None else "no Kalshi price")
            lines.append(f"Kalshi: {'would not buy' if self.mode == 'paper' else 'nothing bought'} - {why}")
        lines.append(f"Live now: Kalshi {k['label']} {_c(rec.get('kalshi_live_ask'))} (alert {_c(k['alert_ask'])}), "
                     f"Robinhood {r['label']} {_c(rec.get('rh_live_ask'))} (alert {_c(r['alert_ask'])})")
        if rec.get("still_locks_live") is not None:
            lines.append("Both live prices still make the arb" if rec["still_locks_live"] else "At live prices the arb is gone")
        if rec.get("locked_sets"):
            lines.append(f"Locked {rec['locked_sets']} sets: {_money(rec['profit_at_alert_rh_price'])} if you paid {_c(r['alert_ask'])} on Robinhood "
                         f"({_money(rec['profit_at_rh_max'])} at {_c(r['max'])})")
        if rec.get("unhedged") and st not in ("expired", "UNKNOWN"):
            lines.append(f"Unhedged: {rec['unhedged']} {r['label']} {str(r['side']).upper()} on Robinhood - sell them (bid {_c(rec.get('rh_live_bid'))}) or wait")
        if self.mode == "paper":
            lines.append("Practice: no order was sent.")
        return title, "\n".join(lines)

    def _at(self, rec: dict[str, Any], k: dict[str, Any]) -> str:
        lv = rec.get("levels") or []
        if lv:
            lo, hi = lv[0][0], lv[-1][0]
            return f"at {_c(lo)}" if abs(hi - lo) < 1e-9 else f"at {_c(lo)}-{_c(hi)}"
        return f"(limit {_c(k['limit'])})"

    def _finish(self, spec: dict[str, Any], rec: dict[str, Any]) -> dict[str, Any]:
        title, body = self.describe(spec, rec)
        rec["title_text"], rec["body_text"] = title, body
        self.results[spec["token"]] = rec
        self._journal(rec)
        if self.alerts is not None:
            try:
                self.alerts.journal("alert", title="ARB FILL", msg=body, event=spec["event_key"], button=rec)
                if getattr(self.alerts, "ntfy", None):
                    self.alerts.push("ARB FILL", body, event=spec["event_key"], side=spec["token"], force=True, headline=title)
            except Exception:
                pass
        return rec

    def _journal(self, rec: dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(self.journal_path) or ".", exist_ok=True)
            with open(self.journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": self.clock(), **rec}, default=str) + "\n")
        except OSError as e:
            self.journal_errors += 1
            if self.journal_errors == 1 and self.alerts is not None:   # once: the ledger keeps real orders
                try:
                    self.alerts.info(f"arb button journal {self.journal_path} not writable: {e!r}")
                except Exception:
                    pass

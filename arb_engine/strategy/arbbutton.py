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

Every step is journalled to ``out/orders/arb_button.jsonl``.
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
                 stream: Optional[Callable[[str], Any]] = None, auto_practice_s: Optional[float] = None) -> None:
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
            "kalshi": {"ticker": meta_k.get("ticker") or str(kl.get("market_id")).split("#")[0], "side": kl.get("side") or meta_k.get("side") or "yes",
                       "label": label(kl, str(kl.get("side") or meta_k.get("side") or "yes").lower()), "outcome": kl.get("outcome"), "alert_ask": k_ask, "limit": k_limit,
                       "exchange_index": meta_k.get("exchange_index"), "url": kl.get("url")},
            "robinhood": {"contract_id": meta_r.get("contract_id") or str(rl.get("market_id")).split("#")[0], "side": rl.get("side") or meta_r.get("side") or "yes",
                          "label": label(rl, str(rl.get("side") or meta_r.get("side") or "yes").lower()), "outcome": rl.get("outcome"), "alert_ask": r_ask, "max": rh_max,
                          "exchange": meta_r.get("exchange"), "url": rl.get("url")},
            "action": {"action": "http", "label": "Robinhood done - buy Kalshi", "url": self.cmd_url, "method": "POST", "body": f"arb {token}", "clear": True},
        }
        spec["_fees"] = (kfee, rfee)
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
        # Caps
        self._roll_day(now)
        room = self.daily_notional - self.spent_today if capped else float("inf")
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
            try:
                ex = self.executor()
                plan = ex.plan(k["ticker"], "buy", k["side"], count, float(k["limit"]), post_only=False, exchange_index=k.get("exchange_index"),
                               note=f"arb button {token}", time_in_force="immediate_or_cancel")
                res = ex.execute(plan, confirm=True)
                od = (res.get("response") or {}).get("order") or res.get("response") or {}
                filled = int(_f(od.get("fill_count")) or 0)
                avg = _f(od.get("taker_fill_cost_dollars") or od.get("fill_cost_dollars"))
                rec.update(status=res.get("status"), order_id=od.get("order_id") or od.get("id"), filled=filled,
                           kalshi_cost=(avg if avg is not None else filled * k["limit"] + float(kfee.fee(k["limit"], max(filled, 1), "taker")) * (filled > 0)))
            except Exception as e:
                rec.update(status="error", reason=repr(e)[:300], filled=0)
        rec["unhedged"] = n - int(rec.get("filled") or 0)
        spent = float(rec.get("kalshi_cost") or 0.0)
        self.spent_today += spent if not simulate else 0.0
        if rec.get("filled"):
            f = int(rec["filled"])
            rh_cost = float(Decimal(str(r["alert_ask"])) * f + Decimal(str(rfee.fee(r["alert_ask"], f, "taker"))))
            rh_cost_max = float(Decimal(str(r["max"])) * f + Decimal(str(rfee.fee(r["max"], f, "taker"))))
            rec["locked_sets"] = f
            rec["profit_at_alert_rh_price"] = round(f - rh_cost - spent, 2)
            rec["profit_at_rh_max"] = round(f - rh_cost_max - spent, 2)
        rec["would_lock"] = bool(rec.get("rh_within_max")) and rec["unhedged"] == 0
        return rec

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
        if rec.get("unhedged") and st != "expired":
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
        except OSError:
            pass

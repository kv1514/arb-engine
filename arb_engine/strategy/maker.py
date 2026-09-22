"""Maker runner: rest Kalshi orders at prices that would *create* a fee-adjusted arbitrage
against the cheapest hedge on another venue, and shout when one fills.

    for each cross-venue event (e.g. NFL total 65.5):
        hedge  = cheapest fillable ask for the other side on Robinhood/Polymarket
        price  = min( max_buy_maker(hedge), kalshi ask - 1 tick )   # rest, never cross
        post-only buy on Kalshi at price for `size` contracts (if margin-if-filled >= min)
    every `interval` seconds:
        refresh Kalshi + hedge quotes, re-price / cancel orders whose margin decayed,
        poll fills -> HEDGE NOW alert with the max price to pay for the other leg
    every `rescan` seconds: full cross-venue scan to refresh the watchlist

Why maker: Kalshi charges makers 1.75% x p(1-p) instead of 7%, and resting below the ask
captures the spread, so tail lines that are 1% arbs as a taker become 2-3% as a maker.
The hedge leg is on a venue without an API (Robinhood/Rothera) or without wallet wiring
(Polymarket), so it is executed by hand from the alert — that leg risk is the strategy's
cost, and the runner keeps it small by only resting while the hedge is deep and by
re-checking every loop.

Brokers: PaperBroker (default; simulated fills from live prices), KalshiBroker (demo or
prod behind the execution gates). Everything is journaled to out/maker_journal.jsonl.

Safety (why each exists):
  * hedge venues default to Robinhood only. Global Polymarket is not executable for US
    persons (``compliance.executable_venues``), so a rest hedged there is naked; an
    operator opts in with ``hedge_venues=(..., "polymarket")`` and every alert then says
    the leg is not executable. Quotes flagged ``meta.restricted`` (Gamma geoblock) are
    never hedges, opt-in or not, and Robinhood's Kalshi-routed book is never a hedge.
  * ``hedge_cash`` bounds the cash the operator must find by hand if every resting order
    fills in one burst (sum of size x hedge ask over resting orders).
  * collateral is bounded by min(max_notional, Kalshi balance when a key exists).
  * the exchange status is polled every 30 s; when trading is not active every order is
    cancelled and nothing is rested until it is (Kalshi pauses around maintenance and
    would otherwise fill a stale rest on reopening).
  * every cancel journals its reason; shutdown cancels everything.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Optional

from .. import compliance
from ..fees.base import FeeModel
from ..fees.registry import fee_model_for_quote
from ..matching.matcher import MergedEvent, merge_snapshots
from ..models import OutcomeQuote
from ..quant.arbitrage import Leg, evaluate, max_price_for_leg
from .alerts import Alerter
from .broker import Broker, PaperBroker, RestingOrder

TICK = 0.01
STATUS_POLL_S = 30.0   # exchange-status cadence: not per loop (rate limit), not per minute (a pause fills stale rests)
BALANCE_POLL_S = 60.0

try:  # P01's settings registry; the module stays importable without it.
    from ..config import declare_setting as _declare_setting
except ImportError:  # pragma: no cover
    _declare_setting = None
if _declare_setting is not None:
    try:
        _declare_setting("maker_hedge_cash", env="MAKER_HEDGE_CASH", default=250.0, cast=float, doc="max dollars of hand-executed hedge legs the maker may leave resting at once (sum of size x hedge ask)")
        _declare_setting("maker_hedge_venues", env="MAKER_HEDGE_VENUES", default="robinhood", cast=str, doc="comma list of hedge venues for the maker; naming polymarket is the explicit opt-in to a non-executable hedge")
    except Exception:
        pass


@dataclass
class MakerConfig:
    sport: str = "nfl"
    market_types: tuple[str, ...] = ("total", "spread", "moneyline")
    size: float = 100            # contracts per resting order
    min_margin: float = 0.01     # required locked margin per $1 if the resting order fills and the hedge is taken now
    target_margin: float = 0.0   # margin baked into the max-buy price (0 = break-even bound; min_margin is the filter)
    max_orders: int = 8
    max_notional: float = 500.0  # dollars of resting collateral across all orders
    max_per_event: int = 1
    queue_ahead: bool = True     # only rest at or above the current best bid (otherwise you are deep in the queue)
    min_hedge_size: float = 1.0  # hedge ask must show at least this many contracts (unknown size = ok)
    interval: float = 10.0       # seconds between refresh loops
    rescan: float = 300.0        # seconds between full cross-venue scans
    cancel_slack: float = 0.005  # cancel when margin-if-filled drops below min_margin - slack
    hedge_venues: tuple[str, ...] = ("robinhood",)  # explicit list = opt-in; polymarket is not executable for US persons
    max_watches: int = 120       # watches kept between rescans (best margin first; orders always kept)
    hedge_cash: float = 250.0    # dollars of hand-executed hedge legs allowed to be resting at once (<= 0 = unbounded)
    status_poll: float = STATUS_POLL_S  # seconds between exchange-status polls


@dataclass
class Watch:
    key: str
    event_key: str
    title: str
    kalshi_ticker: str
    kalshi_side: str
    kalshi_outcome: str
    kalshi_label: str
    kalshi_fee: FeeModel
    exchange_index: Optional[int]
    hedge_venue: str
    hedge_id: str          # robinhood contract id / polymarket token id
    hedge_side: str        # yes | no (robinhood contract side) or "" for polymarket
    hedge_outcome: str
    hedge_label: str
    hedge_fee: FeeModel
    hedge_url: Optional[str]
    hedge_slug: Optional[str] = None
    # live state
    kalshi_bid: Optional[float] = None
    kalshi_ask: Optional[float] = None
    hedge_ask: Optional[float] = None
    hedge_size: Optional[float] = None
    desired_price: Optional[float] = None
    margin_if_filled: Optional[float] = None
    taker_arb: bool = False
    reason: str = ""
    order: Optional[RestingOrder] = None

    def to_public(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if k not in ("kalshi_fee", "hedge_fee", "order")}
        d["order"] = asdict(self.order) if self.order else None
        return d


class MarketFeed:
    """Live quotes for the watchlist (small, fast calls). Tests inject a fake."""

    def __init__(self, kalshi_client, robinhood_adapter=None, polymarket_adapter=None):
        self.kalshi = kalshi_client
        self.rh = robinhood_adapter
        self.pm = polymarket_adapter

    def kalshi_markets(self, tickers: Iterable[str]) -> dict[str, dict[str, Optional[float]]]:
        """Batched: ``GET /markets?tickers=a,b,c`` (50 per call) instead of one call per market."""
        out: dict[str, dict[str, Optional[float]]] = {}
        ts = list(dict.fromkeys(t for t in tickers if t))
        for i in range(0, len(ts), 50):
            chunk = ts[i : i + 50]
            try:
                markets = self.kalshi.get("/markets", {"tickers": ",".join(chunk), "limit": 100}).get("markets", [])
            except Exception:
                continue
            for m in markets:
                f = lambda k: _f(m.get(k))  # noqa: E731
                out[m["ticker"]] = {"yes_bid": f("yes_bid_dollars"), "yes_ask": f("yes_ask_dollars"), "no_bid": f("no_bid_dollars"), "no_ask": f("no_ask_dollars"), "yes_ask_size": f("yes_ask_size_fp"), "yes_bid_size": f("yes_bid_size_fp"), "status": m.get("status")}
        return out

    def robinhood_quotes(self, ids: Iterable[str]) -> dict[str, dict[str, Optional[float]]]:
        ids = list(dict.fromkeys(ids))
        if not ids or self.rh is None:
            return {}
        try:
            raw = self.rh.quotes(ids)
        except Exception:
            return {}
        out = {}
        for cid, q in raw.items():
            f = lambda k: _f(q.get(k))  # noqa: E731
            out[cid] = {"yes_ask": f("yes_ask_price"), "yes_bid": f("yes_bid_price"), "no_ask": f("no_ask_price"), "no_bid": f("no_bid_price"), "yes_ask_size": f("ask_size"), "no_ask_size": f("bid_size")}
        return out

    def polymarket_asks(self, slugs: Iterable[str]) -> dict[str, dict[str, Optional[float]]]:
        out = {}
        if self.pm is None:
            return out
        for slug in dict.fromkeys(s for s in slugs if s):
            try:
                ms = self.pm.http.get("https://gamma-api.polymarket.com/markets", {"slug": slug})
            except Exception:
                continue
            for m in ms or []:
                bb, ba = _f(m.get("bestBid")), _f(m.get("bestAsk"))
                out[slug] = {"ask0": ba, "bid0": bb, "ask1": (1 - bb) if bb is not None else None}
        return out


def _f(x: Any) -> Optional[float]:
    try:
        return float(x) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None


def _accepts_kwarg(fn: Any, name: str) -> bool:
    """Does ``fn`` take ``name`` (or **kwargs)? Lets this item pass ``resting=`` to a broker
    that grows it in another item without breaking the ones that have not."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _positional_arity(fn: Any) -> int:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return 0
    return sum(1 for p in params.values() if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD) and p.default is inspect.Parameter.empty) + sum(1 for p in params.values() if p.kind is inspect.Parameter.VAR_POSITIONAL)


class MakerRunner:
    def __init__(self, config: MakerConfig, feed: MarketFeed, broker: Optional[Broker] = None, alerter: Optional[Alerter] = None, settings: Optional[dict[str, Any]] = None, scan_fn: Optional[Callable[[], list[MergedEvent]]] = None, clock: Callable[[], float] = time.time, exchange_status: Optional[Callable[[], dict]] = None):
        self.cfg = config
        self.feed = feed
        self.broker = broker or PaperBroker()
        self.alerts = alerter or Alerter()
        self.settings = settings or {}
        self.scan_fn = scan_fn
        self.clock = clock
        self.watches: dict[str, Watch] = {}
        self.orders: list[RestingOrder] = []
        self.last_scan = -1e18   # first step() always rescans, whatever clock is injected
        self.fills: list[dict[str, Any]] = []
        # Eligibility: the explicit hedge_venues list is the opt-in; the table only warns.
        self.executable = compliance.executable_venues(self.settings)
        self.hedge_venues = tuple(v for v in (config.hedge_venues or ()) if v != "kalshi") or tuple(sorted(self.executable - {"kalshi"}))
        self.ineligible_hedges = {v: compliance.ineligible_reason(v, self.settings) for v in self.hedge_venues if v not in self.executable}
        self.skipped_ineligible: dict[str, int] = {}   # venue -> hedge candidates dropped (eligibility report)
        # Exchange status pause + balance-bounded collateral.
        self.exchange_status = exchange_status if exchange_status is not None else self._default_status_fn()
        self.paused: bool = False
        self.last_status_poll: float = -1e18
        self.last_balance_poll: float = -1e18
        self.balance: Optional[float] = None
        self._place_accepts_resting = _accepts_kwarg(self.broker.place, "resting")
        self._hedge_cash_refused: set[str] = set()

    def _default_status_fn(self) -> Optional[Callable[[], dict]]:
        client = getattr(self.feed, "kalshi", None)
        fn = getattr(client, "exchange_status", None)
        return fn if callable(fn) else None

    # ---- discovery -------------------------------------------------------------------------
    def discover(self, merged: Iterable[MergedEvent]) -> list[Watch]:
        """Build watches: for every event with a Kalshi quote on one side and a hedgeable ask
        on another venue for the other side (both directions).

        A hedge is a quote on one of ``hedge_venues`` (the explicit opt-in list) whose book is
        not Kalshi's own (Robinhood re-sells it)."""
        found: dict[str, Watch] = {}
        self.skipped_ineligible = {}
        for me in merged:
            info = me.info
            if info.market_type not in self.cfg.market_types or len(info.outcomes) != 2:
                continue
            if info.in_play:
                continue
            kq = {q.outcome: q for q in me.quotes_by_venue.get("kalshi", []) if q.meta.get("ticker")}
            for k_out, kquote in kq.items():
                other = [o for o in info.outcomes if o != k_out][0]
                hedges = []
                for v, qs in me.quotes_by_venue.items():
                    for q in qs:
                        if q.outcome != other or q.ask is None or q.book_id == "kalshi":
                            continue
                        # hedge_venues is the explicit opt-in list, so a venue on it is hedgeable
                        # even when Gamma's venue-wide restricted flag is on the quote; the
                        # eligibility note on every alert still says so.
                        if v not in self.hedge_venues:
                            self.skipped_ineligible[v] = self.skipped_ineligible.get(v, 0) + 1
                            continue
                        hedges.append(q)
                if not hedges:
                    continue
                best = None
                for h in hedges:
                    fee = fee_model_for_quote(h, self.settings)
                    cost = h.ask + fee.per_contract(h.ask, self.cfg.size)
                    if best is None or cost < best[0]:
                        best = (cost, h, fee)
                _, h, hfee = best
                if h.venue == "robinhood":
                    hedge_id, hedge_side = h.venue_market_id.split("#")[0], (h.meta.get("side") or "yes")
                else:  # polymarket: which of the market's two outcome tokens the hedge is
                    hedge_id, hedge_side = h.venue_market_id, str(h.meta.get("outcome_index", 0))
                key = f"{me.event_key}|kalshi:{k_out}"
                w = Watch(key=key, event_key=me.event_key, title=info.title(), kalshi_ticker=kquote.meta["ticker"], kalshi_side=kquote.meta.get("side") or "yes", kalshi_outcome=k_out, kalshi_label=info.labels.get(k_out, k_out), kalshi_fee=fee_model_for_quote(kquote, self.settings), exchange_index=kquote.meta.get("exchange_index"), hedge_venue=h.venue, hedge_id=hedge_id, hedge_side=hedge_side, hedge_outcome=other, hedge_label=info.labels.get(other, other), hedge_fee=hfee, hedge_url=h.url, hedge_slug=h.meta.get("slug"), kalshi_bid=kquote.bid, kalshi_ask=kquote.ask, hedge_ask=h.ask, hedge_size=h.ask_size)
                self._price(w)
                found[key] = w
        return list(found.values())

    # ---- pricing --------------------------------------------------------------------------
    def _price(self, w: Watch) -> None:
        w.desired_price, w.margin_if_filled, w.taker_arb, w.reason = None, None, False, ""
        if w.hedge_ask is None:
            w.reason = "no hedge ask"
            return
        if w.hedge_size is not None and w.hedge_size < self.cfg.min_hedge_size:
            w.reason = f"hedge size {w.hedge_size:g} < {self.cfg.min_hedge_size:g}"
            return
        hedge_leg = Leg(w.hedge_outcome, w.hedge_venue, w.hedge_ask, w.hedge_fee, role="taker")
        max_maker = max_price_for_leg([hedge_leg], w.kalshi_fee, self.cfg.size, self.cfg.target_margin, role="maker")
        if max_maker is None:
            w.reason = "no maker price locks the target margin"
            return
        price = max_maker
        if w.kalshi_ask is not None:
            if max_maker >= w.kalshi_ask - 1e-9:
                w.taker_arb = True  # crossing at the ask already locks the margin
            price = min(price, round(w.kalshi_ask - TICK, 4))
        if price < TICK:
            w.reason = "price below one tick"
            return
        r = evaluate([Leg(w.kalshi_outcome, "kalshi", price, w.kalshi_fee, role="maker"), hedge_leg], self.cfg.size)
        w.margin_if_filled = r.margin  # kept even when not placeable, to rank the watchlist
        if r.margin < self.cfg.min_margin:
            w.reason = f"margin if filled {r.margin:.3%} < min {self.cfg.min_margin:.3%}"
            return
        if self.cfg.queue_ahead and w.kalshi_bid is not None and price < w.kalshi_bid - 1e-9:
            w.reason = f"price {price:.2f} behind best bid {w.kalshi_bid:.2f}"
            return
        w.desired_price = price

    # ---- refresh --------------------------------------------------------------------------
    def refresh(self) -> dict[str, dict[str, float]]:
        ws = list(self.watches.values())
        if len(ws) > self.cfg.max_watches:
            keep = sorted(ws, key=lambda w: (w.order is None or w.order.status != "resting", -(w.margin_if_filled if w.margin_if_filled is not None else -9)))[: self.cfg.max_watches]
            self.watches = {w.key: w for w in keep}
            ws = keep
        km = self.feed.kalshi_markets(w.kalshi_ticker for w in ws)
        rh = self.feed.robinhood_quotes(w.hedge_id for w in ws if w.hedge_venue == "robinhood")
        pm = self.feed.polymarket_asks(w.hedge_slug for w in ws if w.hedge_venue == "polymarket")
        state: dict[str, dict[str, float]] = {}
        for w in ws:
            m = km.get(w.kalshi_ticker)
            if m:
                w.kalshi_bid, w.kalshi_ask = m.get(f"{w.kalshi_side}_bid"), m.get(f"{w.kalshi_side}_ask")
                state[w.kalshi_ticker] = {k: v for k, v in m.items() if isinstance(v, (int, float))}
                if m.get("status") not in (None, "active", "open"):
                    w.reason = f"kalshi market {m.get('status')}"
            if w.hedge_venue == "robinhood":
                q = rh.get(w.hedge_id)
                if q:
                    w.hedge_ask, w.hedge_size = q.get(f"{w.hedge_side}_ask"), q.get(f"{w.hedge_side}_ask_size")
            elif w.hedge_venue == "polymarket" and w.hedge_slug in pm:
                # bestAsk/bestBid describe outcome 0's token; outcome 1's ask is 1 - outcome 0's bid.
                p = pm[w.hedge_slug]
                w.hedge_ask = p.get("ask1") if w.hedge_side == "1" else p.get("ask0")
            self._price(w)
        return state

    # ---- limits ---------------------------------------------------------------------------
    def _cancel(self, o: RestingOrder, reason: str, watch: Optional[str] = None) -> None:
        """Every cancel goes through here so the journal always carries a reason."""
        try:
            self.broker.cancel(o)
            self.alerts.info(f"cancel {o.ticker} {o.side} @ {o.price:.2f} x{o.remaining:g}: {reason}", order_id=o.order_id, watch=watch or o.watch_key, reason=reason)
        except Exception as e:
            self.alerts.info(f"cancel failed {o.order_id} ({reason}): {e}", order_id=o.order_id, watch=watch or o.watch_key, reason=reason)

    def _resting(self) -> list[RestingOrder]:
        return [o for o in self.orders if o.status == "resting"]

    def hedge_exposure(self) -> float:
        """Dollars of hand-executed hedge legs if every resting order filled now (size x hedge ask)."""
        total = 0.0
        for w in self.watches.values():
            o = w.order
            if o is not None and o.status == "resting":
                total += o.remaining * (w.hedge_ask if w.hedge_ask is not None else 1.0)
        return total

    def collateral_cap(self) -> float:
        """min(max_notional, Kalshi balance) — the balance only when the broker has a signed
        client (paper/demo without a key: max_notional alone). Polled at most once a minute.

        Kalshi's ``/portfolio/balance`` already has resting-order collateral deducted, so the
        caller compares ``resting notional + new order`` against max_notional but only the
        new order against the live balance (``available_balance``)."""
        cap = self.cfg.max_notional
        client = getattr(self.broker, "client", None)
        if client is None or not getattr(client, "has_credentials", False) or not callable(getattr(client, "balance", None)):
            return cap
        now = self.clock()
        if now - self.last_balance_poll >= BALANCE_POLL_S:
            self.last_balance_poll = now
            try:
                bal = client.balance()
                cents = bal.get("balance") if isinstance(bal, dict) else bal
                self.balance = float(cents) / 100.0 if cents is not None else None   # Kalshi reports cents
            except Exception as e:
                self.alerts.info(f"balance poll failed: {e!r}")
        return cap

    def available_balance(self) -> Optional[float]:
        """Last polled Kalshi balance in dollars (None without a signed client)."""
        return self.balance

    # ---- reconcile ------------------------------------------------------------------------
    def reconcile(self) -> None:
        cfg = self.cfg
        # 1) cancel orders whose watch no longer wants that price
        for w in self.watches.values():
            o = w.order
            if o is None or o.status != "resting":
                continue
            drop = None
            if w.desired_price is None:
                drop = w.reason or "no longer priceable"
            elif w.margin_if_filled is not None and w.margin_if_filled < cfg.min_margin - cfg.cancel_slack:
                drop = f"margin decayed to {w.margin_if_filled:.3%}"
            elif abs(w.desired_price - o.price) >= TICK - 1e-9:
                drop = f"re-price {o.price:.2f} -> {w.desired_price:.2f}"
            if drop:
                self._cancel(o, drop, watch=w.key)
                w.order = None
        if self.paused:
            return  # exchange not trading: manage nothing new until step() sees it active again
        # 2) place for the best watches within limits
        resting = self._resting()
        notional = sum(o.notional for o in resting)
        collateral_cap = self.collateral_cap()
        available = self.available_balance()
        hedge_cash = self.hedge_exposure()
        per_event: dict[str, int] = {}
        for o in resting:
            per_event[o.watch_key.split("|")[0]] = per_event.get(o.watch_key.split("|")[0], 0) + 1
        ranked = sorted((w for w in self.watches.values() if w.desired_price is not None and w.order is None), key=lambda w: -(w.margin_if_filled or 0))
        for w in ranked:
            if len(self._resting()) >= cfg.max_orders:
                break
            ev = w.event_key
            if per_event.get(ev, 0) >= cfg.max_per_event:
                continue
            cost = w.desired_price * cfg.size
            if notional + cost > collateral_cap:
                continue
            if available is not None and cost > available + 1e-9:  # balance already nets resting collateral
                continue
            hedge_cost = cfg.size * (w.hedge_ask if w.hedge_ask is not None else 1.0)
            if cfg.hedge_cash > 0 and hedge_cash + hedge_cost > cfg.hedge_cash + 1e-9:
                if w.key not in self._hedge_cash_refused:  # once per watch, like the other limit refusals
                    self._hedge_cash_refused.add(w.key)
                    self.alerts.info(f"hedge-cash: not resting {w.kalshi_ticker} {w.kalshi_side} — hedge {w.hedge_label} on {w.hedge_venue} would need ${hedge_cost:.0f} on top of ${hedge_cash:.0f} already exposed (cap ${cfg.hedge_cash:g})", watch=w.key, reason="hedge-cash")
                continue
            if w.taker_arb:
                self.alerts.alert("TAKER ARB", f"{w.title}: buy {w.kalshi_label} on Kalshi at the ask {w.kalshi_ask:.2f} and {w.hedge_label} on {w.hedge_venue} at {w.hedge_ask:.2f} ({compliance.eligibility_note(w.hedge_venue, self.settings)}) — locks ≥ {w.margin_if_filled:.2%}", watch=w.key, event=w.event_key, kalshi_ticker=w.kalshi_ticker, hedge_url=w.hedge_url, hedge_eligibility=compliance.eligibility_note(w.hedge_venue, self.settings))
            kwargs: dict[str, Any] = {"watch_key": w.key, "exchange_index": w.exchange_index}
            if self._place_accepts_resting:  # P12's SelfMatchGuard wants our own open orders on this ticker
                kwargs["resting"] = [o for o in self._resting() if o.ticker == w.kalshi_ticker]
            try:
                o = self.broker.place(w.kalshi_ticker, w.kalshi_side, w.desired_price, cfg.size, **kwargs)
            except Exception as e:
                self.alerts.info(f"place failed {w.kalshi_ticker}: {e}", watch=w.key)
                continue
            if o.status == "rejected":
                self.alerts.info(f"rejected {w.kalshi_ticker} {w.kalshi_side} @ {w.desired_price:.2f}", watch=w.key)
                continue
            w.order = o
            self.orders.append(o)
            notional += cost
            hedge_cash += hedge_cost
            per_event[ev] = per_event.get(ev, 0) + 1
            self._hedge_cash_refused.discard(w.key)
            self.alerts.info(f"rest {self.broker.name}: buy {w.kalshi_side.upper()} {o.ticker} @ {o.price:.2f} x{o.count:g}  ({w.kalshi_label}; hedge {w.hedge_label} on {w.hedge_venue} @ {w.hedge_ask:.2f}, {compliance.eligibility_note(w.hedge_venue, self.settings)}; margin if filled {w.margin_if_filled:.2%}; hedge cash ${hedge_cash:.0f}/{cfg.hedge_cash:g})", order_id=o.order_id, watch=w.key, payload=o.payload)

    # ---- fills ----------------------------------------------------------------------------
    def check_fills(self, state: dict[str, dict[str, float]]) -> None:
        for o, qty, price in self.broker.poll(self.orders, state):
            w = self.watches.get(o.watch_key)
            rec = {"order_id": o.order_id, "ticker": o.ticker, "side": o.side, "price": price, "count": qty, "watch": o.watch_key, "broker": self.broker.name}
            if w is None:
                self.alerts.alert("FILL (unknown watch)", f"{o.ticker} {o.side} {qty:g} @ {price:.2f}", **rec)
                continue
            kalshi_leg = Leg(w.kalshi_outcome, "kalshi", price, w.kalshi_fee, role="maker")
            hedge_max = max_price_for_leg([kalshi_leg], w.hedge_fee, qty, self.cfg.target_margin, role="taker")
            now_margin = evaluate([kalshi_leg, Leg(w.hedge_outcome, w.hedge_venue, w.hedge_ask, w.hedge_fee)], qty).margin if w.hedge_ask is not None else None
            eligibility = compliance.eligibility_note(w.hedge_venue, self.settings)
            rec.update({"hedge_venue": w.hedge_venue, "hedge_label": w.hedge_label, "hedge_max_price": hedge_max, "hedge_ask_now": w.hedge_ask, "margin_if_hedged_now": now_margin, "hedge_url": w.hedge_url, "hedge_eligibility": eligibility})
            self.fills.append(rec)
            msg = (f"filled {qty:g} x {w.kalshi_label} @ {price:.2f} on Kalshi ({o.ticker}). HEDGE NOW: buy {qty:g} x {w.hedge_label} on {w.hedge_venue} at ≤ {hedge_max if hedge_max is not None else float('nan'):.2f}"
                   + (f" (ask now {w.hedge_ask:.2f} → margin {now_margin:.2%})" if w.hedge_ask is not None and now_margin is not None else " (hedge ask unknown)") + f" [{w.hedge_venue}: {eligibility}]" + (f"  {w.hedge_url}" if w.hedge_url else ""))
            self.alerts.alert("HEDGE NOW", msg, **rec)
            if o.status != "resting":
                w.order = None

    # ---- loop -----------------------------------------------------------------------------
    def rescan(self) -> None:
        if self.scan_fn is None:
            return
        self.last_scan = self.clock()
        try:
            merged = self.scan_fn()
        except Exception as e:
            self.alerts.info(f"rescan failed, keeping {len(self.watches)} watches: {e!r}")
            return
        discovered = self.discover(merged)
        if self.watches and len(discovered) < 0.3 * len(self.watches):
            # A venue probably failed (rate limit / outage): keep managing what we have.
            self.alerts.info(f"rescan returned {len(discovered)} candidates vs {len(self.watches)} before — keeping the current watchlist")
            return
        discovered.sort(key=lambda w: -(w.margin_if_filled if w.margin_if_filled is not None else -9))
        new = {w.key: w for w in discovered[: self.cfg.max_watches]}
        # keep live state/orders of watches that survive
        for key, w in new.items():
            old = self.watches.get(key)
            if old is not None:
                w.order = old.order
        # watches that disappeared but still have a resting order: keep them so we can manage the order
        for key, old in self.watches.items():
            if key not in new and old.order is not None and old.order.status == "resting":
                new[key] = old
        self.watches = new
        skipped = ", ".join(f"{v}={n}" for v, n in sorted(self.skipped_ineligible.items()))
        self.alerts.info(f"watchlist: {len(self.watches)} candidates kept of {len(discovered)} ({sum(1 for w in self.watches.values() if w.desired_price is not None)} priceable)" + (f"; hedge quotes skipped as ineligible: {skipped}" if skipped else ""))

    # ---- exchange status ------------------------------------------------------------------
    def poll_exchange_status(self, force: bool = False) -> Optional[bool]:
        """Every ``status_poll`` seconds (not per loop): is Kalshi trading? False pauses the
        runner and cancels everything; True after a pause resumes. None = no status source."""
        if self.exchange_status is None:
            return None
        now = self.clock()
        if not force and now - self.last_status_poll < self.cfg.status_poll:
            return not self.paused
        self.last_status_poll = now
        try:
            st = self.exchange_status() or {}
        except Exception as e:
            self.alerts.info(f"exchange status poll failed (keeping state paused={self.paused}): {e!r}")
            return not self.paused
        active = bool(st.get("trading_active", True)) and bool(st.get("exchange_active", True))
        if not active and not self.paused:
            self.paused = True
            self.alerts.alert("EXCHANGE PAUSED", f"Kalshi trading_active={st.get('trading_active')} exchange_active={st.get('exchange_active')}: cancelling every resting order and pausing", status=st)
            self.cancel_all("exchange not trading")
        elif active and self.paused:
            self.paused = False
            self.alerts.info("exchange trading again: resuming", status=st)
        return active

    def cancel_all(self, reason: str) -> None:
        """Cancel every resting order, journaling ``reason`` for each; batched when the broker
        can (P12's Broker.cancel_all), one by one otherwise."""
        resting = self._resting()
        fn = getattr(self.broker, "cancel_all", None)
        if callable(fn) and resting:
            try:
                if _positional_arity(fn) >= 1:
                    fn(resting)
                elif _accepts_kwarg(fn, "orders"):
                    fn(orders=resting)
                else:
                    fn()
                for o in resting:
                    o.status = "canceled" if o.status == "resting" else o.status
                    self.alerts.info(f"cancel {o.ticker} {o.side} @ {o.price:.2f} x{o.remaining:g}: {reason} (batched)", order_id=o.order_id, watch=o.watch_key, reason=reason)
                resting = self._resting()
            except Exception as e:
                self.alerts.info(f"batched cancel failed ({e!r}); cancelling one by one", reason=reason)
        for o in resting:
            self._cancel(o, reason)
        for w in self.watches.values():
            if w.order is not None and w.order.status != "resting":
                w.order = None

    def step(self) -> None:
        self.poll_exchange_status()
        if self.scan_fn is not None and self.clock() - self.last_scan >= self.cfg.rescan:
            self.rescan()
        state = self.refresh()
        self.check_fills(state)
        self.reconcile()

    def run(self, duration: float = 3600.0, max_iterations: Optional[int] = None) -> None:
        start = time.time()
        n = 0
        self.alerts.info(f"maker runner start: broker={self.broker.name} size={self.cfg.size} min_margin={self.cfg.min_margin:.2%} max_orders={self.cfg.max_orders} max_notional=${self.cfg.max_notional:g} hedge_cash=${self.cfg.hedge_cash:g} hedge_venues={','.join(self.hedge_venues)} executable={','.join(sorted(self.executable))}")
        for v, why in self.ineligible_hedges.items():
            self.alerts.alert("HEDGE VENUE NOT EXECUTABLE", f"hedge venue {v} was opted in explicitly but {why}; every fill there must be hedged some other way", venue=v)
        try:
            while time.time() - start < duration and (max_iterations is None or n < max_iterations):
                try:
                    self.step()
                except Exception as e:  # keep the loop alive
                    self.alerts.info(f"step error: {e!r}")
                n += 1
                time.sleep(self.cfg.interval)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self.cancel_all("shutdown")
        self.alerts.info(f"maker runner stop: {len(self.fills)} fills")

    def snapshot(self) -> dict[str, Any]:
        return {"watches": [w.to_public() for w in self.watches.values()], "orders": [asdict(o) for o in self.orders], "fills": self.fills}

"""Arbitrage alerts: the one place that turns an analysed event into ARB pushes.

Used by the live slate (in play) and the week scanner (every other hour of the week), so an
arb is tiered, sized, throttled and written the same way whenever it shows up:

* **Tiers** (docs/MODEL.md, "Backtest of the arb alerts"): a lock under
  ``arb_push_min_margin`` (1c) is journalled as ARB SMALL, not pushed - legged by hand those lost
  money; from ``arb_big_margin`` (3c) it is BIG ARB at top priority; ARB in between.
* **Stake**: each ticket is sized to ``arb_stake_fraction`` of the bankroll (20 %), fees in,
  because a locked set holds its cost until the game ends.
* **Throttle**: one ARB push per event per ``throttle_s`` (30 s); an ARB CLOSE (within
  ``arb_near_margin``, 3c, of locking) once per ``arb_near_every_s`` or when the gap shrank
  by a cent.
* **Legging**: the legs in buying order - the stale one first (the venue that has moved least
  over the last 30 s: its price is the one about to go; else the thinner book) - each price's
  age, and the most every later leg may cost and still lock.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Callable, Optional

from . import ticket

try:  # the settings registry; this module must import without it
    from ..config import declare_setting as _declare_setting  # type: ignore
except Exception:  # pragma: no cover
    _declare_setting = None
if _declare_setting is not None:
    try:
        _declare_setting("arb_near_margin", env="ARB_NEAR_MARGIN", default=0.03, cast=float, doc="how far below a lock (dollars per contract, fees in) still earns an ARB CLOSE alert - the buffer that says 'this pair is about to cross'")
        _declare_setting("arb_near_every_s", env="ARB_NEAR_EVERY_S", default=300.0, cast=float, doc="seconds before the same event may send another ARB CLOSE unless the gap shrank by a cent")
        _declare_setting("arb_stake_fraction", env="ARB_STAKE_FRACTION", default=0.20, cast=float, doc="share of the bankroll one ARB ticket is sized to (fees in). A locked set holds its cost until the game ends, so an all-in ticket leaves nothing for the next arb; on the first recorded Sunday, 20 % per arb made about twice what all-in did")
        _declare_setting("arb_push_min_margin", env="ARB_PUSH_MIN_MARGIN", default=0.01, cast=float, doc="smallest ARB margin (dollars per contract, fees in) that is pushed; smaller ones are journalled as ARB SMALL. Replayed by hand (a person on the Robinhood leg), arbs under 1c lost money at every leg speed tested")
        _declare_setting("arb_big_margin", env="ARB_BIG_MARGIN", default=0.03, cast=float, doc="ARB margin from which the push is titled BIG ARB at top priority (replayed: arbs of 3c+ made money when legged by hand)")
    except Exception:  # pragma: no cover
        pass


def _setting(settings: Optional[dict[str, Any]], key: str, default: Any) -> Any:
    try:
        from ..config import setting

        return setting(settings, key, default)
    except Exception:
        return (settings or {}).get(key, default)


def _game(event_key: str) -> str:
    from ..matching.normalize import game_event_key

    return game_event_key(event_key)


def _call(fn: Callable, *a: Any, **k: Any) -> Any:
    """Call an alert sink, dropping keyword arguments it does not accept (test doubles)."""
    try:
        return fn(*a, **k)
    except TypeError:
        return fn(*a)


class ArbAlerter:
    def __init__(self, alerts: Any, settings: Optional[dict[str, Any]] = None, bankroll: Optional[float] = None,
                 contracts: float = 100, target_margin: float = 0.0, executable_venues: Optional[set[str]] = None,
                 throttle_s: float = 30.0, moves: Optional[Callable[[str, str, float], Optional[float]]] = None,
                 push_near: bool = True) -> None:
        self.alerts, self.settings, self.bankroll = alerts, settings or {}, bankroll
        self.contracts, self.target_margin, self.executable_venues = contracts, target_margin, executable_venues
        self.throttle_s = throttle_s
        self.near_margin = float(_setting(self.settings, "arb_near_margin", 0.03) or 0.0)
        self.near_every = float(_setting(self.settings, "arb_near_every_s", 300.0) or 300.0)
        self.push_min = float(_setting(self.settings, "arb_push_min_margin", 0.01) or 0.0)
        self.big = float(_setting(self.settings, "arb_big_margin", 0.03) or 0.03)
        self.stake_fraction = float(_setting(self.settings, "arb_stake_fraction", 0.20) or 1.0)
        self._moves_fn = moves
        # ARB CLOSE pushes suit a game in progress, where gaps close in seconds. Between games
        # most near-locks are permanent (far-tail lines whose two sides always cost ~$1.015):
        # the week scanner journals them (push_near=False) and uses them only to decide what to
        # watch fast.
        self.push_near = push_near
        self._mids: dict[tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=400))
        self.last: dict[str, float] = {}
        self.last_margin: dict[str, float] = {}
        self.near_last: dict[str, tuple[float, float]] = {}

    @property
    def stake(self) -> Optional[float]:
        return (self.bankroll * min(1.0, self.stake_fraction)) if self.bankroll else None

    # ---- price history for legging ---------------------------------------------------
    def observe(self, me: Any, now: float) -> None:
        """Remember each venue's mid for the first outcome, to tell the stale leg later."""
        outs = list(me.info.outcomes)
        if not outs:
            return
        o = outs[-1]
        for v, qs in me.quotes_by_venue.items():
            q = next((x for x in qs if x.outcome == o and (x.meta or {}).get("side") != "no" and x.bid is not None and x.ask is not None), None)
            if q is not None:
                self._mids[(me.event_key, v)].append((now, (q.bid + q.ask) / 2.0))

    def _move(self, event_key: str, venue: str, now: float, window_s: float = 30.0) -> Optional[float]:
        if self._moves_fn is not None:
            return self._moves_fn(event_key, venue, now)
        h = [m for t, m in self._mids.get((event_key, venue), ()) if now - window_s - 5 <= t <= now]
        return abs(h[-1] - h[0]) if len(h) >= 2 else None

    # ---- the one entry point --------------------------------------------------------
    def analyse(self, me: Any, now: float, max_quote_age: float = 10.0) -> Any:
        from ..scanner import analyze_event

        return analyze_event(me, self.settings, contracts=self.contracts, target_margin=self.target_margin, max_quote_age=max_quote_age,
                             now=now, executable_venues=self.executable_venues, budget=self.stake)

    def handle(self, me: Any, rep: Any, title: str, now: float, where: str = "") -> list[tuple[str, str]]:
        """Alert on an analysed event. Returns the (kind, text) produced this call (every arb
        seen, throttled or not, so a caller can print or count them)."""
        arb = rep.arb or {}
        stale = "stale-quote" in (rep.flags or [])
        sport = ticket.SPORT_NAMES.get(rep.sport or "", (rep.sport or "").upper())
        out: list[tuple[str, str]] = []
        if arb.get("is_arb") and rep.fillable and not stale:
            sized = rep.sized_arb or arb
            note = ("depth-capped" if not self.bankroll else
                    f"stake {ticket.money(self.stake)} = {self.stake_fraction:.0%} of your {ticket.money(self.bankroll)} (a lock holds its cost until the game ends)")
            if where:
                note = f"{where}; {note}"
            first, why, maxp = self.legging(me, rep, sized, now)
            margin = float(sized.get("margin") or 0.0)
            kind = "BIG ARB" if margin >= self.big else ("ARB" if margin >= self.push_min else "ARB SMALL")
            text = ticket.arb_ticket(title, sized, size_note=note, sport=rep.sport or me.event_key, header=kind,
                                     first=first, first_reason=why, max_prices=maxp, now=now)
            out.append((kind, text))
            # Once per throttle_s per event - sooner if the lock grew by a cent since the last push.
            if now - self.last.get(me.event_key, -1e18) >= self.throttle_s or margin >= self.last_margin.get(me.event_key, 9.0) + 0.01:
                self.last[me.event_key], self.last_margin[me.event_key] = now, margin
                # ARB SMALL is journalled, not pushed (not a default ntfy kind).
                # The push is keyed by game (one per game per minute: a game's spread and total lines
                # can arb together); the journal keeps the market.
                _call(self.alerts.alert, kind, text, event=_game(me.event_key), market=me.event_key, ntfy_title=f"{kind} {sport}".strip(),
                      margin=sized.get("margin"), legs=sized.get("legs"), contracts=sized.get("contracts"),
                      cost=sized.get("total_cost"), profit=sized.get("profit"))
        elif not stale and arb.get("margin") is not None and self.near_margin > 0 and -self.near_margin <= float(arb["margin"]) < 0:
            m = float(arb["margin"])
            last_t, last_m = self.near_last.get(me.event_key, (-1e18, -1.0))
            if now - last_t >= self.near_every or m >= last_m + 0.01:
                self.near_last[me.event_key] = (now, m)
                text = ticket.near_arb_ticket(title, rep, m, sport=rep.sport or me.event_key, bankroll=self.bankroll or None)
                out.append(("ARB CLOSE", text))
                if self.push_near:
                    _call(self.alerts.alert, "ARB CLOSE", text, event=_game(me.event_key), market=me.event_key, ntfy_title=f"ARB CLOSE {sport}".strip(), margin=m)
                else:
                    _call(getattr(self.alerts, "info", lambda *a, **k: None), f"near-lock {m * 100:+.2f}c: {title}", event=me.event_key, margin=m)
        return out

    def legging(self, me: Any, rep: Any, sized: dict, now: float) -> tuple[Optional[int], str, dict]:
        """(index of the leg to buy first, why, {leg index: the most it may cost and still lock})."""
        legs = list(sized.get("legs") or [])
        if len(legs) < 2:
            return None, "", {}
        moves = [self._move(me.event_key, l.get("venue"), now) for l in legs]
        first, why = None, ""
        if all(m is not None for m in moves) and max(moves) - min(moves) >= 0.01:
            first = min(range(len(legs)), key=lambda i: moves[i])
            other = max(range(len(legs)), key=lambda i: moves[i])
            why = (f"{str(legs[first].get('venue')).upper()} has not followed {str(legs[other].get('venue')).upper()}'s "
                   f"{moves[other] * 100:.0f}c move yet: its price is the one about to go")
        else:
            sizes = [l.get("ask_size") for l in legs]
            if all(x is not None for x in sizes) and len(set(sizes)) > 1:
                first = min(range(len(legs)), key=lambda i: sizes[i])
                why = f"thinner book ({float(sizes[first]):g} offered)"
        maxp: dict[int, float] = {}
        for i, l in enumerate(legs):
            for o in rep.outcomes or []:
                if o.outcome != l.get("outcome"):
                    continue
                for v in o.venues:
                    if v.venue == l.get("venue") and v.market_id == l.get("market_id") and v.max_buy_price is not None:
                        maxp[i] = v.max_buy_price
        return first, why, maxp

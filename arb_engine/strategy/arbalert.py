"""Arbitrage alerts: the one place that turns an analysed event into ARB pushes.

Used by the live slate (in play) and the week scanner (every other hour of the week), so an
arb is tiered, sized, throttled and written the same way whenever it shows up:

* **Tiers** (docs/MODEL.md, "Backtest of the arb alerts"): a lock under
  ``arb_push_min_margin`` (1c) is journalled as ARB SMALL, not pushed - legged by hand those lost
  money; from ``arb_big_margin`` (3c) it is BIG ARB at top priority; ARB in between.
* **Stake**: a BIG ARB ticket is sized to ``arb_stake_fraction`` of the bankroll (20 %), an
  ARB (1-3c) to ``arb_stake_fraction_arb`` (5 %), fees in. A locked set holds its cost until the
  game ends, so even a sure edge is not bet all-in; and legged by hand at human speed the 1-3c
  tier barely breaks even (docs/MODEL.md, "How long an arb lasts, and Kelly").
* **Window**: each ticket says how long arbs of its tier stayed open when recorded (median
  ~8 s; about a third still there at 15 s) - the first leg is a now-or-never decision.
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
        _declare_setting("arb_push_style", env="ARB_PUSH_STYLE", default="short", cast=str, doc="what an ARB / ARB CLOSE push shows on the phone: 'short' (what to buy where at what price, and the result; the full ticket stays in the journal) or 'full' (the whole itemised ticket)")
        _declare_setting("arb_stake_fraction_arb", env="ARB_STAKE_FRACTION_ARB", default=0.05, cast=float, doc="share of the bankroll a 1-3c ARB ticket is sized to (BIG ARB uses arb_stake_fraction). Legged by hand the 1-3c tier returned -0.5 % per dollar on 2026-09-20/21 (Kelly: 0); on one bankroll, 20 % BIG + 5 % ARB made $169 vs $80 for 20 % on every tier. 0 = do not alert that tier")
        _declare_setting("arb_push_min_margin", env="ARB_PUSH_MIN_MARGIN", default=0.01, cast=float, doc="smallest ARB margin (dollars per contract, fees in) that is pushed; smaller ones are journalled as ARB SMALL. Replayed by hand (a person on the Robinhood leg), arbs under 1c lost money at every leg speed tested")
        _declare_setting("arb_big_margin", env="ARB_BIG_MARGIN", default=0.03, cast=float, doc="ARB margin from which the push is titled BIG ARB at top priority (replayed: arbs of 3c+ made money when legged by hand)")
    except Exception:  # pragma: no cover
        pass


# How long arbs stayed open, by tier: fillable, fresh locks on 2026-09-20/21 (15 NFL games,
# 197 runs at the recorder's 5 s tick; scripts/arb_backtest.py, arb_windows). (median s,
# share still open at 15 s, at 30 s). Rerun after each recorded slate.
ARB_WINDOWS: dict[str, tuple[float, float, float]] = {
    "BIG ARB": (8.0, 0.38, 0.07),
    "ARB": (8.0, 0.39, 0.13),
    "ARB SMALL": (6.0, 0.20, 0.10),
}


def window_line(kind: str) -> str:
    w = ARB_WINDOWS.get(kind)
    if not w:
        return ""
    return (f"window: arbs this size lasted a median {w[0]:.0f}s; {w[1]:.0%} still open at 15s, {w[2]:.0%} at 30s - "
            f"buy the first leg now or skip")


def guarantee_line(rep: Any, sized: dict) -> str:
    """Whether the lock pays in every result, a tie included (games that can tie only)."""
    try:
        from ..scanner import TIE_SPORTS
    except Exception:  # pragma: no cover
        TIE_SPORTS = frozenset({"nfl"})
    if rep.sport not in TIE_SPORTS or (getattr(rep, "market_type", None) or "moneyline") != "moneyline":
        return ""
    flags = list(rep.flags or [])
    # A rule the settlement registry holds only as unverified (Rothera's NFL tie clause is read
    # from its terms, not captured) cannot back a guarantee: say "on paper".
    unverified = any(f.startswith(("settlement-rule-unverified", "settlement-rule-provisional", "tie-rule-unverified",
                                   "settlement-rule-missing")) for f in flags)
    head = ("tie-proof on paper (Robinhood's tie rule is read from its terms, not yet verified): pays in every result, a tie included"
            if unverified else "guaranteed: pays in every result, a tie included")
    pref = next((f for f in flags if f.startswith("tie-safe-preferred:")), None)
    if pref:
        _, give_up, cheap_tie = pref.split(":")
        return (f"{head} (tie-proof pair; the cheapest pair locked "
                f"{float(give_up) * 100:.1f}c more but paid only ${float(cheap_tie):.2f} a set on a tie)")
    tie_total, tie_margin = sized.get("tie_payout_total"), sized.get("tie_margin")
    if "loses-on-tie" in flags or (tie_margin is not None and float(tie_margin) < 0):
        n = float(sized.get("contracts") or 0)
        loss = f" = {ticket.money(float(tie_margin) * n)}" if tie_margin is not None and n else ""
        return (f"NOT tie-proof: a tied game pays ${float(tie_total or 0):.2f} a set{loss}; NFL ties are rare "
                f"(1-2 a season) and no tie-proof pair locks right now")
    return head


def _venue_label(url: str) -> str:
    u = str(url).lower()
    return "Kalshi" if "kalshi" in u else ("Robinhood" if "robinhood" in u else ("Polymarket" if "polymarket" in u else "Open"))


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
        self.short_push = str(_setting(self.settings, "arb_push_style", "short") or "short").strip().lower() != "full"
        self.stake_fraction_arb = min(self.stake_fraction, max(0.0, float(_setting(self.settings, "arb_stake_fraction_arb", 0.05) or 0.0)))
        self._staked: dict[str, float] = {}   # event -> the fraction its last analysis was sized to
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

    def stake_at(self, fraction: float) -> Optional[float]:
        return (self.bankroll * min(1.0, fraction)) if self.bankroll else None

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
        """Analyse and size to the tier's stake: first at the BIG ARB stake; a lock that comes
        out under ``arb_big_margin`` there is re-sized to the smaller ARB stake (its margin is
        re-read at that size - fees round per order, so it can move either way)."""
        from ..scanner import analyze_event

        def run(fraction: float) -> Any:
            return analyze_event(me, self.settings, contracts=self.contracts, target_margin=self.target_margin, max_quote_age=max_quote_age,
                                 now=now, executable_venues=self.executable_venues, budget=self.stake_at(fraction))

        rep, frac = run(self.stake_fraction), self.stake_fraction
        sized = rep.sized_arb if rep is not None else None
        if (self.bankroll and sized and rep.fillable and self.stake_fraction_arb < self.stake_fraction
                and float(sized.get("margin") or 0.0) < self.big):
            rep, frac = run(self.stake_fraction_arb), self.stake_fraction_arb
        self._staked[me.event_key] = frac
        return rep

    def handle(self, me: Any, rep: Any, title: str, now: float, where: str = "") -> list[tuple[str, str]]:
        """Alert on an analysed event. Returns the (kind, text) produced this call (every arb
        seen, throttled or not, so a caller can print or count them)."""
        arb = rep.arb or {}
        stale = "stale-quote" in (rep.flags or [])
        sport = ticket.SPORT_NAMES.get(rep.sport or "", (rep.sport or "").upper())
        out: list[tuple[str, str]] = []
        if arb.get("is_arb") and rep.fillable and not stale:
            sized = rep.sized_arb or arb
            frac = self._staked.get(me.event_key, self.stake_fraction)
            note = ("depth-capped" if not self.bankroll else
                    f"stake {ticket.money(self.stake_at(frac))} = {frac:.0%} of your {ticket.money(self.bankroll)} (a lock holds its cost until the game ends)")
            if where:
                note = f"{where}; {note}"
            first, why, maxp = self.legging(me, rep, sized, now)
            margin = float(sized.get("margin") or 0.0)
            kind = "BIG ARB" if margin >= self.big else ("ARB" if margin >= self.push_min else "ARB SMALL")
            text = ticket.arb_ticket(title, sized, size_note=note, sport=rep.sport or me.event_key, header=kind,
                                     first=first, first_reason=why, max_prices=maxp, now=now, window=window_line(kind),
                                     guarantee=guarantee_line(rep, sized))
            out.append((kind, text))
            # Once per throttle_s per event - sooner if the lock grew by a cent since the last push.
            if now - self.last.get(me.event_key, -1e18) >= self.throttle_s or margin >= self.last_margin.get(me.event_key, 9.0) + 0.01:
                self.last[me.event_key], self.last_margin[me.event_key] = now, margin
                # ARB SMALL is journalled, not pushed (not a default ntfy kind).
                # The push is keyed by game (one per game per minute: a game's spread and total lines
                # can arb together); the journal keeps the market.
                extra = {}
                if self.short_push:
                    extra["ntfy_body"] = ticket.arb_short(sized, first=first, max_prices=maxp, where=where)
                    buttons = ticket.order_buttons(sized, first=first, max_prices=maxp)
                    extra["ntfy_actions"] = buttons
                    if buttons:
                        extra["ntfy_click"] = buttons[0][1]      # tapping the push opens the leg to buy first
                    head = f"{kind} +{margin * 100:.1f}c - {sport} {title}".replace("  ", " ")
                else:
                    head = f"{kind} {sport}".strip()
                _call(self.alerts.alert, kind, text, event=_game(me.event_key), market=me.event_key, ntfy_title=head,
                      margin=sized.get("margin"), legs=sized.get("legs"), contracts=sized.get("contracts"),
                      cost=sized.get("total_cost"), profit=sized.get("profit"), **extra)
        elif not stale and arb.get("margin") is not None and self.near_margin > 0 and -self.near_margin <= float(arb["margin"]) < 0:
            m = float(arb["margin"])
            last_t, last_m = self.near_last.get(me.event_key, (-1e18, -1.0))
            if now - last_t >= self.near_every or m >= last_m + 0.01:
                self.near_last[me.event_key] = (now, m)
                text = ticket.near_arb_ticket(title, rep, m, sport=rep.sport or me.event_key, bankroll=self.bankroll or None)
                out.append(("ARB CLOSE", text))
                if self.push_near:
                    extra = {"ntfy_body": ticket.near_arb_short(rep)} if self.short_push else {}
                    head = (f"ARB CLOSE {abs(m) * 100:.1f}c away - {sport} {title}" if self.short_push else f"ARB CLOSE {sport}").strip()
                    _call(self.alerts.alert, "ARB CLOSE", text, event=_game(me.event_key), market=me.event_key, ntfy_title=head, margin=m, **extra)
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

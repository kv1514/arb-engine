"""Order tickets for alerts: what to buy, how many, at what price, with the fees.

An alert is only useful if it can be typed into two order tickets without doing arithmetic
on a phone. Every formatter here prints, per leg, the venue, the side, the **contract
count**, the limit price, the fee that venue charges for *that* order and the resulting
cash out — then the set totals. The numbers come from an ``ArbResult`` (or the same fields
as a dict, which is how ``scanner.analyze_event`` reports them), i.e. from
``FeeModel.fee(price, contracts, role)`` at the actual size, never from a per-contract fee
multiplied out: Kalshi rounds its fee up per order and Rothera's has a per-order floor, so
the fee on 47 contracts is not 47 x the fee on one.

Fees counted are the **entry** fees. A contract held to settlement pays no exit fee on
Kalshi, Robinhood or Polymarket; selling before settlement pays the same schedule again, so
a ticket that will be traded out (LAG) says so in its line.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

MAX_URLS = 2
SPORT_NAMES = {"nfl": "NFL", "ncaaf": "CFB", "nba": "NBA", "nhl": "NHL", "tennis": "TENNIS"}


def headline(title: str, sport_or_key: Optional[str] = None) -> str:
    """``'NFL - ATL @ GB'``: the sport first, so a locked phone shows which slate it is.

    Takes the sport name or anything that starts with it (an event key
    ``nfl:ATL|GB:2026-09-24``), and falls back to the title alone when it has neither.
    """
    s = str(sport_or_key or "").split(":")[0].strip().lower()
    name = SPORT_NAMES.get(s, s.upper())
    return f"{name} - {title}" if name else str(title)


def _g(obj: Any, key: str, default: Any = None) -> Any:
    """Field of an ArbResult/LegResult dataclass or of its ``asdict`` form."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def money(x: Optional[float]) -> str:
    """-0.5 -> '-$0.50'. Cash amounts always carry their sign when negative."""
    if x is None:
        return "?"
    return ("-$" if x < 0 else "$") + f"{abs(float(x)):,.2f}"


def cents(x: Optional[float]) -> str:
    """0.0127 -> '1.3c' (a per-contract margin reads better in cents than in dollars)."""
    return "?" if x is None else f"{float(x) * 100:+.1f}¢"


def leg_line(leg: Any, n: int = 0) -> str:
    """``1) KALSHI buy 47 Buffalo YES @ 0.62 -> $29.14 + $0.35 fee = $29.49 (0.6274/ct)``"""
    venue = str(_g(leg, "venue", "?")).upper()
    label = _g(leg, "label") or _g(leg, "outcome") or "?"
    side = _g(leg, "side")
    contracts = float(_g(leg, "contracts", 0) or 0)
    price = float(_g(leg, "price", 0) or 0)
    fee = float(_g(leg, "fee", 0) or 0)
    cost = float(_g(leg, "cost", price * contracts + fee) or 0)
    all_in = _g(leg, "all_in_per_contract")
    head = f"{n}) " if n else ""
    sidetxt = f" {str(side).upper()}" if side else ""
    tail = f" ({float(all_in):.4f}/ct)" if all_in else ""
    return (f"{head}{venue} buy {contracts:g} x {label}{sidetxt} @ {price:.2f} -> "
            f"{money(price * contracts)} + {money(fee)} fee = {money(cost)}{tail}")


def _urls(legs: Sequence[Any]) -> list[str]:
    seen: list[str] = []
    for l in legs:
        u = _g(l, "url")
        if u and u not in seen:
            seen.append(str(u))
    return seen[:MAX_URLS]


def arb_ticket(title: str, result: Any, size_note: str = "", header: str = "ARB", sport: Optional[str] = None) -> str:
    """The whole two-leg (or n-leg) trade as an order ticket.

    ``result`` is an ``ArbResult`` sized the way it would actually be bought (depth and
    bankroll already applied — see ``quant.arbitrage.size_for_budget``), so its per-leg fees
    and totals are the ones the venues charge at that count.
    """
    legs = list(_g(result, "legs", []) or [])
    contracts = float(_g(result, "contracts", 0) or 0)
    cost = float(_g(result, "total_cost", 0) or 0)
    payout = float(_g(result, "payout", contracts) or 0)
    profit = float(_g(result, "profit", payout - cost) or 0)
    margin = _g(result, "margin")
    roi = _g(result, "roi")
    lines = [f"{headline(title, sport)} - {header} {cents(margin)}/ct after fees"]
    lines += [leg_line(l, i) for i, l in enumerate(legs, 1)]
    lines.append(f"stake {money(cost)} -> pays {money(payout)} = {'+' if profit >= 0 else ''}{money(profit)}"
                 + (f" ({float(roi):+.2%} on cost)" if roi is not None else ""))
    tie_total, tie_margin = _g(result, "tie_payout_total"), _g(result, "tie_margin")
    if tie_total is not None and tie_margin is not None:
        tie_cash = float(tie_margin) * contracts
        lines.append(f"tie: pays {money(float(tie_total) * contracts)} = {'+' if tie_cash >= 0 else ''}{money(tie_cash)}"
                     + (" - LOSES on a tie" if tie_cash < 0 else ""))
    lines.append(f"{contracts:g} ct" + (f"; {size_note}" if size_note else "") + "; fees are entry-only (held to settlement)")
    lines += _urls(legs)
    return "\n".join(lines)


def lag_ticket(sig: Any, fee_total: Optional[float] = None) -> str:
    """One-leg ticket for a LAG signal: the laggard's own order, with its order fee.

    ``fee_total`` is the venue's fee for the whole order (``LagSignal.fee_total``); without
    a size there is nothing to charge a fee on and only the per-contract all-in is printed.
    """
    n = _g(sig, "suggested_contracts")
    ask, all_in = float(_g(sig, "follower_ask", 0) or 0), float(_g(sig, "follower_all_in", 0) or 0)
    edge, follower = float(_g(sig, "edge", 0) or 0), str(_g(sig, "follower", "?"))
    label, leader = _g(sig, "label") or _g(sig, "outcome") or "?", str(_g(sig, "leader", "?"))
    head = (f"{headline(_g(sig, 'title', ''), _g(sig, 'event_key'))} - LAG: {leader} moved {float(_g(sig, 'lead_move', 0) or 0):+.2f}, "
            f"{follower} has not ({float(_g(sig, 'follower_move', 0) or 0):+.2f})")
    if n:
        n = int(n)
        fee = float(fee_total) if fee_total is not None else (all_in - ask) * n
        cash = ask * n
        lines = [head,
                 f"{follower.upper()} buy {n} x {label} @ {ask:.2f} -> {money(cash)} + {money(fee)} fee = {money(cash + fee)} ({(cash + fee) / n:.4f}/ct)",
                 f"edge vs {leader} mid {float(_g(sig, 'leader_mid', 0) or 0):.3f}: {cents(edge)}/ct = {money(edge * n)} if it converges (exit fee not counted)"]
    else:
        lines = [head, f"{follower.upper()} buy {label} @ {ask:.2f} (all-in {all_in:.4f}/ct) vs {leader} mid "
                       f"{float(_g(sig, 'leader_mid', 0) or 0):.3f}: {cents(edge)}/ct"]
    depth = _g(sig, "depth")
    if depth:
        lines[-1] += f"; depth {float(depth):g} ct"
    url = _g(sig, "url")
    if url:
        lines.append(str(url))
    return "\n".join(lines)


def _best_venue_price(outcome: Any) -> Optional[Any]:
    """The venue row the scanner picked as the cheapest all-in buy for this outcome."""
    want = _g(outcome, "best_buy_venue")
    rows = list(_g(outcome, "venues", []) or [])
    for v in rows:
        if _g(v, "venue") == want:
            return v
    return rows[0] if rows else None


def near_arb_ticket(title: str, report: Any, margin: float, sport: Optional[str] = None, bankroll: Optional[float] = None) -> str:
    """"Nearly an arb": what each leg costs now and the price that would lock it.

    ``VenuePrice.max_buy_price`` is the most this leg may cost while the *other* legs are
    hedged at their current asks and the set still clears the target margin, so the distance
    between the ask and that price is exactly how far this side has to move. One line per
    outcome, cheapest venue first, plus what the set costs today.
    """
    lines = [f"{headline(title, sport)} - ARB CLOSE {cents(margin)}/ct after fees (not yet a lock)"]
    total_all_in, depths = 0.0, []
    for o in list(_g(report, "outcomes", []) or []):
        v = _best_venue_price(o)
        if v is None:
            continue
        ask, all_in, trigger = _g(v, "ask"), _g(v, "all_in"), _g(v, "max_buy_price")
        total_all_in += float(all_in or 0)
        gap = (float(ask) - float(trigger)) if (ask is not None and trigger is not None) else None
        depth = _g(v, "ask_size")
        if depth:
            depths.append(float(depth))
        lines.append(f"{str(_g(v, 'venue', '?')).upper()} {_g(o, 'label') or _g(o, 'outcome')} @ {float(ask):.2f}"
                     + (f" (all-in {float(all_in):.4f})" if all_in is not None else "")
                     + (f" - locks at {float(trigger):.2f}, {gap * 100:.1f}\u00a2 away" if gap is not None else "")
                     + (f", depth {float(depth):g}" if depth else ""))
    size = None
    if total_all_in > 0:
        by_cash = (float(bankroll) // total_all_in) if bankroll else None
        # The thinnest side is the real cap on a two-leg lock, whatever the bankroll allows.
        size = min([x for x in (by_cash, min(depths) if depths else None) if x is not None], default=None)
    lines.append(f"set costs {money(total_all_in)}/ct with fees; needs {money(max(0.0, total_all_in - 1.0))}/ct more of move"
                 + (f"; ready for {int(size)} ct" + (" (depth)" if depths and min(depths) <= (size or 0) + 1e-9 else f" at {money(bankroll)}") if size else ""))
    return "\n".join(lines)

"""Polymarket public market data (Gamma metadata API + CLOB order books).

* ``https://gamma-api.polymarket.com/events?tag_slug=nfl&active=true&closed=false``
  returns events with nested markets. Sports game markets carry ``sportsMarketType``
  (``moneyline`` | ``spreads`` | ``totals`` | ``tennis_completed_match`` ...),
  ``gameStartTime``, ``line``, two-element ``outcomes`` / ``outcomePrices`` /
  ``clobTokenIds``, and ``feeSchedule`` (used by the fee model).
* ``https://clob.polymarket.com/book?token_id=`` gives depth per outcome token.

No key needed for reads. Trading needs a wallet + the py-clob-client; not wired here.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from ..models import VENUE_POLYMARKET, Book, EventInfo, Level, OutcomeQuote, VenueSnapshot
from ..matching.normalize import et_date, fmt_line, nfl_event_key, parse_iso, person_keys, push_rule_for_line, spread_event_key, spread_outcomes, tennis_event_key, total_event_key, team_event_key
from ..matching.teams import TEAM_SPORTS, nfl_team_city, nfl_team_code, team_code
from .http import HttpClient

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# Polymarket tennis market description (2026-09-18): retirement/default/disqualification after
# the start -> the player who advances; cancelled, tie, delayed > 7 days, or walkover (withdrawal
# before the start) -> 50-50.
TENNIS_SETTLEMENT = {"retirement": "advancer", "walkover": "50-50", "cancelled": "50-50", "postponed": "50-50_after_7d"}

SPORT_TAGS: dict[str, list[str]] = {
    "nfl": ["nfl"],
    "ncaaf": ["cfb"],
    "ncaaf": ["cfb", "college-football"],
    "tennis": ["tennis"],
    "nba": ["nba"],
    "nhl": ["nhl"],
    "mlb": ["mlb"],
}


def _jl(x: Any) -> list:
    if isinstance(x, list):
        return x
    try:
        return json.loads(x) if x else []
    except (TypeError, ValueError):
        return []


def _f(x: Any) -> Optional[float]:
    try:
        return float(x) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None


def parse_clob_book(book: dict) -> Book:
    asks = sorted((Level(float(l["price"]), float(l["size"])) for l in book.get("asks", [])), key=lambda l: l.price)
    bids = sorted((Level(float(l["price"]), float(l["size"])) for l in book.get("bids", [])), key=lambda l: -l.price)
    return Book(asks=asks, bids=bids)


def _event_game_codes(sport: str, ev: dict, slug_parts: list[str]) -> list:
    """[away, home] canonical codes for a game event: NFL from the slug, otherwise from the
    moneyline market's outcomes (full team names). Cached on the event dict."""
    cached = ev.get("_game_codes")
    if cached is not None:
        return list(cached)
    codes: list = [None, None]
    if sport == "nfl" and len(slug_parts) >= 3:
        codes = [nfl_team_code(slug_parts[1]), nfl_team_code(slug_parts[2])]
    if None in codes:
        for m in ev.get("markets") or []:
            if (m.get("sportsMarketType") or "") == "moneyline":
                outs = _jl(m.get("outcomes"))
                if len(outs) == 2:
                    c = [team_code(sport, o) for o in outs]
                    if None not in c:
                        codes = c
                        break
    ev["_game_codes"] = codes
    return list(codes)


class PolymarketAdapter:
    venue = VENUE_POLYMARKET

    def __init__(self, http: Optional[HttpClient] = None, with_books: bool = False, page_size: int = 100, max_pages: int = 10):
        self.http = http or HttpClient()
        self.with_books = with_books
        self.page_size = page_size
        self.max_pages = max_pages

    def events(self, tag_slug: str) -> list[dict]:
        out: list[dict] = []
        for page in range(self.max_pages):
            data = self.http.get(f"{GAMMA}/events", {"tag_slug": tag_slug, "active": "true", "closed": "false", "limit": self.page_size, "offset": page * self.page_size, "order": "startDate", "ascending": "false"})
            if not data:
                break
            out.extend(data)
            if len(data) < self.page_size:
                break
        return out

    def book(self, token_id: str) -> Book:
        return parse_clob_book(self.http.get(f"{CLOB}/book", {"token_id": token_id}))

    def books(self, token_ids: list[str]) -> dict[str, Book]:
        """Batch order books (POST /books)."""
        try:
            data = self.http.post(f"{CLOB}/books", [{"token_id": t} for t in token_ids])
        except Exception:
            return {t: self.book(t) for t in token_ids}
        return {b.get("asset_id", ""): parse_clob_book(b) for b in data or []}

    def fetch(self, sport: str) -> VenueSnapshot:
        snap = VenueSnapshot(venue=self.venue, fetched_at=time.time())
        seen: set[str] = set()
        for tag in SPORT_TAGS.get(sport, [sport]):
            try:
                evs = self.events(tag)
            except Exception as e:
                snap.errors.append(f"tag {tag}: {e}")
                continue
            for ev in evs:
                if ev.get("id") in seen:
                    continue
                seen.add(ev.get("id"))
                self._ingest_event(snap, sport, ev)
        if self.with_books and snap.quotes:
            self.attach_books_for(snap.quotes, snap.errors)
        return snap

    def attach_books_for(self, quotes: list[OutcomeQuote], errors: Optional[list[str]] = None) -> None:
        tokens = list(dict.fromkeys(q.venue_market_id for q in quotes if q.venue_market_id))
        books: dict[str, Book] = {}
        for i in range(0, len(tokens), 50):
            try:
                books.update(self.books(tokens[i : i + 50]))
            except Exception as e:
                if errors is not None:
                    errors.append(f"books: {e}")
        for q in quotes:
            b = books.get(q.venue_market_id)
            if b is None:
                continue
            q.book = b
            if b.asks:
                q.ask, q.ask_size = b.asks[0].price, b.asks[0].size
            if b.bids:
                q.bid, q.bid_size = b.bids[0].price, b.bids[0].size

    def _ingest_event(self, snap: VenueSnapshot, sport: str, ev: dict) -> None:
        for m in ev.get("markets", []):
            mt = m.get("sportsMarketType")
            if m.get("closed") or not m.get("active", True):
                continue
            if mt in ("spreads", "totals") and sport in TEAM_SPORTS:
                self._ingest_line_market(snap, sport, ev, m, "spread" if mt == "spreads" else "total")
                continue
            if mt != "moneyline":
                continue
            outcomes = _jl(m.get("outcomes"))
            prices = [_f(p) for p in _jl(m.get("outcomePrices"))]
            tokens = _jl(m.get("clobTokenIds"))
            if len(outcomes) != 2 or len(tokens) != 2:
                continue
            start = parse_iso(m.get("gameStartTime"))  # ev.startDate is the listing date, not kickoff
            date = et_date(start)
            if sport in TEAM_SPORTS:
                codes = [team_code(sport, o) for o in outcomes]
                slug = ev.get("slug", "")
                if any(c is None for c in codes) and sport == "nfl":
                    parts = slug.split("-")
                    if len(parts) >= 3:
                        codes = [nfl_team_code(parts[1]), nfl_team_code(parts[2])]
                if any(c is None for c in codes):
                    continue
                key = team_event_key(sport, codes, date)  # type: ignore[arg-type]
                tie_rule = "half"
            elif sport == "tennis":
                codes = person_keys(outcomes)
                key = tennis_event_key(outcomes, date)
                tie_rule = "void"
                settlement = dict(TENNIS_SETTLEMENT)
            else:
                codes = [o for o in outcomes]
                key = f"{sport}:" + "|".join(sorted(codes)) + f":{date or ''}"
                tie_rule = "half"
            if not key:
                continue
            url = f"https://polymarket.com/event/{ev.get('slug')}"
            venue_meta: dict[str, Any] = {"event_id": ev.get("id"), "market_id": m.get("id"), "condition_id": m.get("conditionId"), "url": url}
            if sport == "tennis":
                venue_meta["settlement"] = settlement
            info = EventInfo(event_key=key, sport=sport, market_type="moneyline", outcomes=sorted(codes), labels={codes[i]: outcomes[i] for i in range(2)}, start_time=start, tie_rule=tie_rule, venues={self.venue: venue_meta})
            snap.events.setdefault(key, info)
            fee_params = {"feeSchedule": m.get("feeSchedule"), "feesEnabled": m.get("feesEnabled", True), "feeType": m.get("feeType")}
            # bestBid/bestAsk on the market object refer to outcome[0]'s token; outcome[1] is the complement.
            bb, ba = _f(m.get("bestBid")), _f(m.get("bestAsk"))
            sides = [(bb, ba), ((1 - ba) if ba is not None else None, (1 - bb) if bb is not None else None)]
            for i in range(2):
                bid, ask = sides[i]
                q = OutcomeQuote(
                    venue=self.venue, venue_market_id=str(tokens[i]), event_key=key, outcome=codes[i], outcome_label=outcomes[i],
                    ask=round(ask, 4) if ask is not None and 0 < ask < 1 else None, bid=round(bid, 4) if bid is not None and 0 < bid < 1 else None,
                    fee_params=fee_params, url=url, ts=snap.fetched_at,
                    meta={"mid_price": prices[i] if i < len(prices) else None, "condition_id": m.get("conditionId"), "slug": m.get("slug"), "outcome_index": i, "tick": _f(m.get("orderPriceMinTickSize")), "min_size": _f(m.get("orderMinSize")), "volume24h": _f(m.get("volume24hr")), "liquidity": _f(m.get("liquidityNum")), "neg_risk": m.get("negRisk")},
                )
                snap.quotes.append(q)


    def _ingest_line_market(self, snap: VenueSnapshot, sport: str, ev: dict, m: dict, mtype: str) -> None:
        outcomes = _jl(m.get("outcomes"))
        tokens = _jl(m.get("clobTokenIds"))
        line = _f(m.get("line"))
        if len(outcomes) != 2 or len(tokens) != 2 or line is None:
            return
        start = parse_iso(m.get("gameStartTime"))
        date = et_date(start)
        url = f"https://polymarket.com/event/{ev.get('slug')}"
        # Event slug is away-home; needed to name totals and to sanity-check codes. NFL slugs use
        # the standard codes; college slugs use Polymarket's own (frest, sjst), so take the
        # teams from the event's moneyline outcomes instead.
        slug_parts = str(ev.get("slug", "")).split("-")
        game_codes = _event_game_codes(sport, ev, slug_parts)
        if mtype == "spread":
            codes = [team_code(sport, o) for o in outcomes]
            if any(c is None for c in codes):
                return
            if line < 0:
                fav, dog, L = codes[0], codes[1], -line
                keys = list(spread_outcomes(fav, dog, L))          # outcome0 = fav covers
            else:
                fav, dog, L = codes[1], codes[0], line
                yk, nk = spread_outcomes(fav, dog, L)
                keys = [nk, yk]                                      # outcome0 = dog with +line
            key = spread_event_key(sport, [fav, dog], date, fav, L)  # type: ignore[list-item]
            labels = {keys[0]: f"{outcomes[0]} {'-' if line < 0 else '+'}{fmt_line(abs(line))}", keys[1]: f"{outcomes[1]} {'+' if line < 0 else '-'}{fmt_line(abs(line))}"}
            out_keys = sorted(keys)
        else:
            if None in game_codes:
                return
            key = total_event_key(sport, game_codes, date, line)  # type: ignore[arg-type]
            keys = ["over" if outcomes[0].lower().startswith("over") else "under", "under" if outcomes[0].lower().startswith("over") else "over"]
            labels = {"over": f"Over {fmt_line(line)}", "under": f"Under {fmt_line(line)}"}
            out_keys = ["over", "under"]
        info = EventInfo(event_key=key, sport=sport, market_type=mtype, outcomes=out_keys, labels=labels, start_time=start, line=abs(line) if mtype == "spread" else line, tie_rule=push_rule_for_line(abs(line)), venues={self.venue: {"event_id": ev.get("id"), "market_id": m.get("id"), "slug": m.get("slug"), "url": url}, "_teams": {"title": f"{game_codes[0]} @ {game_codes[1]}" if None not in game_codes else ""}})
        snap.events.setdefault(key, info)
        fee_params = {"feeSchedule": m.get("feeSchedule"), "feesEnabled": m.get("feesEnabled", True), "feeType": m.get("feeType")}
        bb, ba = _f(m.get("bestBid")), _f(m.get("bestAsk"))
        sides = [(bb, ba), ((1 - ba) if ba is not None else None, (1 - bb) if bb is not None else None)]
        for i in range(2):
            bid, ask = sides[i]
            snap.quotes.append(OutcomeQuote(venue=self.venue, venue_market_id=str(tokens[i]), event_key=key, outcome=keys[i], outcome_label=labels[keys[i]], ask=round(ask, 4) if ask is not None and 0 < ask < 1 else None, bid=round(bid, 4) if bid is not None and 0 < bid < 1 else None, fee_params=fee_params, url=url, ts=snap.fetched_at, meta={"condition_id": m.get("conditionId"), "slug": m.get("slug"), "outcome_index": i, "line": line, "tick": _f(m.get("orderPriceMinTickSize")), "min_size": _f(m.get("orderMinSize"))}))

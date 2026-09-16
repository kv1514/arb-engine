"""Analyse one Robinhood event page URL across venues (what the overlay shows).

Robinhood re-uses Kalshi's ticker scheme, so a Rothera contract ``NFLGAME-26SEP20PHITEN-PHI``
maps to Kalshi ``KXNFLGAME-26SEP20PHITEN-PHI`` and a Kalshi-routed contract keeps its ticker.
Polymarket NFL moneylines live at ``nfl-{away}-{home}-{utc-date}``; tennis uses the public
search endpoint and matches on the players' surnames.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict
from typing import Any, Optional

from .fees.registry import fee_model_for
from .fees.robinhood import exchange_from_symbol_or_enum
from .matching.normalize import kalshi_ticker_date, person_key, person_keys
from .matching.teams import nfl_team_code
from .models import EventInfo, OutcomeQuote
from .quant.arbitrage import Leg, best_leg_per_outcome, evaluate, max_price_for_leg
from .quant.fairvalue import consensus_fair_value
from .venues.kalshi import KalshiClient
from .venues.polymarket import GAMMA, PolymarketAdapter, _f, _jl
from .venues.robinhood import RobinhoodAdapter, clean_label

_SYM = re.compile(r"^(KX)?([A-Z0-9]+)-(\d{2})([A-Z]{3})(\d{2})([A-Z0-9]+)-([A-Z0-9]+)$")


def parse_symbol(symbol: str) -> Optional[dict[str, Any]]:
    m = _SYM.match(symbol or "")
    if not m:
        return None
    pair, side = m.group(6), m.group(7)
    teams = None
    if pair.endswith(side):
        teams = [pair[: -len(side)], side]
    elif pair.startswith(side):
        teams = [side, pair[len(side):]]
    return {"family": m.group(2), "date": kalshi_ticker_date(symbol), "pair": pair, "side": side, "teams": teams, "kalshi_ticker": ("" if m.group(1) else "KX") + symbol, "routed": "kalshi" if m.group(1) else "other"}


def polymarket_nfl_slugs(teams: list[str], date: str) -> list[str]:
    from datetime import datetime, timedelta

    d0 = datetime.strptime(date, "%Y-%m-%d")
    out = []
    for d in (d0, d0 + timedelta(days=1)):
        ds = d.strftime("%Y-%m-%d")
        a, b = teams[0].lower(), teams[1].lower()
        out += [f"nfl-{a}-{b}-{ds}", f"nfl-{b}-{a}-{ds}"]
    return out


class EventAnalyzer:
    def __init__(self, robinhood: Optional[RobinhoodAdapter] = None, kalshi: Optional[KalshiClient] = None, polymarket: Optional[PolymarketAdapter] = None):
        self.rh = robinhood or RobinhoodAdapter()
        self.kalshi = kalshi or KalshiClient(env="prod")
        self.pm = polymarket or PolymarketAdapter()
        self._series_cache: dict[str, dict] = {}

    # ---- lookups ------------------------------------------------------------------------
    def _series(self, ticker: str) -> dict:
        s = ticker.split("-")[0]
        if s not in self._series_cache:
            try:
                self._series_cache[s] = self.kalshi.series(s)
            except Exception:
                self._series_cache[s] = {"fee_type": "quadratic", "fee_multiplier": 1}
        return self._series_cache[s]

    def polymarket_nfl(self, teams: list[str], date: str) -> Optional[dict]:
        for slug in polymarket_nfl_slugs(teams, date):
            try:
                ms = self.pm.http.get(f"{GAMMA}/markets", {"slug": slug})
            except Exception:
                continue
            for m in ms or []:
                if m.get("sportsMarketType") == "moneyline":
                    return m
        return None

    def polymarket_tennis(self, names: list[str]) -> Optional[dict]:
        q = " ".join(person_key(n) for n in names)
        keys = sorted(person_keys(names))
        try:
            d = self.pm.http.get(f"{GAMMA}/public-search", {"q": q, "limit_per_type": 3})
        except Exception:
            return None
        for ev in d.get("events", []):
            for m in ev.get("markets", []):
                if m.get("sportsMarketType") != "moneyline" or m.get("closed"):
                    continue
                outs = _jl(m.get("outcomes"))
                if len(outs) == 2 and sorted(person_keys(outs)) == keys:
                    m.setdefault("events", [{"slug": ev.get("slug")}])
                    return m
        return None

    # ---- analysis -----------------------------------------------------------------------
    def analyze_url(self, url: str, settings: Optional[dict[str, Any]] = None, contracts: float = 100, target_margin: float = 0.0) -> dict[str, Any]:
        settings = settings or {}
        m = re.search(r"/prediction-markets/([^/]+)/events/([^/?#]+)", url)
        if not m:
            return {"ok": False, "error": "not a Robinhood prediction-market event URL"}
        category, slug = m.group(1), m.group(2)
        pp = self.rh.event_page(category, slug)
        ev = pp.get("event") or {}
        contracts_raw = list((ev.get("eventContracts") or {}).values())
        if len(contracts_raw) != 2:
            return {"ok": True, "event": {"id": ev.get("id"), "name": ev.get("name")}, "analysis": None, "note": f"{len(contracts_raw)} contracts — only two-outcome game/match markets are analysed"}
        parsed = [parse_symbol(c.get("symbol", "")) for c in contracts_raw]
        if any(p is None for p in parsed):
            return {"ok": True, "event": {"id": ev.get("id"), "name": ev.get("name")}, "analysis": None, "note": "unrecognised contract symbols"}
        family = parsed[0]["family"]  # type: ignore[index]
        is_nfl = family == "NFLGAME"
        is_tennis = bool(re.search(r"(ATP|WTA|ITF)", family) and "MATCH" in family)
        sport = "nfl" if is_nfl else "tennis" if is_tennis else "other"
        names = [clean_label(c.get("displayLongName") or c.get("displayShortName") or "") for c in contracts_raw]
        if is_nfl:
            outcomes = [nfl_team_code(c.get("displayShortName")) or nfl_team_code(n) or p["side"] for c, n, p in zip(contracts_raw, names, parsed)]  # type: ignore[index]
        else:
            outcomes = person_keys(names)
        labels = dict(zip(outcomes, names))
        event_key = f"{sport}:" + "|".join(sorted(outcomes)) + f":{parsed[0]['date'] or ''}"  # type: ignore[index]
        now = time.time()

        quotes_by_venue: dict[str, list[OutcomeQuote]] = {"robinhood": []}
        try:
            live = self.rh.quotes([c["id"] for c in contracts_raw])
        except Exception:
            live = {}
        ssr = pp.get("quotes") or {}
        for c, o, p in zip(contracts_raw, outcomes, parsed):
            qd = live.get(c["id"]) or ssr.get(c["id"]) or {}
            exch = exchange_from_symbol_or_enum(c.get("symbol"), c.get("exchange"))
            quotes_by_venue["robinhood"].append(OutcomeQuote(venue="robinhood", venue_market_id=c["id"], event_key=event_key, outcome=o, outcome_label=labels[o], ask=_f(qd.get("yes_ask_price")), bid=_f(qd.get("yes_bid_price")), ask_size=_f(qd.get("ask_size")), fee_params={"exchange": exch, "symbol": c.get("symbol")}, url=url, ts=now, meta={"exchange": exch, "symbol": c.get("symbol")}, book_id="kalshi" if exch == "kalshi" else exch))

        errors: list[str] = []
        kq: list[OutcomeQuote] = []
        for o, p in zip(outcomes, parsed):
            ticker = p["kalshi_ticker"]  # type: ignore[index]
            try:
                km = self.kalshi.market(ticker)
            except Exception as e:
                errors.append(f"kalshi {ticker}: {e}")
                continue
            series = self._series(ticker)
            ask, bid = _f(km.get("yes_ask_dollars")), _f(km.get("yes_bid_dollars"))
            kq.append(OutcomeQuote(venue="kalshi", venue_market_id=ticker, event_key=event_key, outcome=o, outcome_label=labels[o], ask=ask if ask and ask < 1 else None, bid=bid if bid and bid > 0 else None, ask_size=_f(km.get("yes_ask_size_fp")), fee_params={"fee_type": series.get("fee_type"), "fee_multiplier": series.get("fee_multiplier", 1)}, url=f"https://kalshi.com/markets/{ticker.split('-')[0].lower()}/{str(km.get('event_ticker', '')).lower()}", ts=now, meta={"status": km.get("status")}))
        if kq:
            quotes_by_venue["kalshi"] = kq

        pm_market = None
        if is_nfl and parsed[0]["teams"] and parsed[0]["date"]:  # type: ignore[index]
            pm_market = self.polymarket_nfl(parsed[0]["teams"], parsed[0]["date"])  # type: ignore[index]
        elif is_tennis:
            pm_market = self.polymarket_tennis(names)
        if pm_market:
            outs = _jl(pm_market.get("outcomes"))
            bb, ba = _f(pm_market.get("bestBid")), _f(pm_market.get("bestAsk"))
            sides = [(bb, ba), ((1 - ba) if ba is not None else None, (1 - bb) if bb is not None else None)]
            pq: list[OutcomeQuote] = []
            tokens = _jl(pm_market.get("clobTokenIds"))
            for i, label in enumerate(outs):
                key = (nfl_team_code(label) if is_nfl else person_key(label)) or ""
                if key not in outcomes:
                    continue
                bid, ask = sides[i]
                slug_ev = (pm_market.get("events") or [{}])[0].get("slug") or pm_market.get("slug")
                pq.append(OutcomeQuote(venue="polymarket", venue_market_id=str(tokens[i]) if i < len(tokens) else "", event_key=event_key, outcome=key, outcome_label=label, ask=round(ask, 4) if ask and 0 < ask < 1 else None, bid=round(bid, 4) if bid and 0 < bid < 1 else None, fee_params={"feeSchedule": pm_market.get("feeSchedule"), "feesEnabled": pm_market.get("feesEnabled", True)}, url=f"https://polymarket.com/event/{slug_ev}", ts=now))
            if len(pq) == 2:
                quotes_by_venue["polymarket"] = pq
            else:
                errors.append("polymarket: outcome names did not match")
        else:
            errors.append("polymarket: no matching market found")

        info = EventInfo(event_key=event_key, sport=sport, market_type="moneyline", outcomes=sorted(outcomes), labels=labels)
        from .matching.matcher import MergedEvent
        from .scanner import analyze_event

        me = MergedEvent(event_key=event_key, info=info, quotes_by_venue=quotes_by_venue)
        report = analyze_event(me, settings, contracts=contracts, target_margin=target_margin, now=now)
        out = asdict(report)
        out["errors"] = errors
        return {"ok": True, "event": {"id": ev.get("id"), "name": ev.get("name"), "sport": sport, "url": url, "key": event_key}, "analysis": out}

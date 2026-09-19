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
from .matching.normalize import fmt_line, kalshi_ticker_date, person_key, person_keys, push_rule_for_line, split_pair, spread_event_key, spread_outcomes, strip_digits, ticker_pair, total_event_key
from .matching.teams import nfl_team_city, nfl_team_code, team_code
from .models import EventInfo, OutcomeQuote
from .quant.arbitrage import Leg, best_leg_per_outcome, evaluate, max_price_for_leg
from .quant.fairvalue import consensus_fair_value
from .venues.kalshi import KalshiClient
from .venues.polymarket import GAMMA, PolymarketAdapter, _f, _jl
from .matching.normalize import parse_iso
from .venues.robinhood import LINE_SYMBOL_PREFIXES, RobinhoodAdapter, _event_day_from_timeline, _in_play_from_progress, clean_label

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
        self.last_event = None            # MergedEvent from the last game-winner analyze_url
        self.last_url: Optional[str] = None
        self.last_analyzed_at: float = 0.0

    PUBLIC_CATEGORIES = ("nfl", "tennis", "college-football", "nba", "nhl", "baseball", "soccer", "mma", "golf", "pro-football", "esports", "cricket")

    def resolve_public_event(self, slug: str) -> tuple[Optional[str], Optional[dict]]:
        """Find the public category page that serves this event slug (cached per slug)."""
        cache = getattr(self, "_slug_category", None)
        if cache is None:
            cache = self._slug_category = {}
        cats = ([cache[slug]] if slug in cache else []) + [c for c in self.PUBLIC_CATEGORIES if c != cache.get(slug)]
        for cat in cats:
            try:
                pp = self.rh.event_page(cat, slug)
            except Exception:
                continue
            if pp.get("event"):
                cache[slug] = cat
                return cat, pp
        return None, None

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

    def _parse_cdna_college(self, contracts_raw: list[dict], names: list[str], ev: dict) -> list[Optional[dict[str, Any]]]:
        """CDNA symbols (NX.F.OPT.CFB-00027-260919-M.O.1.1.20270228) carry no team codes, so
        build the same dict parse_symbol() returns from the contract names: canonical codes,
        the game date from the symbol, and the Kalshi ticker KXNCAAFGAME-{YYMONDD}{AWAY}{HOME}-{TEAM}
        (Robinhood names the event "Away vs Home", matching Kalshi's pair order)."""
        from .matching.teams import team_table
        from .venues.robinhood import _cdna_symbol_date

        codes = [team_code("ncaaf", c.get("displayShortName")) or team_code("ncaaf", n) for c, n in zip(contracts_raw, names)]
        date = _cdna_symbol_date(contracts_raw[0].get("symbol", ""))
        if any(c is None for c in codes) or codes[0] == codes[1] or not date:
            return [None, None]
        table = team_table("ncaaf")
        kcodes = [(table.get(c) or {}).get("kalshi") or c for c in codes]
        y, m, d = date.split("-")
        mon = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"][int(m) - 1]
        # Name order "Purdue vs UCLA" = away, home -> KXNCAAFGAME-26SEP19PURUCLA
        order = list(range(len(contracts_raw)))
        name_parts = [x.strip() for x in re.split(r"\s+vs\.?\s+", str(ev.get("name") or ""), maxsplit=1)]
        if len(name_parts) == 2:
            first = team_code("ncaaf", name_parts[0])
            if first == codes[1]:
                order = [1, 0]
        pair = "".join(kcodes[i] for i in order)
        return [{"family": "CFBGAME", "date": date, "pair": pair, "side": kcodes[i], "teams": [kcodes[j] for j in order], "kalshi_ticker": f"KXNCAAFGAME-{y[2:]}{mon}{d}{pair}-{kcodes[i]}", "routed": "cdna"} for i in range(len(contracts_raw))]

    def polymarket_cfb(self, names: list[str], outcomes: list[str], date: str) -> Optional[dict]:
        """College moneyline on Polymarket: search by the two team names, keep the event whose
        slug is cfb-…-{date} (UTC date of kickoff may be the ET date or the day after), then read
        the moneyline market by slug."""
        from datetime import datetime, timedelta

        d0 = datetime.strptime(date, "%Y-%m-%d")
        dates = {(d0 + timedelta(days=k)).strftime("%Y-%m-%d") for k in (0, 1)}
        q = " ".join(names)
        try:
            found = self.pm.http.get(f"{GAMMA}/public-search", {"q": q, "limit_per_type": 10})
        except Exception:
            return None
        for cand in (found or {}).get("events") or []:
            slug = str(cand.get("slug") or "")
            m = re.match(r"^cfb-[a-z0-9]+-[a-z0-9]+-(\d{4}-\d{2}-\d{2})$", slug)
            if not m or m.group(1) not in dates:
                continue
            try:
                ms = self.pm.http.get(f"{GAMMA}/markets", {"slug": slug})
            except Exception:
                ms = None
            for mk in ms or []:
                outs = _jl(mk.get("outcomes"))
                if mk.get("sportsMarketType") == "moneyline" and len(outs) == 2 and {team_code("ncaaf", o) for o in outs} == set(outcomes):
                    return mk
            if not ms:  # closed/odd markets: read the event itself
                try:
                    evs = self.pm.http.get(f"{GAMMA}/events", {"slug": slug})
                except Exception:
                    evs = None
                for mk in ((evs or [{}])[0].get("markets") or []):
                    outs = _jl(mk.get("outcomes"))
                    if mk.get("sportsMarketType") == "moneyline" and len(outs) == 2 and {team_code("ncaaf", o) for o in outs} == set(outcomes):
                        return mk
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
        if m:
            category, slug = m.group(1), m.group(2)
            pp = self.rh.event_page(category, slug)
        else:
            # Logged-in trading route: robinhood.com/events/<slug>?contract=<id> (client-rendered,
            # no category in the URL). The public page for the same slug carries the event data.
            m = re.search(r"robinhood\.com/events/([^/?#]+)", url)
            if not m:
                return {"ok": False, "error": "not a Robinhood prediction-market event URL"}
            slug = m.group(1)
            category, pp = self.resolve_public_event(slug)
            if pp is None:
                return {"ok": False, "error": f"could not find a public event page for slug {slug!r}"}
        ev = pp.get("event") or {}
        contracts_raw = list((ev.get("eventContracts") or {}).values())
        line_types = {mt for c in contracts_raw for pfx, mt in LINE_SYMBOL_PREFIXES.get("nfl", {}).items() if str(c.get("symbol", "")).startswith(pfx)}
        if contracts_raw and len(line_types) == 1:
            return self.analyze_lines(url, pp, ev, contracts_raw, line_types.pop(), settings=settings, contracts=contracts, target_margin=target_margin)
        if len(contracts_raw) != 2:
            return {"ok": True, "event": {"id": ev.get("id"), "name": ev.get("name")}, "analysis": None, "note": f"{len(contracts_raw)} contracts — only two-outcome game/match markets are analysed"}
        names = [clean_label(c.get("displayLongName") or c.get("displayShortName") or "") for c in contracts_raw]
        parsed = [parse_symbol(c.get("symbol", "")) for c in contracts_raw]
        is_cdna_college = all(str(c.get("symbol", "")).startswith("NX.F.OPT.CFB") for c in contracts_raw)
        if is_cdna_college:
            parsed = self._parse_cdna_college(contracts_raw, names, ev)
        if any(p is None for p in parsed):
            return {"ok": True, "event": {"id": ev.get("id"), "name": ev.get("name")}, "analysis": None, "note": "unrecognised contract symbols"}
        family = parsed[0]["family"]  # type: ignore[index]
        is_nfl = family == "NFLGAME"
        is_ncaaf = family in ("NCAAFGAME", "CFBGAME")
        is_tennis = bool(re.search(r"(ATP|WTA|ITF)", family) and "MATCH" in family)
        sport = "nfl" if is_nfl else "ncaaf" if is_ncaaf else "tennis" if is_tennis else "other"
        if is_nfl:
            outcomes = [nfl_team_code(c.get("displayShortName")) or nfl_team_code(n) or p["side"] for c, n, p in zip(contracts_raw, names, parsed)]  # type: ignore[index]
        elif is_ncaaf:
            outcomes = [team_code("ncaaf", c.get("displayShortName")) or team_code("ncaaf", n) or team_code("ncaaf", p["side"]) or p["side"] for c, n, p in zip(contracts_raw, names, parsed)]  # type: ignore[index]
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
        elif is_ncaaf and parsed[0]["date"]:  # type: ignore[index]
            pm_market = self.polymarket_cfb(names, outcomes, parsed[0]["date"])  # type: ignore[index]
        elif is_tennis:
            pm_market = self.polymarket_tennis(names)
        if pm_market:
            outs = _jl(pm_market.get("outcomes"))
            bb, ba = _f(pm_market.get("bestBid")), _f(pm_market.get("bestAsk"))
            sides = [(bb, ba), ((1 - ba) if ba is not None else None, (1 - bb) if bb is not None else None)]
            pq: list[OutcomeQuote] = []
            tokens = _jl(pm_market.get("clobTokenIds"))
            for i, label in enumerate(outs):
                key = (nfl_team_code(label) if is_nfl else team_code("ncaaf", label) if is_ncaaf else person_key(label)) or ""
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

        info = EventInfo(event_key=event_key, sport=sport, market_type="moneyline", outcomes=sorted(outcomes), labels=labels, in_play=_in_play_from_progress(str((pp.get("eventStates") or {}).get(ev.get("id"), {}).get("eventProgress") or "").strip(), None) if isinstance(pp.get("eventStates"), dict) else None)
        from .matching.matcher import MergedEvent
        from .scanner import analyze_event

        me = MergedEvent(event_key=event_key, info=info, quotes_by_venue=quotes_by_venue)
        self.last_event = me  # reused by the in-play watcher and the bridge's /inplay
        self.last_url, self.last_analyzed_at = url, now
        report = analyze_event(me, settings, contracts=contracts, target_margin=target_margin, now=now)
        out = asdict(report)
        out["errors"] = errors
        return {"ok": True, "event": {"id": ev.get("id"), "name": ev.get("name"), "sport": sport, "url": url, "key": event_key}, "analysis": out}


    # ---- spread / total pages: one binary contract per line -------------------------------
    def polymarket_game(self, teams: list[str], date: str) -> Optional[dict]:
        """Full Polymarket event (all lines) for an NFL game, trying both team orders and
        the ET date / next UTC date."""
        for slug in polymarket_nfl_slugs(teams, date):
            try:
                evs = self.pm.http.get(f"{GAMMA}/events", {"slug": slug})
            except Exception:
                continue
            if evs:
                return evs[0]
        return None

    def analyze_lines(self, url: str, pp: dict, ev: dict, contracts_raw: list[dict], mtype: str, settings: Optional[dict[str, Any]] = None, contracts: float = 100, target_margin: float = 0.0) -> dict[str, Any]:
        from .matching.matcher import MergedEvent
        from .scanner import analyze_event

        settings = settings or {}
        now = time.time()
        sym0 = contracts_raw[0].get("symbol", "")
        p0 = parse_symbol(sym0)
        if not p0:
            return {"ok": True, "event": {"id": ev.get("id"), "name": ev.get("name")}, "analysis": None, "note": "unrecognised contract symbols"}
        pair = p0["pair"]
        codes: list[str] = []
        for cut in range(2, len(pair) - 1):
            a, b = pair[:cut], pair[cut:]
            if nfl_team_code(a) and nfl_team_code(b):
                codes = [nfl_team_code(a), nfl_team_code(b)]  # type: ignore[list-item]
                break
        if not codes:
            return {"ok": True, "event": {"id": ev.get("id"), "name": ev.get("name")}, "analysis": None, "note": f"could not split team pair {pair}"}
        away, home = codes
        date = p0["date"] or ""
        try:
            live = self.rh.quotes([c["id"] for c in contracts_raw])
        except Exception:
            live = {}
        ssr = pp.get("quotes") or {}
        states = pp.get("eventStates") or {}
        st = states.get(ev.get("id"), {}) if isinstance(states, dict) else {}
        in_play = _in_play_from_progress(str(st.get("eventProgress") or "").strip(), st.get("eventStatus"))
        pm_event = self.polymarket_game([away, home], date)
        pm_index: dict[tuple[str, str], dict] = {}
        for m in (pm_event or {}).get("markets", []):
            smt = m.get("sportsMarketType")
            ln = _f(m.get("line"))
            if ln is None:
                continue
            if smt == "spreads":
                outs = _jl(m.get("outcomes"))
                oc = [nfl_team_code(o) for o in outs]
                if len(oc) == 2 and None not in oc:
                    fav = oc[0] if ln < 0 else oc[1]
                    pm_index[("spread", f"{fav}-{fmt_line(abs(ln))}")] = m
            elif smt == "totals":
                pm_index[("total", fmt_line(ln))] = m
        errors: list[str] = []
        if pm_event is None:
            errors.append("polymarket: game not found")
        family_kx = "KX" + p0["family"]
        # All of Kalshi's lines for this game in one call, indexed by (team, line) / line.
        kalshi_event = f"{family_kx}-{sym0.split('-')[1]}"
        k_index: dict[tuple[str, float], dict] = {}
        try:
            for km in self.kalshi.get("/markets", {"event_ticker": kalshi_event, "limit": 200}).get("markets", []):
                if km.get("status") not in (None, "active", "open"):
                    continue
                kl = _f(km.get("floor_strike"))
                if kl is None:
                    continue
                k_index[(strip_digits(km["ticker"].rsplit("-", 1)[-1]) if mtype == "spread" else "", kl)] = km
        except Exception as e:
            errors.append(f"kalshi {kalshi_event}: {str(e)[:80]}")
        k_series = self._series(kalshi_event)
        k_fee = {"fee_type": k_series.get("fee_type"), "fee_multiplier": k_series.get("fee_multiplier", 1)}
        start_time = parse_iso(st.get("gameStart")) or _event_day_from_timeline(ev.get("timeline"))
        missing_k: list[str] = []
        lines_out: list[dict[str, Any]] = []
        for c in contracts_raw:
            sym = c.get("symbol", "")
            line = _f(c.get("floorStrikeValue"))
            if line is None:
                continue
            exch = exchange_from_symbol_or_enum(sym, c.get("exchange"))
            if mtype == "spread":
                team_raw = strip_digits(sym.rsplit("-", 1)[-1])
                fav = nfl_team_code(team_raw)
                dog = nfl_team_code(split_pair(pair, team_raw) or "")
                if not fav or not dog:
                    continue
                key = spread_event_key("nfl", [fav, dog], date, fav, line)
                yes_key, no_key = spread_outcomes(fav, dog, line)
                labels = {yes_key: f"{nfl_team_city(fav)} -{fmt_line(line)}", no_key: f"{nfl_team_city(dog)} +{fmt_line(line)}"}
                km = k_index.get((team_raw, line))
                pm_m = pm_index.get(("spread", f"{fav}-{fmt_line(line)}"))
            else:
                key = total_event_key("nfl", codes, date, line)
                yes_key, no_key = "over", "under"
                labels = {"over": f"Over {fmt_line(line)}", "under": f"Under {fmt_line(line)}"}
                km = k_index.get(("", line))
                pm_m = pm_index.get(("total", fmt_line(line)))
            info = EventInfo(event_key=key, sport="nfl", market_type=mtype, outcomes=[yes_key, no_key], labels=labels, line=line, tie_rule=push_rule_for_line(line), venues={"_teams": {"title": f"{away} @ {home}"}}, in_play=in_play, start_time=start_time)
            qd = live.get(c["id"]) or ssr.get(c["id"]) or {}
            rh_common = dict(venue="robinhood", event_key=key, fee_params={"exchange": exch, "symbol": sym}, url=url, ts=now, book_id="kalshi" if exch == "kalshi" else exch)
            qbv: dict[str, list[OutcomeQuote]] = {"robinhood": [
                OutcomeQuote(venue_market_id=c["id"], outcome=yes_key, outcome_label=labels[yes_key], ask=_f(qd.get("yes_ask_price")), bid=_f(qd.get("yes_bid_price")), ask_size=_f(qd.get("ask_size")), meta={"symbol": sym, "exchange": exch, "side": "yes"}, **rh_common),
                OutcomeQuote(venue_market_id=c["id"] + "#no", outcome=no_key, outcome_label=labels[no_key], ask=_f(qd.get("no_ask_price")), bid=_f(qd.get("no_bid_price")), ask_size=_f(qd.get("bid_size")), meta={"symbol": sym, "exchange": exch, "side": "no"}, **rh_common),
            ]}
            if km is not None:
                kalshi_ticker = km["ticker"]
                ya, yb, na, nb = _f(km.get("yes_ask_dollars")), _f(km.get("yes_bid_dollars")), _f(km.get("no_ask_dollars")), _f(km.get("no_bid_dollars"))
                kurl = f"https://kalshi.com/markets/{family_kx.lower()}/{kalshi_event.lower()}"
                qbv["kalshi"] = [
                    OutcomeQuote(venue="kalshi", venue_market_id=kalshi_ticker, event_key=key, outcome=yes_key, outcome_label=labels[yes_key], ask=ya if ya and ya < 1 else None, bid=yb if yb and yb > 0 else None, ask_size=_f(km.get("yes_ask_size_fp")), fee_params=k_fee, url=kurl, ts=now, meta={"ticker": kalshi_ticker, "side": "yes"}),
                    OutcomeQuote(venue="kalshi", venue_market_id=kalshi_ticker + "#no", event_key=key, outcome=no_key, outcome_label=labels[no_key], ask=na if na and na < 1 else None, bid=nb if nb and nb > 0 else None, ask_size=_f(km.get("yes_bid_size_fp")), fee_params=k_fee, url=kurl, ts=now, meta={"ticker": kalshi_ticker, "side": "no"}),
                ]
            else:
                missing_k.append(fmt_line(line))
            if pm_m:
                outs = _jl(pm_m.get("outcomes"))
                bb, ba = _f(pm_m.get("bestBid")), _f(pm_m.get("bestAsk"))
                sides = [(bb, ba), ((1 - ba) if ba is not None else None, (1 - bb) if bb is not None else None)]
                if mtype == "spread":
                    ln = _f(pm_m.get("line")) or 0.0
                    keys = [yes_key, no_key] if ln < 0 else [no_key, yes_key]
                else:
                    keys = ["over", "under"] if str(outs[0]).lower().startswith("over") else ["under", "over"]
                tokens = _jl(pm_m.get("clobTokenIds"))
                purl = f"https://polymarket.com/event/{(pm_event or {}).get('slug')}"
                qbv["polymarket"] = [
                    OutcomeQuote(venue="polymarket", venue_market_id=str(tokens[i]) if i < len(tokens) else "", event_key=key, outcome=keys[i], outcome_label=labels[keys[i]], ask=round(sides[i][1], 4) if sides[i][1] and 0 < sides[i][1] < 1 else None, bid=round(sides[i][0], 4) if sides[i][0] and 0 < sides[i][0] < 1 else None, fee_params={"feeSchedule": pm_m.get("feeSchedule"), "feesEnabled": pm_m.get("feesEnabled", True)}, url=purl, ts=now, meta={"slug": pm_m.get("slug")})
                    for i in range(2)
                ]
            me = MergedEvent(event_key=key, info=info, quotes_by_venue=qbv)
            self.last_lines = getattr(self, "last_lines", {})
            self.last_lines[key] = me
            rep = analyze_event(me, settings, contracts=contracts, target_margin=target_margin, now=now)
            d = asdict(rep)
            d["contract_id"] = c["id"]
            d["symbol"] = sym
            lines_out.append(d)
        if missing_k:
            errors.append(f"kalshi lists no market for lines: {', '.join(dict.fromkeys(missing_k))}")
        lines_out.sort(key=lambda x: (not x["fillable"], -(x["margin"] if x["margin"] is not None else -9), x["line"] or 0))
        return {"ok": True, "event": {"id": ev.get("id"), "name": ev.get("name"), "sport": "nfl", "url": url, "market_type": mtype, "game": f"{away} @ {home}"}, "analysis": {"market_type": mtype, "lines": lines_out, "errors": errors, "venues": ["robinhood", "kalshi", "polymarket"], "contracts": contracts, "target_margin": target_margin}}

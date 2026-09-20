"""Refresh the offline test fixtures from the live public APIs (trimmed to a few events).

    python scripts/capture_fixtures.py            # venue snapshots (Kalshi / Polymarket / Robinhood NFL)
    python scripts/capture_fixtures.py --rules    # settlement rule texts -> tests/fixtures/rules/*.txt (+ sha256s)
    python scripts/capture_fixtures.py --tennis   # trimmed Kalshi settled tennis feed for the walkover-share parser

Fixtures are deliberately small so the tests stay readable; every venue adapter is
exercised against them in ``tests/test_venues.py`` without network access. Polymarket
market descriptions are kept trimmed to their settlement sentences (postponed / canceled /
tie / overtime) and the Gamma ``restricted`` field is preserved, because both are inputs to
the settlement registry and the compliance gate.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb_engine.venues import KalshiAdapter, PolymarketAdapter, RobinhoodAdapter  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
RULES_OUT = OUT / "rules"
SETTLEMENT_WORDS = re.compile(r"postpone|cancel|\btie\b|overtime|walkover|retire|50-50|shootout|days after|remain open", re.I)


def trim_description(desc: str | None, max_chars: int = 900) -> str:
    """Keep only the sentences that decide settlement (what parse_polymarket reads)."""
    if not desc:
        return ""
    keep = [ln.strip() for ln in re.split(r"(?<=[.])\s+|\n+", desc) if SETTLEMENT_WORDS.search(ln)]
    return "\n".join(keep)[:max_chars]


def _rule_text(header: str, sections: list[tuple[str, str]]) -> str:
    body = "\n\n".join(f"[{name}]\n{(text or '').strip()}" for name, text in sections)
    return f"# {header}\n\n{body}\n"


def capture_rules(today: str) -> None:
    """Verbatim rule texts, one file per (venue, sport, market_type). Prints each sha256 so the
    matching ``source.sha256`` in ``arb_engine/data/settlement_rules.json`` can be updated by
    hand (a changed hash is a venue rule change and deserves a look, not an auto-update)."""
    RULES_OUT.mkdir(parents=True, exist_ok=True)
    k = KalshiAdapter()
    kalshi = {"kalshi_nfl_moneyline.txt": "KXNFLGAME", "kalshi_nfl_spread.txt": "KXNFLSPREAD", "kalshi_nfl_total.txt": "KXNFLTOTAL", "kalshi_ncaaf_moneyline.txt": "KXNCAAFGAME", "kalshi_ncaaf_spread.txt": "KXNCAAFSPREAD", "kalshi_ncaaf_total.txt": "KXNCAAFTOTAL", "kalshi_nba_moneyline.txt": "KXNBAGAME", "kalshi_nhl_moneyline.txt": "KXNHLGAME", "kalshi_tennis_moneyline.txt": "KXWTAMATCH"}
    for name, series in kalshi.items():
        ms = k.client.markets(series, status="open", limit=1, max_pages=1) or k.client.markets(series, status="settled", limit=1, max_pages=1)
        if not ms:
            print(f"{name}: no {series} market listed, kept the old fixture")
            continue
        m = ms[0]
        text = _rule_text(f"Kalshi {series} market {m['ticker']} (GET /trade-api/v2/markets, captured {today})", [("rules_primary", m.get("rules_primary")), ("rules_secondary", m.get("rules_secondary"))])
        (RULES_OUT / name).write_text(text, encoding="utf-8")
        print(name, hashlib.sha256(text.encode("utf-8")).hexdigest())
    p = PolymarketAdapter()
    want = {("nfl", "moneyline"): "polymarket_nfl_moneyline.txt", ("nfl", "spreads"): "polymarket_nfl_spread.txt", ("nfl", "totals"): "polymarket_nfl_total.txt", ("cfb", "moneyline"): "polymarket_ncaaf_moneyline.txt", ("cfb", "spreads"): "polymarket_ncaaf_spread.txt", ("cfb", "totals"): "polymarket_ncaaf_total.txt", ("nhl", "moneyline"): "polymarket_nhl_moneyline.txt", ("nhl", "totals"): "polymarket_nhl_total.txt", ("nba", "moneyline"): "polymarket_nba_moneyline.txt", ("tennis", "moneyline"): "polymarket_tennis_moneyline.txt"}
    for tag in ("nfl", "cfb", "nhl", "nba", "tennis"):
        for ev in p.events(tag):
            for m in ev.get("markets", []):
                key = (tag, m.get("sportsMarketType"))
                if key not in want or not m.get("description"):
                    continue
                if tag == "tennis" and "advances" not in m["description"]:
                    continue
                text = _rule_text(f"Polymarket Gamma market {m.get('slug')} (event {ev.get('slug')}, sportsMarketType={m.get('sportsMarketType')}, restricted={m.get('restricted')}, captured {today})", [("description", m["description"])])
                name = want.pop(key)
                (RULES_OUT / name).write_text(text, encoding="utf-8")
                print(name, hashlib.sha256(text.encode("utf-8")).hexdigest())
    for key, name in want.items():
        print(f"{name}: no {key} market listed on Gamma, kept the old fixture")


def capture_settled_tennis(today: str, per_series: int = 3) -> None:
    """Trimmed settled tennis feed: a few scalar (fair-price) and binary matches per series."""
    k = KalshiAdapter()
    keep = ("ticker", "event_ticker", "status", "result", "settlement_value_dollars", "close_time", "title", "yes_sub_title", "expiration_value")
    out = []
    for series in ("KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH"):
        by_ev: dict[str, list[dict]] = {}
        for m in k.client.markets(series, status="settled", limit=200, max_pages=6):
            by_ev.setdefault(m["event_ticker"], []).append(m)
        scalar = [e for e, ms in by_ev.items() if any(x.get("result") == "scalar" for x in ms)][:per_series]
        binary = [e for e, ms in by_ev.items() if all(x.get("result") in ("yes", "no") for x in ms)][: per_series + 4]
        for e in scalar + binary:
            out.extend({f: m.get(f) for f in keep} for m in by_ev[e])
    (OUT / "kalshi_settled_tennis_trim.json").write_text(json.dumps({"markets": out, "cursor": None, "_note": f"trimmed from GET /trade-api/v2/markets?series_ticker=KX{{ATP,WTA}}{{,CHALLENGER}}MATCH&status=settled on {today}: {per_series} scalar (fair-price) matches + {per_series + 4} binary matches per series"}, indent=1))
    print("settled tennis fixture:", len(out), "markets")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    k = KalshiAdapter()
    markets = k.client.markets("KXNFLGAME")
    by_event: dict[str, list[dict]] = {}
    for m in markets:
        by_event.setdefault(m["event_ticker"], []).append(m)
    ev = sorted(by_event)[0]
    (OUT / "kalshi_markets_nfl.json").write_text(json.dumps({"markets": by_event[ev]}, indent=1))
    (OUT / "kalshi_series_kxnflgame.json").write_text(json.dumps({"series": k.client.series("KXNFLGAME")}, indent=1))
    (OUT / "kalshi_orderbook.json").write_text(json.dumps(k.client.orderbook(by_event[ev][0]["ticker"], 5), indent=1))
    # Spread + total lines for the same game (two spread markets, one total).
    game_code = ev.split("-", 1)[1]  # 26SEP17DETBUF
    spreads = [m for m in k.client.markets("KXNFLSPREAD") if m["event_ticker"] == f"KXNFLSPREAD-{game_code}" and m.get("floor_strike") == 1.5]
    totals = [m for m in k.client.markets("KXNFLTOTAL") if m["event_ticker"] == f"KXNFLTOTAL-{game_code}" and m.get("floor_strike") == 49.5]
    for m in spreads + totals:
        m.pop("rules_primary", None); m.pop("rules_secondary", None)
    (OUT / "kalshi_markets_nfl_lines.json").write_text(json.dumps({"spreads": spreads, "totals": totals}, indent=1))

    p = PolymarketAdapter()
    evs = p.events("nfl")
    # Prefer the same game as the Kalshi fixture so the scanner test can merge venues.
    codes = sorted(m["ticker"].rsplit("-", 1)[-1].lower() for m in by_event[ev])
    def _same_game(e: dict) -> bool:
        parts = e.get("slug", "").split("-")
        has_ml = any(m.get("sportsMarketType") == "moneyline" for m in e.get("markets", []))
        return has_ml and len(parts) >= 3 and sorted(parts[1:3]) == codes
    game = next((e for e in evs if _same_game(e)), None) or next(e for e in evs if any(m.get("sportsMarketType") == "moneyline" for m in e.get("markets", [])))
    slim = dict(game)
    ml = [m for m in game["markets"] if m.get("sportsMarketType") == "moneyline"][:1]
    sp = [m for m in game["markets"] if m.get("sportsMarketType") == "spreads" and m.get("line") == -1.5][:1]
    tt = [m for m in game["markets"] if m.get("sportsMarketType") == "totals" and m.get("line") == 49.5][:1]
    slim["markets"] = ml + sp + tt
    for m in slim["markets"]:
        m["description"] = trim_description(m.get("description"))  # settlement sentences only; restricted stays
    slim["description"] = trim_description(slim.get("description"))
    (OUT / "polymarket_events_nfl.json").write_text(json.dumps([slim], indent=1))
    tok = json.loads(slim["markets"][0]["clobTokenIds"])[0]
    (OUT / "polymarket_book.json").write_text(json.dumps(p.http.get("https://clob.polymarket.com/book", {"token_id": tok}), indent=1))

    r = RobinhoodAdapter()
    pp = r.category_page("nfl")
    games = r.select_game_events("nfl", pp["events"])
    games = sorted(games, key=lambda g: 0 if sorted(c["symbol"].rsplit("-", 1)[-1].lower() for c in g["contracts"]) == codes else 1)[:2]
    # Spread/total events for the first game, trimmed to the 1.5 spreads and the 49.5 total.
    line_events = []
    for x in r.select_line_contracts("nfl", pp["events"]):
        sym = x["contract"]["symbol"]
        if game_code not in sym:
            continue
        fl = float(x["contract"].get("floorStrikeValue") or 0)
        if (x["market_type"] == "spread" and fl == 1.5) or (x["market_type"] == "total" and fl == 49.5):
            line_events.append(x)
    line_by_event: dict[str, dict] = {}
    for x in line_events:
        e = line_by_event.setdefault(x["event"]["id"], dict(x["event"], eventContracts={}))
        e["eventContracts"][str(len(e["eventContracts"]))] = x["contract"]
    events_out = [g["event"] for g in games] + list(line_by_event.values())
    ids = [c["id"] for g in games for c in g["contracts"]] + [x["contract"]["id"] for x in line_events]
    quotes = r.quotes(ids)
    slim_pp = {
        "events": [{k2: v for k2, v in e.items() if k2 in ("id", "name", "eventContracts", "urlSlugs", "category", "mutuallyExclusive", "timeline", "eventType")} for e in events_out],
        "eventStates": {e["id"]: pp["eventStates"].get(e["id"], {}) for e in events_out},
        "quotes": {i: quotes[i] for i in ids if i in quotes},
    }
    for e in slim_pp["events"]:
        for c in e["eventContracts"].values():
            for junk in ("iconAsset", "color", "imageUrl", "attestationValues"):
                c.pop(junk, None)
    (OUT / "robinhood_page_props_nfl.json").write_text(json.dumps(slim_pp, indent=1))
    print("fixtures written to", OUT)


USAGE = """usage: capture_fixtures.py [--rules | --tennis]
  (no flag)  refresh the trimmed venue fixtures under tests/fixtures/ from the live public APIs
  --rules    capture the settlement rule texts into tests/fixtures/rules/ (sha256-pinned)
  --tennis   capture the settled Kalshi tennis feed sample
Every mode hits the network and REWRITES committed fixtures; there is nothing to preview."""


if __name__ == "__main__":
    from datetime import date

    if "-h" in sys.argv or "--help" in sys.argv or any(a.startswith("-") and a not in ("--rules", "--tennis") for a in sys.argv[1:]):
        print(USAGE)
        raise SystemExit(0 if ("-h" in sys.argv or "--help" in sys.argv) else 2)
    _today = date.today().isoformat()
    if "--rules" in sys.argv:
        capture_rules(_today)
    elif "--tennis" in sys.argv:
        capture_settled_tennis(_today)
    else:
        main()

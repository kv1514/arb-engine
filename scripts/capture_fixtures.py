"""Refresh the offline test fixtures from the live public APIs (trimmed to a few events).

    python scripts/capture_fixtures.py

Fixtures are deliberately small so the tests stay readable; every venue adapter is
exercised against them in ``tests/test_venues.py`` without network access.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb_engine.venues import KalshiAdapter, PolymarketAdapter, RobinhoodAdapter  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


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
        m.pop("description", None)
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


if __name__ == "__main__":
    main()

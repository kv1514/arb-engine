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
    slim["markets"] = [m for m in game["markets"] if m.get("sportsMarketType") in ("moneyline", "spreads")][:2]
    for m in slim["markets"]:
        m.pop("description", None)
    (OUT / "polymarket_events_nfl.json").write_text(json.dumps([slim], indent=1))
    tok = json.loads(slim["markets"][0]["clobTokenIds"])[0]
    (OUT / "polymarket_book.json").write_text(json.dumps(p.http.get("https://clob.polymarket.com/book", {"token_id": tok}), indent=1))

    r = RobinhoodAdapter()
    pp = r.category_page("nfl")
    games = r.select_game_events("nfl", pp["events"])
    games = sorted(games, key=lambda g: 0 if sorted(c["symbol"].rsplit("-", 1)[-1].lower() for c in g["contracts"]) == codes else 1)[:2]
    ids = [c["id"] for g in games for c in g["contracts"]]
    quotes = r.quotes(ids)
    slim_pp = {
        "events": [{k2: v for k2, v in g["event"].items() if k2 in ("id", "name", "eventContracts", "urlSlugs", "category", "mutuallyExclusive", "timeline", "eventType")} for g in games],
        "eventStates": {g["event"]["id"]: pp["eventStates"].get(g["event"]["id"], {}) for g in games},
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

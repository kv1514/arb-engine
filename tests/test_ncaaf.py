"""College football across the three venues (Kalshi KXNCAAFGAME, Polymarket cfb, Robinhood/CDNA)
and ESPN's college scoreboard — offline fixtures recorded 2026-09-18."""

import json
import unittest

from arb_engine.fees.robinhood import exchange_from_symbol_or_enum
from arb_engine.matching.teams import ncaaf_team_code, team_code, team_name
from arb_engine.scanner import scan
from arb_engine.venues import KalshiAdapter, PolymarketAdapter, RobinhoodAdapter
from arb_engine.venues.espn import ESPNClient, ESPNFeed
from arb_engine.venues.kalshi import KalshiClient

from .helpers import FakeHttp, load


def _adapters():
    kal = FakeHttp({
        "series_ticker=KXNCAAFGAME&": load("ncaaf/kalshi_markets_ncaaf.json"),
        "series_ticker=KXNCAAFSPREAD&": {"markets": []},
        "series_ticker=KXNCAAFTOTAL&": {"markets": []},
        "/series/KXNCAAFGAME": load("ncaaf/kalshi_series_kxncaafgame.json"),
        "/series/": {"series": {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}},
    })
    events = load("ncaaf/polymarket_events_cfb.json")
    poly = FakeHttp({"gamma-api.polymarket.com/events": lambda: list(events)})
    pp = load("ncaaf/robinhood_page_props_cfb.json")
    html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": pp}}) + "</script>"
    rh = FakeHttp({"/us/en/prediction-markets/college-football/": html})
    return [KalshiAdapter(client=KalshiClient(env="prod", http=kal)), PolymarketAdapter(http=poly), RobinhoodAdapter(http=rh, refresh_quotes=False)]


class TeamCodeTests(unittest.TestCase):
    def test_venue_spellings_resolve_to_one_code(self):
        for spelling in ("SJSU", "San Jose St.", "San Jose State", "San José State", "San José St"):
            self.assertEqual(ncaaf_team_code(spelling), "SJSU", spelling)
        self.assertEqual(ncaaf_team_code("Fresno St."), "FRES")
        self.assertEqual(ncaaf_team_code("Fresno State"), "FRES")
        self.assertEqual(ncaaf_team_code("Portland St."), "PRST")
        self.assertEqual(ncaaf_team_code("Coastal Carolina"), "CCU")
        self.assertEqual(ncaaf_team_code("CCAR"), "CCU")            # Robinhood/CDNA short name
        self.assertEqual(ncaaf_team_code("NW"), "NU")                # Kalshi ticker code
        self.assertEqual(ncaaf_team_code("Murray St."), "MUR")
        self.assertEqual(ncaaf_team_code("North Carolina St."), "NCSU")
        self.assertEqual(ncaaf_team_code("University at Albany"), "UALB")
        self.assertEqual(ncaaf_team_code("UTRGV"), "UTRGV")

    def test_no_fuzzy_collisions(self):
        self.assertEqual(ncaaf_team_code("Miami"), "MIA")
        self.assertEqual(ncaaf_team_code("Miami (OH)"), "M-OH")
        self.assertEqual(ncaaf_team_code("Washington"), "WASH")
        self.assertEqual(ncaaf_team_code("Washington State"), "WSU")
        self.assertIsNone(ncaaf_team_code("Oregon -93.5 points"))    # CDNA spread contract name
        self.assertIsNone(ncaaf_team_code("Over 49.5 points"))
        self.assertIsNone(ncaaf_team_code(""))
        self.assertEqual(team_name("ncaaf", "SJSU"), "San José State")
        self.assertEqual(team_code("nfl", "Bills"), "BUF")            # NFL path untouched

    def test_cdna_exchange(self):
        self.assertEqual(exchange_from_symbol_or_enum("NX.F.OPT.CFB-00027-260919-M.O.1.1.20270228", "EXCHANGE_SOURCE_CDNA"), "cdna")
        self.assertEqual(exchange_from_symbol_or_enum("NX.F.OPT.CFB-00027-260919-M.O.1.1.20270228", None), "cdna")
        self.assertEqual(exchange_from_symbol_or_enum("KXNCAAFGAME-26SEP19VILLLIU-VILL", "EXCHANGE_SOURCE_KALSHI"), "kalshi")


class ScanTests(unittest.TestCase):
    def test_three_venues_merge_on_one_key(self):
        res = scan("ncaaf", _adapters(), settings={}, market_types={"moneyline"})
        by_key = {e.event_key: e for e in res.events}
        self.assertIn("ncaaf:PUR|UCLA:2026-09-19", by_key)
        self.assertIn("ncaaf:FRES|SJSU:2026-09-19", by_key)
        ev = by_key["ncaaf:PUR|UCLA:2026-09-19"]
        self.assertEqual(sorted(ev.venues), ["kalshi", "polymarket", "robinhood"])
        pur = next(o for o in ev.outcomes if o.outcome == "PUR")
        self.assertEqual(pur.label in ("Purdue", "Purdue Boilermakers"), True)
        rh = next(v for v in pur.venues if v.venue == "robinhood")
        self.assertEqual(rh.exchange, "cdna")
        self.assertEqual(rh.ask, 0.15)
        self.assertEqual(rh.fee_per_contract, 0.02)       # $0.01 commission cap + $0.01 CDNA exchange fee
        self.assertAlmostEqual(rh.all_in, 0.17)
        self.assertIsNone(rh.mirror_of)                   # CDNA is its own book, not a Kalshi re-sale
        kal = next(v for v in pur.venues if v.venue == "kalshi")
        self.assertEqual(kal.ask, 0.15)
        pm = next(v for v in pur.venues if v.venue == "polymarket")
        self.assertEqual(pm.ask, 0.16)
        # The Kalshi-routed Robinhood game (Villanova vs LIU) is keyed the same way and flagged as Kalshi's book.
        vill = by_key.get("ncaaf:LIU|VILL:2026-09-19")
        self.assertIsNotNone(vill)
        self.assertEqual(vill.venues, ["robinhood"])
        self.assertEqual(vill.outcomes[0].venues[0].exchange, "kalshi")
        self.assertFalse(any(e.margin and e.margin > 0 for e in res.events))


class ESPNCollegeTests(unittest.TestCase):
    def test_scoreboard_keys_match_the_venues(self):
        client = ESPNClient(http=FakeHttp({"/scoreboard": load("ncaaf/espn_scoreboard_cfb.json")}), sport="ncaaf")
        self.assertIn("college-football", client.base_url)
        games = ESPNFeed(client).games()
        keys = {g.event_key: g for g in games}
        self.assertIn("ncaaf:PUR|UCLA:2026-09-19", keys)
        self.assertIn("ncaaf:FRES|SJSU:2026-09-19", keys)
        live = keys["ncaaf:ORE|PRST:2026-09-18"]
        self.assertEqual((live.sport, live.status, live.home, live.away), ("ncaaf", "live", "ORE", "PRST"))
        self.assertGreater(live.home_score, 50)
        self.assertIn(live.possession, ("home", "away", None))
        self.assertIsNotNone(live.game_seconds_remaining)
        pre = keys["ncaaf:PUR|UCLA:2026-09-19"]
        self.assertEqual(pre.status, "pre")
        self.assertIsNotNone(pre.vegas_spread_home)


if __name__ == "__main__":
    unittest.main()

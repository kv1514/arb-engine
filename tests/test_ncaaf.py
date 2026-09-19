"""College football across the three venues (Kalshi KXNCAAFGAME, Polymarket cfb, Robinhood/CDNA)
and ESPN's college scoreboard — offline fixtures recorded 2026-09-18."""

import json
import unittest

from arb_engine.eventlookup import EventAnalyzer
from arb_engine.fees.robinhood import exchange_from_symbol_or_enum
from arb_engine.matching.teams import ncaaf_team_code, team_code, team_name
from arb_engine.scanner import scan
from arb_engine.venues import KalshiAdapter, PolymarketAdapter, RobinhoodAdapter
from arb_engine.venues.espn import ESPNClient, ESPNFeed
from arb_engine.venues.kalshi import KalshiClient

from .helpers import FakeHttp, load


def _adapters():
    lines = load("ncaaf/kalshi_markets_ncaaf_lines.json")
    kal = FakeHttp({
        "series_ticker=KXNCAAFGAME&": load("ncaaf/kalshi_markets_ncaaf.json"),
        "series_ticker=KXNCAAFSPREAD&": {"markets": lines["spreads"]},
        "series_ticker=KXNCAAFTOTAL&": {"markets": lines["totals"]},
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


class LineTests(unittest.TestCase):
    """CDNA lists one contract per line ("Fresno State -23.5 points", "Over 57.5 points");
    Kalshi's KXNCAAFSPREAD/TOTAL use the same ceil(line) suffix convention as the NFL."""

    def test_cdna_lines_merge_with_kalshi(self):
        res = scan("ncaaf", _adapters(), settings={}, market_types={"spread", "total"})
        by_key = {e.event_key: e for e in res.events}
        sp = by_key.get("ncaaf:FRES|SJSU:2026-09-19:spread:FRES-23.5")
        self.assertIsNotNone(sp, sorted(k for k in by_key if "spread" in k)[:6])
        self.assertEqual(sorted(sp.venues), ["kalshi", "polymarket", "robinhood"])
        self.assertEqual(sp.line, 23.5)
        yes = next(o for o in sp.outcomes if o.outcome == "FRES-23.5")
        self.assertEqual(yes.label, "Fresno State -23.5")
        rh = next(v for v in yes.venues if v.venue == "robinhood")
        self.assertEqual(rh.exchange, "cdna")
        self.assertIsNotNone(rh.ask)
        kal = next(v for v in yes.venues if v.venue == "kalshi")
        self.assertIsNotNone(kal.ask)
        no = next(o for o in sp.outcomes if o.outcome == "SJSU+23.5")
        self.assertEqual(no.label, "San José State +23.5")
        # The other side's contract ("San Jose State -23.5 points") is its own line, favourite SJSU.
        self.assertIn("ncaaf:FRES|SJSU:2026-09-19:spread:SJSU-23.5", by_key)
        tot = by_key.get("ncaaf:FRES|SJSU:2026-09-19:total:57.5")
        self.assertIsNotNone(tot, sorted(k for k in by_key if "total" in k)[:6])
        self.assertEqual(sorted(tot.venues), ["kalshi", "polymarket", "robinhood"])
        over = next(o for o in tot.outcomes if o.outcome == "over")
        self.assertEqual({v.venue for v in over.venues}, {"kalshi", "polymarket", "robinhood"})
        under = next(o for o in tot.outcomes if o.outcome == "under")
        self.assertEqual({v.venue for v in under.venues}, {"kalshi", "polymarket", "robinhood"})
        pm_over = next(v for v in over.venues if v.venue == "polymarket")
        self.assertIsNotNone(pm_over.ask)
        self.assertEqual(tot.tie_rule, "no_push")  # half-point line cannot push


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


class EventPageTests(unittest.TestCase):
    """The overlay/bridge path for one CDNA-routed college event page."""

    def _analyzer(self):
        pp = load("ncaaf/robinhood_page_props_cfb.json")
        ev = next(e for e in pp["events"] if e["name"] == "Purdue vs UCLA")
        page = {"event": ev, "quotes": pp["quotes"], "eventStates": pp["eventStates"]}
        html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": page}}) + "</script>"
        rh = FakeHttp({"/prediction-markets/college-football/events/3fbbb7f6/": html, "/marketdata/event/contract/quotes/v1/": pp["quotes"]})
        km = {m["ticker"]: m for m in load("ncaaf/kalshi_markets_ncaaf.json")["markets"]}
        kal = FakeHttp({"/markets/KXNCAAFGAME-26SEP19PURUCLA-PUR": {"market": km["KXNCAAFGAME-26SEP19PURUCLA-PUR"]}, "/markets/KXNCAAFGAME-26SEP19PURUCLA-UCLA": {"market": km["KXNCAAFGAME-26SEP19PURUCLA-UCLA"]}, "/series/KXNCAAFGAME": load("ncaaf/kalshi_series_kxncaafgame.json")})
        pm_ev = next(e for e in load("ncaaf/polymarket_events_cfb.json") if e["slug"] == "cfb-pur-ucla-2026-09-19")
        pm = FakeHttp({"/public-search": {"events": [{"slug": "ncaa-football-2026-national-champion"}, {"slug": "cfb-pur-ucla-2025-09-20"}, {"slug": "cfb-pur-ucla-2026-09-19"}]}, "/markets?slug=cfb-pur-ucla-2026-09-19": pm_ev["markets"], "clob.polymarket.com/book?token_id=": {"bids": [{"price": "0.15", "size": "400"}], "asks": [{"price": "0.16", "size": "250"}]}})
        return EventAnalyzer(robinhood=RobinhoodAdapter(http=rh), kalshi=KalshiClient(env="prod", http=kal), polymarket=PolymarketAdapter(http=pm))

    def test_cdna_event_page_gets_all_three_venues(self):
        an = self._analyzer()
        res = an.analyze_url("https://robinhood.com/us/en/prediction-markets/college-football/events/3fbbb7f6/", settings={})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["event"]["sport"], "ncaaf")
        self.assertEqual(res["event"]["key"], "ncaaf:PUR|UCLA:2026-09-19")
        a = res["analysis"]
        self.assertEqual(a["errors"], [])
        self.assertEqual(sorted(a["venues"]), ["kalshi", "polymarket", "robinhood"])
        pur = next(o for o in a["outcomes"] if o["outcome"] == "PUR")
        by_venue = {v["venue"]: v for v in pur["venues"]}
        self.assertEqual(by_venue["robinhood"]["exchange"], "cdna")
        self.assertEqual(by_venue["robinhood"]["ask"], 0.15)
        self.assertEqual(by_venue["kalshi"]["ask"], 0.15)
        self.assertEqual(by_venue["polymarket"]["ask"], 0.16)
        self.assertEqual(by_venue["polymarket"]["ask_size"], 250.0)   # from the CLOB book, so a STEAL/arb here can be sized
        self.assertIsNotNone(by_venue["robinhood"]["max_buy_price"])
        self.assertEqual(an.last_event.info.sport, "ncaaf")


if __name__ == "__main__":
    unittest.main()

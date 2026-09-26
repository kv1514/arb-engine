import unittest
from datetime import datetime, timezone

from arb_engine.matching import et_date, fmt_line, kalshi_ticker_date, merge_snapshots, nfl_event_key, nfl_team_code, parse_iso, person_key, person_keys, push_rule_for_line, split_pair, split_ticker_pair, spread_event_key, spread_outcomes, strip_digits, tennis_event_key, ticker_pair, total_event_key
from arb_engine.models import EventInfo, OutcomeQuote, VenueSnapshot


class TeamTests(unittest.TestCase):
    def test_all_venue_spellings(self):
        cases = {"Los Angeles R": "LAR", "Rams": "LAR", "lar": "LAR", "LA Rams": "LAR", "Los Angeles C": "LAC", "New York G": "NYG", "N.Y. Jets": "NYJ", "Lions": "DET", "Buffalo": "BUF", "jax": "JAX", "JAC": "JAX", "Commanders": "WAS", "WSH": "WAS", "Spread: Bills (-1.5)": "BUF", "49ers": "SF"}
        for name, code in cases.items():
            self.assertEqual(nfl_team_code(name), code, name)
        self.assertIsNone(nfl_team_code("Over 49.5"))
        self.assertIsNone(nfl_team_code(""))


class DateTests(unittest.TestCase):
    def test_et_date_crosses_midnight(self):
        # Thursday night game listed as 2026-09-18 00:15 UTC is Sept 17 in Eastern time.
        self.assertEqual(et_date(datetime(2026, 9, 18, 0, 15, tzinfo=timezone.utc)), "2026-09-17")
        self.assertEqual(et_date(parse_iso("2026-09-20 17:00:00+00")), "2026-09-20")
        self.assertEqual(et_date(parse_iso("2026-12-01T01:00:00Z")), "2026-11-30")  # EST in December

    def test_parse_iso_variants(self):
        self.assertEqual(parse_iso("2026-09-15T22:47:42.498000851Z").second, 42)
        self.assertEqual(parse_iso("2026-09-20 17:00:00+00").hour, 17)
        self.assertIsNone(parse_iso(None))
        self.assertIsNone(parse_iso("garbage"))

    def test_kalshi_ticker_date(self):
        self.assertEqual(kalshi_ticker_date("KXNFLGAME-26SEP27BALDAL-BAL"), "2026-09-27")
        self.assertEqual(kalshi_ticker_date("KXWTAMATCH-26SEP16SEMKOS"), "2026-09-16")
        self.assertIsNone(kalshi_ticker_date("NOPE"))


class PersonTests(unittest.TestCase):
    def test_person_keys(self):
        self.assertEqual(person_key("Xiaodi You"), "you")
        self.assertEqual(person_key("X. You"), "you")
        self.assertEqual(person_key("R. Pacheco Mendez"), "pacheco mendez")
        self.assertEqual(person_key("K. Miyoshi (b. 2004)"), "miyoshi")
        self.assertEqual(person_key("Zeynep Sönmez"), "sonmez")
        self.assertEqual(person_key("Diego Dedura-Palomero"), "dedura palomero")

    def test_duplicate_surnames_fall_back_to_full_names(self):
        self.assertEqual(person_keys(["Ben Shelton", "Bryan Shelton"]), ["ben shelton", "bryan shelton"])

    def test_event_keys(self):
        self.assertEqual(tennis_event_key(["Xiaodi You", "Alina Charaeva"], "2026-09-16"), "tennis:charaeva|you:2026-09-16")
        self.assertEqual(nfl_event_key(["Lions", "Bills"], "2026-09-17"), "nfl:BUF|DET:2026-09-17")
        self.assertIsNone(nfl_event_key(["Lions", "Nobody"], "2026-09-17"))


class MergeTests(unittest.TestCase):
    def _snap(self, venue, key, start=None, in_play=None):
        info = EventInfo(event_key=key, sport="tennis", market_type="moneyline", outcomes=["a", "b"], labels={"a": "A", "b": "B"}, start_time=start, in_play=in_play)
        q = OutcomeQuote(venue, f"{venue}-a", key, "a", ask=0.5, bid=0.49)
        return VenueSnapshot(venue=venue, events={key: info}, quotes=[q])

    def test_date_tolerance_merges_adjacent_days(self):
        s1 = self._snap("kalshi", "tennis:a|b:2026-09-16", start=datetime(2026, 9, 16, 20, tzinfo=timezone.utc))
        s2 = self._snap("polymarket", "tennis:a|b:2026-09-17", start=datetime(2026, 9, 16, 18, tzinfo=timezone.utc), in_play=True)
        merged = merge_snapshots([s1, s2])
        self.assertEqual(len(merged), 1)
        me = merged["tennis:a|b:2026-09-16"]
        self.assertEqual(me.venues, ["kalshi", "polymarket"])
        self.assertEqual(me.info.start_time.hour, 18)  # earliest start wins
        self.assertTrue(me.info.in_play)
        self.assertEqual(me.quotes_by_venue["polymarket"][0].event_key, "tennis:a|b:2026-09-16")

    def test_far_dates_stay_separate(self):
        merged = merge_snapshots([self._snap("kalshi", "tennis:a|b:2026-09-16"), self._snap("polymarket", "tennis:a|b:2026-09-20")])
        self.assertEqual(len(merged), 2)


if __name__ == "__main__":
    unittest.main()


class LineKeyTests(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(fmt_line(1.5), "1.5")
        self.assertEqual(fmt_line(49.5), "49.5")
        self.assertEqual(fmt_line(3.0), "3")
        self.assertEqual(spread_outcomes("BUF", "DET", 1.5), ("BUF-1.5", "DET+1.5"))
        self.assertEqual(spread_event_key("nfl", ["DET", "BUF"], "2026-09-17", "BUF", 1.5), "nfl:BUF|DET:2026-09-17:spread:BUF-1.5")
        self.assertEqual(total_event_key("nfl", ["DET", "BUF"], "2026-09-17", 49.5), "nfl:BUF|DET:2026-09-17:total:49.5")
        self.assertEqual(split_pair("DETBUF", "BUF"), "DET")
        self.assertEqual(split_pair("NYGLAR", "NYG"), "LAR")
        self.assertIsNone(split_pair("DETBUF", "KC"))
        self.assertEqual(ticker_pair("KXNFLSPREAD-26SEP17DETBUF"), "DETBUF")
        self.assertEqual(strip_digits("BUF12"), "BUF")
        self.assertEqual(push_rule_for_line(1.5), "no_push")
        self.assertEqual(push_rule_for_line(3), "push_possible")

    def test_ticker_pair_refuses_ambiguous_college_codes(self):
        # Suffix-first splitting of MURMU returns MUR (Murray State). MU is also
        # Methodist, and RMU is Robert Morris, so both MUR+MU and MU+RMU are real.
        self.assertIsNone(split_ticker_pair("MURMU", "ncaaf", known="MU"))
        self.assertIsNone(split_ticker_pair("MURMU", "ncaaf"))
        # BENCAPU is Benedict+Capital or Benedictine College+Azusa Pacific.
        self.assertIsNone(split_ticker_pair("BENCAPU", "ncaaf"))
        self.assertEqual(split_ticker_pair("BENCAPU", "ncaaf", known="BEN"), ("BEN", "CAPU"))
        self.assertEqual(split_ticker_pair("BENCAPU", "ncaaf", known="BENC"), ("BENC", "APU"))
        # NWUNW is Northwestern (ticker NW)+Northwestern (MN), or Nebraska Wesleyan+Northwestern.
        self.assertIsNone(split_ticker_pair("NWUNW", "ncaaf", known="NW"))

    def test_ticker_pair_unique_games(self):
        self.assertEqual(split_ticker_pair("DETBUF", "nfl"), ("DET", "BUF"))
        self.assertEqual(split_ticker_pair("NYGLAR", "nfl", known="NYG"), ("NYG", "LAR"))
        # endswith('LA') on LAFLA used to return LAF and drop the Kings spread.
        self.assertEqual(split_ticker_pair("LAFLA", "nhl", known="LA"), ("LA", "FLA"))
        self.assertEqual(split_ticker_pair("LAFLA", "nhl"), ("LA", "FLA"))

    def test_ticker_pair_reads_kalshi_spellings_kept_as_aliases(self):
        # Real 2026-09-26/27 tickers. Kalshi writes the Jaguars JAC, NC State NCST and Albany
        # ALBY; the tables keep those as aliases, and every one of these games is on both venues.
        self.assertEqual(split_ticker_pair("NEJAC", "nfl"), ("NE", "JAX"))
        self.assertEqual(split_ticker_pair("NEJAC", "nfl", known="JAC"), ("NE", "JAX"))
        self.assertEqual(split_ticker_pair("LADAL", "nfl", known="LA"), ("LAR", "DAL"))
        self.assertEqual(split_ticker_pair("APPNCST", "ncaaf"), ("APP", "NCSU"))
        self.assertEqual(split_ticker_pair("ALBYPRIN", "ncaaf", known="PRIN"), ("UALB", "PRIN"))

    def test_ticker_pair_two_readings_settled_by_kalshi_spelling(self):
        # TOWSDSU cuts as TOW+SDSU and TOWS+DSU. Kalshi spells Towson TOWS, so the game is
        # Towson @ Delaware State; first-cut-wins had keyed it Towson @ San Diego State.
        self.assertEqual(split_ticker_pair("TOWSDSU", "ncaaf"), ("TOW", "DSU"))

    def test_merge_keeps_lines_separate(self):
        def snap(venue, key, line):
            info = EventInfo(event_key=key, sport="nfl", market_type="spread", outcomes=["BUF-1.5", "DET+1.5"], line=line)
            return VenueSnapshot(venue=venue, events={key: info}, quotes=[OutcomeQuote(venue, venue + key, key, "BUF-1.5", ask=0.6)])
        merged = merge_snapshots([snap("kalshi", "nfl:BUF|DET:2026-09-17:spread:BUF-1.5", 1.5), snap("polymarket", "nfl:BUF|DET:2026-09-17:spread:BUF-1.5", 1.5), snap("polymarket", "nfl:BUF|DET:2026-09-17:spread:BUF-2.5", 2.5), snap("robinhood", "nfl:BUF|DET:2026-09-18:spread:BUF-1.5", 1.5)])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged["nfl:BUF|DET:2026-09-17:spread:BUF-1.5"].venues, ["kalshi", "polymarket", "robinhood"])  # date drift tolerated

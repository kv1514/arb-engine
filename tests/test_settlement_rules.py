"""Settlement-rule registry, rule-text parsers, pair flags, tennis gates, Polymarket book meta."""

import importlib.util
import json
import unittest
from pathlib import Path

from arb_engine.matching import settlement_rules as sr
from arb_engine.matching.matcher import merge_snapshots
from arb_engine.models import Book, Level, OutcomeQuote, VenueSnapshot
from arb_engine.venues.kalshi import KalshiAdapter, KalshiClient
from arb_engine.venues.polymarket import PolymarketAdapter, settlement_from_description
from arb_engine.venues.robinhood import RobinhoodAdapter

from .helpers import FIXTURES, FakeHttp, load

RULES = FIXTURES / "rules"


def _sections(name: str) -> dict[str, str]:
    """{'rules_primary': ..., 'description': ...} from a rules fixture."""
    text = (RULES / name).read_text(encoding="utf-8")
    out: dict[str, str] = {}
    cur = None
    for line in text.splitlines():
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1]
            out[cur] = ""
        elif cur:
            out[cur] += line + "\n"
    return out


def _q(venue: str, outcome: str, ask: float, bid: float = None, size: float = 100.0, exchange: str = None, **meta) -> OutcomeQuote:
    m = dict(meta)
    if exchange:
        m["exchange"] = exchange
    return OutcomeQuote(venue=venue, venue_market_id=meta.get("ticker") or f"{venue}-{outcome}", event_key="tennis:a|b:2026-09-20", outcome=outcome, ask=ask, bid=bid if bid is not None else round(ask - 0.02, 4), ask_size=size, meta=m, fee_params={"exchange": exchange} if exchange else {})


def _script(name: str):
    path = Path(__file__).resolve().parents[1] / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class RegistryTests(unittest.TestCase):
    def test_registry_loads_and_every_row_is_sourced(self):
        reg = sr.load()
        self.assertGreater(len(reg["rules"]), 15)
        self.assertEqual(sr.verify(), [])
        for row in reg["rules"]:
            self.assertIn(row["status"], sr.STATUSES)
            self.assertEqual(sr.sha256_text((RULES / row["source"]["fixture"]).read_text(encoding="utf-8")), row["source"]["sha256"])
            for f in sr.FIELDS:
                self.assertIn(f, row)

    def test_verbatim_rows_match_their_parsed_fixture_text(self):
        """A verbatim row must say exactly what the parser reads from the cited text, so a
        template change on the venue fails here (hash) and there (fields)."""
        for row in sr.rules():
            if row["status"] != "verbatim":
                continue
            sec = _sections(row["source"]["fixture"])
            parsed = sr.parse_kalshi(sec.get("rules_primary"), sec.get("rules_secondary")) if row["venue"] == "kalshi" else sr.parse_polymarket(sec.get("description"))
            self.assertEqual({f: row[f] for f in sr.FIELDS}, parsed, msg=f"{row['venue']}/{row['sport']}/{row['market_type']}")

    def test_verify_reports_a_stale_hash(self):
        reg = json.loads(sr.DATA_PATH.read_text(encoding="utf-8"))
        reg["rules"][0]["source"]["sha256"] = "0" * 64
        tmp = Path(__file__).resolve().parent / "fixtures" / "_tmp_registry.json"
        tmp.write_text(json.dumps(reg), encoding="utf-8")
        try:
            problems = sr.verify(str(tmp))
        finally:
            tmp.unlink()
        self.assertEqual(len(problems), 1)
        self.assertIn("sha256", problems[0])

    def test_lookup_resolves_mirrors_and_wildcards(self):
        self.assertEqual(sr.lookup("kalshi", "nfl", "moneyline")["tie"], "half")
        self.assertIs(sr.lookup("robinhood", "nfl", "moneyline", "kalshi"), sr.lookup("kalshi", "nfl", "moneyline"))
        self.assertEqual(sr.lookup("robinhood", "nfl", "moneyline", "rothera")["tie"], "no_winner")
        self.assertEqual(sr.lookup("robinhood", "nfl", "spread", "rothera")["status"], "unverified")
        self.assertEqual(sr.lookup("robinhood", "ncaaf", "moneyline", "cdna")["cancelled"], "vwap_1w")
        self.assertIsNone(sr.lookup("robinhood", "nfl", "moneyline", "forecastex"))
        self.assertIsNone(sr.lookup("kalshi", "mlb", "moneyline"))

    def test_tennis_constants_re_exported_from_json(self):
        self.assertEqual(sr.KALSHI_TENNIS_SETTLEMENT, {"retirement": "advancer", "walkover": "fair_price", "cancelled": "fair_price", "postponed": "open_2w"})
        self.assertEqual(sr.POLYMARKET_TENNIS_SETTLEMENT["walkover"], "50-50")
        self.assertEqual(sr.POLYMARKET_TENNIS_SETTLEMENT["postponed"], "50-50_after_14d")
        from arb_engine.venues.kalshi import TENNIS_SETTLEMENT as K  # adapter literal still agrees
        from arb_engine.venues.polymarket import TENNIS_SETTLEMENT as P

        self.assertEqual(K, sr.KALSHI_TENNIS_SETTLEMENT)
        self.assertEqual(P, sr.POLYMARKET_TENNIS_SETTLEMENT)

    def test_walkover_rows_are_derived_with_n_or_labelled_provisional(self):
        rows = {r["tier"]: r for r in sr.load()["tennis_walkover"]}
        self.assertEqual(set(rows), set(sr.TIERS))
        for tier, r in rows.items():
            if r["status"] == "derived":
                self.assertGreaterEqual(r["n_markets"], 200)
                self.assertAlmostEqual(r["p_walkover"], r["n_scalar"] / r["n_markets"], places=4)
            else:
                self.assertEqual(r["status"], "provisional")
        self.assertEqual(sr.walkover_probability("tour")[1], "derived")
        self.assertEqual(sr.walkover_probability("itf"), (0.09, "provisional"))
        self.assertEqual(sr.walkover_probability("tour", {"tennis_walkover_p_tour": 0.05}), (0.05, "settings"))


class ParserTests(unittest.TestCase):
    def test_parse_kalshi_game_and_tennis_templates(self):
        nfl = _sections("kalshi_nfl_moneyline.txt")
        self.assertEqual(sr.parse_kalshi(nfl["rules_primary"], nfl["rules_secondary"]), {"tie": "half", "postponed": "open_48h", "cancelled": "fair_price", "walkover": None, "retirement": None, "ot_included": None})
        ten = _sections("kalshi_tennis_moneyline.txt")
        self.assertEqual(sr.parse_kalshi(ten["rules_primary"], ten["rules_secondary"]), {"tie": None, "postponed": "open_2w", "cancelled": "fair_price", "walkover": "fair_price", "retirement": "advancer", "ot_included": None})
        self.assertEqual(sr.parse_kalshi("", None), {f: None for f in sr.FIELDS})

    def test_parse_polymarket_7_day_14_day_and_date_templates(self):
        seven = ("This market will resolve to 'A' if A advances against B. If the match is canceled, ends in a tie, or is delayed beyond 7 days "
                 "without a winner, this market will resolve to 50-50. If the match begins but is not completed, and one player advances due to the "
                 "opponent's retirement, default, or disqualification, this market will resolve to the player who advances. If the match ends in a walkover "
                 "(player withdraws before the start), this market will resolve to 50-50.")
        self.assertEqual(sr.parse_polymarket(seven), {"tie": "half", "postponed": "50-50_after_7d", "cancelled": "50-50", "walkover": "50-50", "retirement": "advancer", "ot_included": None})
        fourteen = _sections("polymarket_tennis_moneyline.txt")["description"]
        self.assertEqual(sr.parse_polymarket(fourteen)["postponed"], "50-50_after_14d")
        dated = "If the match is canceled (not played at all), ends in a tie, or a winner has not been determined by October 4, 2026, 11:59 PM ET, this market will resolve to 50-50."
        self.assertEqual(sr.parse_polymarket(dated)["postponed"], "50-50_after_date")
        game = _sections("polymarket_nfl_moneyline.txt")["description"]
        self.assertEqual(sr.parse_polymarket(game), {"tie": "half", "postponed": "open_until_complete", "cancelled": "50-50", "walkover": None, "retirement": None, "ot_included": True})
        self.assertEqual(sr.parse_polymarket(None), {f: None for f in sr.FIELDS})


class PairFlagTests(unittest.TestCase):
    def test_kalshi_x_polymarket_nfl_mismatches_on_cancellation_and_postponement(self):
        flags = sr.pair_flags(_q("kalshi", "DET", 0.33), _q("polymarket", "BUF", 0.67), "nfl", "moneyline")
        self.assertIn("settlement-mismatch:cancelled", flags)
        self.assertIn("settlement-mismatch:postponed", flags)
        self.assertNotIn("settlement-mismatch:tie", flags)  # both pay 50 cents on a tie
        self.assertEqual(sr.pair_flags(_q("kalshi", "DET", 0.33), _q("polymarket", "BUF", 0.67), "nhl", "moneyline"), ["settlement-mismatch:cancelled", "settlement-mismatch:postponed", "settlement-unstated:ot_included", "settlement-unstated:tie"])

    def test_kalshi_x_robinhood_kx_is_one_book_and_never_flags(self):
        self.assertEqual(sr.pair_flags(_q("kalshi", "A", 0.4), _q("robinhood", "B", 0.6, exchange="kalshi"), "tennis"), [])
        self.assertEqual(sr.pair_flags(_q("kalshi", "A", 0.4), _q("robinhood", "B", 0.6, exchange="kalshi"), "nfl"), [])

    def test_rothera_and_cdna_rows_are_flagged_unverified(self):
        flags = sr.pair_flags(_q("kalshi", "DET", 0.33), _q("robinhood", "BUF", 0.67, exchange="rothera"), "nfl", "moneyline")
        self.assertIn("tie-rule-unverified", flags)
        self.assertIn("settlement-rule-unverified:robinhood", flags)
        self.assertIn("settlement-mismatch:tie", flags)  # half vs no_winner (if 40.2(d) reads as the plan says)
        flags = sr.pair_flags(_q("polymarket", "PUR", 0.3), _q("robinhood", "UCLA", 0.7, exchange="cdna"), "ncaaf", "moneyline")
        self.assertIn("settlement-mismatch:cancelled", flags)  # 50-50 vs vwap_1w
        self.assertIn("settlement-rule-unverified:robinhood", flags)

    def test_missing_row_is_a_flag_not_a_silent_pass(self):
        self.assertEqual(sr.pair_flags(_q("kalshi", "A", 0.4), _q("polymarket_us", "B", 0.6), "nfl"), ["settlement-rule-missing:polymarket_us"])

    def test_every_cross_book_pair_on_the_fixture_scans_is_covered(self):
        """Acceptance: each Kalshi x Polymarket (and x Rothera/CDNA) pair on the NFL and NCAAF
        fixtures carries a settlement flag or two matched registry rows."""
        for sport, adapters in (("nfl", _nfl_adapters()), ("ncaaf", _ncaaf_adapters())):
            merged = merge_snapshots([a.fetch(sport) for a in adapters])
            pairs = covered = 0
            for me in merged.values():
                quotes = [q for qs in me.quotes_by_venue.values() for q in qs]
                for a in quotes:
                    for b in quotes:
                        if a.outcome >= b.outcome or a.book_id == b.book_id:
                            continue
                        pairs += 1
                        flags = sr.pair_flags(a, b, sport, me.info.market_type)
                        matched = sr.rule_for_quote(a, sport, me.info.market_type) is not None and sr.rule_for_quote(b, sport, me.info.market_type) is not None
                        self.assertTrue(flags or matched, msg=f"{sport} {me.event_key} {a.venue}/{sr.quote_exchange(a)} x {b.venue}/{sr.quote_exchange(b)}")
                        self.assertFalse(any(f.startswith("settlement-rule-missing") for f in flags), msg=f"{me.event_key}: {flags}")
                        covered += 1
            self.assertGreater(pairs, 0, sport)
            self.assertEqual(covered, pairs)


def _nfl_adapters():
    lines = load("kalshi_markets_nfl_lines.json")
    kal = FakeHttp({"series_ticker=KXNFLGAME&": load("kalshi_markets_nfl.json"), "series_ticker=KXNFLSPREAD&": {"markets": lines["spreads"]}, "series_ticker=KXNFLTOTAL&": {"markets": lines["totals"]}, "/series/KXNFLGAME": load("kalshi_series_kxnflgame.json"), "/series/": {"series": {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}}})
    events = load("polymarket_events_nfl.json")
    poly = FakeHttp({"gamma-api.polymarket.com/events": lambda: list(events)})
    pp = load("robinhood_page_props_nfl.json")
    html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": pp}}) + "</script>"
    rh = FakeHttp({"/us/en/prediction-markets/nfl/": html})
    return [KalshiAdapter(client=KalshiClient(env="prod", http=kal)), PolymarketAdapter(http=poly), RobinhoodAdapter(http=rh, refresh_quotes=False)]


def _ncaaf_adapters():
    lines = load("ncaaf/kalshi_markets_ncaaf_lines.json")
    kal = FakeHttp({"series_ticker=KXNCAAFGAME&": load("ncaaf/kalshi_markets_ncaaf.json"), "series_ticker=KXNCAAFSPREAD&": {"markets": lines["spreads"]}, "series_ticker=KXNCAAFTOTAL&": {"markets": lines["totals"]}, "/series/KXNCAAFGAME": load("ncaaf/kalshi_series_kxncaafgame.json"), "/series/": {"series": {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}}})
    events = load("ncaaf/polymarket_events_cfb.json")
    poly = FakeHttp({"gamma-api.polymarket.com/events": lambda: list(events)})
    pp = load("ncaaf/robinhood_page_props_cfb.json")
    html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": pp}}) + "</script>"
    rh = FakeHttp({"/us/en/prediction-markets/college-football/": html})
    return [KalshiAdapter(client=KalshiClient(env="prod", http=kal)), PolymarketAdapter(http=poly), RobinhoodAdapter(http=rh, refresh_quotes=False)]


class TennisGateTests(unittest.TestCase):
    def test_walkover_exposed_when_favourite_sits_on_polymarket(self):
        fav = _q("polymarket", "zidansek", 0.85, 0.84, slug="wta-zidansek-jeong-2026-09-19")
        dog = _q("kalshi", "jeong", 0.14, 0.13, ticker="KXWTAMATCH-26SEP19ZIDJEO-JEO")
        # p_walkover(tour) x (0.85 - 0.5) = 0.0325 x 0.35 = 0.011375 expected loss per contract
        self.assertEqual(sr.tennis_pair_flags({"legs": [fav, dog], "margin": 0.005}), ["walkover-exposed"])
        self.assertEqual(sr.tennis_pair_flags({"legs": [fav, dog], "margin": 0.02}), [])
        # Favourite on Kalshi: a walkover pays the Polymarket dog 50 cents on a 14-cent ticket -> no exposure.
        fav_k = _q("kalshi", "zidansek", 0.85, 0.84, ticker="KXWTAMATCH-26SEP19ZIDJEO-ZID")
        dog_p = _q("polymarket", "jeong", 0.14, 0.13, slug="wta-zidansek-jeong-2026-09-19")
        self.assertEqual(sr.tennis_pair_flags({"legs": [fav_k, dog_p], "margin": 0.0}), [])
        # Settings override wins over the registry.
        self.assertEqual(sr.tennis_pair_flags({"legs": [fav, dog], "margin": 0.02}, {"tennis_walkover_p_tour": 0.1}), ["walkover-exposed"])

    def test_tier_flags_from_ticker_or_slug(self):
        self.assertEqual(sr.tennis_tier("KXATPCHALLENGERMATCH-26SEP16MAYCAS-MAY"), "challenger")
        self.assertEqual(sr.tennis_tier("itf-gaspar1-consta1-2026-09-20"), "itf")
        self.assertEqual(sr.tennis_tier("W15 Constanta"), "itf")
        self.assertEqual(sr.tennis_tier("WTA 125K Valencia"), "challenger")
        self.assertEqual(sr.tennis_tier("wta-quevedo-podoros-2026-09-20"), "tour")
        a = _q("kalshi", "a", 0.45, 0.44, ticker="KXATPCHALLENGERMATCH-26SEP16MAYCAS-MAY")
        b = _q("polymarket", "b", 0.54, 0.53, slug="atp-mayot-cassone-2026-09-16")
        self.assertEqual(sr.tennis_pair_flags({"legs": [a, b], "margin": 0.01}), ["tier:challenger"])
        self.assertEqual(sr.tennis_pair_flags({"legs": [a, b], "margin": 0.01, "tier": "itf"}), ["tier:itf"])

    def test_thin_book_on_wide_spread_or_small_size(self):
        a = _q("kalshi", "a", 0.45, 0.40)  # ask - bid = 0.05 > 0.03
        b = _q("polymarket", "b", 0.54, 0.53)
        self.assertEqual(sr.tennis_pair_flags({"legs": [a, b], "margin": 0.01}), ["thin-book"])
        a = _q("kalshi", "a", 0.45, 0.44, size=10)  # 10 < 20 contracts at the top
        self.assertEqual(sr.tennis_pair_flags({"legs": [a, b], "margin": 0.01}), ["thin-book"])
        a = _q("kalshi", "a", 0.45, 0.44)
        a.book = Book(asks=[Level(0.45, 15.0)], bids=[Level(0.44, 500.0)])  # attached book wins over the summary size
        self.assertEqual(sr.tennis_pair_flags({"legs": [a, b], "margin": 0.01}), ["thin-book"])
        self.assertEqual(sr.tennis_pair_flags({"legs": [a, b], "margin": 0.01}, {"tennis_thin_book_size": 10}), [])
        self.assertEqual(sr.tennis_pair_flags({"legs": []}), [])


class SettlementShareScriptTests(unittest.TestCase):
    def test_counts_scalar_vs_binary_per_tier_on_the_trimmed_feed(self):
        mod = _script("tennis_settlement_share.py")
        summary = mod.summarise(mod.flatten(load("kalshi_settled_tennis_trim.json")))
        self.assertEqual(set(summary), {"tour", "challenger"})
        for tier in summary:
            self.assertEqual(summary[tier]["n_markets"], 40)
            self.assertEqual(summary[tier]["n_matches"], 20)
            self.assertEqual(summary[tier]["n_scalar"], 12)
            self.assertEqual(summary[tier]["n_binary"], 28)
            self.assertEqual(summary[tier]["n_other"], 0)
            self.assertEqual(summary[tier]["p_scalar"], 0.3)
        scalar = [m for m in load("kalshi_settled_tennis_trim.json")["markets"] if m["result"] == "scalar"]
        self.assertTrue(all(0 < float(m["settlement_value_dollars"]) < 1 for m in scalar))
        self.assertEqual(mod.classify({"result": "void"}), "other")
        rows = mod.rows_from_summary(summary, "test")
        self.assertTrue(all(r["status"] == "provisional" and r["p_walkover"] is None for r in rows))  # 40 < 200

    def test_write_registry_keeps_provisional_rows(self):
        mod = _script("tennis_settlement_share.py")
        tmp = Path(__file__).resolve().parent / "fixtures" / "_tmp_registry2.json"
        tmp.write_text(sr.DATA_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        try:
            mod.write_registry([{"tier": "tour", "p_walkover": 0.04, "n_markets": 500, "n_matches": 250, "n_scalar": 20, "status": "derived", "source": "t"}, {"tier": "itf", "p_walkover": None, "n_markets": 3, "n_matches": 2, "n_scalar": 0, "status": "provisional", "source": "t"}], tmp)
            rows = {r["tier"]: r for r in json.loads(tmp.read_text())["tennis_walkover"]}
        finally:
            tmp.unlink()
        self.assertEqual(rows["tour"]["p_walkover"], 0.04)
        self.assertEqual(rows["itf"]["p_walkover"], 0.09)
        self.assertEqual(rows["itf"]["status"], "provisional")


class PolymarketMetaTests(unittest.TestCase):
    def test_book_meta_and_restricted_travel_with_the_quote(self):
        events = load("polymarket_events_nfl.json")
        token = json.loads(events[0]["markets"][0]["clobTokenIds"])[0]
        book = dict(load("polymarket_book.json"), asset_id=token, tick_size="0.001", min_order_size="5")
        http = FakeHttp({"gamma-api.polymarket.com/events": lambda: list(events), "clob.polymarket.com/books": [book]})
        snap = PolymarketAdapter(http=http, with_books=True).fetch("nfl")
        q = next(q for q in snap.quotes if q.venue_market_id == token)
        self.assertEqual((q.meta["tick_size"], q.meta["min_order_size"]), (0.001, 5.0))
        self.assertEqual((q.meta["tick"], q.meta["min_size"]), (0.001, 5.0))
        self.assertTrue(q.meta["restricted"])
        self.assertTrue(all(q.meta.get("restricted") for q in snap.quotes))  # fixture events are all restricted
        self.assertTrue(snap.events[q.event_key].venues["polymarket"]["restricted"])
        other = next(o for o in snap.quotes if o.venue_market_id != token and o.event_key == q.event_key)
        self.assertNotIn("tick_size", other.meta)  # no book fetched for that token

    def test_unrestricted_event_carries_false(self):
        events = load("polymarket_events_nfl.json")
        ev = json.loads(json.dumps(events[0]))
        ev["restricted"] = False
        for m in ev["markets"]:
            m["restricted"] = False
        snap = PolymarketAdapter(http=FakeHttp({"gamma-api.polymarket.com/events": lambda: [ev]})).fetch("nfl")
        self.assertTrue(snap.quotes)
        self.assertTrue(all(q.meta["restricted"] is False for q in snap.quotes))

    def test_tennis_settlement_parsed_per_market(self):
        desc = _sections("polymarket_tennis_moneyline.txt")["description"]
        self.assertEqual(settlement_from_description("tennis", desc), {"retirement": "advancer", "walkover": "50-50", "cancelled": "50-50", "postponed": "50-50_after_14d"})
        self.assertEqual(settlement_from_description("tennis", desc.replace("(14 days after the scheduled start)", "(7 days after the scheduled start)"))["postponed"], "50-50_after_7d")
        self.assertEqual(settlement_from_description("tennis", None), sr.POLYMARKET_TENNIS_SETTLEMENT)
        self.assertEqual(settlement_from_description("nfl", _sections("polymarket_nfl_moneyline.txt")["description"]), {"tie": "half", "postponed": "open_until_complete", "cancelled": "50-50", "ot_included": True})
        self.assertEqual(settlement_from_description("nfl", None), {})
        snap = VenueSnapshot(venue="polymarket")
        ev = {"id": "1", "slug": "wta-zidansek-jeong-2026-09-19", "restricted": True, "markets": [{"id": "m", "sportsMarketType": "moneyline", "outcomes": '["Tamara Zidansek", "Bo Young Jeong"]', "outcomePrices": '["0.85", "0.15"]', "clobTokenIds": '["1", "2"]', "gameStartTime": "2026-09-19 04:00:00+00", "bestBid": 0.84, "bestAsk": 0.86, "description": desc, "feeSchedule": {}}]}
        PolymarketAdapter(http=FakeHttp({}))._ingest_event(snap, "tennis", ev)
        info = next(iter(snap.events.values()))
        self.assertEqual(info.venues["polymarket"]["settlement"]["postponed"], "50-50_after_14d")
        self.assertTrue(info.venues["polymarket"]["restricted"])


if __name__ == "__main__":
    unittest.main()

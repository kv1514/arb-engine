"""Adversarial contract-identity and simultaneous-observation tests for microdata."""
import json
import unittest
from unittest import mock

from arb_engine.quant.microdata import build, contract_key, settlement_for, settlement_values
from arb_engine.store import Store
from arb_engine.venues.robinhood import RobinhoodAdapter

from .helpers import FakeHttp, load


def _row(t, mid, book, market, **extra):
    row = {"event_key": "nfl:A|B:2026-09-20", "venue": book, "book_id": book,
           "venue_market_id": market, "outcome": "A", "side": "yes", "obs_ts": float(t),
           "req_ts": float(t), "quote_time": float(t), "refreshed": 1, "in_play": True,
           "bid": mid - .01, "ask": mid + .01, "bid_size": 100, "ask_size": 100,
           "tie_payout": .5}
    row.update(extra)
    return row


class AtomicObservationTests(unittest.TestCase):
    def test_equal_time_cross_book_features_ignore_input_and_name_order(self):
        def history(book, market, final):
            return [_row(t, .40 if t < 30 else final, book, market) for t in range(31)]

        original = history("alpha", "A", .50) + history("omega", "O", .46)
        renamed = [dict(r, book_id={"alpha": "zeta", "omega": "beta"}[r["book_id"]],
                        venue={"alpha": "zeta", "omega": "beta"}[r["venue"]]) for r in reversed(original)]

        def snapshot(rows, names):
            with mock.patch("arb_engine.quant.microdata.settlement_relation", return_value="identical"):
                samples = [s for s in build(rows, sample="unconditional", horizons=(), fee_for_row=lambda r: None)
                           if s["t"] == 30]
            return {names[s["book_id"]]: {"leader": names[s["leader_book"]],
                    "gap": s["gap_leader"], "move": s["leader_dmid_30"]} for s in samples}

        first = snapshot(original, {"alpha": "alpha", "omega": "omega"})
        second = snapshot(renamed, {"zeta": "alpha", "beta": "omega"})
        self.assertEqual(first, second)
        self.assertEqual(set(first), {"alpha", "omega"})

    def test_future_append_still_changes_no_past_sample(self):
        rows = [_row(t, .40 + .001 * t, "alpha", "A") for t in range(41)]
        rows += [_row(t, .40 + .0005 * t, "omega", "O") for t in range(41)]
        future = [_row(41, .90, "alpha", "A"), _row(41, .10, "omega", "O")]
        with mock.patch("arb_engine.quant.microdata.settlement_relation", return_value="identical"):
            before = build(rows, sample="all", horizons=(), fee_for_row=lambda r: None)
            after = build(rows + future, sample="all", horizons=(), fee_for_row=lambda r: None)
        self.assertEqual(before, [s for s in after if s["t"] <= 40])


class SettlementIdentityTests(unittest.TestCase):
    @staticmethod
    def _robinhood_rows():
        props = load("robinhood_page_props_nfl.json")
        page = {"props": {"pageProps": props}}
        html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(page) + "</script>"
        quotes = {"status": "SUCCESS", "data": [{"status": "SUCCESS", "data": q} for q in props["quotes"].values()]}
        snap = RobinhoodAdapter(http=FakeHttp({"/us/en/prediction-markets/nfl/": html,
                                                "/marketdata/event/contract/quotes/v1/": quotes})).fetch("nfl", emit_no_side=True)
        event = "nfl:BUF|DET:2026-09-17"
        encoded = Store.l1_from_quotes({"robinhood": [q for q in snap.quotes if q.event_key == event]},
                                       req_ts=1, obs_ts=2)
        return event, [dict(r, event_key=event) for r in encoded["rows"]]

    def test_adapter_normalized_no_rows_settle_by_purchased_outcome(self):
        event, rows = self._robinhood_rows()
        values = settlement_values(rows, {event: ("BUF", "DET", "BUF")})
        for row in rows:
            expected = 1.0 if row["outcome"] == "BUF" else 0.0
            self.assertEqual(values[contract_key(row)], expected)
        no_det = next(r for r in rows if r.get("no_of") == "DET")
        self.assertEqual((no_det["side"], no_det["outcome"], values[contract_key(no_det)]), ("no", "BUF", 1.0))

    def test_adapter_tie_payouts_and_book_keys_do_not_overwrite(self):
        event, rows = self._robinhood_rows()
        rh_yes = next(r for r in rows if r["outcome"] == "BUF" and r["side"] == "yes")
        rh_no = next(r for r in rows if r["outcome"] == "BUF" and r["side"] == "no")
        kalshi = dict(rh_yes, venue="kalshi", book_id="kalshi", venue_market_id="K-BUF", tie_payout=.5)
        values = settlement_values([rh_yes, rh_no, kalshi], {event: ("BUF", "DET", None)})
        self.assertEqual(values[contract_key(rh_yes)], 0.0)
        self.assertEqual(values[contract_key(rh_no)], 1.0)
        self.assertEqual(values[contract_key(kalshi)], .5)
        self.assertNotIn((event, "BUF", "yes"), values)  # ambiguous across books: no unsafe alias
        self.assertEqual(settlement_for(values, contract_key(kalshi)), .5)

    def test_contradictory_no_identity_is_excluded(self):
        event, rows = self._robinhood_rows()
        bad = dict(next(r for r in rows if r.get("no_of") == "DET"), outcome="DET")
        values = settlement_values([bad], {event: ("BUF", "DET", "BUF")})
        self.assertNotIn(contract_key(bad), values)


if __name__ == "__main__":
    unittest.main()

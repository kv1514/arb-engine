"""Quote selection must not resurrect invalidated books or mixed game identity."""
import copy
import unittest

from arb_engine.quant.us_arbitrage import find_candidates
from tests.test_us_arbitrage import NOW, KEY, quote, snapshots


class QuoteSelectionAuditTests(unittest.TestCase):
    def report(self, *rows):
        return find_candidates(snapshots(*rows, quote('polymarket_us', 'NYJ', .5)), now=NOW)

    def test_new_failed_or_carried_receipt_masks_older_cheap_liquidity(self):
        for meta in ({'refreshed': False}, {'arb_ineligible': 'failed-response'}):
            with self.subTest(meta=meta):
                old = quote('kalshi', 'CHI', .4, ts=NOW-1)
                bad = quote('kalshi', 'CHI', .4, **meta)
                self.assertEqual(self.report(old, bad)['candidates'], [])

    def test_equal_receipt_conflicting_prices_or_sizes_are_not_cherry_picked(self):
        cheap = quote('kalshi', 'CHI', .4)
        for change in ({'ask': .45}, {'ask_size': 200}):
            changed = copy.deepcopy(cheap)
            for field, value in change.items():
                setattr(changed, field, value)
            for rows in ((cheap, changed), (changed, cheap)):
                with self.subTest(change=change, rows=rows):
                    self.assertEqual(self.report(*rows)['candidates'], [])

    def test_future_append_is_invisible(self):
        current = quote('kalshi', 'CHI', .4)
        future = quote('kalshi', 'CHI', .9, ts=NOW+1)
        self.assertEqual(self.report(current), self.report(current, future))

    def test_one_venue_reporting_in_play_prevents_pregame_candidate(self):
        data = snapshots(quote('kalshi', 'CHI', .4), quote('polymarket_us', 'NYJ', .5))
        data[1].events[KEY].in_play = True
        self.assertEqual(find_candidates(data, now=NOW)['candidates'], [])

    def test_conflicting_kickoff_identity_is_not_merged_as_verified_game(self):
        from datetime import timedelta
        data = snapshots(quote('kalshi', 'CHI', .4), quote('polymarket_us', 'NYJ', .5))
        data[1].events[KEY].start_time += timedelta(hours=1)
        for ordered in (data, list(reversed(data))):
            self.assertEqual(find_candidates(ordered, now=NOW)['candidates'], [])


if __name__ == '__main__':
    unittest.main()

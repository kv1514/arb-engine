"""Real captured descriptions expose incompatible exceptions, not arb proof."""
import json
import unittest
from pathlib import Path

from arb_engine.execution.settlement_audit import audit_nfl_terms


class SettlementAuditTests(unittest.TestCase):
    def setUp(self):
        fixtures = Path(__file__).parent/'fixtures'
        kal = (fixtures/'rules'/'kalshi_nfl_moneyline.txt').read_text()
        self.primary = kal.split('[rules_primary]\n', 1)[1].split('[rules_secondary]', 1)[0].strip()
        self.secondary = kal.split('[rules_secondary]\n', 1)[1]
        self.us = json.loads((fixtures/'polymarket_us'/'events_nfl_live_trimmed.json').read_text())['events'][0]['markets'][0]['description']

    def audit(self):
        return audit_nfl_terms(self.primary, self.secondary, self.us)

    def test_adapter_derived_terms_have_48_hour_vs_two_week_mismatch(self):
        r = self.audit()
        self.assertEqual(r['kalshi']['postponed_hours'], 48)
        self.assertEqual(r['polymarket_us']['postponed_hours'], 336)
        self.assertIn('mismatched-postponement-window', r['blockers'])

    def test_same_tie_payoff_does_not_approve_fair_price_exceptions(self):
        r = self.audit()
        self.assertEqual(r['kalshi']['tie'], r['polymarket_us']['tie'])
        self.assertIn('independent-discretionary-exception-payoffs', r['blockers'])
        self.assertFalse(r['guaranteed_payoff_verified'])

    def test_same_fair_price_words_still_do_not_prove_same_payout(self):
        r = audit_nfl_terms(self.primary, self.secondary, self.secondary)
        self.assertFalse(r['guaranteed_payoff_verified'])
        self.assertIn('independent-discretionary-exception-payoffs', r['blockers'])

    def test_integrity_hash_changes_when_product_text_changes(self):
        a = self.audit()['evidence_hashes']
        self.us += ' Additional rules apply.'
        b = self.audit()['evidence_hashes']
        self.assertEqual(a['kalshi_primary'], b['kalshi_primary'])
        self.assertNotEqual(a['us_description'], b['us_description'])

    def test_missing_and_unknown_clauses_are_never_assumed(self):
        r = audit_nfl_terms('A wins.', 'Other rules apply.', 'A wins.')
        self.assertIn('unknown-tie', r['blockers'])
        self.assertIn('unknown-postponement-window', r['blockers'])
        self.assertEqual(r['status'], 'BLOCKED')

    def test_empty_text_refused(self):
        with self.assertRaises(ValueError):
            audit_nfl_terms(self.primary, '', self.us)

    def test_postponement_fair_price_never_invents_a_cancellation_clause(self):
        r = self.audit()['polymarket_us']
        self.assertEqual(r['exception_payoff'], 'independent_fair_price')
        self.assertIsNone(r['cancelled'])

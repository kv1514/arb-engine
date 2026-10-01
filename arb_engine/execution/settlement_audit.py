"""Explain NFL product-rule gaps; text extraction is NOT settlement approval.

The same phrase 'fair price' on independent exchanges is not a common payout.
This diagnostic intentionally cannot mark descriptions or FAQs binding/verified.
No registry row is promoted and no discretionary payoff is assigned a number.
"""
from __future__ import annotations

import hashlib
import re

from ..matching.settlement_rules import parse_kalshi


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('nonempty venue rule text required')
    return value


def audit_nfl_terms(kalshi_primary, kalshi_secondary, us_description):
    """Audit captured product-family text, not proof of a specific matched game.

    Report extracted fields, hashes and explicit blockers. Even matching parsed
    clauses require binding full terms, exact game/contract identity and source
    verification; regex extraction never supplies a production allow boolean.
    """
    primary, secondary, us = map(_text, (kalshi_primary, kalshi_secondary, us_description))
    kal = parse_kalshi(primary, secondary)
    tie = bool(re.search(r'\btie\b[^.]*\$0\.50', us, re.I))
    fair = bool(re.search(r'last fair market price|fair price', us, re.I))
    postponed = re.search(r'within\s+(two|fourteen|14|2)\s+(weeks?|days?)', us, re.I)
    hours = None
    if postponed:
        number = {'two': 2, 'fourteen': 14}.get(postponed[1].lower())
        number = int(postponed[1]) if number is None else number
        hours = number*24*(7 if postponed[2].lower().startswith('week') else 1)
    cancelled = bool(re.search(r'cancel\w*[^.]*fair (?:market )?price', us, re.I))
    us_fields = {'tie': 'half' if tie else None, 'postponed_hours': hours,
                 'cancelled': 'independent_fair_price' if cancelled else None,
                 'exception_payoff': 'independent_fair_price' if fair else None,
                 'ot_included': True if re.search(r'overtime is included|overtime.*included', us, re.I) else None}
    blockers = ['binding-market-specific-terms-not-verified', 'exact-cross-venue-contract-identity-not-verified']
    for field in ('tie', 'ot_included'):
        if kal[field] is None or us_fields[field] is None:
            blockers.append('unknown-'+field)
        elif kal[field] != us_fields[field]:
            blockers.append('mismatched-'+field)
    kh = re.fullmatch(r'open_(\d+)h', kal['postponed'] or '')
    kal_hours = int(kh[1]) if kh else None
    if kal_hours is None or hours is None:
        blockers.append('unknown-postponement-window')
    elif kal_hours != hours:
        blockers.append('mismatched-postponement-window')
    if kal['cancelled'] == 'fair_price' or fair:
        blockers.append('independent-discretionary-exception-payoffs')
    else:
        blockers.append('exception-payoffs-not-proven-complementary')
    hashes = [hashlib.sha256(t.encode()).hexdigest() for t in (primary, secondary, us)]
    return {'status': 'BLOCKED', 'guaranteed_payoff_verified': False,
            'kalshi': {**kal, 'postponed_hours': kal_hours}, 'polymarket_us': us_fields,
            'evidence_hashes': {'kalshi_primary': hashes[0], 'kalshi_secondary': hashes[1], 'us_description': hashes[2]},
            'blockers': sorted(set(blockers)),
            'note': 'captured text diagnostic only; hashes prove integrity, not legal interpretation or atomic fills'}

#!/usr/bin/env python3
"""Offline product-family rule diagnostic. Never authorizes orders or writes feeds."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from arb_engine.execution.settlement_audit import audit_nfl_terms


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--kalshi-text', type=Path, default=ROOT/'tests/fixtures/rules/kalshi_nfl_moneyline.txt')
    p.add_argument('--us-events', type=Path, default=ROOT/'tests/fixtures/polymarket_us/events_nfl_live_trimmed.json')
    args = p.parse_args(argv)
    text = args.kalshi_text.read_text(encoding='utf-8')
    primary = text.split('[rules_primary]\n', 1)[1].split('[rules_secondary]', 1)[0].strip()
    secondary = text.split('[rules_secondary]\n', 1)[1]
    markets = [m for event in json.loads(args.us_events.read_text(encoding='utf-8'))['events'] for m in event['markets']]
    if len(markets) != 1:
        raise ValueError('one captured US market required; do not select an arbitrary market')
    result = audit_nfl_terms(primary, secondary, markets[0]['description'])
    result['scope'] = 'captured product-family examples, not a matched-game settlement certification'
    print(json.dumps(result, sort_keys=True))
    return 3 if not result['guaranteed_payoff_verified'] else 0


if __name__ == '__main__':
    raise SystemExit(main())

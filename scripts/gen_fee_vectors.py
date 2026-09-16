"""Regenerate tests/fixtures/fee_vectors.json from the Python fee models.

The extension's arb-core.js is checked against this file by scripts/test_js.sh, so run it
whenever a fee model changes.
"""

from __future__ import annotations

import itertools
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb_engine.fees import KalshiFees, PolymarketFees, PolymarketUSFees, RobinhoodFees  # noqa: E402

PRICES = [0.01, 0.02, 0.05, 0.07, 0.10, 0.13, 0.15, 0.20, 0.25, 0.30, 0.33, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 0.99, 0.4850, 0.5150]
SIZES = [1, 2, 3, 7, 10, 25, 50, 100, 250, 1000]


def main() -> None:
    vectors = []
    for p, c in itertools.product(PRICES, SIZES):
        k = KalshiFees.from_series({"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1})
        vectors.append({"venue": "kalshi", "price": p, "contracts": c, "role": "taker", "params": {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}, "fee": float(k.fee(p, c))})
        vectors.append({"venue": "kalshi", "price": p, "contracts": c, "role": "maker", "params": {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}, "fee": float(k.fee(p, c, "maker"))})
        h = KalshiFees.from_series({"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 0.5})
        vectors.append({"venue": "kalshi", "price": p, "contracts": c, "role": "taker", "params": {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 0.5}, "fee": float(h.fee(p, c))})
        for gold in (False, True):
            r = RobinhoodFees(gold=gold, exchange="rothera")
            vectors.append({"venue": "robinhood", "price": p, "contracts": c, "role": "taker", "settings": {"gold": gold}, "params": {"exchange": "rothera"}, "fee": float(r.fee(p, c))})
        pm = PolymarketFees.from_market({"feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True, "rebateRate": 0.15}, "feesEnabled": True})
        vectors.append({"venue": "polymarket", "price": p, "contracts": c, "role": "taker", "params": {"feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True}, "feesEnabled": True}, "fee": float(pm.fee(p, c))})
        pu = PolymarketUSFees(taker_theta=Decimal("0.0695"))
        vectors.append({"venue": "polymarket_us", "price": p, "contracts": c, "role": "taker", "params": {"takerTheta": 0.0695}, "fee": float(pu.fee(p, c))})
        vectors.append({"venue": "polymarket_us", "price": p, "contracts": c, "role": "maker", "params": {"takerTheta": 0.0695}, "fee": float(pu.fee(p, c, "maker"))})
    out = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "fee_vectors.json"
    out.write_text(json.dumps(vectors, indent=0))
    print(f"wrote {len(vectors)} vectors to {out}")


if __name__ == "__main__":
    main()

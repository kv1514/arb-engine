"""Regenerate tests/fixtures/fee_vectors.json from the Python fee models.

The extension's arb-core.js is checked against this file by scripts/test_js.sh, and
tests/test_fees.py replays every row through ``fee_model_for`` so Python, JS and the
registry's settings plumbing agree. Run it whenever a fee model changes.

Row shape: ``{venue, price, contracts, role, params, settings?, fee}`` where ``params`` is
the quote's ``fee_params`` (what ``fee_model_for`` receives) and ``settings`` the user-level
knobs in the extension's camelCase (``gold``, ``rotheraFeeModel``, ``cdnaFeeModel``);
``model_from_vector`` in tests/test_fees.py maps them back to the Python settings keys.

Coverage: Kalshi taker/maker at multiplier 1 and taker at 0.5; Robinhood x Rothera under
``flat_001`` and ``quadratic`` (Gold and not; plus the schedule's own k=0.06 example at
$0.35); Robinhood x CDNA under all three models; Polymarket sports; Polymarket US taker and
maker (the 1,000 @ $0.50 worked example is the row at that price/size).
"""

from __future__ import annotations

import itertools
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb_engine.fees import KalshiFees, PolymarketFees, PolymarketUSFees, RobinhoodFees  # noqa: E402
from arb_engine.fees.robinhood import CDNA_FEE_MODELS  # noqa: E402

PRICES = [0.01, 0.02, 0.05, 0.07, 0.10, 0.13, 0.15, 0.20, 0.25, 0.30, 0.33, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 0.97, 0.99, 0.4850, 0.5150]
SIZES = [1, 2, 3, 7, 10, 25, 50, 100, 250, 1000]


def build_vectors() -> list[dict]:
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
            rq = RobinhoodFees(gold=gold, exchange="rothera", rothera_fee_model="quadratic")
            vectors.append({"venue": "robinhood", "price": p, "contracts": c, "role": "taker", "settings": {"gold": gold, "rotheraFeeModel": "quadratic"}, "params": {"exchange": "rothera"}, "fee": float(rq.fee(p, c))})
        if p == 0.35:
            # Rothera Fee Schedule 20260520's own example uses k = 0.06 ($1.37 on 100 @ $0.35).
            rk = RobinhoodFees(gold=False, exchange="rothera", rothera_fee_model="quadratic", rothera_k=Decimal("0.06"))
            vectors.append({"venue": "robinhood", "price": p, "contracts": c, "role": "taker", "settings": {"gold": False, "rotheraFeeModel": "quadratic"}, "params": {"exchange": "rothera", "rothera_k": 0.06}, "fee": float(rk.fee(p, c))})
        for model in CDNA_FEE_MODELS:
            cd = RobinhoodFees(gold=False, exchange="cdna", cdna_fee_model=model)
            vectors.append({"venue": "robinhood", "price": p, "contracts": c, "role": "taker", "settings": {"gold": False, "cdnaFeeModel": model}, "params": {"exchange": "cdna"}, "fee": float(cd.fee(p, c))})
        pm = PolymarketFees.from_market({"feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True, "rebateRate": 0.15}, "feesEnabled": True})
        vectors.append({"venue": "polymarket", "price": p, "contracts": c, "role": "taker", "params": {"feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True}, "feesEnabled": True}, "fee": float(pm.fee(p, c))})
        pu = PolymarketUSFees(taker_theta=Decimal("0.0695"))
        vectors.append({"venue": "polymarket_us", "price": p, "contracts": c, "role": "taker", "params": {"takerTheta": 0.0695}, "fee": float(pu.fee(p, c))})
        vectors.append({"venue": "polymarket_us", "price": p, "contracts": c, "role": "maker", "params": {"takerTheta": 0.0695}, "fee": float(pu.fee(p, c, "maker"))})
    return vectors


def main() -> None:
    vectors = build_vectors()
    out = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "fee_vectors.json"
    out.write_text(json.dumps(vectors, indent=0))
    print(f"wrote {len(vectors)} vectors to {out}")


if __name__ == "__main__":
    main()

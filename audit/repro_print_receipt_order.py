#!/usr/bin/env python3
"""``microdata._Prints`` ordered prints that share a receipt time by the order they arrived in.

One ``/markets/trades`` response delivers many prints with one local ``obs_ts``.  ``_Prints``
sorted them with ``sorted(v, key=lambda x: x[0])`` - a stable sort on the receipt time alone -
so inside that instant the input order survived, and ``last_print_minus_mid`` (the sample's
"last print against the mid") took whichever print the page happened to list last.
``load_db`` selects ``trade_prints`` with no ``ORDER BY``, so that order is the database's,
not the tape's.

The flow sums are order-free; only the last-print feature flips.  No rule, metric or report
field reads it, so no published number moves - but the sample column was not reproducible.

Synthetic data only; no database, no network, no order.

    python3 audit/repro_print_receipt_order.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arb_engine.quant.microdata import _Prints                        # noqa: E402

PAGE = [{"ticker": "K", "ts": 10.0, "price": .60, "count": 5, "taker_side": "yes", "obs_ts": 20.0, "trade_id": "a"},
        {"ticker": "K", "ts": 11.0, "price": .40, "count": 5, "taker_side": "no", "obs_ts": 20.0, "trade_id": "b"}]


def main() -> int:
    print(__doc__.splitlines()[0])
    print()
    forward = _Prints(PAGE).at("K", 25.0, "yes", .5)
    backward = _Prints(list(reversed(PAGE))).at("K", 25.0, "yes", .5)
    print(f"  page listed a, b : last_print_minus_mid = {forward['last_print_minus_mid']:+.3f}   flow_30 = {forward['flow_30']}")
    print(f"  page listed b, a : last_print_minus_mid = {backward['last_print_minus_mid']:+.3f}   flow_30 = {backward['flow_30']}")
    print()
    print("EXPECTED: one response is one instant; its prints are ordered by their own content")
    print("          (trade_id first), so the feature is the same however the page listed them.")
    print(f"OBSERVED: identical? {forward == backward}")
    if forward != backward:
        print("          -> the feature flips sign with the arrival order. REPRODUCED.")
        return 1
    print("          -> the order inside a receipt instant is now content-defined.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

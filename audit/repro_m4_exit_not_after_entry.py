#!/usr/bin/env python3
"""M4 (paperexec half): ``ioc_round_trip`` can sell a position into the very observation
that filled it - a quote that was already on the screen when the order was sent.

The entry meets the book in ``[decision + latency, decision + latency + entry_tol]``; the
exit is looked for in ``[decision + latency + horizon, ... + max(1, .2 horizon)]``.  Those
two windows OVERLAP whenever ``horizon_s <= entry_tol_s``, and nothing in the exit search
requires the mark to be observed strictly after the fill.  When the only observation in the
arrival window sits at ``decision + latency + horizon``, the same row buys at its ask and
sells at its bid: a free round trip across the spread out of one snapshot, with the paper
book never having existed between the two.

The same hole makes the ROLL candidates and the settlement decision hang off a mark that is
not known to be later than the fill.

No caller in ``microdata.build`` / ``ExecCtx`` reaches it today (their horizons are 5 s and
up against a 2 s entry tolerance), so this is a latent optimism in a public function, not a
number that has already been published.  ``two_leg_arb``'s unwind is not affected: it starts
from ``max(known_a, known_b) + unwind_latency``.

Synthetic data only; no database, no network, no order.

    python3 audit/repro_m4_exit_not_after_entry.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arb_engine.fees.base import ZeroFees                       # noqa: E402
from arb_engine.quant.paperexec import ioc_round_trip           # noqa: E402


def main() -> int:
    print(__doc__.splitlines()[0])
    print()
    # One observation only, at t = 3.0.  Decision at t = 0, latency 1 s, entry tolerance 5 s
    # (window [1, 6]) and a 2 s horizon (exit window [3, 4]).
    rows = [{"obs_ts": 3.0, "refreshed": 1, "bid": .70, "ask": .50, "bid_size": 100, "ask_size": 100,
             "book_id": "kalshi", "venue_market_id": "K-A-yes", "side": "yes"}]
    tr = ioc_round_trip(rows, decision_ts=0.0, limit=.50, order=10, fee_model=ZeroFees(),
                        latency_s=1.0, horizon_s=2.0, entry_tol_s=5.0)
    print(f"  rows                 : one observation at t=3.0, ask .50 / bid .70")
    print(f"  entry arrival window : [1.0, 6.0]")
    print(f"  exit  horizon window : [3.0, 4.0]")
    print(f"  filled               : {tr.filled} @ {tr.entry_price}")
    print(f"  exits                : {[(t, p, n) for t, p, n, _ in tr.exits]}")
    print(f"  pnl per contract     : {tr.pnl_per_contract}")
    print()
    print("EXPECTED: the exit must meet the book strictly AFTER the fill.  With no later")
    print("          observation the position is unresolved (pnl None), not closed at a bid")
    print("          that was on the screen at the moment the order was sent.")
    same_row = bool(tr.exits) and tr.exits[0][0] == 3.0 and tr.filled
    print(f"OBSERVED: sold into the filling observation? {bool(same_row)}")
    if same_row:
        print("          -> a 20c round trip out of one snapshot. M4 (paperexec) REPRODUCED.")
        return 1
    print("          -> the exit is now strictly after the fill.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

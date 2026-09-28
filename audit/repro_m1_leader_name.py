#!/usr/bin/env python3
"""M1: the H3 leader of an instant is picked by BOOK NAME when two books tie on |dmid_30|.

``microdata.build`` chooses the leader with

    max(sorted(cross.items()), key=lambda kv: abs(kv[1]["dmid_30"] or 0))

``max`` keeps the FIRST maximal element of its input, and the input was sorted by book name,
so an exact tie in |dmid_30| is broken alphabetically.  Two independent books that moved
equally far in OPPOSITE directions therefore hand the follower whichever signal the
alphabetically smaller book carries: renaming the books flips the trade from "buy the
follower" to "buy the follower's complement", with no economic change whatsoever.

Synthetic data only; no database, no network, no order.

    python3 audit/repro_m1_leader_name.py
"""
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EV = "nfl:A|B:2026-09-27"
FOLLOWER = "rothera"          # the lagging book (Robinhood / Rothera), venue "robinhood"


def row(t, book, outcome, mid, venue=None, side="yes", tie=0.5, size=100):
    return {"event_key": EV, "obs_ts": float(t), "req_ts": float(t), "quote_time": float(t), "refreshed": 1,
            "in_play": True, "source": "fast", "venue": venue or book, "book_id": book,
            "venue_market_id": f"{book}-{outcome}-{side}", "outcome": outcome, "side": side,
            "bid": round(mid - .01, 3), "ask": round(mid + .01, 3), "bid_size": size, "ask_size": size,
            "tie_payout": tie}


def rows(up_book, down_book):
    """60 s of 1 s observations.

    The follower sits still at .50.  ``up_book`` rises .47 -> .53 over the 30 s before t=40,
    ``down_book`` falls .53 -> .47 over exactly the same 30 s.  Both leaders therefore have
    |dmid_30| = .06 at t=40 and gaps of +.03 / -.03 against the follower - an exact economic
    tie in the sort key, and a flat contradiction in what they say to do.
    """
    out = []
    for t in range(0, 61):
        step = min(max(t - 10, 0), 30) / 30.0        # 0 before t=10, 1 from t=40
        out.append(row(t, FOLLOWER, "A", .50, venue="robinhood"))
        out.append(row(t, FOLLOWER, "B", .50, venue="robinhood", tie=0.5))
        out.append(row(t, up_book, "A", .47 + .06 * step, venue="kalshi"))
        out.append(row(t, down_book, "A", .53 - .06 * step, venue="polymarket"))
    return out


def decision(up_book, down_book):
    from arb_engine.quant.microdata import build
    from scripts.microstructure_eval import _h3_direction, select_trades

    with mock.patch("arb_engine.quant.microdata.settlement_relation", return_value="identical"):
        samples = build(rows(up_book, down_book), sample="unconditional", horizons=(), fee_for_row=lambda r: None)
    at40 = [s for s in samples if s["t"] == 40.0 and s["book_id"] == FOLLOWER and s["outcome"] == "A"]
    lead = at40[0] if at40 else None
    trades, _ = select_trades(samples, _h3_direction, 60.0)
    mine = [(s["t"], d, tuple(bc["key"])) for s, d, bc in trades if s["book_id"] == FOLLOWER and s["outcome"] == "A"]
    return lead, mine


def main() -> int:
    print(__doc__.splitlines()[0])
    print()
    out = {}
    for label, (up, down) in (("alpha rises, zulu falls", ("alpha", "zulu")),
                              ("zulu rises, alpha falls", ("zulu", "alpha"))):
        lead, trades = decision(up, down)
        out[label] = (lead, trades)
        print(f"--- {label} ---")
        num = lambda x: "None" if x is None else f"{x:+.3f}"   # noqa: E731
        print(f"  leader_book      = {lead['leader_book']!r}")
        print(f"  leader_dmid_30   = {num(lead['leader_dmid_30'])}")
        print(f"  gap_leader       = {num(lead['gap_leader'])}")
        print(f"  H3 trades        = {trades}")
        print()
    a, b = out["alpha rises, zulu falls"], out["zulu rises, alpha falls"]
    same = [(d, k) for _, d, k in a[1]] == [(d, k) for _, d, k in b[1]]
    print("EXPECTED: the two runs are economically identical (one book up 6c, one book down 6c,")
    print("          the follower flat), so the H3 decision must be the same in both - or absent")
    print("          in both, because the two leaders contradict each other.")
    print(f"OBSERVED: identical decisions? {same}")
    if not same:
        print("          -> renaming the books flipped the trade. M1 REPRODUCED.")
        return 1
    print("          -> the leader no longer depends on the books' names.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

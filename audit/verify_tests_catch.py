"""Prove each new regression test really catches its defect: revert one fix at a time in a
scratch copy of the tree and show the test that covers it fails there.

    python3 -B audit/verify_tests_catch.py

The scratch copy lives under $TMPDIR; this tree is never modified.  No network, no orders.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (name, file, fixed text -> the text before the fix, tests that must fail without it)
REVERTS = [
    ("H6 unfiltered listing before a release", "arb_engine/execution/ledger.py",
     """            rows, truncated = list_orders(client, ticker=r["ticker"], min_ts=int(float(r["created_ts"])) - 60)
            order = find(rows)
            if order is None and not truncated:""",
     """            rows, truncated = list_orders(client, ticker=r["ticker"], min_ts=int(float(r["created_ts"])) - 60)
            order = find(rows)
            if False:""",
     ["tests.test_ledger_hardening.ClockSkewTests.test_a_local_clock_two_minutes_ahead_does_not_release_a_filled_order"]),

    ("H10 a missing fill count is unknown", "arb_engine/strategy/broker.py",
     """            filled = _count(od, "fill_count")
            if filled is None:
                remaining = _count(od, "remaining_count")
                filled = (o.count - remaining) if remaining is not None else None""",
     """            filled = _fp(od.get("fill_count_fp") or od.get("fill_count") or 0.0)
            if filled is None:
                remaining = _fp(od.get("remaining_count_fp") or od.get("remaining_count"))
                filled = (o.count - remaining) if remaining is not None else 0.0""",
     ["tests.test_ledger_hardening.MakerPollTests.test_a_row_without_a_fill_count_does_not_report_zero_fills"]),

    ("H11 the fill cost bounds", "arb_engine/execution/ledger.py",
     """        problem = self._cost_problem(r, count, cost)""",
     """        problem = None""",
     ["tests.test_ledger_hardening.FillCostBoundTests.test_a_cost_below_a_cent_a_contract_releases_nothing",
      "tests.test_ledger_hardening.FillCostBoundTests.test_a_cost_above_the_limit_contradicts_the_row"]),

    ("H11 the fills listing's own cost", "arb_engine/execution/ledger.py",
     """        if fills.cost is not None and count > 0:""",
     """        if False:""",
     ["tests.test_ledger_hardening.FillCostBoundTests.test_the_fills_listing_own_prices_keep_the_larger_cost"]),

    ("H13 a sell is short 1 - limit", "arb_engine/execution/ledger.py",
     """        if "action" in row.keys() and str(row["action"] or "buy").lower() == "sell":
            return Decimal(1) - limit
        return limit""",
     """        return limit""",
     ["tests.test_ledger_hardening.SellExposureTests.test_a_partly_filled_sell_counts_its_short_side",
      "tests.test_ledger_hardening.SellExposureTests.test_a_finished_sell_is_bounded_by_what_it_is_short"]),

    ("H13 a sell's reservation", "arb_engine/execution/ledger.py",
     """            exposed = (Decimal(1) - limit) if str(action).lower() == "sell" else limit""",
     """            exposed = limit""",
     ["tests.test_ledger_hardening.SellExposureTests.test_a_sell_reserved_without_a_per_contract_cap_is_still_sized_to_its_short_side"]),

    ("H14 the daily cap counts what is still open", "arb_engine/execution/ledger.py",
     """                    rooms.append(D(budget.daily) - self._sum(c, "strategy = ? AND (day = ? OR state IN ('pending', 'ambiguous', 'accepted'))",
                                                             (strategy, day)))""",
     """                    rooms.append(D(budget.daily) - self._sum(c, "strategy = ? AND day = ?", (strategy, day)))""",
     ["tests.test_ledger_hardening.DailyBudgetTests.test_an_order_open_from_yesterday_still_counts_today"]),

    ("H16 de-duplicating on the trade id too", "arb_engine/execution/ledger.py",
     """        prior = (by_fill.get(fid) if fid else None) or (by_trade.get(tid) if tid else None)""",
     """        prior = by_fill.get(fid) if fid else by_trade.get(tid)""",
     ["tests.test_ledger_hardening.TradeIdDedupeTests.test_one_trade_under_two_fill_ids_counts_once",
      "tests.test_ledger_hardening.TradeIdDedupeTests.test_a_duplicated_trade_does_not_contradict_the_order"]),

    ("s15 the position cap is this entry's share", "arb_engine/strategy/lagexec.py",
     """            others = self.ledger.contracts_committed_elsewhere(parent_id)""",
     """            others = Decimal(0)""",
     ["tests.test_ledger_hardening.LockPositionCapTests.test_a_position_short_of_both_entries_hedges_neither_blindly"]),

    ("s17 another entry's hedge is not an exit", "arb_engine/execution/ledger.py",
     """               AND ((action = 'sell' AND side = ?) OR (action = 'buy' AND side != ? AND parent_id IS NULL))\"\"\",
            (parent["ticker"], parent_id, parent["created_ts"], parent["side"], parent["side"])).fetchall()""",
     """               AND ((action = 'sell' AND side = ?) OR (action = 'buy' AND side != ? AND COALESCE(parent_id, '') != ?))\"\"\",
            (parent["ticker"], parent_id, parent["created_ts"], parent["side"], parent["side"], parent_id)).fetchall()""",
     ["tests.test_ledger_hardening.LockExitsTests.test_each_entry_hedges_its_own_contracts_on_the_same_ticker"]),

    ("reserve refuses instead of raising", "arb_engine/execution/ledger.py",
     """        except (TypeError, ValueError, OverflowError, InvalidOperation):
            return Reservation(False, "count/limit not numeric")
        if count <= 0:
            return Reservation(False, "count must be positive")
        if count > MAX_CONTRACTS:
            return Reservation(False, f"count {count} above the {MAX_CONTRACTS} contracts one intent may hold")""",
     """        except (TypeError, ValueError, InvalidOperation):
            return Reservation(False, "count/limit not numeric")
        if count <= 0:
            return Reservation(False, "count must be positive")""",
     ["tests.test_ledger_hardening.FailOpenTests.test_reserve_refuses_an_infinite_count_instead_of_raising"]),

    ("rejected never releases what filled", "arb_engine/execution/ledger.py",
     """            seen = _dec(cur["fill_seen"])
            if seen is not None and seen > 0:""",
     """            seen = _dec(cur["fill_seen"])
            if False:""",
     ["tests.test_ledger_hardening.FailOpenTests.test_rejected_never_releases_an_intent_that_showed_fills"]),

    ("done() needs an acknowledged order", "arb_engine/execution/ledger.py",
     """            if row is None or row["state"] not in OPEN or not row["order_id"]:""",
     """            if row is None or row["state"] not in OPEN:""",
     ["tests.test_ledger_hardening.FailOpenTests.test_done_refuses_an_intent_the_exchange_never_acknowledged"]),

    ("list_orders reports a truncated fallback", "arb_engine/execution/ledger.py",
     """    rows = list(client.orders_v2(**params) or [])
    return rows, bool(getattr(client, "last_truncated", False))""",
     """    return list(client.orders_v2(**params) or []), False""",
     ["tests.test_ledger_hardening.FailOpenTests.test_list_orders_reports_a_non_paging_client_s_truncation"]),
]


def main() -> int:
    scratch = tempfile.mkdtemp(prefix="audit_revert_")
    tree = os.path.join(scratch, "repo")
    shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns(".git", "out", "__pycache__", "*.sqlite3"))
    bad = 0
    for name, rel, fixed, before, tests in REVERTS:
        path = os.path.join(tree, rel)
        with open(path, encoding="utf-8") as f:
            original = f.read()
        if original.count(fixed) != 1:
            print(f"  [SKIP]  {name}: the fixed text appears {original.count(fixed)} times in {rel}")
            bad += 1
            continue
        with open(path, "w", encoding="utf-8") as f:
            f.write(original.replace(fixed, before))
        try:
            proc = subprocess.run([sys.executable, "-B", "-m", "unittest", *tests],
                                  cwd=tree, capture_output=True, text=True)
            failed = proc.returncode != 0
            tail = [l for l in proc.stderr.splitlines() if l.startswith(("FAIL:", "ERROR:", "AssertionError", "OK", "FAILED"))]
            print(f"  [{'CAUGHT' if failed else 'MISSED'}] {name}\n           {' | '.join(tail[:3]) or proc.stderr.strip().splitlines()[-1:]}")
            bad += not failed
        finally:
            with open(path, "w", encoding="utf-8") as f:
                f.write(original)
    print(f"\n{len(REVERTS) - bad}/{len(REVERTS)} reverted fixes are caught by a test")
    shutil.rmtree(scratch, ignore_errors=True)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

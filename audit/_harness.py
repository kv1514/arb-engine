"""Shared offline harness for the execution-ledger audit.

Everything here is offline: fake HTTP clients, temporary SQLite ledgers, injected clocks.
No network call, no order, no credential is ever touched.  Run any script in this directory
from the repo root:  ``python3 -B audit/<script>.py``
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ARB_ORDER_LEDGER_DIR", tempfile.mkdtemp(prefix="audit_ledgers_"))

from tests.test_order_ledger import (DEMO_URL, KEY, TICKER, Clock, FakeKalshi, Fills,  # noqa: E402
                                     executor, fill_row, maker, order_row, quotes, sig, tmp)

DEN = TICKER.replace("-KC", "-DEN")

_results: list[tuple[str, bool, str]] = []


def check(name: str, holds: bool, note: str = "") -> bool:
    """Record one audited claim.  ``holds`` = the safety property is enforced."""
    _results.append((name, holds, note))
    print(f"  [{'HOLDS' if holds else 'FAILS'}] {name}" + (f"\n          {note}" if note else ""))
    return holds


def report(title: str) -> int:
    bad = [r for r in _results if not r[1]]
    print(f"\n{title}: {len(_results) - len(bad)}/{len(_results)} hold")
    for name, _, note in bad:
        print(f"  DEFECT: {name} -- {note}")
    return 1 if bad else 0

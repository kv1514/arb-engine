"""Test package. Every order-ledger path a test does not name lands in a throw-away directory:
nothing a test runs may write the real out/orders ledgers (AGENTS.md rule 3a)."""
import os as _os
import tempfile as _tempfile

_os.environ.setdefault("ARB_ORDER_LEDGER_DIR", _tempfile.mkdtemp(prefix="arb_test_ledgers_"))

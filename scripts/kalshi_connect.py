#!/usr/bin/env python3
"""Is my Kalshi account connected? A read-only check, safe on demo *and* production.

    python3 scripts/kalshi_connect.py              # uses KALSHI_ENV (default demo)
    python3 scripts/kalshi_connect.py --env prod   # check a production key

It reads ``KALSHI_API_KEY`` (the key ID Kalshi shows next to the key) and
``KALSHI_PRIVATE_KEY_PATH`` (the private-key file Kalshi downloads once, when the key is
created), then walks the connection one step at a time and stops at the first that fails,
saying what to fix:

1. both variables are set                        (values are never printed)
2. the key file exists, is readable only by you  (0600), and parses as a private key
3. an unsigned GET /exchange/status answers       (the host is reachable)
4. a signed GET /portfolio/balance answers        (Kalshi accepts this key on this host)

Nothing here places, changes or cancels anything: the only authenticated call is the
balance read. A demo key only works on the demo host and a production key only on
production — a 401 at step 4 is almost always that mismatch or a key-ID / file mismatch.
``~/.kalshi/env`` (two ``export`` lines) is read first when it exists, so the launcher, this
script and your shell can share one place; see docs/RUNBOOK.md "Connect your Kalshi account".
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arb_engine.venues.http import HttpError  # noqa: E402
from arb_engine.venues.kalshi import KalshiClient  # noqa: E402

ENV_FILE = Path("~/.kalshi/env").expanduser()
OK, FAIL, WARN = "PASS", "FAIL", "WARN"


def load_env_file(path: Path = ENV_FILE, environ: dict = os.environ) -> list[str]:
    """``export KEY=value`` / ``KEY=value`` lines from ``path`` into ``environ`` (an already
    exported variable wins). Returns the names it set; never the values."""
    if not path.is_file():
        return []
    set_names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if sep and name.startswith("KALSHI_") and name not in environ:
            environ[name] = os.path.expanduser(value)
            set_names.append(name)
    return set_names


def check(env: str, api_key: str | None, key_path: str | None, http=None) -> list[tuple[str, str, str]]:
    """The four steps as (status, step, detail); stops after the first FAIL."""
    out: list[tuple[str, str, str]] = []
    missing = [n for n, v in (("KALSHI_API_KEY", api_key), ("KALSHI_PRIVATE_KEY_PATH", key_path)) if not v]
    if missing:
        out.append((FAIL, "variables", f"{' and '.join(missing)} not set (export them, or put them in {ENV_FILE})"))
        return out
    out.append((OK, "variables", f"KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH set; env={env}"))

    p = Path(os.path.expanduser(key_path))
    if not p.is_file():
        out.append((FAIL, "key file", f"{p} does not exist (Kalshi downloads it once, when the key is created)"))
        return out
    mode = stat.S_IMODE(p.stat().st_mode)
    try:
        from cryptography.hazmat.primitives import serialization

        serialization.load_pem_private_key(p.read_bytes(), password=None)
    except ImportError:
        out.append((FAIL, "key file", "python3 -m pip install cryptography  (needed to sign Kalshi requests)"))
        return out
    except Exception as e:  # noqa: BLE001 - any parse error means the wrong file
        out.append((FAIL, "key file", f"{p} is not an unencrypted PEM private key ({type(e).__name__}); use the file Kalshi downloaded"))
        return out
    if mode & 0o077:
        out.append((WARN, "key file", f"{p} is readable by others (mode {mode:o}); run: chmod 600 {p}"))
    else:
        out.append((OK, "key file", f"{p} parses as a private key, mode {mode:o}"))

    client = KalshiClient(env=env, api_key=api_key, private_key_path=str(p), http=http)
    try:
        st = client.get("/exchange/status")
        active = st.get("trading_active") if isinstance(st, dict) else None
        out.append((OK, "host", f"{client.base_url} answers (trading_active={active})"))
    except Exception as e:  # noqa: BLE001
        out.append((FAIL, "host", f"{client.base_url}: {e}"))
        return out

    try:
        bal = client.balance()
    except HttpError as e:
        hint = {401: f"Kalshi rejected the signature: a {'production' if env == 'demo' else 'demo'} key on the {env} host, or a key ID that does not belong to this file",
                403: "the key has no portfolio permission"}.get(e.status, str(e))
        out.append((FAIL, "signed read", f"HTTP {e.status}: {hint}"))
        return out
    except Exception as e:  # noqa: BLE001
        out.append((FAIL, "signed read", str(e)))
        return out
    cents = bal.get("balance") if isinstance(bal, dict) else None
    value = bal.get("portfolio_value") if isinstance(bal, dict) else None
    detail = f"cash ${cents / 100:,.2f}" if isinstance(cents, (int, float)) else f"balance {bal!r}"
    if isinstance(value, (int, float)):
        detail += f", positions ${value / 100:,.2f}"
    out.append((OK, "signed read", f"connected: {detail}"))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only Kalshi connection check (never places or cancels orders).")
    ap.add_argument("--env", choices=("demo", "prod"), help="host to check (default: KALSHI_ENV, else demo)")
    ap.add_argument("--no-env-file", action="store_true", help=f"do not read {ENV_FILE}")
    a = ap.parse_args(argv)
    if not a.no_env_file:
        names = load_env_file()
        if names:
            print(f"read {', '.join(sorted(names))} from {ENV_FILE}")
    env = (a.env or os.environ.get("KALSHI_ENV") or "demo").lower()
    rows = check(env, os.environ.get("KALSHI_API_KEY"), os.environ.get("KALSHI_PRIVATE_KEY_PATH"))
    for status, step, detail in rows:
        print(f"{status:<4} {step:<12} {detail}")
    ok = rows and rows[-1][0] != FAIL and len(rows) == 4
    print("CONNECTED" if ok else "NOT CONNECTED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

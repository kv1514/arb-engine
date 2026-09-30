#!/usr/bin/env python3
"""Local Polymarket US credential setup and read-only connection check.

Never accepts a secret on the command line, prints it, or submits an order.
Credentials live in secrets/polymarket_us.env (git-ignored, mode 600).
Uses the existing optional cryptography installation for Ed25519 signing.
Sources: docs.polymarket.us/api-reference/authentication and
api-reference/account/get-account-balances, read 2026-09-30.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

DEFAULT_FILE = Path(__file__).resolve().parents[1] / "secrets" / "polymarket_us.env"
API = "https://api.polymarket.us"
BALANCES_PATH = "/v1/account/balances"


def load_credentials(path: Path) -> dict[str, str]:
    if path.is_symlink():
        raise ValueError("credential file must not be a symlink")
    if path.stat().st_mode & 0o077:
        raise ValueError("credential file permissions must be 600")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key in ("POLYMARKET_KEY_ID", "POLYMARKET_SECRET_KEY"):
            if key in values:
                raise ValueError("duplicate credential field")
            values[key] = value.strip()
    return values


def secret_bytes(secret: str) -> bytes:
    try:
        decoded = base64.b64decode(secret, validate=True)
    except (ValueError, TypeError):
        raise ValueError("secret must be the base64 Secret Key from Polymarket US") from None
    if len(decoded) not in (32, 64):
        raise ValueError("expected a 32- or 64-byte base64 Ed25519 secret")
    return decoded[:32]  # official raw-request example uses the first 32 bytes


def save_credentials(path: Path, key_id: str, secret: str) -> None:
    key_id = str(uuid.UUID(key_id))
    secret_bytes(secret)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("credential path must not be a symlink")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".polymarket-", dir=path.parent)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"POLYMARKET_KEY_ID={key_id}\nPOLYMARKET_SECRET_KEY={secret}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _sign(seed: bytes, message: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        raise ValueError("cryptography is needed for account signing; public scanning needs no key") from None
    return Ed25519PrivateKey.from_private_bytes(seed).sign(message)


def auth_headers(values: dict[str, str], timestamp_ms: int) -> dict[str, str]:
    key_id = str(uuid.UUID(values.get("POLYMARKET_KEY_ID", "")))
    seed = secret_bytes(values.get("POLYMARKET_SECRET_KEY", ""))
    timestamp = str(timestamp_ms)
    signature = _sign(seed, f"{timestamp}GET{BALANCES_PATH}".encode("utf-8"))
    return {"X-PM-Access-Key": key_id, "X-PM-Timestamp": timestamp,
            "X-PM-Signature": base64.b64encode(signature).decode("ascii"), "Accept": "application/json"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("authenticated redirects are refused")


def check_account(values: dict[str, str]) -> int:
    request = urllib.request.Request(API + BALANCES_PATH,
                                    headers=auth_headers(values, int(time.time() * 1000)), method="GET")
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("balances"), list):
        raise ValueError("account endpoint returned an unexpected response")
    return len(payload["balances"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("setup", "check"))
    parser.add_argument("--key-id", help="public Key ID; never pass the secret as an argument")
    args = parser.parse_args(argv)
    try:
        values = load_credentials(DEFAULT_FILE) if DEFAULT_FILE.exists() else {}
        if args.command == "setup":
            if not sys.stdin.isatty():
                raise ValueError("run setup in your own interactive Terminal; secret input must be hidden")
            key_id = args.key_id or values.get("POLYMARKET_KEY_ID")
            if not key_id:
                raise ValueError("missing public Key ID; supply --key-id")
            uuid.UUID(key_id)
            secret = getpass.getpass("Paste Polymarket US Secret Key (hidden), then press Enter: ").strip()
            save_credentials(DEFAULT_FILE, key_id, secret)
            print("Secret saved locally with permissions 600. Trading is NOT enabled.")
            print("Next: python3 scripts/polymarket_us_account.py check")
        else:
            currencies = check_account(values)
            print(f"Authentication succeeded (read-only); {currencies} balance record(s) returned. No orders sent.")
        return 0
    except urllib.error.HTTPError as error:
        print(f"Read-only account check failed: HTTP {error.code}. Check key/account approval; no orders sent.", file=sys.stderr)
    except urllib.error.URLError:
        print("Read-only account check failed: network connection unavailable.", file=sys.stderr)
    except (ValueError, OSError, EOFError):
        print("Setup/check failed. Check key format, permissions (600), and interactive Terminal input. Secret not displayed.", file=sys.stderr)
    except KeyboardInterrupt:
        print("Cancelled; no orders sent.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

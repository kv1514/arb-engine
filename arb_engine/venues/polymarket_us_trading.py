"""Signed US-only transport. One send per request, no redirects or POST retries.

This is NOT the international wallet/CLOB API. Mutation methods are private and
used only by execution.polymarket_us after a durable reservation. Do not log a
Request object or headers. Sources: docs.polymarket.us/api-reference/{authentication,
orders/create-order,orders/get-order,orders/cancel-order}, verified 2026-09-30.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from ..config import declare_setting, setting

API = "https://api.polymarket.us"
declare_setting("polymarket_us_live_trading", env="POLYMARKET_US_LIVE_TRADING", default="0", cast=str,
                doc="US order mutations require exactly 1, ARB_LIVE_TRADING=1 and explicit confirmation; default off.")


class USAPIError(RuntimeError):
    """Sanitized error: never includes account payloads, headers or credentials."""


def gate_problem():
    import os
    if os.environ.get("ARB_LIVE_TRADING") != "1" or setting(None, "polymarket_us_live_trading") != "1":
        return "requires ARB_LIVE_TRADING=1 and POLYMARKET_US_LIVE_TRADING=1"
    return None


def secret_bytes(secret):
    try:
        seed = base64.b64decode(secret, validate=True)
    except (ValueError, TypeError):
        raise ValueError("invalid US credential format") from None
    if len(seed) not in (32, 64):
        raise ValueError("invalid US credential length")
    return seed[:32]


def load_credentials(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("US credential file must be private and not a symlink")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key in ("POLYMARKET_KEY_ID", "POLYMARKET_SECRET_KEY"):
            if key in values:
                raise ValueError("duplicate credential field")
            values[key] = value.strip()
    # Validate without revealing any offending value.
    try:
        uuid.UUID(values.get("POLYMARKET_KEY_ID", ""))
        secret_bytes(values.get("POLYMARKET_SECRET_KEY", ""))
    except ValueError:
        raise ValueError("invalid US credential format") from None
    return values


def _sign(seed, message):
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        raise USAPIError("optional cryptography installation required for signing") from None
    return Ed25519PrivateKey.from_private_bytes(seed).sign(message)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise USAPIError("authenticated redirects are refused")


def _transport(request):
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
        raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise USAPIError("oversized authenticated response")
        return json.loads(raw)


class PolymarketUSTradingClient:
    base_url = API

    def __init__(self, values, *, clock=time.time, transport=None, signer=None):
        try:
            self._key = str(uuid.UUID(values.get("POLYMARKET_KEY_ID", "")))
            self._seed = secret_bytes(values.get("POLYMARKET_SECRET_KEY", ""))
        except ValueError:
            raise ValueError("invalid US credential format") from None
        self.fingerprint = hashlib.sha256(self._key.encode()).hexdigest()
        self.clock, self._transport, self._signer = clock, transport or _transport, signer or _sign

    def _request(self, method, path, payload=None, *, confirm=False):
        if self.base_url != API:
            raise USAPIError("only the exact Polymarket US production host is allowed")
        if not re.fullmatch(r"/v1/(?:account/balances|orders|order/[A-Za-z0-9_-]+(?:/cancel)?)", path):
            raise USAPIError("unsupported US account path")
        if method not in ("GET", "POST"):
            raise USAPIError("unsupported US method")
        if method == "POST" and (confirm is not True or gate_problem()):
            raise USAPIError("US mutations are disabled")
        now = self.clock()
        if not math.isfinite(now) or now < 0:
            raise USAPIError("invalid signing time")
        stamp = str(int(now * 1000))
        signature = self._signer(self._seed, f"{stamp}{method}{path}".encode())
        headers = {"X-PM-Access-Key": self._key, "X-PM-Timestamp": stamp,
                   "X-PM-Signature": base64.b64encode(signature).decode(), "Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(API + path, headers=headers, method=method, data=data)
        try:
            result = self._transport(request)
        except urllib.error.HTTPError as error:
            raise USAPIError(f"US API HTTP {error.code}; outcome requires reconciliation") from None
        except Exception:
            raise USAPIError("US API request failed; outcome requires reconciliation") from None
        if not isinstance(result, dict):
            raise USAPIError("unexpected US API response")
        return result

    def balances(self):
        return self._request("GET", "/v1/account/balances")

    def order(self, order_id):
        return self._request("GET", f"/v1/order/{order_id}")

    def _create(self, payload, *, confirm=False):
        return self._request("POST", "/v1/orders", payload, confirm=confirm)

    def _cancel(self, order_id, slug, *, confirm=False):
        return self._request("POST", f"/v1/order/{order_id}/cancel", {"marketSlug": slug}, confirm=confirm)

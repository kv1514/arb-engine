"""Private single-attempt IOC transport for ledgered Kalshi dispatch.

Unlike the legacy HttpClient path, this never retries, falls back to curl or
another host, or follows redirects. Only KalshiExecutor calls it after claiming
the ledger's client_order_id. Errors are deliberately secret-free and ambiguous.
"""
from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request

from .ledger import env_host_problem

PATH = "/portfolio/events/orders"


class KalshiOnceError(RuntimeError):
    """A failed single send requires reconciliation, not another POST."""

    attempts = 1


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise KalshiOnceError("authenticated redirects are refused")


def _transport(request):
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=15) as response:
        raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise KalshiOnceError("oversized authenticated response")
        return json.loads(raw)


def _create_once(client, payload, *, confirm, not_after, clock, transport=None):
    # Intentionally private: this is transport, not independent permission to
    # submit. The executor requires the matching pending ledger reservation.
    start = clock()
    if (isinstance(start, bool) or not isinstance(start, (int, float)) or
            not math.isfinite(start) or start < 0 or isinstance(not_after, bool) or
            not isinstance(not_after, (int, float)) or not math.isfinite(not_after) or
            start >= not_after):
        raise KalshiOnceError("request deadline expired or invalid")
    original = (client.env, client.base_url, client.api_key,
                getattr(client, "private_key_path", None))

    def check():
        if confirm is not True or env_host_problem(client.env, client.base_url):
            raise KalshiOnceError("Kalshi mutations disabled or host invalid")
        if client.env == "prod" and os.environ.get("ARB_LIVE_TRADING") != "1":
            raise KalshiOnceError("Kalshi production mutations disabled")
        if original != (client.env, client.base_url, client.api_key,
                        getattr(client, "private_key_path", None)):
            raise KalshiOnceError("Kalshi client identity changed before transport")
        now = clock()
        if (isinstance(now, bool) or not isinstance(now, (int, float)) or
                not math.isfinite(now) or now < start or now >= not_after):
            raise KalshiOnceError("request expired or clock regressed before transport")

    check()
    try:
        data = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
        headers = client._auth_headers("POST", PATH)
        # Never serialize signed headers into a subprocess's argv.
        headers = dict(headers, **{"Content-Type": "application/json", "Accept": "application/json"})
        request = urllib.request.Request(original[1] + PATH, data=data, headers=headers, method="POST")
    except Exception:
        raise KalshiOnceError("Kalshi request preparation failed; reservation retained") from None
    check()  # Slow signing/serialization and removed gates count against freshness.
    try:
        result = (transport or _transport)(request)
    except urllib.error.HTTPError as error:
        # Do not attach .status: even an HTTP error can be supplied by a proxy.
        # This transport never authorizes releasing an unknown order by absence.
        raise KalshiOnceError(f"Kalshi API HTTP {error.code}; reconcile the claimed intent") from None
    except Exception:
        raise KalshiOnceError("Kalshi request failed; reconcile the claimed intent") from None
    if not isinstance(result, dict):
        raise KalshiOnceError("unexpected Kalshi response; reconcile the claimed intent")
    return result

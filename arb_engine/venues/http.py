"""Minimal JSON HTTP client on urllib (honours HTTP(S)_PROXY, retries on 429/5xx).

Some proxies truncate HTTP/1.1 chunked responses to Python's ``http.client`` while curl
(HTTP/2) is fine, so on ``IncompleteRead`` we transparently retry through the ``curl``
binary when it is available. Force it with ``ARB_HTTP_TRANSPORT=curl``.

ESPN's Akamai edge sometimes answers 403 to ``site.api.espn.com`` for one user agent while
``site.web.api.espn.com`` (the same API behind the web front-end) and a plain urllib UA
still work. A 403 from a host in ``HOST_FALLBACKS`` therefore retries once on the fallback
host and once with the plain UA; the failing (host, UA) pair is remembered for
``FALLBACK_TTL_S`` so the next calls skip straight to what worked. The final ``HttpError``
carries a ``reason`` listing every attempt so an ``espn: none`` row can say why.
"""

from __future__ import annotations

import gzip
import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Optional

DEFAULT_UA = "arb-engine/0.1 (+https://github.com/kv1514/arb-engine)"
PLAIN_UA = f"Python-urllib/{sys.version_info.major}.{sys.version_info.minor}"  # what urllib sends when no UA is set
HOST_FALLBACKS: dict[str, tuple[str, ...]] = {"site.api.espn.com": ("site.web.api.espn.com",)}
FALLBACK_TTL_S = 60.0
FALLBACK_STATUSES = frozenset({403})


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str, reason: Optional[str] = None):
        self.status = status
        self.url = url
        self.body = body
        self.reason = reason
        msg = f"HTTP {status} for {url}: {body[:300]}"
        if reason:
            msg += f" [{reason}]"
        super().__init__(msg)


class RateLimiter:
    """Thread-safe token bucket: at most ``rate`` requests per second (burst ``burst``)."""

    def __init__(self, rate: float, burst: int = 1):
        self.rate = float(rate)
        self.capacity = float(max(1, burst))
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            time.sleep(wait)


class HttpClient:
    def __init__(self, timeout: float = 20.0, retries: int = 2, user_agent: str = DEFAULT_UA, headers: Optional[Mapping[str, str]] = None, transport: Optional[str] = None, rate_limit: Optional[float] = None):
        self.timeout = timeout
        self.retries = retries
        self.headers = {"User-Agent": user_agent, "Accept": "application/json", "Accept-Encoding": "gzip", **(headers or {})}
        self.transport = transport or os.environ.get("ARB_HTTP_TRANSPORT", "auto")
        self._curl = shutil.which("curl")
        self.limiter = RateLimiter(rate_limit, burst=int(rate_limit)) if rate_limit else None
        self.fallback_failures: dict[tuple[str, str], float] = {}  # (host, UA) -> monotonic time of the last 403
        self.fallback_log: list[str] = []                           # last request's attempt trail (tests / diagnostics)

    # ---- public -----------------------------------------------------------------------
    def request(self, method: str, url: str, params: Optional[Mapping[str, Any]] = None, json_body: Any = None, headers: Optional[Mapping[str, str]] = None, raw: bool = False) -> Any:
        if params:
            qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
            url = f"{url}{'&' if '?' in url else '?'}{qs}"
        hdrs = dict(self.headers)
        if headers:
            hdrs.update(headers)
        data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        if data is not None:
            hdrs["Content-Type"] = "application/json"
        candidates = self._candidates(url, hdrs)
        if len(candidates) == 1:
            return self._request_retrying(method, url, hdrs, data, raw)
        self.fallback_log = []
        last: Optional[HttpError] = None
        now = time.monotonic()
        live = [(u, h, key) for u, h, key in candidates if now - self.fallback_failures.get(key, -1e9) >= FALLBACK_TTL_S]
        skipped = [key for _, _, key in candidates if now - self.fallback_failures.get(key, -1e9) < FALLBACK_TTL_S]
        for host, ua in skipped:
            self.fallback_log.append(f"{host} ({ua}): skipped, 403 within {int(FALLBACK_TTL_S)}s")
        for cand_url, cand_hdrs, key in live or candidates[:1]:
            try:
                out = self._request_retrying(method, cand_url, cand_hdrs, data, raw)
            except HttpError as e:
                if e.status not in FALLBACK_STATUSES:
                    raise
                self.fallback_failures[key] = time.monotonic()
                self.fallback_log.append(f"{key[0]} ({key[1]}): HTTP {e.status}")
                last = e
                continue
            if key != candidates[0][2]:
                self.fallback_log.append(f"{key[0]} ({key[1]}): ok")
            return out
        assert last is not None
        raise HttpError(last.status, url, last.body, reason="; ".join(self.fallback_log))

    def _candidates(self, url: str, hdrs: dict) -> list[tuple[str, dict, tuple[str, str]]]:
        """(url, headers, (host, UA)) to try in order: the request as given, then each
        fallback host with the same UA, then the original host with the plain urllib UA."""
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        ua = hdrs.get("User-Agent", "")
        out = [(url, hdrs, (host, ua))]
        for alt in HOST_FALLBACKS.get(host, ()):
            netloc = parsed.netloc.replace(host, alt, 1)
            out.append((urllib.parse.urlunsplit(parsed._replace(netloc=netloc)), hdrs, (alt, ua)))
        if host in HOST_FALLBACKS and ua != PLAIN_UA:
            out.append((url, {**hdrs, "User-Agent": PLAIN_UA}, (host, PLAIN_UA)))
        return out

    def _request_retrying(self, method: str, url: str, hdrs: dict, data: Optional[bytes], raw: bool) -> Any:
        """One URL with the 429 / 5xx / transport retry loop."""
        last_err: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            if self.limiter:
                self.limiter.acquire()
            try:
                if self.transport == "curl" and self._curl:
                    status, body = self._via_curl(method, url, hdrs, data)
                else:
                    try:
                        status, body = self._via_urllib(method, url, hdrs, data)
                    except http.client.IncompleteRead:
                        if not self._curl:
                            raise
                        self.transport = "curl"  # remember for the rest of the session
                        status, body = self._via_curl(method, url, hdrs, data)
            except HttpError as e:
                last_err = e
                if e.status in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep((2.0 if e.status == 429 else 0.5) * (2 ** attempt))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, http.client.HTTPException, subprocess.SubprocessError) as e:
                last_err = e
                if attempt < self.retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise
            if status >= 400:
                raise HttpError(status, url, body)
            if raw:
                return body
            return json.loads(body) if body.strip() else {}
        raise last_err  # pragma: no cover

    def get(self, url: str, params: Optional[Mapping[str, Any]] = None, headers: Optional[Mapping[str, str]] = None, raw: bool = False) -> Any:
        return self.request("GET", url, params=params, headers=headers, raw=raw)

    def post(self, url: str, json_body: Any = None, headers: Optional[Mapping[str, str]] = None) -> Any:
        return self.request("POST", url, json_body=json_body, headers=headers)

    def delete(self, url: str, headers: Optional[Mapping[str, str]] = None) -> Any:
        return self.request("DELETE", url, headers=headers)

    # ---- transports -------------------------------------------------------------------
    def _via_urllib(self, method: str, url: str, hdrs: dict, data: Optional[bytes]) -> tuple[int, str]:
        req = urllib.request.Request(url, data=data, method=method.upper(), headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                buf = bytearray()
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    buf.extend(chunk)
                raw = bytes(buf)
                if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                return resp.status, raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            return e.code, body

    def _via_curl(self, method: str, url: str, hdrs: dict, data: Optional[bytes]) -> tuple[int, str]:
        cmd = [self._curl or "curl", "-sS", "-L", "--compressed", "--max-time", str(int(self.timeout) + 5), "-X", method.upper(), "-w", "\n__STATUS__:%{http_code}"]
        for k, v in hdrs.items():
            cmd += ["-H", f"{k}: {v}"]
        if data is not None:
            cmd += ["--data-binary", "@-"]
        cmd.append(url)
        proc = subprocess.run(cmd, input=data, capture_output=True, timeout=self.timeout + 10)
        if proc.returncode != 0:
            raise HttpError(0, url, proc.stderr.decode("utf-8", errors="replace"))
        out = proc.stdout.decode("utf-8", errors="replace")
        body, _, status = out.rpartition("\n__STATUS__:")
        try:
            code = int(status.strip() or 0)
        except ValueError:
            code = 0
        return code, body

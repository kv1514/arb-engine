from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> Any:
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


def load_text(name: str) -> str:
    with open(FIXTURES / name, encoding="utf-8") as f:
        return f.read()


class FakeHttp:
    """Routes URLs to fixture payloads; records every request."""

    transport = "fake"
    _curl = None

    def __init__(self, routes: dict[str, Any]):
        self.routes = routes
        self.calls: list[str] = []

    def _match(self, url: str) -> Any:
        self.calls.append(url)
        for needle, payload in self.routes.items():
            if needle in url:
                return payload() if callable(payload) else payload
        raise AssertionError(f"unexpected URL in test: {url}")

    def get(self, url: str, params: dict | None = None, headers: dict | None = None, raw: bool = False) -> Any:
        if params:
            url = url + "?" + "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        payload = self._match(url)
        if raw and not isinstance(payload, str):
            return json.dumps(payload)
        return payload

    def post(self, url: str, json_body: Any = None, headers: dict | None = None) -> Any:
        return self._match(url)

    def delete(self, url: str, headers: dict | None = None) -> Any:
        return self._match(url)


class SequencedFakeHttp:
    """Scripted transport: the n-th call gets the n-th response, whatever the URL.

    ``responses`` items are ``(status, body)`` tuples, a bare payload (status 200), or an
    exception instance to raise. A status >= 400 raises ``HttpError`` exactly like the real
    ``HttpClient``. Every call is appended to ``requests`` as ``(host, path, headers)`` (the
    per-call headers merged over ``base_headers``) so a test can assert the *order* in which
    a client fell back across hosts / user agents; ``calls`` keeps the full URLs for parity
    with ``FakeHttp``. Running past the script is a test failure, not a silent 200.
    """

    transport = "fake"
    _curl = None

    def __init__(self, responses: list[Any], base_headers: dict[str, str] | None = None):
        self.responses = list(responses)
        self.base_headers = dict(base_headers or {})
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.calls: list[str] = []
        self.methods: list[str] = []
        self.bodies: list[Any] = []

    @property
    def remaining(self) -> int:
        return len(self.responses)

    def _next(self, method: str, url: str, headers: dict | None, raw: bool, json_body: Any = None) -> Any:
        from urllib.parse import urlsplit

        from arb_engine.venues.http import HttpError

        parts = urlsplit(url)
        self.calls.append(url)
        self.methods.append(method)
        self.bodies.append(json_body)
        self.requests.append((parts.netloc, parts.path, {**self.base_headers, **(headers or {})}))
        if not self.responses:
            raise AssertionError(f"SequencedFakeHttp: no scripted response left for {method} {url}")
        item = self.responses.pop(0)
        if callable(item) and not isinstance(item, BaseException):
            item = item()
        if isinstance(item, BaseException):
            raise item
        status, body = item if isinstance(item, tuple) else (200, item)
        if status >= 400:
            raise HttpError(status, url, body if isinstance(body, str) else json.dumps(body))
        if raw:
            return body if isinstance(body, str) else json.dumps(body)
        return json.loads(body) if isinstance(body, str) else body

    def get(self, url: str, params: dict | None = None, headers: dict | None = None, raw: bool = False) -> Any:
        if params:
            url = url + "?" + "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        return self._next("GET", url, headers, raw)

    def post(self, url: str, json_body: Any = None, headers: dict | None = None) -> Any:
        return self._next("POST", url, headers, False, json_body)

    def delete(self, url: str, headers: dict | None = None) -> Any:
        return self._next("DELETE", url, headers, False)

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

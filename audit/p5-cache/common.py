"""Shared offline harness for the trade-cache audit: no network, no real cache directory."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

ROOT = os.path.abspath(__file__)
for _ in range(6):                         # walk up to the repo root (the one holding arb_engine/)
    ROOT = os.path.dirname(ROOT)
    if os.path.isdir(os.path.join(ROOT, "arb_engine")):
        break
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TK = "KXNFLGAME-26SEP27DENKC-KC"
BASE = 1_790_000_000.0


def row(i, sec, price=0.50, count=1.0, ticker=TK, tid=None, frac=0.25):
    t = datetime.fromtimestamp(BASE + sec + frac, tz=timezone.utc)
    return {"trade_id": f"t{i:04d}" if tid is None else tid, "ticker": ticker,
            "created_time": t.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "yes_price_dollars": f"{price:.4f}", "count_fp": f"{count:.2f}", "taker_side": "yes"}


class Pages:
    """``GET /markets/trades`` by cursor. ``pages[cursor]`` is a page dict or an exception."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, params=None, headers=None, raw=False):
        assert url.endswith("/markets/trades"), url
        self.calls.append(dict(params or {}))
        v = self.pages[(params or {}).get("cursor")]
        if isinstance(v, list):
            v = v.pop(0) if len(v) > 1 else v[0]
        if isinstance(v, BaseException):
            raise v
        return v


class NoNetwork:
    def get(self, *a, **k):
        raise AssertionError("must be answered from the cache")


def one_page(rows):
    return {None: {"trades": rows, "cursor": ""}}


def banner(name):
    print("=" * 78)
    print(name)
    print("=" * 78)

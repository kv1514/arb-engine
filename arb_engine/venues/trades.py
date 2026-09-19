"""Public trade tapes (Kalshi, Polymarket) with a read-through cache, for event studies.

Both venues publish every print with a sub-minute timestamp, which is what an event study
around a model move needs (candles blur the first 30 s of repricing):

* Kalshi ``GET /trade-api/v2/markets/trades?ticker=&min_ts=&max_ts=&limit=1000&cursor=``
  → ``{"trades": [{trade_id, ticker, count | count_fp, yes_price (cents) | yes_price_dollars,
  no_price, taker_side, created_time (ISO, µs)}], "cursor": "..."}``, newest first, cursor
  paginated (empty cursor = last page). Prices are the YES side of the ticker.
* Polymarket data API ``GET https://data-api.polymarket.com/trades?market=<conditionId>&
  limit=&offset=`` → a JSON list ``[{asset (token id), conditionId, side (BUY|SELL), size,
  price, timestamp (epoch s), outcome, outcomeIndex, transactionHash}]``, newest first,
  offset paginated. The API filters on the condition id; the token id is filtered here.

Every fetch is cached under ``out/cache/trades/<venue>-<market>-<min_ts>-<max_ts>.json`` so
an event study reruns offline (``TradesClient(offline=True)`` refuses to fetch). Timestamps
keep whatever precision the venue gave (float seconds; ``ts_ms`` is the integer form).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Optional

from ..matching.normalize import parse_iso
from .http import HttpClient

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
POLY_DATA = "https://data-api.polymarket.com"
KALSHI_PAGE = 1000
POLY_PAGE = 500


@dataclass
class Trade:
    venue: str
    market: str          # Kalshi ticker or Polymarket token id
    ts: float            # epoch seconds, sub-second precision preserved
    price: float         # dollars per $1 payout of the market's YES / the token's outcome
    size: float          # contracts / shares
    side: str = ""       # Kalshi taker_side ('yes'|'no'); Polymarket 'BUY'|'SELL'
    trade_id: str = ""

    @property
    def ts_ms(self) -> int:
        return int(round(self.ts * 1000))


def _f(x: Any) -> Optional[float]:
    try:
        return float(x) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None


def parse_kalshi_trade(t: dict[str, Any]) -> Optional[Trade]:
    """One Kalshi print -> Trade (YES price in dollars; cents field as the fallback)."""
    price = _f(t.get("yes_price_dollars"))
    if price is None:
        cents = _f(t.get("yes_price"))
        price = cents / 100.0 if cents is not None else None
    dt = parse_iso(t.get("created_time"))
    if price is None or dt is None:
        return None
    size = _f(t.get("count_fp"))
    if size is None:
        size = _f(t.get("count")) or 0.0
    return Trade(venue="kalshi", market=str(t.get("ticker") or ""), ts=dt.timestamp(), price=price, size=size, side=str(t.get("taker_side") or ""), trade_id=str(t.get("trade_id") or ""))


def parse_polymarket_trade(t: dict[str, Any]) -> Optional[Trade]:
    price, ts = _f(t.get("price")), _f(t.get("timestamp"))
    if price is None or ts is None:
        return None
    if ts > 1e12:  # defensive: a millisecond stamp
        ts = ts / 1000.0
    return Trade(venue="polymarket", market=str(t.get("asset") or ""), ts=ts, price=price, size=_f(t.get("size")) or 0.0, side=str(t.get("side") or ""), trade_id=str(t.get("transactionHash") or t.get("id") or ""))


def _cache_key(venue: str, market: str, min_ts: Optional[float], max_ts: Optional[float]) -> str:
    raw = f"{venue}-{market}-{'none' if min_ts is None else int(min_ts)}-{'none' if max_ts is None else int(max_ts)}"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw)


class TradesClient:
    def __init__(self, http: Optional[HttpClient] = None, cache_dir: str = "out/cache/trades", offline: bool = False):
        self.http = http or HttpClient(rate_limit=6)
        self.cache_dir = cache_dir
        self.offline = offline

    # ---- cache -----------------------------------------------------------------------
    def _cache_path(self, key: str) -> str:
        return os.path.join(self.cache_dir, key + ".json")

    def _cached(self, key: str) -> Optional[list[Trade]]:
        p = self._cache_path(key)
        if not os.path.exists(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                doc = json.load(f)
            return [Trade(**t) for t in doc.get("trades", [])]
        except (OSError, ValueError, TypeError):
            return None

    def _save(self, key: str, venue: str, market: str, min_ts: Optional[float], max_ts: Optional[float], trades: list[Trade]) -> None:
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(self._cache_path(key), "w", encoding="utf-8") as f:
                json.dump({"venue": venue, "market": market, "min_ts": min_ts, "max_ts": max_ts, "fetched_at": time.time(), "trades": [asdict(t) for t in trades]}, f)
        except OSError:
            pass

    def _through(self, venue: str, market: str, min_ts: Optional[float], max_ts: Optional[float], fetch) -> list[Trade]:
        key = _cache_key(venue, market, min_ts, max_ts)
        hit = self._cached(key)
        if hit is not None:
            return hit
        if self.offline:
            raise FileNotFoundError(f"offline: no cached trades at {self._cache_path(key)}")
        trades = fetch()
        self._save(key, venue, market, min_ts, max_ts, trades)
        return trades

    # ---- venues ----------------------------------------------------------------------
    def kalshi_trades(self, ticker: str, min_ts: Optional[float] = None, max_ts: Optional[float] = None, max_pages: int = 50) -> list[Trade]:
        """Every print of ``ticker`` in [min_ts, max_ts], oldest first (cursor paginated)."""
        def fetch() -> list[Trade]:
            out: list[Trade] = []
            cursor: Optional[str] = None
            for _ in range(max_pages):
                params: dict[str, Any] = {"ticker": ticker, "limit": KALSHI_PAGE, "min_ts": int(min_ts) if min_ts is not None else None, "max_ts": int(max_ts) if max_ts is not None else None, "cursor": cursor}
                data = self.http.get(f"{KALSHI}/markets/trades", params) or {}
                for t in data.get("trades", []) or []:
                    tr = parse_kalshi_trade(t)
                    if tr is not None and (min_ts is None or tr.ts >= min_ts) and (max_ts is None or tr.ts <= max_ts):
                        out.append(tr)
                cursor = data.get("cursor") or None
                if not cursor:
                    break
            out.sort(key=lambda t: (t.ts, t.trade_id))
            return out

        return self._through("kalshi", ticker, min_ts, max_ts, fetch)

    def polymarket_trades(self, token_id: str, min_ts: Optional[float] = None, max_ts: Optional[float] = None, condition_id: Optional[str] = None, max_pages: int = 40) -> list[Trade]:
        """Every print of ``token_id`` in [min_ts, max_ts], oldest first (offset paginated on
        the data API, which filters by condition id; pass it to avoid paging the whole tape)."""
        def fetch() -> list[Trade]:
            out: list[Trade] = []
            offset = 0
            for _ in range(max_pages):
                params: dict[str, Any] = {"limit": POLY_PAGE, "offset": offset, "takerOnly": "true"}
                if condition_id:
                    params["market"] = condition_id
                else:
                    params["asset"] = token_id
                data = self.http.get(f"{POLY_DATA}/trades", params) or []
                batch = data if isinstance(data, list) else (data.get("trades") or data.get("data") or [])
                oldest = None
                for t in batch:
                    tr = parse_polymarket_trade(t)
                    if tr is None:
                        continue
                    oldest = tr.ts if oldest is None else min(oldest, tr.ts)
                    if tr.market != str(token_id):
                        continue
                    if (min_ts is None or tr.ts >= min_ts) and (max_ts is None or tr.ts <= max_ts):
                        out.append(tr)
                if len(batch) < POLY_PAGE or (min_ts is not None and oldest is not None and oldest < min_ts):
                    break
                offset += len(batch)
            out.sort(key=lambda t: (t.ts, t.trade_id))
            return out

        return self._through("polymarket", str(token_id), min_ts, max_ts, fetch)


def as_home_prices(trades: Iterable[Trade], is_home: bool = True) -> list[tuple[float, float, float]]:
    """``[(ts, P(home), size)]`` from a market's prints: the away market's YES is 1 - P(home)."""
    return [(t.ts, t.price if is_home else 1.0 - t.price, t.size) for t in trades]

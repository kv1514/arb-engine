"""Historical price series (public) for back-tests.

* Kalshi 1-minute candlesticks: ``GET /series/{series}/markets/{ticker}/candlesticks``
  with ``start_ts``/``end_ts`` (epoch s) and ``period_interval=1``; each candle carries
  ``yes_bid``/``yes_ask``/``price`` OHLC in dollars, ``volume_fp``, ``open_interest_fp``.
* Robinhood 5-minute bars: ``GET api.robinhood.com/marketdata/event/contract/historicals/v1/
  ?ids=…&interval=5minute&start=<iso>`` → ``data_points[] {begins_at, open/close/high/low_price,
  volume}`` — trade prices, no bid/ask.
* Polymarket: ``GET clob.polymarket.com/prices-history?market=<token>&startTs&endTs&fidelity=1``
  → ``history[] {t, p}`` (mid price, one point per minute).
* ESPN play timeline: the summary's ``drives.previous[*].plays[*]`` with ``wallclock``, period,
  clock, scores and the start situation → a GameState-like row per play, plus ESPN's own win
  probability aligned to those plays.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ..matching.normalize import parse_iso
from .espn import _possession_side, parse_clock, yardline_100_from
from .http import HttpClient

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
RH_API = "https://api.robinhood.com"
CLOB = "https://clob.polymarket.com"


def _f(x: Any) -> Optional[float]:
    try:
        return float(x) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None


@dataclass
class Bar:
    ts: float                 # epoch seconds (end of the bar for Kalshi, start for Robinhood, point time for Polymarket)
    bid: Optional[float]
    ask: Optional[float]
    close: Optional[float]
    volume: Optional[float] = None

    @property
    def mid(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2.0
        return self.close


class HistoryClient:
    def __init__(self, http: Optional[HttpClient] = None):
        self.http = http or HttpClient(rate_limit=6)

    def kalshi_candles(self, ticker: str, start_ts: int, end_ts: int, period_minutes: int = 1) -> list[Bar]:
        series = ticker.split("-")[0]
        data = self.http.get(f"{KALSHI}/series/{series}/markets/{ticker}/candlesticks", {"start_ts": int(start_ts), "end_ts": int(end_ts), "period_interval": period_minutes})
        out: list[Bar] = []
        for c in data.get("candlesticks", []):
            yb, ya, pr = c.get("yes_bid") or {}, c.get("yes_ask") or {}, c.get("price") or {}
            bid, ask = _f(yb.get("close_dollars")), _f(ya.get("close_dollars"))
            out.append(Bar(ts=float(c.get("end_period_ts")), bid=bid if bid and bid > 0 else None, ask=ask if ask and ask < 1 else None, close=_f(pr.get("close_dollars")), volume=_f(c.get("volume_fp"))))
        out.sort(key=lambda b: b.ts)
        return out

    def robinhood_bars(self, contract_ids: Iterable[str], start_iso: str, interval: str = "5minute") -> dict[str, list[Bar]]:
        ids = list(dict.fromkeys(contract_ids))
        out: dict[str, list[Bar]] = {}
        for i in range(0, len(ids), 20):
            data = self.http.get(f"{RH_API}/marketdata/event/contract/historicals/v1/", {"ids": ",".join(ids[i : i + 20]), "interval": interval, "start": start_iso}, headers={"Accept": "application/json"})
            for item in data.get("data", []):
                d = item.get("data") or {}
                pts = d.get("data_points") or []
                cid = d.get("contract_id") or (pts[0].get("contract_id") if pts else None)
                if not cid:
                    continue
                bars = []
                for p in pts:
                    t = parse_iso(p.get("begins_at"))
                    if t is None:
                        continue
                    bars.append(Bar(ts=t.timestamp(), bid=None, ask=None, close=_f(p.get("close_price")), volume=_f(p.get("volume"))))
                out[cid] = sorted(bars, key=lambda b: b.ts)
        return out

    def polymarket_history(self, token_id: str, start_ts: int, end_ts: int, fidelity_minutes: int = 1) -> list[Bar]:
        data = self.http.get(f"{CLOB}/prices-history", {"market": token_id, "startTs": int(start_ts), "endTs": int(end_ts), "fidelity": fidelity_minutes})
        return sorted((Bar(ts=float(h["t"]), bid=None, ask=None, close=_f(h.get("p"))) for h in data.get("history", []) if h.get("t") is not None), key=lambda b: b.ts)


@dataclass
class PlayRow:
    """Game state at the *start* of a play, stamped with the wall-clock time of the play."""

    play_id: str
    ts: float
    period: int
    clock_seconds: Optional[int]
    game_seconds_remaining: Optional[int]
    home_score: int          # score before the play
    away_score: int
    possession: Optional[str]
    down: Optional[int]
    distance: Optional[int]
    yardline_100: Optional[int]
    home_score_after: int
    away_score_after: int
    espn_home_wp: Optional[float]
    text: str = ""


def espn_timeline(summary: dict) -> tuple[list[PlayRow], dict[str, Any]]:
    """Per-play rows from a finished (or in-progress) ESPN summary, plus game metadata
    (home/away codes and ids, final score, kickoff time)."""
    comp = ((summary.get("header") or {}).get("competitions") or [{}])[0]
    home = next((c for c in comp.get("competitors", []) if c.get("homeAway") == "home"), {})
    away = next((c for c in comp.get("competitors", []) if c.get("homeAway") == "away"), {})
    home_id, away_id = str(home.get("id")), str(away.get("id"))
    meta = {
        "home": (home.get("team") or {}).get("abbreviation"), "away": (away.get("team") or {}).get("abbreviation"),
        "home_id": home_id, "away_id": away_id,
        "home_score": int(_f(home.get("score")) or 0), "away_score": int(_f(away.get("score")) or 0),
        "kickoff": parse_iso(comp.get("date")),
        "status": ((comp.get("status") or {}).get("type") or {}).get("state"),
    }
    plays: list[dict] = []
    for drive in (summary.get("drives") or {}).get("previous", []) or []:
        plays.extend(drive.get("plays") or [])
    cur = (summary.get("drives") or {}).get("current")
    if cur:
        plays.extend(cur.get("plays") or [])
    plays = [p for p in plays if p.get("wallclock")]
    plays.sort(key=lambda p: (parse_iso(p["wallclock"]).timestamp(), str(p.get("id"))))
    # ESPN win probability is one entry per play in play order (a pre-game entry first).
    wp = summary.get("winprobability") or []
    wp_by_id = {str(w.get("playId")): _f(w.get("homeWinPercentage")) for w in wp}
    rows: list[PlayRow] = []
    prev_home, prev_away = 0, 0
    for i, p in enumerate(plays):
        t = parse_iso(p["wallclock"]).timestamp()
        period = int(((p.get("period") or {}).get("number")) or 0)
        clk = parse_clock(((p.get("clock") or {}).get("displayValue")))
        gsr = None
        if period:
            if period <= 4:
                gsr = (4 - period) * 900 + (clk or 0)
            else:
                gsr = min(clk or 0, 600)
        st = p.get("start") or {}
        poss = _possession_side(((st.get("team") or {}).get("id")), home_id, away_id)
        yl = yardline_100_from(st.get("yardLine"), poss, st.get("possessionText"), meta["home"], meta["away"])
        if yl is None and st.get("yardsToEndzone") not in (None, 0):
            yl = int(st["yardsToEndzone"])
        ehw = wp_by_id.get(str(p.get("id")))
        if ehw is None and i + 1 < len(wp):
            ehw = _f(wp[i + 1].get("homeWinPercentage"))  # index alignment (entry 0 = pre-game)
        rows.append(PlayRow(play_id=str(p.get("id")), ts=t, period=period, clock_seconds=clk, game_seconds_remaining=gsr, home_score=prev_home, away_score=prev_away, possession=poss, down=st.get("down"), distance=st.get("distance"), yardline_100=yl, home_score_after=int(p.get("homeScore") or 0), away_score_after=int(p.get("awayScore") or 0), espn_home_wp=ehw, text=str(p.get("text") or "")[:120]))
        prev_home, prev_away = int(p.get("homeScore") or 0), int(p.get("awayScore") or 0)
    return rows, meta


def bar_at(bars: list[Bar], ts: float, mode: str = "kalshi", max_gap: float = 900.0) -> Optional[Bar]:
    """The bar that describes the market at wall-clock ``ts``: for Kalshi (bar ts = end of
    minute) the first bar ending at or after ts; for Robinhood/Polymarket (bar ts = start /
    point time) the last bar at or before ts. None if the nearest is further than max_gap."""
    if not bars:
        return None
    if mode == "kalshi":
        cand = [b for b in bars if b.ts >= ts]
        b = cand[0] if cand else bars[-1]
        return b if abs(b.ts - ts) <= max_gap else None
    cand = [b for b in bars if b.ts <= ts]
    b = cand[-1] if cand else bars[0]
    return b if abs(b.ts - ts) <= max_gap else None

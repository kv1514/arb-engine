"""Historical price series (public) for back-tests, plus the ESPN play timeline.

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

Market alignment (``bar_at``)
-----------------------------
A Kalshi candle is stamped with the *end* of its minute. Scoring the market against the
outcome with the first candle ending **after** a play (``mode="kalshi"``) gives the market
the play's information while the model, scored on the state *before* the play, has none:
that flatters the market. ``"kalshi_before"`` (the last candle ending at or before the
play) is the strict lower bound on what the market knew; ``"kalshi"`` is the upper bound.
The replay reports both; ``"kalshi_prev"`` (anchored on the previous play) is kept as a
third, non-default alignment.

Offline cache
-------------
``HistoryClient(cache_dir=…)`` reads every raw response through
``<cache_dir>/<venue>/<key>.json`` (kalshi candles and series, polymarket history,
robinhood bars, the ESPN week scoreboard and summaries) so a replay is repeatable without
the network; ``offline=True`` raises ``OfflineCacheMiss`` instead of fetching.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from ..matching.normalize import parse_iso
from .espn import _possession_side, parse_clock, yardline_100_from
from .http import HttpClient

# Pure per-sport helpers land in venues/espn.py in a later item; use them when present so the
# live feed and the replay share one clock / play-class / timeout definition.
try:  # pragma: no cover - exercised only once that item exists
    from .espn import period_clock_to_gsr as _espn_period_clock_to_gsr  # type: ignore[attr-defined]
except ImportError:
    _espn_period_clock_to_gsr = None
try:  # pragma: no cover
    from .espn import classify_play as _espn_classify_play  # type: ignore[attr-defined]
except ImportError:
    _espn_classify_play = None
try:  # pragma: no cover
    from .espn import count_timeouts as _espn_count_timeouts  # type: ignore[attr-defined]
except ImportError:
    _espn_count_timeouts = None

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
RH_API = "https://api.robinhood.com"
CLOB = "https://clob.polymarket.com"

# (regulation periods, seconds per period, overtime seconds or None when OT is untimed).
SPORT_CLOCK: dict[str, tuple[int, int, Optional[int]]] = {
    "nfl": (4, 900, 600),
    "ncaaf": (4, 900, None),  # college OT is untimed possessions: no game clock to model
    "nba": (4, 720, 300),
    "nhl": (3, 1200, 300),
}
# Timeouts per half and per overtime period (NFL: 2 in OT; college: 1 per OT period).
SPORT_TIMEOUTS: dict[str, tuple[int, int]] = {"nfl": (3, 2), "ncaaf": (3, 1)}
PLAY_CLASSES = ("scrimmage", "kickoff", "try", "timeout", "kneel", "end_period", "ot", "try_synth", "kickoff_pending_synth")
NFL_TYPE_CLASSES = {"53": "kickoff", "21": "timeout", "66": "end_period", "2": "end_period", "75": "timeout"}  # 75 = two-minute warning
TRY_YARDLINE = {"nfl": 15, "ncaaf": 3}
KICKOFF_YARDLINE = 35  # the receiver's yards to the opponent goal at the kicking spot (nflverse convention)


def _f(x: Any) -> Optional[float]:
    try:
        return float(x) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None


def _i(x: Any) -> Optional[int]:
    v = _f(x)
    return int(v) if v is not None else None


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


class OfflineCacheMiss(RuntimeError):
    """Raised when ``offline=True`` and the response is not in the cache."""


def cache_key(*parts: Any) -> str:
    """Filesystem-safe key; long keys (many ids) are hashed."""
    raw = "_".join(str(p) for p in parts if p is not None)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)
    return safe if len(safe) <= 120 else safe[:60] + "_" + hashlib.sha1(raw.encode()).hexdigest()[:16]


class HistoryClient:
    def __init__(self, http: Optional[HttpClient] = None, cache_dir: Optional[str] = None, offline: bool = False):
        self.http = http or HttpClient(rate_limit=6)
        self.cache_dir = cache_dir
        self.offline = offline
        self.cache_hits = 0
        self.cache_misses = 0

    # ---- read-through cache ------------------------------------------------------------
    def _cached(self, venue: str, key: str, fetch: Callable[[], Any]) -> Any:
        if not self.cache_dir:
            if self.offline:
                raise OfflineCacheMiss(f"offline and no cache_dir for {venue}/{key}")
            return fetch()
        path = os.path.join(self.cache_dir, venue, f"{key}.json")
        if os.path.exists(path):
            self.cache_hits += 1
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        if self.offline:
            raise OfflineCacheMiss(f"offline: {path} missing")
        data = fetch()
        self.cache_misses += 1
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        return data

    # ---- venues --------------------------------------------------------------------------
    def kalshi_candles(self, ticker: str, start_ts: int, end_ts: int, period_minutes: int = 1) -> list[Bar]:
        series = ticker.split("-")[0]
        data = self._cached("kalshi", cache_key("candles", ticker, int(start_ts), int(end_ts), period_minutes), lambda: self.http.get(f"{KALSHI}/series/{series}/markets/{ticker}/candlesticks", {"start_ts": int(start_ts), "end_ts": int(end_ts), "period_interval": period_minutes}))
        out: list[Bar] = []
        for c in (data or {}).get("candlesticks", []):
            yb, ya, pr = c.get("yes_bid") or {}, c.get("yes_ask") or {}, c.get("price") or {}
            bid, ask = _f(yb.get("close_dollars")), _f(ya.get("close_dollars"))
            out.append(Bar(ts=float(c.get("end_period_ts")), bid=bid if bid and bid > 0 else None, ask=ask if ask and ask < 1 else None, close=_f(pr.get("close_dollars")), volume=_f(c.get("volume_fp"))))
        out.sort(key=lambda b: b.ts)
        return out

    def kalshi_series(self, series: str) -> dict:
        """The series object (fee_type / fee_multiplier) for a ticker prefix such as KXNFLGAME."""
        data = self._cached("kalshi", cache_key("series", series), lambda: self.http.get(f"{KALSHI}/series/{series}"))
        return (data or {}).get("series") or {}

    def kalshi_fee_multiplier(self, ticker_or_series: str) -> float:
        """``fee_multiplier`` of the market's series (1 for game series, 0.5 for KXMLBGAME-type
        series); 1 when the lookup fails so a replay never silently drops fees."""
        series = ticker_or_series.split("-")[0]
        try:
            v = _f(self.kalshi_series(series).get("fee_multiplier"))
        except OfflineCacheMiss:
            raise  # an offline replay must fail loudly, like every other cached lookup
        except Exception:
            v = None
        return v if v is not None else 1.0

    def robinhood_bars(self, contract_ids: Iterable[str], start_iso: str, interval: str = "5minute") -> dict[str, list[Bar]]:
        ids = list(dict.fromkeys(contract_ids))
        out: dict[str, list[Bar]] = {}
        for i in range(0, len(ids), 20):
            chunk = ids[i : i + 20]
            data = self._cached("robinhood", cache_key("bars", ",".join(chunk), interval, start_iso), lambda chunk=chunk: self.http.get(f"{RH_API}/marketdata/event/contract/historicals/v1/", {"ids": ",".join(chunk), "interval": interval, "start": start_iso}, headers={"Accept": "application/json"}))
            for item in (data or {}).get("data", []):
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
        data = self._cached("polymarket", cache_key("history", token_id, int(start_ts), int(end_ts), fidelity_minutes), lambda: self.http.get(f"{CLOB}/prices-history", {"market": token_id, "startTs": int(start_ts), "endTs": int(end_ts), "fidelity": fidelity_minutes}))
        return sorted((Bar(ts=float(h["t"]), bid=None, ask=None, close=_f(h.get("p"))) for h in (data or {}).get("history", []) if h.get("t") is not None), key=lambda b: b.ts)

    # ---- ESPN through the same cache -------------------------------------------------------
    def espn_summary(self, espn: Any, event_id: str) -> dict:
        sport = getattr(espn, "sport", "nfl")
        return self._cached("espn", cache_key("summary", sport, event_id), lambda: espn.summary(event_id))

    def espn_scoreboard_week(self, espn: Any, season: int, week: int, seasontype: int = 2) -> dict:
        sport = getattr(espn, "sport", "nfl")
        return self._cached("espn", cache_key("scoreboard", sport, season, seasontype, week), lambda: espn.scoreboard_week(season, week, seasontype))

    def cached_http(self, venue: str) -> "CachedHttp":
        """A GET-only transport that reads through this cache (Gamma lookups in the week replay)."""
        return CachedHttp(self, venue)


class CachedHttp:
    """Wraps ``HistoryClient.http.get`` in the read-through cache so every lookup a replay
    makes (not only the price series) is repeatable with ``offline=True``."""

    def __init__(self, history: HistoryClient, venue: str):
        self.history = history
        self.venue = venue

    def get(self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None, raw: bool = False) -> Any:
        key = cache_key(url.split("//", 1)[-1], *(f"{k}={v}" for k, v in sorted((params or {}).items())))
        return self.history._cached(self.venue, key, lambda: self.history.http.get(url, params, headers=headers, raw=raw))


# ---- pure game-clock / play-class / timeout helpers (local fallbacks for venues/espn.py) ----

def period_clock_to_gsr(sport: str, period: int, clock: Optional[int]) -> Optional[int]:
    """Seconds left in the game for (period, clock); None when the sport's overtime has no
    game clock (college football) or the period is unknown."""
    if _espn_period_clock_to_gsr is not None:
        try:
            v = _espn_period_clock_to_gsr(sport, period, clock)
            return int(v[0]) if isinstance(v, (tuple, list)) and v and v[0] is not None else (int(v) if isinstance(v, (int, float)) else None)
        except Exception:
            pass
    reg_periods, secs, ot_secs = SPORT_CLOCK.get(sport, SPORT_CLOCK["nfl"])
    if period is None or period <= 0:  # pre-game: the full regulation, as venues/espn.py says
        return reg_periods * secs
    if period <= reg_periods:
        return (reg_periods - period) * secs + (clock or 0)
    if ot_secs is None:
        return None
    return min(clock or 0, ot_secs)


_KICKOFF_RE = re.compile(r"\bkicks?\b.*\byards? from\b|\bkickoff\b|\bonside\b", re.I)
_TRY_RE = re.compile(r"\bextra point\b|\btwo-point\b|\b2-pt\b|\bconversion\b", re.I)
_PAT_RE = re.compile(r"\bPAT\b")  # case-sensitive: college feeds carry full names ("Pat Bryant rush ...")
_TIMEOUT_RE = re.compile(r"\btimeout\b", re.I)
_KNEEL_RE = re.compile(r"\bkneels?\b|\bkneel\b", re.I)
_END_RE = re.compile(r"\bend (of )?(game|quarter|period|half|1st|2nd|3rd|4th)\b|\bend game\b", re.I)
_TWO_MINUTE_RE = re.compile(r"\btwo-minute warning\b", re.I)


def classify_play(text: Any, type_id: Any = None, type_text: Any = None) -> Optional[str]:
    """'kickoff' | 'try' | 'timeout' | 'kneel' | 'scrimmage' | 'end_period' | None.

    NFL type ids first (exact), then the play text (ESPN's college feed carries fewer ids).
    A two-minute warning is a stoppage charged to nobody ('timeout', as venues/espn.py
    classifies it); end of quarter/half is 'end_period'; neither enters the scrimmage headline."""
    if _espn_classify_play is not None:
        try:
            return _espn_classify_play(text, type_id)
        except Exception:
            pass
    tid = str(type_id) if type_id is not None else None
    if tid in NFL_TYPE_CLASSES:
        return NFL_TYPE_CLASSES[tid]
    s = f"{type_text or ''} {text or ''}".strip()
    if not s:
        return None
    if _TIMEOUT_RE.search(s) or _TWO_MINUTE_RE.search(s):
        return "timeout"
    if _END_RE.search(s):
        return "end_period"
    if _KICKOFF_RE.search(s):
        return "kickoff"
    if (_TRY_RE.search(s) or _PAT_RE.search(s)) and "touchdown" not in s.lower():
        return "try"  # ESPN appends the PAT to the TD play's text: that row is still the scrimmage play
    if _KNEEL_RE.search(s):
        return "kneel"
    return "scrimmage"


_TIMEOUT_TEXT_RE = re.compile(r"timeout\s*(?:#\s*(\d+))?\s*by\s+([A-Za-z&' .-]+?)(?:\s+at\b|\s*[.,]|$)", re.I)


def timeouts_timeline(plays: list[dict], home_abbr: Optional[str], away_abbr: Optional[str], sport: str = "nfl", home_id: Optional[str] = None, away_id: Optional[str] = None) -> list[tuple[Optional[int], Optional[int]]]:
    """Timeouts remaining (home, away) *after* each play, in play order.

    Allotments reset to 3 at each half and to the sport's OT allotment each overtime
    period. A timeout play named "Timeout #N by BUF" sets that team's remaining to
    ``allot - N`` (the feed's own count beats our arithmetic when earlier plays are
    missing); an unnumbered "Timeout by BUF" decrements; "Official Timeout" rows (TV,
    injury, review: same type id) change nothing. Other plays carry the running state, so
    a timeout row is scored on the post-timeout state (the policy P05 documents)."""
    if _espn_count_timeouts is not None:
        try:
            got = _espn_count_timeouts(plays, sport)
            if isinstance(got, list) and len(got) == len(plays) and all(isinstance(g, (tuple, list)) and len(g) == 2 for g in got):
                return [(_i(g[0]), _i(g[1])) for g in got]
        except Exception:
            pass
    per_half, per_ot = SPORT_TIMEOUTS.get(sport, SPORT_TIMEOUTS["nfl"])
    reg_periods = SPORT_CLOCK.get(sport, SPORT_CLOCK["nfl"])[0]
    out: list[tuple[Optional[int], Optional[int]]] = []
    remaining = {"home": per_half, "away": per_half}
    seen_period = None
    for p in plays:
        period = int(((p.get("period") or {}).get("number")) or 0)
        if period != seen_period:
            if period > reg_periods:
                remaining = {"home": per_ot, "away": per_ot}
            elif seen_period is None or period == 1 or period == reg_periods // 2 + 1:
                remaining = {"home": per_half, "away": per_half}
            seen_period = period
        text = str(p.get("text") or "")
        tid = str(((p.get("type") or {}).get("id")) or "")
        if tid == "21" or _TIMEOUT_RE.search(text):
            # Only "Timeout #N by TEAM" is a team timeout; "Official Timeout" / injury / TV stops
            # (same ESPN type id) change nothing, and the possession team is not the caller.
            m = _TIMEOUT_TEXT_RE.search(text)
            side = None
            if m:
                who = m.group(2).strip().upper()
                if home_abbr and who == str(home_abbr).upper():
                    side = "home"
                elif away_abbr and who == str(away_abbr).upper():
                    side = "away"
            if side in remaining:
                allot = per_ot if period > reg_periods else per_half
                n = int(m.group(1)) if m and m.group(1) else None
                remaining[side] = max(0, allot - n) if n is not None else max(0, remaining[side] - 1)
        out.append((remaining["home"], remaining["away"]))
    return out


@dataclass
class PlayRow:
    """Game state at the *start* of a play, stamped with the wall-clock time of the play,
    plus the state after it (for post-play scoring) and its play class."""

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
    play_type: Optional[str] = None
    play_type_id: Optional[str] = None
    scoring_play: bool = False
    play_class: Optional[str] = None
    home_timeouts: Optional[int] = None   # remaining *after* the play (post-timeout state on timeout rows)
    away_timeouts: Optional[int] = None
    possession_after: Optional[str] = None
    down_after: Optional[int] = None
    distance_after: Optional[int] = None
    yardline_100_after: Optional[int] = None
    gsr_after: Optional[int] = None
    overtime: bool = False
    synthetic: bool = False


def _td_side(row: PlayRow) -> Optional[str]:
    """Which side scored a touchdown on this play (score jump of 6+ on a scoring play)."""
    if not row.scoring_play and "touchdown" not in row.text.lower():
        return None
    if row.home_score_after - row.home_score >= 6:
        return "home"
    if row.away_score_after - row.away_score >= 6:
        return "away"
    return None


def synthetic_rows(row: PlayRow, sport: str = "nfl") -> list[PlayRow]:
    """ESPN folds the try into the touchdown play (the TD row's score-after already includes
    the PAT / 2-pt result). Emit a 'try_synth' row (score before the try, scoring team in
    possession) and a 'kickoff_pending_synth' row (score after the try, receiver in
    possession) at the TD's wall-clock so the try / kickoff rules have rows to score."""
    side = _td_side(row)
    if side is None:
        return []
    h6 = row.home_score + (6 if side == "home" else 0)
    a6 = row.away_score + (6 if side == "away" else 0)
    receiver = "away" if side == "home" else "home"
    base = dict(ts=row.ts, period=row.period, clock_seconds=row.clock_seconds, game_seconds_remaining=row.gsr_after if row.gsr_after is not None else row.game_seconds_remaining, espn_home_wp=None, play_type=row.play_type, play_type_id=row.play_type_id, scoring_play=False, home_timeouts=row.home_timeouts, away_timeouts=row.away_timeouts, gsr_after=row.gsr_after, overtime=row.overtime, synthetic=True, down=None, distance=None)
    try_row = PlayRow(play_id=f"{row.play_id}:try", home_score=h6, away_score=a6, possession=side, yardline_100=TRY_YARDLINE.get(sport, 15), home_score_after=row.home_score_after, away_score_after=row.away_score_after, text=f"[synthetic try after] {row.text}"[:120], play_class="try_synth", possession_after=receiver, down_after=None, distance_after=None, yardline_100_after=KICKOFF_YARDLINE, **base)
    ko_row = PlayRow(play_id=f"{row.play_id}:kickoff", home_score=row.home_score_after, away_score=row.away_score_after, possession=receiver, yardline_100=KICKOFF_YARDLINE, home_score_after=row.home_score_after, away_score_after=row.away_score_after, text=f"[synthetic kickoff pending] {row.text}"[:120], play_class="kickoff_pending_synth", possession_after=receiver, down_after=1, distance_after=10, yardline_100_after=75, **base)
    return [try_row, ko_row]


def _start_state(p: dict, home_id: str, away_id: str, home_abbr: Optional[str], away_abbr: Optional[str]) -> tuple[Optional[str], Optional[int], Optional[int], Optional[int], Optional[str]]:
    """(possession, down, distance, yardline_100, play_class) at the *start* of an ESPN play.
    Kickoff rows carry the receiver (ESPN's start team is the kicker) at the mirrored
    yardline; dead-ball rows whose start block has no field position give yardline None."""
    st = p.get("start") or {}
    poss = _possession_side(((st.get("team") or {}).get("id")), home_id, away_id)
    yl = yardline_100_from(st.get("yardLine"), poss, st.get("possessionText"), home_abbr, away_abbr)
    if yl is None and st.get("yardsToEndzone") not in (None, 0):
        yl = int(st["yardsToEndzone"])
    ptype = p.get("type") or {}
    cls = classify_play(p.get("text"), ptype.get("id"), ptype.get("text"))
    down, dist = _i(st.get("down")), _i(st.get("distance"))
    if cls == "kickoff" and poss in ("home", "away"):
        poss = "away" if poss == "home" else "home"
        yl = (100 - yl) if yl is not None else KICKOFF_YARDLINE
        down, dist = None, None
    return poss, (down if down and down > 0 else None), dist, yl, cls


def _dead_end(p: dict) -> bool:
    """True when ESPN's ``end`` block is not a snap-able state: scoring plays end with
    ``{down: -1, yardLine: 0, yardsToEndzone: 0, team: scorer}`` (the scorer "on the goal
    line"), which is not the state the next snap sees. The after-state of such a play is
    the next play's start (with the kickoff flip) or the kickoff-pending state."""
    en = p.get("end") or {}
    if not en:
        return True
    d = _i(en.get("down"))
    return bool(p.get("scoringPlay")) or (d is not None and d < 0) or _i(en.get("yardsToEndzone")) == 0


def _kickoff_pending(row_home_before: int, row_away_before: int, home_after: int, away_after: int) -> tuple[Optional[str], Optional[int], Optional[int], Optional[int]]:
    """Post-score state when no next play is available: the scored-against team receives
    (a safety is the exception: the scoring team receives the free kick)."""
    dh, da = home_after - row_home_before, away_after - row_away_before
    scorer = "home" if dh > 0 else ("away" if da > 0 else None)
    if scorer is None:
        return None, None, None, None
    jump = dh if scorer == "home" else da
    receiver = scorer if jump == 2 else ("away" if scorer == "home" else "home")
    return receiver, None, None, KICKOFF_YARDLINE


def espn_timeline(summary: dict, sport: str = "nfl", synthetic: bool = True) -> tuple[list[PlayRow], dict[str, Any]]:
    """Per-play rows from a finished (or in-progress) ESPN summary, plus game metadata
    (home/away codes and ids, final score, kickoff time). Kickoff rows flip possession to
    the receiver; college overtime rows are kept with ``game_seconds_remaining=None`` and
    ``play_class='ot'``; ``synthetic`` adds the try / kickoff-pending rows after each TD.
    The after-state of a scoring play is the state the next snap sees (the receiver at the
    kickoff spot), never ESPN's ``end`` block for a score (the scorer at yardline 0)."""
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
        "sport": sport,
    }
    plays: list[dict] = []
    for drive in (summary.get("drives") or {}).get("previous", []) or []:
        plays.extend(drive.get("plays") or [])
    cur = (summary.get("drives") or {}).get("current")
    if cur:
        plays.extend(cur.get("plays") or [])
    plays = [p for p in plays if p.get("wallclock") and parse_iso(p["wallclock"]) is not None]
    plays.sort(key=lambda p: (parse_iso(p["wallclock"]).timestamp(), str(p.get("id"))))
    # ESPN win probability is one entry per play in play order (a pre-game entry first).
    wp = summary.get("winprobability") or []
    wp_by_id = {str(w.get("playId")): _f(w.get("homeWinPercentage")) for w in wp}
    reg_periods = SPORT_CLOCK.get(sport, SPORT_CLOCK["nfl"])[0]
    tos = timeouts_timeline(plays, meta["home"], meta["away"], sport, home_id, away_id)
    rows: list[PlayRow] = []
    prev_home, prev_away = 0, 0
    for i, p in enumerate(plays):
        t = parse_iso(p["wallclock"]).timestamp()
        period = int(((p.get("period") or {}).get("number")) or 0)
        clk = parse_clock(((p.get("clock") or {}).get("displayValue")))
        gsr = period_clock_to_gsr(sport, period, clk)
        overtime = period > reg_periods
        en = p.get("end") or {}
        ptype = p.get("type") or {}
        poss, down, dist, yl, cls = _start_state(p, home_id, away_id, meta["home"], meta["away"])
        if overtime and gsr is None and cls not in ("end_period", "timeout"):
            cls = "ot"  # untimed college OT possession: no state the clock model can score
        h_after, a_after = int(p.get("homeScore") or 0), int(p.get("awayScore") or 0)
        poss_after = _possession_side(((en.get("team") or {}).get("id")), home_id, away_id)
        yl_after = yardline_100_from(en.get("yardLine"), poss_after, en.get("possessionText"), meta["home"], meta["away"]) if en else None
        if yl_after is None and en.get("yardsToEndzone") not in (None, 0):
            yl_after = int(en["yardsToEndzone"])
        d_after = _i(en.get("down"))
        down_after, dist_after = (d_after if d_after and d_after > 0 else None), _i(en.get("distance"))
        nxt = plays[i + 1] if i + 1 < len(plays) else None
        if nxt is not None:
            n_period = int(((nxt.get("period") or {}).get("number")) or 0)
            gsr_after = period_clock_to_gsr(sport, n_period, parse_clock(((nxt.get("clock") or {}).get("displayValue"))))
        else:
            gsr_after = 0 if meta["status"] == "post" and not (overtime and gsr is None) else gsr
        if _dead_end(p):
            # Scoring plays (and any end block with no snap-able field position): the state the
            # next snap sees is the next live play's start — after a TD that is the kickoff row,
            # whose flip yields (receiver, no down, KICKOFF_YARDLINE) — else kickoff-pending.
            nstate = None
            for q in plays[i + 1:i + 4]:
                q_poss, q_down, q_dist, q_yl, q_cls = _start_state(q, home_id, away_id, meta["home"], meta["away"])
                if q_cls in ("timeout", "end_period") or q_yl is None:
                    continue
                nstate = (q_poss, q_down, q_dist, q_yl)
                break
            if nstate is None and (h_after != prev_home or a_after != prev_away):
                nstate = _kickoff_pending(prev_home, prev_away, h_after, a_after)
            if nstate is not None and nstate[0] is not None:
                poss_after, down_after, dist_after, yl_after = nstate
        if poss_after is None and nxt is not None:
            poss_after = _start_state(nxt, home_id, away_id, meta["home"], meta["away"])[0]
        ehw = wp_by_id.get(str(p.get("id")))
        if ehw is None and i + 1 < len(wp):
            ehw = _f(wp[i + 1].get("homeWinPercentage"))  # index alignment (entry 0 = pre-game)
        h_to, a_to = tos[i] if i < len(tos) else (None, None)
        row = PlayRow(play_id=str(p.get("id")), ts=t, period=period, clock_seconds=clk, game_seconds_remaining=gsr, home_score=prev_home, away_score=prev_away, possession=poss, down=down, distance=dist, yardline_100=yl, home_score_after=h_after, away_score_after=a_after, espn_home_wp=ehw, text=str(p.get("text") or "")[:120],
                      play_type=ptype.get("text"), play_type_id=str(ptype.get("id")) if ptype.get("id") is not None else None, scoring_play=bool(p.get("scoringPlay")), play_class=cls, home_timeouts=h_to, away_timeouts=a_to,
                      possession_after=poss_after, down_after=down_after, distance_after=dist_after, yardline_100_after=yl_after, gsr_after=gsr_after, overtime=overtime)
        rows.append(row)
        if synthetic and cls not in ("ot",):
            rows.extend(synthetic_rows(row, sport))
        prev_home, prev_away = h_after, a_after
    return rows, meta


def bar_at(bars: list[Bar], ts: float, mode: str = "kalshi", max_gap: float = 900.0, anchor_ts: Optional[float] = None) -> Optional[Bar]:
    """The bar that describes the market at wall-clock ``ts``.

    * ``"kalshi"`` (after): the first candle ending at or after ``ts`` — the market has
      seen the play (upper bound on its information).
    * ``"kalshi_before"``: the last candle ending at or before ``ts`` — strictly what the
      market knew before the play (lower bound).
    * ``"kalshi_prev"``: the last candle ending at or before ``anchor_ts`` (the previous
      play's time; ``ts - 60`` when no anchor is given).
    * ``"start"``: Robinhood / Polymarket bars stamped at their start — the last bar at or
      before ``ts``.

    None when the chosen bar is further than ``max_gap`` seconds from ``ts`` (for the
    ``before`` modes the bar must also not be *after* ``ts``: no fallback forward)."""
    if not bars:
        return None
    if mode == "kalshi":
        cand = [b for b in bars if b.ts >= ts]
        b = cand[0] if cand else bars[-1]
        return b if abs(b.ts - ts) <= max_gap else None
    if mode in ("kalshi_before", "kalshi_prev"):
        ref = ts if mode == "kalshi_before" else (anchor_ts if anchor_ts is not None else ts - 60.0)
        cand = [b for b in bars if b.ts <= ref]
        if not cand:
            return None
        b = cand[-1]
        return b if abs(ref - b.ts) <= max_gap else None
    cand = [b for b in bars if b.ts <= ts]
    b = cand[-1] if cand else bars[0]
    return b if abs(b.ts - ts) <= max_gap else None

"""ESPN public site API — live NFL game state (score, clock, down/distance, win probability).

Unofficial and undocumented: ESPN serves these JSON endpoints to its own web front-end and
may change them without notice. They need no key and no browser ``Origin`` tricks. Nothing
here is a price feed; the engine uses the game state to (a) tell staleness from edge when
venues disagree in play (AGENTS.md rule 6) and (b) feed the in-play fair-value model.

Endpoints (both public JSON, verified 2026-09-18):

* ``GET /apis/site/v2/sports/football/nfl/scoreboard[?dates=YYYYMMDD]`` — every game of the
  current (or given) week in one call. Live games carry ``competitions[0].situation`` with
  ``down``, ``distance``, ``yardLine``, ``possession`` (team id), ``possessionText``,
  ``downDistanceText``, ``homeTimeouts`` / ``awayTimeouts``, ``lastPlay``.
* ``GET /apis/site/v2/sports/football/nfl/summary?event=<id>`` — one game: ``winprobability``
  (ESPN's own model, one entry per play), ``pickcenter`` (DraftKings spread / total /
  moneyline), ``header`` (status + competitors), ``drives`` (play-by-play).

Field position convention (verified on 342 plays of DET@BUF 401872932): ``yardLine`` is the
absolute position measured from the **home** team's goal line (0 = home goal line, 100 =
away goal line). So yards to the opponent's goal line for the team in possession is
``100 - yardLine`` when the home team has the ball and ``yardLine`` when the away team does.

Feed hardening. The feed is known to lie in three ways — the score lands before ``lastPlay``
(so ESPN's WP row belongs to the previous play), a reviewed play takes points back, and a
'final' is posted before a review flips the game back to live — so every state that leaves
``ESPNFeed`` passes through a per-event ``StateGuard`` (``suspect`` / ``review_pending`` /
``final_soft`` on ``GameState``). The clock and play-classification helpers
(``period_clock_to_gsr``, ``classify_play``, ``count_timeouts`` / ``timeouts_timeline``) are
pure so the replay (``history.py``) imports them instead of keeping its own copies. NFL OT
takes its length from the summary's ``format.overtime.clock`` (900 in the playoffs); college
OT is untimed, hence ``overtime_sentinel`` and ``game_seconds_remaining=None``.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date as _date, datetime
from typing import Any, Iterable, Optional

from ..matching.normalize import et_date, nfl_event_key, parse_iso, team_event_key
from ..matching.teams import nfl_team_code, team_code
from .http import HttpClient

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
SPORT_BASE_URL = {
    "nfl": BASE_URL,
    "ncaaf": "https://site.api.espn.com/apis/site/v2/sports/football/college-football",
    "nba": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba",
    "nhl": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl",
}
FOOTBALL = ("nfl", "ncaaf")  # sports the win-probability model and the situation parser apply to
SPORT_SCOREBOARD_PARAMS = {"ncaaf": {"groups": 80, "limit": 300}}  # FBS only; the default page is 25 games

REGULATION_PERIODS = 4
REGULATION_PERIOD_SECONDS = 900
OVERTIME_PERIOD_SECONDS = 600  # regular-season OT is one 10-minute period (ESPN ``format.overtime.clock``)

# Per-sport clock tables. ``ot`` is the timed overtime period length; ``None`` means the sport's
# overtime is untimed (college football: alternating possessions from the 25), so no
# game-seconds-remaining exists and callers get the ``overtime_sentinel`` instead. The NFL
# table is the regular-season default; a summary's ``format.overtime.clock`` (900 in the
# playoffs) overrides it via ``fmt``. NBA / NHL numbers are the league rule books, not ESPN.
SPORT_CLOCK: dict[str, dict[str, Optional[int]]] = {
    "nfl": {"periods": 4, "period": 900, "ot": 600},
    "ncaaf": {"periods": 4, "period": 900, "ot": None},
    "nba": {"periods": 4, "period": 720, "ot": 300},
    "nhl": {"periods": 3, "period": 1200, "ot": 300},
}

_STATE_MAP = {"pre": "pre", "in": "live", "post": "final"}


def sport_clock(sport: str, fmt: Optional[dict] = None) -> dict[str, Optional[int]]:
    """The clock table for ``sport`` with an ESPN ``format`` block (summary top level or the
    scoreboard competition) overriding what it carries: ``regulation.periods`` /
    ``regulation.clock`` / ``overtime.clock``. Unknown sports fall back to the NFL table."""
    base = dict(SPORT_CLOCK.get(sport) or SPORT_CLOCK["nfl"])
    if fmt:
        reg = fmt.get("regulation") or {}
        ot = fmt.get("overtime") or {}
        if _int(reg.get("periods")):
            base["periods"] = _int(reg.get("periods"))
        if _int(reg.get("clock")):
            base["period"] = _int(reg.get("clock"))
        if base["ot"] is not None and _int(ot.get("clock")):
            base["ot"] = _int(ot.get("clock"))
    return base


def period_clock_to_gsr(sport: str, period: int, clock: Optional[int], fmt: Optional[dict] = None) -> tuple[Optional[int], bool]:
    """Game seconds remaining from (period, clock) -> ``(gsr, overtime_sentinel)``.

    Pure and I/O-free so ``history.py`` (replay) and the live feed share one clock: NFL is 4 x
    900 with a timed OT (600 regular season, ``fmt.overtime.clock`` = 900 in the playoffs, any
    number of OT periods with only the current one counted); NBA 4 x 720 + 300; NHL 3 x 1200 +
    300; college football regulation is 4 x 900 but its overtime is untimed, so period >= 5
    returns ``(None, True)`` — the sentinel tells the WP model it has no clock to work with.
    period <= 0 (pre-game) -> the full regulation; an unknown clock in a real period -> None.
    """
    tbl = sport_clock(sport, fmt)
    periods, plen, ot = int(tbl["periods"] or 4), int(tbl["period"] or 900), tbl["ot"]
    if period is None or period <= 0:
        return periods * plen, False
    if period > periods and ot is None:
        return None, True
    if clock is None:
        return None, False
    if period <= periods:
        return (periods - period) * plen + min(int(clock), plen), False
    return min(int(clock), int(ot)), False  # overtime: only this period is left


# ---- small parsers ------------------------------------------------------------------------

def parse_clock(display: Any) -> Optional[int]:
    """'12:34' -> 754, '0:00' -> 0, '1:05.3' -> 65, 754.0 -> 754. Unparseable -> None."""
    if display is None:
        return None
    if isinstance(display, (int, float)):
        return int(display)
    s = str(display).strip()
    m = re.match(r"^(\d+):(\d{1,2})(?:\.\d+)?$", s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.match(r"^(\d+)(?:\.\d+)?$", s)
    if m:
        return int(m.group(1))
    return None


def game_seconds_remaining(status: str, period: int, clock_seconds: Optional[int], sport: str = "nfl", fmt: Optional[dict] = None) -> Optional[int]:
    """Seconds left in the game per ``period_clock_to_gsr`` (NFL: 4 x 900 + a 600 s OT).

    Pre-game -> full regulation; final -> 0; unknown clock during a live game -> None; college
    overtime -> None (untimed; see ``GameState.overtime_sentinel``).
    """
    if status == "pre":
        return period_clock_to_gsr(sport, 0, None, fmt)[0]
    if status == "final":
        return 0
    if clock_seconds is None and period <= 0:
        return None
    return period_clock_to_gsr(sport, period, clock_seconds, fmt)[0]


def map_status(status_obj: Any) -> str:
    """ESPN ``status.type`` -> 'pre' | 'live' | 'final' | 'other'.

    ``state`` is the reliable discriminator ('pre'/'in'/'post'); ``completed`` separates a
    final from a postponed / cancelled game (both are state 'post').
    """
    t = (status_obj or {}).get("type") or {}
    name = str(t.get("name") or "").upper()
    state = str(t.get("state") or "").lower()
    if state == "post":
        if t.get("completed") or name.startswith("STATUS_FINAL"):
            return "final"
        return "other"
    if state in _STATE_MAP:
        return _STATE_MAP[state]
    if name in ("STATUS_SCHEDULED",):
        return "pre"
    if name in ("STATUS_IN_PROGRESS", "STATUS_HALFTIME", "STATUS_END_PERIOD"):
        return "live"
    if name.startswith("STATUS_FINAL"):
        return "final"
    return "other"


def _num(x: Any) -> Optional[float]:
    if x is None or x == "":
        return None
    try:
        return float(str(x).replace("+", "").strip())
    except ValueError:
        return None


def _int(x: Any) -> Optional[int]:
    v = _num(x)
    return int(v) if v is not None else None


def normalize_home_spread(odds: Optional[dict], home_code: Optional[str], sport: str = "nfl") -> Optional[float]:
    """Home-team spread (negative = home favoured) from an ESPN odds / pickcenter block.

    Priority: ``pointSpread.home.close.line`` ('-4.5' / '+2.5', explicitly home-relative) ->
    ``details`` ('CHI -4.5', favourite-relative; flip the sign when the named team is not the
    home team) -> numeric ``spread`` (observed to be home-relative on the scoreboard).
    """
    if not odds:
        return None
    ps = odds.get("pointSpread") or {}
    home_close = ((ps.get("home") or {}).get("close") or {}).get("line")
    v = _num(home_close)
    if v is not None:
        return v
    details = str(odds.get("details") or "").strip()
    m = re.match(r"^(.+?)\s+([+-]?\d+(?:\.\d+)?)$", details)
    if m:
        team = team_code(sport, m.group(1))
        line = float(m.group(2))
        if team and home_code:
            return line if team == home_code else -line
    if details.upper() in ("EVEN", "PK", "PICK"):
        return 0.0
    return _num(odds.get("spread"))


def _total_from(odds: Optional[dict]) -> Optional[float]:
    if not odds:
        return None
    v = _num(odds.get("overUnder"))
    if v is not None:
        return v
    over = (((odds.get("total") or {}).get("over") or {}).get("close") or {}).get("line")
    if over:
        return _num(str(over).lstrip("oOuU"))
    return None


def _american(x: Any) -> Optional[int]:
    """'-245' / '+200' / -245.0 / 'EVEN' -> American moneyline as an int (EVEN = +100)."""
    if x is None or x == "":
        return None
    s = str(x).strip().upper()
    if s in ("EVEN", "EV", "PK"):
        return 100
    v = _num(s)
    return int(v) if v is not None and v != 0 else None


def parse_pickcenter_moneylines(pick: Optional[dict]) -> dict[str, Optional[int]]:
    """Pre-game sportsbook moneylines from a ``pickcenter`` row -> ``{home, away, home_open,
    away_open}`` (American odds). Close is ``moneyline.<side>.close.odds`` with
    ``<side>TeamOdds.moneyLine`` as the fallback; open is ``moneyline.<side>.open.odds``. The
    closing line is the pre-game anchor the live spread fallback and the CLV metric use, so it
    is kept separate from the scoreboard's in-play odds block."""
    out: dict[str, Optional[int]] = {"home": None, "away": None, "home_open": None, "away_open": None}
    if not pick:
        return out
    ml = pick.get("moneyline") or {}
    for side in ("home", "away"):
        block = ml.get(side) or {}
        close = _american((block.get("close") or {}).get("odds"))
        if close is None:
            close = _american((pick.get(f"{side}TeamOdds") or {}).get("moneyLine"))
        out[side] = close
        out[f"{side}_open"] = _american((block.get("open") or {}).get("odds"))
    return out


# ---- play classification (pure; shared with history.py) ------------------------------------

# NFL play-type ids as ESPN's play-by-play emits them. Verified on the recorded DET@BUF summary:
# 3, 5, 7, 8, 21, 24, 53, 66, 67, 75. The others follow ESPN's public play-type table and are
# only a hint: the text patterns below decide when the two disagree, because college football
# reuses ids loosely and the scoreboard ``lastPlay.type`` is sometimes missing.
NFL_PLAY_TYPE_CLASS: dict[str, str] = {
    "53": "kickoff", "12": "kickoff", "32": "kickoff", "40": "kickoff",
    "21": "timeout", "75": "timeout",  # 75 = two-minute warning: a dead-ball stoppage with no team charged
    "2": "end_period", "65": "end_period", "66": "end_period",
    "3": "scrimmage", "5": "scrimmage", "7": "scrimmage", "8": "scrimmage", "9": "scrimmage", "24": "scrimmage",
    "26": "scrimmage", "29": "scrimmage", "52": "scrimmage", "59": "scrimmage", "60": "scrimmage", "67": "scrimmage", "68": "scrimmage",
}
REVERSAL_TYPE_IDS = frozenset({"74"})  # NFL "play reversed / nullified" marker (per the feed-hardening plan; text-guarded too)
_RE_KICKOFF = re.compile(r"\bkicks?\s+(?:onside\s+)?\d+\s+yards?\b|\bkicks?\s+off\b|\bkickoff\b|\bonside kick\b", re.I)
_RE_TRY = re.compile(r"\bextra point\b|\btwo[- ]point\b|\b2[- ]?pt\b|\bkick attempt\b|\bconversion attempt\b", re.I)
_RE_PAT = re.compile(r"\bPAT\b")  # case-sensitive on purpose: "Pat Bryant" is a receiver, "PAT" is the try
_RE_TD = re.compile(r"\btouchdown\b", re.I)
_RE_TIMEOUT = re.compile(r"^\s*(?:\(?[\d:]+\)?\s*)?timeout\b|\btwo[- ]minute warning\b|\bofficial timeout\b|\binjury timeout\b", re.I)
_RE_TEAM_TIMEOUT = re.compile(r"\b[Tt]imeout\s+(?:#?\s*\d+\s+by\s+)?([A-Z][A-Za-z&. ]{1,30}?)(?:\s+at\b|\s*[.,]|\s*$)")  # NFL "Timeout #1 by BUF at 01:48." and college "Timeout ORE, clock 01:57"
_RE_KNEEL = re.compile(r"\bkneels?\b|\btakes a knee\b|\bkneel[- ]?down\b", re.I)
_RE_END = re.compile(r"^\s*end\s+(?:of\s+)?(?:game|half|quarter|period|regulation|\d(?:st|nd|rd|th))\b|\bend of (?:game|half|quarter|period|regulation)\b|^\s*end game\b", re.I)
REVERSAL_RE = re.compile(r"\bREVERSED\b|\bOVERTURNED\b|\bNo Play\b|\bNULLIFIED\b|\bno goal\b", re.I)
REVIEW_RE = re.compile(r"\breview(?:ed|ing)?\b|\bchallenge[ds]?\b|\bofficial timeout\b|\bunder review\b", re.I)


def classify_play(text: Any, type_id: Any = None) -> Optional[str]:
    """'kickoff' | 'try' | 'timeout' | 'kneel' | 'scrimmage' | 'end_period' | None.

    Text first, type id second: a touchdown play that ends with "J.Bates extra point is GOOD"
    is a scrimmage play (ESPN folds the NFL try into the TD play), a standalone "extra point"
    / "TWO-POINT CONVERSION ATTEMPT" play is the try, "1st & Goal at BUF 2" is ordinary
    scrimmage, "Timeout #1 by BUF" and the two-minute warning are stoppages, kneel-downs are
    their own class so the replay can price the kneel-out rule, and end-of-period markers are
    not plays at all. Empty text with an unknown id -> None.
    """
    s = str(text or "").strip()
    tid = str(type_id).strip() if type_id not in (None, "") else ""
    if s:
        if _RE_END.search(s):
            return "end_period"
        if _RE_TIMEOUT.search(s) and not _RE_KICKOFF.search(s):
            return "timeout"
        if _RE_KICKOFF.search(s) and not _RE_TD.search(s[: s.lower().find("kick")]):
            return "kickoff"
        m = _RE_TRY.search(s) or _RE_PAT.search(s)
        if m:
            td = _RE_TD.search(s)
            if td is None or td.start() > m.start():
                return "try"
            return "scrimmage"  # the try text trails a touchdown description
        if _RE_KNEEL.search(s):
            return "kneel"
    if tid in NFL_PLAY_TYPE_CLASS:
        return NFL_PLAY_TYPE_CLASS[tid]
    if tid in REVERSAL_TYPE_IDS:
        return "scrimmage"
    return "scrimmage" if s else None


def _timeout_caller(text: str, home_abbr: Optional[str], away_abbr: Optional[str], sport: str = "nfl") -> Optional[str]:
    """'home' | 'away' | None for a "Timeout #N by DET at 01:33." style text."""
    m = _RE_TEAM_TIMEOUT.search(text or "")
    if not m:
        return None
    name = m.group(1).strip()
    for side, abbr in (("home", home_abbr), ("away", away_abbr)):
        if abbr and name.upper() == str(abbr).upper():
            return side
    code = team_code(sport, name)
    if code:
        for side, abbr in (("home", home_abbr), ("away", away_abbr)):
            if abbr and team_code(sport, abbr) == code:
                return side
    return None


def timeouts_timeline(plays: Iterable[dict], sport: str = "nfl", home_abbr: Optional[str] = None, away_abbr: Optional[str] = None) -> list[tuple[int, int]]:
    """Per play, ``(home_remaining, away_remaining)`` *before* that play, from the timeout
    texts alone ("Timeout #N by TEAM"): football gets 3 per half and 2 per overtime period
    (NFL) or 1 (college); NBA 7 for the game and 2 per OT; NHL 1 for the game. Official
    stoppages, the two-minute warning and unattributable timeouts charge nobody. The count
    floors at 0 so a mis-attributed text cannot go negative."""
    is_fb = sport in FOOTBALL
    rules: dict[str, tuple[int, Optional[int], bool]] = {"nfl": (3, 2, True), "ncaaf": (3, 1, True), "nba": (7, 2, False), "nhl": (1, None, False)}  # (start, per OT period or carry over, reset at the half)
    start, per_ot, per_half = rules.get(sport, rules["nfl"])
    periods = int(SPORT_CLOCK.get(sport, SPORT_CLOCK["nfl"])["periods"] or 4)
    half = periods // 2
    out: list[tuple[int, int]] = []
    home = away = start
    seen_period = 0
    for p in plays:
        period = _int((p.get("period") or {}).get("number") if isinstance(p.get("period"), dict) else p.get("period")) or 0
        if period != seen_period:
            if is_fb and per_half and half < period <= periods and seen_period <= half:
                home = away = start  # second half reset
            elif period > periods and per_ot is not None:
                home = away = per_ot  # each OT period starts fresh (NHL carries the one timeout over)
            seen_period = period
        out.append((home, away))
        if classify_play(p.get("text"), (p.get("type") or {}).get("id") if isinstance(p.get("type"), dict) else None) == "timeout":
            side = _timeout_caller(str(p.get("text") or ""), home_abbr, away_abbr, sport)
            if side == "home":
                home = max(0, home - 1)
            elif side == "away":
                away = max(0, away - 1)
    return out


def count_timeouts(plays: Iterable[dict], sport: str = "nfl", home_abbr: Optional[str] = None, away_abbr: Optional[str] = None) -> dict[str, int]:
    """Timeouts remaining *after* the last play -> ``{"home": n, "away": n}`` (see
    ``timeouts_timeline`` for the rules). Used as the fallback when the ESPN ``situation``
    block lacks ``homeTimeouts`` / ``awayTimeouts``; never overrides a present value."""
    plays = list(plays)
    tl = timeouts_timeline(plays, sport, home_abbr, away_abbr)
    if not plays:
        start = {"nfl": 3, "ncaaf": 3, "nba": 7, "nhl": 1}.get(sport, 3)
        return {"home": start, "away": start}
    home, away = tl[-1]
    last = plays[-1]
    if classify_play(last.get("text"), (last.get("type") or {}).get("id") if isinstance(last.get("type"), dict) else None) == "timeout":
        side = _timeout_caller(str(last.get("text") or ""), home_abbr, away_abbr, sport)
        if side == "home":
            home = max(0, home - 1)
        elif side == "away":
            away = max(0, away - 1)
    return {"home": home, "away": away}


def summary_plays(summary: dict) -> list[dict]:
    """Every play of a summary's drives in order (previous drives, then the current one)."""
    drives = summary.get("drives") or {}
    plays: list[dict] = []
    for d in drives.get("previous") or []:
        plays.extend(d.get("plays") or [])
    cur = drives.get("current")
    if cur:
        plays.extend(cur.get("plays") or [])
    return plays


def _coerce_date(d: Any) -> Optional[str]:
    """None | 'YYYY-MM-DD' | 'YYYYMMDD' | date | datetime -> 'YYYYMMDD' for ``?dates=``."""
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.strftime("%Y%m%d")
    if isinstance(d, _date):
        return d.strftime("%Y%m%d")
    s = str(d).strip().replace("-", "")
    return s if re.fullmatch(r"\d{8}", s) else None


# ---- state ------------------------------------------------------------------------------

@dataclass
class GameState:
    event_id: str
    home: Optional[str]
    away: Optional[str]
    home_score: int = 0
    away_score: int = 0
    status: str = "other"  # 'pre' | 'live' | 'final' | 'other'
    period: int = 0
    clock_seconds_remaining_in_period: Optional[int] = None
    game_seconds_remaining: Optional[int] = None
    possession: Optional[str] = None  # 'home' | 'away' | None
    down: Optional[int] = None
    distance: Optional[int] = None
    yardline_100: Optional[int] = None  # yards to the opponent goal line for the team in possession
    home_timeouts: Optional[int] = None
    away_timeouts: Optional[int] = None
    espn_home_wp: Optional[float] = None
    espn_wp_series: list[dict] = field(default_factory=list)  # [{play_id, home_wp, tie}] in play order
    vegas_spread_home: Optional[float] = None
    vegas_total: Optional[float] = None
    odds_provider: Optional[str] = None
    start_time: Optional[datetime] = None
    event_key: Optional[str] = None
    home_team_id: Optional[str] = None
    away_team_id: Optional[str] = None
    status_name: Optional[str] = None
    status_detail: Optional[str] = None
    last_play_text: Optional[str] = None
    is_red_zone: Optional[bool] = None
    receive_2h_ko_home: Optional[bool] = None  # home kicked off to open the game -> receives the 2H kickoff
    enriched: bool = False
    sport: str = "nfl"
    # -- feed-hardening fields (all optional; as_dict carries them for the recorder) ---------
    last_play_id: Optional[str] = None          # situation.lastPlay.id; negative provisional ids are ignored
    last_play_type_id: Optional[str] = None     # lastPlay.type.id (NFL table in NFL_PLAY_TYPE_CLASS)
    last_play_type: Optional[str] = None        # lastPlay.type.text ('Rush', 'Timeout', ...)
    play_class: Optional[str] = None            # classify_play(last_play_text, last_play_type_id)
    overtime: bool = False                      # period beyond regulation
    overtime_sentinel: bool = False             # untimed overtime (college football): no game clock exists
    regulation_period_seconds: Optional[int] = None
    ot_seconds: Optional[int] = None            # timed OT period length (None when untimed)
    espn_tie: Optional[float] = None            # tiePercentage of the latest WP row
    suspect: bool = False                       # StateGuard doubts this snapshot (held decrease, score before lastPlay)
    review_pending: bool = False                # a review / challenge / official timeout may still change the score
    final_soft: bool = False                    # 'final' seen < 300 s ago: a final -> live flip is still expected
    state_changed_ts: Optional[float] = None    # wall clock of the last score / period / clock / possession / down change
    sportsbook_ml_home: Optional[int] = None    # pickcenter closing moneylines (American), the pre-game anchor
    sportsbook_ml_away: Optional[int] = None
    sportsbook_ml_home_open: Optional[int] = None
    sportsbook_ml_away_open: Optional[int] = None
    pickcenter_spread: Optional[float] = None   # home spread from the summary's pickcenter only (vegas_spread_home may come from the scoreboard)
    season: Optional[int] = None

    @property
    def score_diff_home(self) -> int:
        return self.home_score - self.away_score

    @property
    def et_date(self) -> Optional[str]:
        return et_date(self.start_time)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["start_time"] = self.start_time.isoformat() if self.start_time else None
        d["score_diff_home"] = self.score_diff_home
        return d


# ---- parsing ------------------------------------------------------------------------------

def _team_code(team: dict, sport: str = "nfl") -> Optional[str]:
    t = team or {}
    return team_code(sport, t.get("abbreviation")) or team_code(sport, t.get("displayName")) or team_code(sport, t.get("location")) or team_code(sport, t.get("name"))


def _possession_side(team_id: Any, home_id: Optional[str], away_id: Optional[str]) -> Optional[str]:
    if team_id is None or team_id == "":
        return None
    tid = str(team_id)
    if home_id and tid == home_id:
        return "home"
    if away_id and tid == away_id:
        return "away"
    return None


def yardline_100_from(yard_line: Any, possession: Optional[str], possession_text: Any = None, home: Optional[str] = None, away: Optional[str] = None, sport: str = "nfl") -> Optional[int]:
    """Yards to the opponent goal line for the team with the ball.

    ``yard_line`` is ESPN's absolute field position (0 = home goal line, see module doc). When
    it is missing, fall back to ``possession_text`` ('DET 34' / 'BUF 41' / '50'): a team's own
    side means 100 - n, the opponent's side means n.
    """
    if possession not in ("home", "away"):
        return None
    yl = _int(yard_line)
    if yl is not None and 0 <= yl <= 100:
        return 100 - yl if possession == "home" else yl
    txt = str(possession_text or "").strip()
    if not txt:
        return None
    if txt == "50":
        return 50
    m = re.match(r"^([A-Za-z.]+)\s+(\d{1,2})$", txt)
    if not m:
        return None
    side = team_code(sport, m.group(1))
    n = int(m.group(2))
    own = home if possession == "home" else away
    if side is None or own is None:
        return None
    return 100 - n if side == own else n


def parse_situation(sit: Optional[dict], state: GameState) -> None:
    """Fill possession / down / distance / yardline_100 / timeouts from a ``situation`` block."""
    if not sit:
        return
    poss = sit.get("possession")
    if isinstance(poss, dict):
        poss = poss.get("id")
    state.possession = _possession_side(poss, state.home_team_id, state.away_team_id)
    down = _int(sit.get("down"))
    state.down = down if down and down > 0 else None
    dist = _int(sit.get("distance"))
    state.distance = dist if state.down is not None else None
    state.yardline_100 = yardline_100_from(sit.get("yardLine"), state.possession, sit.get("possessionText"), state.home, state.away, state.sport)
    if sit.get("homeTimeouts") is not None:
        state.home_timeouts = _int(sit.get("homeTimeouts"))
    if sit.get("awayTimeouts") is not None:
        state.away_timeouts = _int(sit.get("awayTimeouts"))
    if sit.get("isRedZone") is not None:
        state.is_red_zone = bool(sit.get("isRedZone"))
    lp = sit.get("lastPlay") or {}
    if lp.get("text"):
        state.last_play_text = lp.get("text")
    apply_last_play_meta(state, lp)
    prob = lp.get("probability") or {}
    if prob.get("homeWinPercentage") is not None and state.espn_home_wp is None:
        state.espn_home_wp = _num(prob.get("homeWinPercentage"))
        if prob.get("tiePercentage") is not None:
            state.espn_tie = _num(prob.get("tiePercentage"))


def _play_id(raw: Any) -> Optional[str]:
    """ESPN stamps provisional plays with negative ids before the official one lands; they are
    not stable across polls, so they never count as 'the play advanced'."""
    if raw is None or raw == "":
        return None
    s = str(raw).strip()
    if s.startswith("-"):
        return None
    return s or None


def apply_last_play_meta(state: GameState, play: Optional[dict]) -> None:
    """Fill last_play_id / type / class from a ``lastPlay`` (scoreboard) or drive play (summary)."""
    if not play:
        return
    pid = _play_id(play.get("id"))
    if pid:
        state.last_play_id = pid
    t = play.get("type") or {}
    if isinstance(t, dict):
        if t.get("id") not in (None, ""):
            state.last_play_type_id = str(t.get("id"))
        if t.get("text"):
            state.last_play_type = str(t.get("text"))
    state.play_class = classify_play(state.last_play_text, state.last_play_type_id)


def apply_clock_meta(state: GameState, fmt: Optional[dict] = None) -> None:
    """Overtime flags and the per-sport period lengths, recomputing game_seconds_remaining."""
    tbl = sport_clock(state.sport, fmt)
    state.regulation_period_seconds = tbl["period"]
    state.ot_seconds = tbl["ot"]
    state.overtime = bool(state.period and state.period > int(tbl["periods"] or 4))
    gsr, sentinel = period_clock_to_gsr(state.sport, state.period, state.clock_seconds_remaining_in_period, fmt)
    state.overtime_sentinel = bool(sentinel and state.status != "final")
    state.game_seconds_remaining = game_seconds_remaining(state.status, state.period, state.clock_seconds_remaining_in_period, state.sport, fmt)


def parse_scoreboard_event(ev: dict, sport: str = "nfl") -> GameState:
    comp = (ev.get("competitions") or [{}])[0]
    home_c = next((c for c in comp.get("competitors", []) if c.get("homeAway") == "home"), {})
    away_c = next((c for c in comp.get("competitors", []) if c.get("homeAway") == "away"), {})
    status_obj = comp.get("status") or ev.get("status") or {}
    status = map_status(status_obj)
    period = _int(status_obj.get("period")) or 0
    clock = parse_clock(status_obj.get("displayClock"))
    if clock is None:
        clock = parse_clock(status_obj.get("clock"))
    start = parse_iso(comp.get("date") or ev.get("date"))
    home = _team_code(home_c.get("team") or {}, sport)
    away = _team_code(away_c.get("team") or {}, sport)
    st = GameState(
        event_id=str(ev.get("id")),
        home=home,
        away=away,
        home_score=_int(home_c.get("score")) or 0,
        away_score=_int(away_c.get("score")) or 0,
        status=status,
        period=period,
        clock_seconds_remaining_in_period=clock,
        game_seconds_remaining=game_seconds_remaining(status, period, clock),
        start_time=start,
        event_key=team_event_key(sport, [away, home], et_date(start)) if home and away else None,
        sport=sport,
        home_team_id=str(home_c.get("id") or (home_c.get("team") or {}).get("id") or "") or None,
        away_team_id=str(away_c.get("id") or (away_c.get("team") or {}).get("id") or "") or None,
        status_name=((status_obj.get("type") or {}).get("name")),
        status_detail=((status_obj.get("type") or {}).get("shortDetail")),
        season=_int((ev.get("season") or {}).get("year")),
    )
    apply_clock_meta(st, comp.get("format") or ev.get("format"))
    if home_c.get("possession") is True:
        st.possession = "home"
    elif away_c.get("possession") is True:
        st.possession = "away"
    odds = (comp.get("odds") or [None])[0]
    if odds:
        st.vegas_spread_home = normalize_home_spread(odds, home, sport)
        st.vegas_total = _total_from(odds)
        st.odds_provider = (odds.get("provider") or {}).get("name")
    parse_situation(comp.get("situation"), st)
    return st


def parse_wp_series(summary: dict) -> list[dict]:
    out = []
    for row in summary.get("winprobability") or []:
        hw = _num(row.get("homeWinPercentage"))
        if hw is None:
            continue
        out.append({"play_id": str(row.get("playId") or ""), "home_wp": hw, "tie": _num(row.get("tiePercentage")) or 0.0})
    return out


def _last_play(summary: dict) -> Optional[dict]:
    drives = summary.get("drives") or {}
    cur = drives.get("current")
    if cur and cur.get("plays"):
        return cur["plays"][-1]
    prev = drives.get("previous") or []
    if prev and prev[-1].get("plays"):
        return prev[-1]["plays"][-1]
    return None


def _first_drive(summary: dict) -> Optional[dict]:
    drives = summary.get("drives") or {}
    prev = drives.get("previous") or []
    if prev:
        return prev[0]
    return drives.get("current") or None


def receive_2h_ko_home_from(summary: dict, home_id: Optional[str], away_id: Optional[str]) -> Optional[bool]:
    """Whether the home team receives the second-half kickoff: the team of the game's first
    drive received the opening kickoff, so the *other* team gets the ball after halftime.
    ``None`` when the summary has no drives yet (pre-game) or the team cannot be resolved."""
    first = _first_drive(summary)
    if not first:
        return None
    side = _possession_side((first.get("team") or {}).get("id"), home_id, away_id)
    if side is None:
        return None
    return side == "away"


def apply_summary(state: GameState, summary: dict) -> GameState:
    """Merge summary data (WP series, closing odds, header status, last play) into ``state``."""
    header = summary.get("header") or {}
    hcomp = (header.get("competitions") or [{}])[0]
    if hcomp.get("status"):
        status = map_status(hcomp["status"])
        if status != "other":
            state.status = status
        state.status_name = ((hcomp["status"].get("type") or {}).get("name")) or state.status_name
        period = _int(hcomp["status"].get("period"))
        if period:
            state.period = period
        clock = parse_clock(hcomp["status"].get("displayClock"))
        if clock is not None:
            state.clock_seconds_remaining_in_period = clock
    for c in hcomp.get("competitors") or []:
        side = c.get("homeAway")
        score = _int(c.get("score"))
        cid = str(c.get("id") or (c.get("team") or {}).get("id") or "") or None
        if side == "home":
            if state.home_team_id is None and cid:
                state.home_team_id = cid
            if score is not None:
                state.home_score = score
            if c.get("possession") is True:
                state.possession = "home"
        elif side == "away":
            if state.away_team_id is None and cid:
                state.away_team_id = cid
            if score is not None:
                state.away_score = score
            if c.get("possession") is True:
                state.possession = "away"
    season = _int((header.get("season") or {}).get("year"))
    if season:
        state.season = season
    apply_clock_meta(state, summary.get("format"))
    ko = receive_2h_ko_home_from(summary, state.home_team_id, state.away_team_id)
    if ko is not None:
        state.receive_2h_ko_home = ko
    series = parse_wp_series(summary)
    if series:
        state.espn_wp_series = series
        state.espn_home_wp = series[-1]["home_wp"]
        state.espn_tie = series[-1]["tie"]
    pick = (summary.get("pickcenter") or summary.get("odds") or [None])[0]
    if pick:
        spread = normalize_home_spread(pick, state.home, state.sport)
        if spread is not None:
            state.vegas_spread_home = spread
            state.pickcenter_spread = spread
        total = _total_from(pick)
        if total is not None:
            state.vegas_total = total
        state.odds_provider = (pick.get("provider") or {}).get("name") or state.odds_provider
        ml = parse_pickcenter_moneylines(pick)
        state.sportsbook_ml_home, state.sportsbook_ml_away = ml["home"], ml["away"]
        state.sportsbook_ml_home_open, state.sportsbook_ml_away_open = ml["home_open"], ml["away_open"]
    if hcomp.get("situation"):
        parse_situation(hcomp.get("situation"), state)
    play = _last_play(summary)
    if play:
        if state.last_play_id is None or state.last_play_id == _play_id(play.get("id")):
            # The scoreboard's lastPlay (polled every tick) is fresher than the drives of a
            # summary refreshed every 30 s; only fill from the drives when it gave nothing.
            state.last_play_text = play.get("text") or state.last_play_text
            apply_last_play_meta(state, play)
        end = play.get("end") or {}
        if state.status == "live" and state.down is None and end:
            poss = _possession_side((end.get("team") or {}).get("id"), state.home_team_id, state.away_team_id)
            if poss:
                state.possession = poss
                down = _int(end.get("down"))
                state.down = down if down and down > 0 else None
                state.distance = _int(end.get("distance")) if state.down is not None else None
                yte = _int(end.get("yardsToEndzone"))
                state.yardline_100 = yte if yte is not None and 0 < yte <= 100 else yardline_100_from(end.get("yardLine"), poss, end.get("possessionText"), state.home, state.away, state.sport)
    if state.status == "live" and (state.home_timeouts is None or state.away_timeouts is None) and state.sport in FOOTBALL:
        plays = summary_plays(summary)
        if plays:
            abbr = {c.get("homeAway"): (c.get("team") or {}).get("abbreviation") for c in hcomp.get("competitors") or []}
            counted = count_timeouts(plays, state.sport, abbr.get("home") or state.home, abbr.get("away") or state.away)
            if state.home_timeouts is None:
                state.home_timeouts = counted["home"]
            if state.away_timeouts is None:
                state.away_timeouts = counted["away"]
    state.enriched = True
    return state


# ---- state guard ---------------------------------------------------------------------------

_PRE_FINAL_SOFT_S = 300.0    # a 'final' younger than this may still flip back to 'live' (ESPN posts finals early)
_REVIEW_MAX_S = 120.0        # review_pending clears by itself after this long
_DECREASE_HOLD_POLLS = 1     # unexplained score decreases are held for this many distinct polls, then accepted


@dataclass
class StateGuard:
    """Per-event defence against the feed's known lies: score-before-lastPlay, reversed plays
    and premature finals. Pure (no I/O, no clock reads): ``apply(state, now, poll)`` returns
    the state the caller should trust and remembers what it confirmed.

    * A score *decrease* is accepted at once only when the last play explains it (REVERSED /
      OVERTURNED / No Play / NULLIFIED / no goal, or NFL type 74). Otherwise the last confirmed
      state is returned for one extra *distinct* poll with ``suspect=True``; on the next distinct
      poll the decrease is accepted (still ``suspect``) so a genuine correction never sticks.
      A decrease first seen on a repeated poll id (enrich after the scoreboard) is held too and
      does not spend the budget. Each accepted decrease resets the budget, so a second
      unexplained decrease right after one is held again. Increases are never held.
    * A score change without a new ``last_play_id`` means ESPN's WP row lags the score:
      ``espn_home_wp`` is nulled and ``suspect`` set until the id advances.
    * ``review_pending`` is set when the last play mentions a review / challenge / official
      timeout after a score or turnover, or when the clock is frozen for >= 2 polls after a
      score change; it clears on the next distinct play id or after 120 s.
    * ``final`` is soft for 300 s (``final_soft``): final -> live in that window is accepted
      cleanly; after it the flip is accepted but marked ``suspect``.
    * ``state_changed_ts`` moves on any score / period / clock / possession / down change.
    """

    event_id: str
    confirmed: Optional[GameState] = None
    last_poll: Optional[Any] = None
    held_polls: int = 0
    pending_score_id: Optional[str] = None    # last_play_id at the time of a score change the play has not caught up with
    score_changed_ts: Optional[float] = None
    possession_changed_ts: Optional[float] = None
    frozen_polls: int = 0
    review_since: Optional[float] = None
    review_anchor_id: Optional[str] = None
    final_since: Optional[float] = None
    anomalies: int = 0                        # how many polls the guard altered (a P07 metric input)

    def apply(self, new: GameState, now: float, poll: Optional[Any] = None) -> GameState:
        prev = self.confirmed
        new_poll = poll is None or poll != self.last_poll
        if new_poll:
            self.last_poll = poll
        if prev is None:
            new.state_changed_ts = now
            self._track_final(new, now)
            self.confirmed = _copy_state(new)  # a copy: callers (enrich) mutate the returned state
            return new
        # -- carry-over of guard-owned fields -------------------------------------------
        new.state_changed_ts = prev.state_changed_ts
        text = new.last_play_text or ""
        decreased = new.home_score < prev.home_score or new.away_score < prev.away_score
        explained = bool(REVERSAL_RE.search(text)) or (new.last_play_type_id in REVERSAL_TYPE_IDS)
        if decreased and not explained:
            # Hold while the distinct-poll budget lasts. A sighting on a repeated poll id (the
            # summary enrich that follows a scoreboard poll) is always held and never spends
            # the budget: a one-tick summary glitch must not leak just because it was seen
            # first via enrich, and the cap counts scoreboard polls, not calls.
            if self.held_polls < _DECREASE_HOLD_POLLS or not new_poll:
                if new_poll:
                    self.held_polls += 1
                    self.anomalies += 1
                held = _copy_state(prev)
                held.suspect = True
                return held
            new.suspect = True   # accepted after the hold: an unexplained correction
            self.held_polls = 0  # the next unexplained decrease starts its own hold
        elif not decreased:
            self.held_polls = 0
        score_changed = (new.home_score, new.away_score) != (prev.home_score, prev.away_score)
        possession_changed = new.possession is not None and prev.possession is not None and new.possession != prev.possession
        changed = score_changed or new.period != prev.period or new.clock_seconds_remaining_in_period != prev.clock_seconds_remaining_in_period or new.possession != prev.possession or new.down != prev.down
        if changed:
            new.state_changed_ts = now
        if score_changed:
            self.score_changed_ts = now
            self.frozen_polls = 0
            if new.last_play_id and prev.last_play_id and new.last_play_id == prev.last_play_id:
                self.pending_score_id = new.last_play_id
        if possession_changed:
            self.possession_changed_ts = now
        # -- score before lastPlay: the WP row belongs to the previous play ------------------
        if self.pending_score_id is not None:
            if new.last_play_id and new.last_play_id != self.pending_score_id:
                self.pending_score_id = None
            else:
                new.espn_home_wp = None
                new.suspect = True
                if new_poll:
                    self.anomalies += 1
        # -- review window -------------------------------------------------------------------
        recent = [t for t in (self.score_changed_ts, self.possession_changed_ts) if t is not None]
        recent_event = bool(recent) and now - max(recent) <= _REVIEW_MAX_S
        clock_frozen = new.clock_seconds_remaining_in_period == prev.clock_seconds_remaining_in_period and new.period == prev.period
        if new_poll and not score_changed:
            self.frozen_polls = self.frozen_polls + 1 if (clock_frozen and new.status == "live" and self.score_changed_ts is not None) else 0
        if self.review_since is None:
            if (REVIEW_RE.search(text) and recent_event) or (self.frozen_polls >= 2 and self.score_changed_ts is not None and now - self.score_changed_ts <= _REVIEW_MAX_S):
                self.review_since = now
                self.review_anchor_id = new.last_play_id
        else:
            if (new.last_play_id and new.last_play_id != self.review_anchor_id and not REVIEW_RE.search(text)) or now - self.review_since >= _REVIEW_MAX_S:
                self.review_since = None
                self.review_anchor_id = None
                self.frozen_polls = 0
                if not score_changed:
                    self.score_changed_ts = None  # the review window is over; a stopped clock is now ordinary
        new.review_pending = self.review_since is not None
        # -- soft finals ---------------------------------------------------------------------
        if prev.status == "final" and new.status == "live":
            if self.final_since is not None and now - self.final_since > _PRE_FINAL_SOFT_S:
                new.suspect = True
            self.final_since = None
            new.state_changed_ts = now
        self._track_final(new, now)
        self.confirmed = _copy_state(new)
        return new

    def _track_final(self, st: GameState, now: float) -> None:
        if st.status == "final":
            if self.final_since is None:
                self.final_since = now
            st.final_soft = now - self.final_since < _PRE_FINAL_SOFT_S
        else:
            st.final_soft = False
            if st.status == "live":
                self.final_since = None


def _copy_state(st: GameState) -> GameState:
    """Shallow dataclass copy with its own WP-series list (the guard hands copies out)."""
    d = {f: getattr(st, f) for f in st.__dataclass_fields__}
    d["espn_wp_series"] = list(st.espn_wp_series)
    return GameState(**d)


# ---- client / feed ------------------------------------------------------------------------

class ESPNClient:
    def __init__(self, http: Optional[HttpClient] = None, base_url: Optional[str] = None, sport: str = "nfl"):
        self.http = http or HttpClient(headers={"Accept": "application/json"}, rate_limit=4.0)
        self.sport = sport
        self.base_url = (base_url or SPORT_BASE_URL.get(sport, BASE_URL)).rstrip("/")

    def scoreboard(self, date: Any = None) -> dict:
        params = dict(SPORT_SCOREBOARD_PARAMS.get(self.sport, {}))
        if date is not None:
            params["dates"] = _coerce_date(date)
        return self.http.get(f"{self.base_url}/scoreboard", params=params or None, headers={"Accept": "application/json"})

    def scoreboard_week(self, season: int, week: int, seasontype: int = 2) -> dict:
        """All games of one regular-season (2) / post-season (3) week, including finals."""
        params = dict(SPORT_SCOREBOARD_PARAMS.get(self.sport, {}))
        params.update({"dates": int(season), "seasontype": int(seasontype), "week": int(week)})
        return self.http.get(f"{self.base_url}/scoreboard", params=params, headers={"Accept": "application/json"})

    def summary(self, event_id: str) -> dict:
        return self.http.get(f"{self.base_url}/summary", params={"event": str(event_id)}, headers={"Accept": "application/json"})


class ESPNFeed:
    """Cheap scoreboard polling with on-demand per-game enrichment.

    Every state that leaves the feed passes through the event's ``StateGuard`` (``guard=False``
    disables it for raw parsing). A scoreboard poll and the enrichment that follows it share
    one poll id so the guard's "one extra poll" hold counts scoreboard polls, not calls."""

    def __init__(self, client: Optional[ESPNClient] = None, guard: bool = True):
        self.client = client or ESPNClient()
        self.last_scoreboard: Optional[dict] = None
        self.guards: dict[str, StateGuard] = {}
        self.guard_enabled = guard
        self._poll = 0

    def guard_for(self, event_id: str) -> StateGuard:
        g = self.guards.get(event_id)
        if g is None:
            g = self.guards[event_id] = StateGuard(event_id=event_id)
        return g

    def _guarded(self, state: GameState, now: Optional[float]) -> GameState:
        if not self.guard_enabled:
            return state
        return self.guard_for(state.event_id).apply(state, now if now is not None else time.time(), self._poll)

    def games(self, date: Any = None, now: Optional[float] = None) -> list[GameState]:
        sb = self.client.scoreboard(date)
        self.last_scoreboard = sb
        self._poll += 1
        out = []
        for ev in sb.get("events") or []:
            try:
                st = parse_scoreboard_event(ev, self.client.sport)
            except Exception:  # one malformed event must not sink the poll
                continue
            out.append(self._guarded(st, now))
        return out

    def enrich(self, state: GameState, now: Optional[float] = None) -> GameState:
        return self._guarded(apply_summary(state, self.client.summary(state.event_id)), now)

    def find(self, event_key: str, date: Any = None) -> Optional[GameState]:
        for g in self.games(date):
            if g.event_key == event_key:
                return g
        return None

    def state_for_robinhood_teams(self, away: str, home: str, date: Any = None) -> Optional[GameState]:
        """Match a Robinhood/Kalshi game by team spellings and (optionally) the ET date.

        Without a date, the current-week scoreboard is used and the first game between the
        two teams (either home/away orientation) is returned.
        """
        a, h = nfl_team_code(away), nfl_team_code(home)
        if not a or not h:
            return None
        date_str = None
        if date is not None:
            raw = _coerce_date(date)
            date_str = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}" if raw else None
        games = self.games(date)
        if date_str:
            key = nfl_event_key([a, h], date_str)
            return next((g for g in games if g.event_key == key), None)
        return next((g for g in games if {g.home, g.away} == {a, h}), None)


def live_games(states: Iterable[GameState]) -> list[GameState]:
    return [s for s in states if s.status == "live"]

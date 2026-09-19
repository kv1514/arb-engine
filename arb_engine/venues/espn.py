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
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date as _date, datetime
from typing import Any, Iterable, Optional

from ..matching.normalize import et_date, nfl_event_key, parse_iso
from ..matching.teams import nfl_team_code
from .http import HttpClient

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"

REGULATION_PERIODS = 4
REGULATION_PERIOD_SECONDS = 900
OVERTIME_PERIOD_SECONDS = 600  # regular-season OT is one 10-minute period (ESPN ``format.overtime.clock``)

_STATE_MAP = {"pre": "pre", "in": "live", "post": "final"}


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


def game_seconds_remaining(status: str, period: int, clock_seconds: Optional[int]) -> Optional[int]:
    """Seconds left in the game. Regulation is 4 x 900; overtime is period 5 with 600 s.

    Pre-game -> 3600; final -> 0; unknown clock during a live game -> None.
    """
    if status == "pre":
        return REGULATION_PERIODS * REGULATION_PERIOD_SECONDS
    if status == "final":
        return 0
    if clock_seconds is None:
        return None
    if period <= 0:
        return REGULATION_PERIODS * REGULATION_PERIOD_SECONDS
    if period <= REGULATION_PERIODS:
        return (REGULATION_PERIODS - period) * REGULATION_PERIOD_SECONDS + clock_seconds
    return min(clock_seconds, OVERTIME_PERIOD_SECONDS)  # overtime: only this period is left


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


def normalize_home_spread(odds: Optional[dict], home_code: Optional[str]) -> Optional[float]:
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
        team = nfl_team_code(m.group(1))
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

def _team_code(team: dict) -> Optional[str]:
    t = team or {}
    return nfl_team_code(t.get("abbreviation")) or nfl_team_code(t.get("displayName")) or nfl_team_code(t.get("name"))


def _possession_side(team_id: Any, home_id: Optional[str], away_id: Optional[str]) -> Optional[str]:
    if team_id is None or team_id == "":
        return None
    tid = str(team_id)
    if home_id and tid == home_id:
        return "home"
    if away_id and tid == away_id:
        return "away"
    return None


def yardline_100_from(yard_line: Any, possession: Optional[str], possession_text: Any = None, home: Optional[str] = None, away: Optional[str] = None) -> Optional[int]:
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
    side = nfl_team_code(m.group(1))
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
    state.yardline_100 = yardline_100_from(sit.get("yardLine"), state.possession, sit.get("possessionText"), state.home, state.away)
    if sit.get("homeTimeouts") is not None:
        state.home_timeouts = _int(sit.get("homeTimeouts"))
    if sit.get("awayTimeouts") is not None:
        state.away_timeouts = _int(sit.get("awayTimeouts"))
    if sit.get("isRedZone") is not None:
        state.is_red_zone = bool(sit.get("isRedZone"))
    lp = sit.get("lastPlay") or {}
    if lp.get("text"):
        state.last_play_text = lp.get("text")
    prob = lp.get("probability") or {}
    if prob.get("homeWinPercentage") is not None and state.espn_home_wp is None:
        state.espn_home_wp = _num(prob.get("homeWinPercentage"))


def parse_scoreboard_event(ev: dict) -> GameState:
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
    home = _team_code(home_c.get("team") or {})
    away = _team_code(away_c.get("team") or {})
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
        event_key=nfl_event_key([away, home], et_date(start)) if home and away else None,
        home_team_id=str(home_c.get("id") or (home_c.get("team") or {}).get("id") or "") or None,
        away_team_id=str(away_c.get("id") or (away_c.get("team") or {}).get("id") or "") or None,
        status_name=((status_obj.get("type") or {}).get("name")),
        status_detail=((status_obj.get("type") or {}).get("shortDetail")),
    )
    if home_c.get("possession") is True:
        st.possession = "home"
    elif away_c.get("possession") is True:
        st.possession = "away"
    odds = (comp.get("odds") or [None])[0]
    if odds:
        st.vegas_spread_home = normalize_home_spread(odds, home)
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
    state.game_seconds_remaining = game_seconds_remaining(state.status, state.period, state.clock_seconds_remaining_in_period)
    ko = receive_2h_ko_home_from(summary, state.home_team_id, state.away_team_id)
    if ko is not None:
        state.receive_2h_ko_home = ko
    series = parse_wp_series(summary)
    if series:
        state.espn_wp_series = series
        state.espn_home_wp = series[-1]["home_wp"]
    pick = (summary.get("pickcenter") or summary.get("odds") or [None])[0]
    if pick:
        spread = normalize_home_spread(pick, state.home)
        if spread is not None:
            state.vegas_spread_home = spread
        total = _total_from(pick)
        if total is not None:
            state.vegas_total = total
        state.odds_provider = (pick.get("provider") or {}).get("name") or state.odds_provider
    if hcomp.get("situation"):
        parse_situation(hcomp.get("situation"), state)
    play = _last_play(summary)
    if play:
        state.last_play_text = play.get("text") or state.last_play_text
        end = play.get("end") or {}
        if state.status == "live" and state.down is None and end:
            poss = _possession_side((end.get("team") or {}).get("id"), state.home_team_id, state.away_team_id)
            if poss:
                state.possession = poss
                down = _int(end.get("down"))
                state.down = down if down and down > 0 else None
                state.distance = _int(end.get("distance")) if state.down is not None else None
                yte = _int(end.get("yardsToEndzone"))
                state.yardline_100 = yte if yte is not None and 0 < yte <= 100 else yardline_100_from(end.get("yardLine"), poss, end.get("possessionText"), state.home, state.away)
    state.enriched = True
    return state


# ---- client / feed ------------------------------------------------------------------------

class ESPNClient:
    def __init__(self, http: Optional[HttpClient] = None, base_url: str = BASE_URL):
        self.http = http or HttpClient(headers={"Accept": "application/json"}, rate_limit=4.0)
        self.base_url = base_url.rstrip("/")

    def scoreboard(self, date: Any = None) -> dict:
        params = {"dates": _coerce_date(date)} if date is not None else None
        return self.http.get(f"{self.base_url}/scoreboard", params=params, headers={"Accept": "application/json"})

    def scoreboard_week(self, season: int, week: int, seasontype: int = 2) -> dict:
        """All games of one regular-season (2) / post-season (3) week, including finals."""
        return self.http.get(f"{self.base_url}/scoreboard", params={"dates": int(season), "seasontype": int(seasontype), "week": int(week)}, headers={"Accept": "application/json"})

    def summary(self, event_id: str) -> dict:
        return self.http.get(f"{self.base_url}/summary", params={"event": str(event_id)}, headers={"Accept": "application/json"})


class ESPNFeed:
    """Cheap scoreboard polling with on-demand per-game enrichment."""

    def __init__(self, client: Optional[ESPNClient] = None):
        self.client = client or ESPNClient()
        self.last_scoreboard: Optional[dict] = None

    def games(self, date: Any = None) -> list[GameState]:
        sb = self.client.scoreboard(date)
        self.last_scoreboard = sb
        out = []
        for ev in sb.get("events") or []:
            try:
                out.append(parse_scoreboard_event(ev))
            except Exception:  # one malformed event must not sink the poll
                continue
        return out

    def enrich(self, state: GameState) -> GameState:
        return apply_summary(state, self.client.summary(state.event_id))

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

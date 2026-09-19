"""Verify the replay's game-state inputs against an independent source.

The replay (``backtest.py`` over ``history.espn_timeline``) reconstructs down, distance,
field position, possession and timeouts for every play from ESPN's summary feed. Nothing in
the engine checks those numbers against anything else, so a "95 % populated" criterion
cannot tell right from wrong, and every timeout-dependent model rule would inherit an ESPN
parsing error silently. This module compares the ESPN-derived rows with nflverse's
play-by-play (the data the WP model was trained on) play by play, and separately asks
whether ESPN's ``winprobability[playId]`` describes the state *before* or *after* the play,
which changes what the ESPN column in every published table means.

Conventions (checked against nflverse 2025 rows, see ``tests/test_feedparity.py``):

* nflverse ``down`` / ``ydstogo`` / ``yardline_100`` / ``posteam`` / ``time`` describe the
  start of the play — the same as ESPN's ``start`` block and clock.
* nflverse ``total_home_score`` is the score **after** the play and the timeout columns are
  already decremented on the timeout row itself. ESPN folds the try into the touchdown
  play (its ``homeScore`` after a TD already includes the PAT) so the alignment key uses
  the score **before** the play, which both sources agree on, and timeouts are compared
  as "remaining after this play".
* On kickoffs nflverse's ``posteam`` is the receiving team and ``yardline_100`` is from the
  receiver's goal line; ESPN's ``start.team`` is the kicker. The ESPN rows are flipped to
  the receiver here (the convention the replay adopts) so a kickoff is not a spurious
  possession disagreement.
* Play ids: an ESPN play id is ``<event id><nflverse play_id>`` (``40187293240`` = event
  401872932, play 40). The state alignment never uses ids; the share of aligned pairs
  whose ids agree is reported as a check on the alignment itself.

Everything here is standard library and pure (no I/O beyond reading a csv.gz); the scripts
under ``scripts/`` do the fetching and the report files.
"""

from __future__ import annotations

import csv
import gzip
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from ..matching.normalize import et_date, parse_iso
from ..matching.teams import team_code
from ..venues.espn import parse_clock
import inspect

from ..venues.history import PlayRow, espn_timeline

# Columns read from nflverse ``play_by_play_<season>.csv.gz`` (the trimmed test fixture
# holds exactly these, in this order).
NFL_COLUMNS: tuple[str, ...] = (
    "game_id", "play_id", "qtr", "time", "down", "ydstogo", "yardline_100", "posteam",
    "home_team", "away_team", "total_home_score", "total_away_score",
    "home_timeouts_remaining", "away_timeouts_remaining", "desc", "game_date", "week",
)

FIELDS: tuple[str, ...] = ("down", "distance", "yardline_100", "possession", "home_timeouts", "away_timeouts", "clock")
MAX_DISAGREEMENTS = 20
REGULATION_TIMEOUTS = 3
OVERTIME_TIMEOUTS = 2

# ESPN play texts use the NFL's GSIS club codes, which differ from the scoreboard abbreviations
# for a few teams ("Timeout #1 by BLT"); nflverse and the team table use the standard codes.
GSIS_CODES = {"ARZ": "ARI", "BLT": "BAL", "CLV": "CLE", "HST": "HOU", "LA": "LAR", "SL": "LAR", "SD": "LAC", "OAK": "LV", "WSH": "WAS"}


def nfl_code(x: Any) -> Optional[str]:
    s = str(x or "").strip().upper()
    return team_code("nfl", GSIS_CODES.get(s, s)) or (GSIS_CODES.get(s) or s or None)


_KICKOFF_RE = re.compile(r"\bkicks?\b.*\byards?\b|\bkickoff\b|\bonside\b", re.I)
_TIMEOUT_RE = re.compile(r"Timeout\s*#?(\d+)?\s*by\s+([A-Za-z.]+)", re.I)


def _int(x: Any) -> Optional[int]:
    try:
        if x is None or x == "":
            return None
        return int(float(x))
    except (TypeError, ValueError):
        return None


@dataclass
class StatePlay:
    """One play in the shape both sources are reduced to.

    ``home_score`` / ``away_score`` are the score *before* the play (alignment key);
    ``*_after`` the score after it; ``home_timeouts`` / ``away_timeouts`` are the timeouts
    remaining *after* the play (nflverse's convention). ``down`` is None on dead-ball rows
    (kickoffs, tries, timeouts, end-of-period markers) on both sides.
    """

    source: str
    game_id: str
    play_id: str
    period: int
    clock: Optional[int]
    home_score: int
    away_score: int
    possession: Optional[str]
    down: Optional[int]
    distance: Optional[int]
    yardline_100: Optional[int]
    home_timeouts: Optional[int]
    away_timeouts: Optional[int]
    home_score_after: int = 0
    away_score_after: int = 0
    kickoff: bool = False
    text: str = ""
    ts: Optional[float] = None


@dataclass
class NflGame:
    game_id: str
    home: str
    away: str
    date: Optional[str]
    week: Optional[int]
    plays: list[StatePlay] = field(default_factory=list)


def is_kickoff_text(text: str) -> bool:
    return bool(_KICKOFF_RE.search(text or ""))


def _classify(text: str, type_id: Any = None) -> Optional[str]:
    """P02's ``espn.classify_play`` when present (guarded import), else a local kickoff test."""
    try:
        from ..venues.espn import classify_play  # type: ignore[attr-defined]

        out = classify_play(text, type_id)
        if out is not None:
            return out
    except Exception:
        pass
    if str(type_id) == "53" or is_kickoff_text(text):
        return "kickoff"
    return None


# ---- nflverse -------------------------------------------------------------------------------

def load_nflverse_pbp(path: str | os.PathLike, games: Optional[Iterable[str]] = None, weeks: Optional[Iterable[int]] = None, limit: Optional[int] = None) -> dict[str, NflGame]:
    """Stream ``play_by_play_<season>.csv.gz`` (or a trimmed .csv) into ``{game_id: NflGame}``,
    keeping only ``NFL_COLUMNS``. ``games`` / ``weeks`` filter, ``limit`` caps the number of
    games (first seen). Team codes are canonicalised (nflverse ``LA`` -> ``LAR``, ``WAS``)."""
    want = set(games) if games else None
    wweeks = {int(w) for w in weeks} if weeks else None
    out: dict[str, NflGame] = {}
    p = Path(path)
    opener = gzip.open if p.suffix == ".gz" else open
    with opener(p, "rt", encoding="utf-8", newline="") as f:  # type: ignore[operator]
        reader = csv.reader(f)
        header = next(reader)
        idx = {c: header.index(c) for c in NFL_COLUMNS if c in header}
        missing = [c for c in NFL_COLUMNS if c not in idx and c != "week"]
        if missing:
            raise ValueError(f"nflverse file lacks columns {missing}")
        prev_scores: dict[str, tuple[int, int]] = {}
        for row in reader:
            gid = row[idx["game_id"]]
            if want is not None and gid not in want:
                continue
            if wweeks is not None and "week" in idx and _int(row[idx["week"]]) not in wweeks:
                continue
            g = out.get(gid)
            if g is None:
                if limit is not None and len(out) >= limit:
                    continue
                home_raw, away_raw = row[idx["home_team"]], row[idx["away_team"]]
                g = NflGame(game_id=gid, home=nfl_code(home_raw) or home_raw, away=nfl_code(away_raw) or away_raw, date=row[idx["game_date"]] or None, week=_int(row[idx["week"]]) if "week" in idx else None)
                out[gid] = g
                prev_scores[gid] = (0, 0)
            pos_raw = row[idx["posteam"]]
            pos_code = nfl_code(pos_raw) if pos_raw else None
            possession = "home" if pos_code and pos_code == g.home else ("away" if pos_code and pos_code == g.away else None)
            down = _int(row[idx["down"]])
            down = down if down and down > 0 else None
            hs_after, as_after = _int(row[idx["total_home_score"]]) or 0, _int(row[idx["total_away_score"]]) or 0
            hs_before, as_before = prev_scores[gid]
            desc = row[idx["desc"]]
            g.plays.append(StatePlay(
                source="nflverse", game_id=gid, play_id=row[idx["play_id"]], period=_int(row[idx["qtr"]]) or 0, clock=parse_clock(row[idx["time"]]),
                home_score=hs_before, away_score=as_before, possession=possession, down=down,
                distance=_int(row[idx["ydstogo"]]) if down else None, yardline_100=_int(row[idx["yardline_100"]]),
                home_timeouts=_int(row[idx["home_timeouts_remaining"]]), away_timeouts=_int(row[idx["away_timeouts_remaining"]]),
                home_score_after=hs_after, away_score_after=as_after, kickoff=is_kickoff_text(desc), text=desc[:160],
            ))
            prev_scores[gid] = (hs_after, as_after)
    return out


def nfl_game_key(home: str, away: str, date: Optional[str]) -> tuple[str, str, Optional[str]]:
    return (nfl_code(home) or home, nfl_code(away) or away, date)


def find_nfl_game(games: dict[str, NflGame], home: str, away: str, kickoff: Any) -> Optional[NflGame]:
    """The nflverse game for an ESPN (home, away, kickoff): same canonical codes and the same
    Eastern date (nflverse ``game_date`` is the local date); falls back to codes alone when the
    date is unknown and the pair is unique."""
    if isinstance(kickoff, str):
        kickoff = parse_iso(kickoff)
    date = et_date(kickoff) if kickoff is not None else None
    key = nfl_game_key(home, away, date)
    for g in games.values():
        if (g.home, g.away, g.date) == key:
            return g
    cands = [g for g in games.values() if (g.home, g.away) == key[:2]]
    return cands[0] if len(cands) == 1 else None


# ---- ESPN -----------------------------------------------------------------------------------

_CHALLENGE_RE = re.compile(r"((?:[A-Z][A-Za-z.]+ ){0,2}[A-Z][A-Za-z.]+) challenged\b[^.]*\band the play was Upheld", re.I)


def _team_from_phrase(phrase: str) -> Optional[str]:
    """'TOUCHDOWN. New York Giants' -> NYG: try the trailing 3, 2, 1 words as a team name."""
    words = phrase.replace(".", " ").split()
    for k in (3, 2, 1):
        if len(words) >= k:
            code = team_code("nfl", " ".join(words[-k:]))
            if code:
                return code
    return None


def timeouts_after_play(texts: Iterable[tuple[int, str, Any]], home: str, away: str) -> list[tuple[Optional[int], Optional[int]]]:
    """Timeouts remaining after each play from the play texts ``(period, text, type_id)``:
    3 per half (reset at period 3), 2 in overtime. A ``Timeout #N by TEAM`` row sets the
    team to ``min(allowance - N, remaining - 1)`` — the number alone would miss a row the
    feed dropped, the count alone would trust the GSIS typo that numbers two timeouts "#2"
    (BAL, 2026 week 1). A lost coach's challenge (``TEAM challenged … and the play was
    Upheld``) costs a timeout; a reversed one and a booth review do not. Independent of P02's
    ``count_timeouts`` on purpose — this is the check on it."""
    out: list[tuple[Optional[int], Optional[int]]] = []
    remaining = {"home": REGULATION_TIMEOUTS, "away": REGULATION_TIMEOUTS}
    half = 1
    for period, text, type_id in texts:
        h = 1 if period <= 2 else (2 if period <= 4 else 3)
        if h != half:
            half = h
            allowance = OVERTIME_TIMEOUTS if h == 3 else REGULATION_TIMEOUTS
            remaining = {"home": allowance, "away": allowance}
        allowance = OVERTIME_TIMEOUTS if half == 3 else REGULATION_TIMEOUTS
        m = _TIMEOUT_RE.search(text or "")
        if m or str(type_id) == "21":
            side = None
            if m:
                code = nfl_code(m.group(2))
                side = "home" if code == home else ("away" if code == away else None)
            if side is not None:
                n = _int(m.group(1)) if m and m.group(1) else None
                by_count = max(0, remaining[side] - 1)
                remaining[side] = min(by_count, max(0, allowance - n)) if n is not None else by_count
        c = _CHALLENGE_RE.search(text or "")
        if c:
            code = _team_from_phrase(c.group(1))
            side = "home" if code == home else ("away" if code == away else None)
            if side is not None:
                remaining[side] = max(0, remaining[side] - 1)
        out.append((remaining["home"], remaining["away"]))
    return out


WALLCLOCK_SLACK = 300.0  # seconds a wallclock may run backwards before it counts as an anomaly


def _timeline_accepts(name: str) -> bool:
    try:
        return name in inspect.signature(espn_timeline).parameters
    except (TypeError, ValueError):
        return False


def ordered_rows(summary: dict) -> tuple[list[PlayRow], list[str], list[Any], dict[str, Any]]:
    """``history.espn_timeline`` rows re-sorted into ESPN's own play order
    (``drives[*].plays[*]``, i.e. ``sequenceNumber``) with the score before each play
    recomputed in that order, plus each play's full text and type id. The replay sorts by
    wallclock, but ESPN stamps some timeout rows with a wallclock exactly one day late and
    the replay then carries them, with a Q4 score, to the end of the game;
    ``meta["wallclock_anomalies"]`` counts such rows (a P03 bug when non-zero)."""
    rows, meta = espn_timeline(summary, synthetic=False) if _timeline_accepts("synthetic") else espn_timeline(summary)
    # Back to ESPN's own convention: the replay's timeline (P03) flips kickoff rows to the
    # receiver and drops in synthetic try / kickoff-pending rows; this module reads the feed as
    # ESPN prints it (start.team = the kicker) and flips only in espn_state_plays.
    rows = [r for r in rows if not getattr(r, "synthetic", False)]
    for r in rows:
        if getattr(r, "play_class", None) == "kickoff" and r.possession in ("home", "away"):
            r.possession = "away" if r.possession == "home" else "home"
            r.yardline_100 = 100 - r.yardline_100 if r.yardline_100 is not None else None
    order: dict[str, int] = {}
    plays_by_id: dict[str, dict] = {}
    drives = list((summary.get("drives") or {}).get("previous", []) or [])
    cur = (summary.get("drives") or {}).get("current")
    if cur:
        drives.append(cur)
    for drive in drives:
        for p in drive.get("plays") or []:
            pid = str(p.get("id"))
            plays_by_id[pid] = p
            order.setdefault(pid, len(order))
    by_wall = {r.play_id: i for i, r in enumerate(rows)}
    rows = sorted(rows, key=lambda r: (order.get(r.play_id, len(order) + by_wall[r.play_id]), by_wall[r.play_id]))
    anomalies = 0
    prev_h = prev_a = 0
    for i, r in enumerate(rows):
        # a row stamped out of sequence sits beyond *both* neighbours by more than the slack
        nb = [rows[j].ts for j in (i - 1, i + 1) if 0 <= j < len(rows)]
        if nb and (all(r.ts > t + WALLCLOCK_SLACK for t in nb) or all(r.ts < t - WALLCLOCK_SLACK for t in nb)):
            anomalies += 1
        r.home_score, r.away_score = prev_h, prev_a
        prev_h, prev_a = r.home_score_after, r.away_score_after
    # espn_timeline truncates text to 120 chars; a lost challenge is described past that.
    texts = [str((plays_by_id.get(r.play_id) or {}).get("text") or r.text) for r in rows]
    type_ids = [((plays_by_id.get(r.play_id) or {}).get("type") or {}).get("id") for r in rows]
    meta = dict(meta, event_id=str((summary.get("header") or {}).get("id") or ""), wallclock_anomalies=anomalies)
    return rows, texts, type_ids, meta


def receive_2h_ko_home(rows: list[PlayRow], texts: Optional[list[str]] = None, type_ids: Optional[list[Any]] = None) -> Optional[bool]:
    """The model's ``receive_2h_ko_home`` flag from ESPN's own (unflipped) rows: the team that
    kicks off to open the game receives the second-half kickoff, and on a kickoff row ESPN's
    ``start.team`` (the row's ``possession``) is the *kicker* — so the flag is ``True`` when
    the opening kickoff's possession is ``"home"``. The kickoff is the first row classified
    as one (in period 1 or 2 — a delayed-start game may carry an admin row first); when no
    such row has a possession, ``None`` lets the model average both possibilities.
    ``backtest.py`` derives the same flag from the flipped replay convention."""
    for i, r in enumerate(rows):
        if r.period and r.period > 2:
            break
        txt = texts[i] if texts is not None and i < len(texts) else r.text
        tid = type_ids[i] if type_ids is not None and i < len(type_ids) else None
        if _classify(str(txt or ""), tid) == "kickoff" and r.possession in ("home", "away"):
            return r.possession == "home"
    return None


def espn_state_plays(summary: dict, timeouts: str = "text") -> tuple[list[StatePlay], dict[str, Any]]:
    """The replay's per-play rows reduced to ``StatePlay`` in ESPN's play order
    (``ordered_rows``). ``timeouts="text"`` recounts timeouts from the play texts (the
    independent check); ``"rows"`` takes whatever the PlayRow carries (P03's
    ``home_timeouts``/``away_timeouts`` when that item is present, else None). Kickoff rows
    are flipped to the receiver, the convention the replay adopts."""
    rows, texts, type_ids, meta = ordered_rows(summary)
    home, away = nfl_code(meta.get("home")) or meta.get("home"), nfl_code(meta.get("away")) or meta.get("away")
    counted = timeouts_after_play(((r.period, txt, tid) for r, txt, tid in zip(rows, texts, type_ids)), home, away) if timeouts == "text" else None
    out: list[StatePlay] = []
    for i, r in enumerate(rows):
        kickoff = _classify(texts[i], type_ids[i]) == "kickoff"
        poss, yl = r.possession, r.yardline_100
        if kickoff and poss in ("home", "away"):
            poss = "away" if poss == "home" else "home"
            yl = 100 - yl if yl is not None else None
        down = r.down if r.down and r.down > 0 else None
        if counted is not None:
            ht, at = counted[i]
        else:
            ht, at = getattr(r, "home_timeouts", None), getattr(r, "away_timeouts", None)
        out.append(StatePlay(
            source="espn", game_id=meta["event_id"], play_id=r.play_id, period=r.period, clock=r.clock_seconds,
            home_score=r.home_score, away_score=r.away_score, possession=poss, down=down,
            distance=r.distance if down else None, yardline_100=yl, home_timeouts=ht, away_timeouts=at,
            home_score_after=r.home_score_after, away_score_after=r.away_score_after, kickoff=kickoff, text=texts[i][:240], ts=r.ts,
        ))
    return out, dict(meta, home=home, away=away)


# ---- alignment and agreement ----------------------------------------------------------------

@dataclass
class Pair:
    espn: Optional[StatePlay]
    nfl: Optional[StatePlay]
    shift: int = 0  # nfl index minus the running cursor when matched (0 = in step)


def _key(p: StatePlay) -> tuple[int, int, int]:
    return (p.period, p.home_score, p.away_score)


_ADMIN_RE = re.compile(r"^(Official Timeout|Two-Minute Warning|Injury Timeout|GAME$|Timeout at )", re.I)
_TOKEN_RE = re.compile(r"[A-Za-z]{3,}")


def is_admin_text(text: str) -> bool:
    """Rows that describe no play and only one source carries (ESPN's official / injury
    timeouts, nflverse's two-minute warning and GAME marker). Team timeouts are plays here."""
    return bool(_ADMIN_RE.match((text or "").strip()))


def text_similarity(a: str, b: str) -> float:
    """Jaccard overlap of the alphabetic tokens (names, verbs; jersey numbers and yard
    counts dropped) — 'B.Grupe kicks 65 yards from NO 35' vs '19-B.Grupe kicks 65 yards from
    NO 35' is ~1, a timeout vs a kickoff ~0."""
    ta = {t.lower() for t in _TOKEN_RE.findall(a or "")}
    tb = {t.lower() for t in _TOKEN_RE.findall(b or "")}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


MIN_SIMILARITY = 0.2


def _state(p: StatePlay) -> tuple:
    return (p.down, p.distance, p.possession, p.yardline_100)


def align_plays(espn_rows: list[StatePlay], nfl_rows: list[StatePlay], clock_tolerance: int = 0, window: int = 8) -> list[Pair]:
    """Pair ESPN rows with nflverse rows by (period, score before the play) and clock within
    ``clock_tolerance`` seconds, walking both lists in order. For each ESPN row the search
    covers the still-unmatched nflverse rows up to ``window`` behind the cursor (a row ESPN
    lists in a different order — the ±1 play tolerance, but a skipped twin stays reachable)
    and ``window`` rows ahead (rows ESPN does not carry: tries folded into the TD,
    two-minute warnings). Candidates are ranked by exact clock, then text similarity,
    then the same down/possession, then nearness to the cursor; a candidate whose text shares
    almost nothing with the ESPN row (``MIN_SIMILARITY``) is taken only when its full state
    (down, distance, possession, yardline) agrees, so an ESPN-only row cannot steal the twin
    of the next play. Administrative rows (``is_admin_text``) are never aligned. Unmatched
    rows on either side come back with the other half None (nflverse leftovers at the end)."""
    used = [False] * len(nfl_rows)
    admin_nfl = [is_admin_text(n.text) for n in nfl_rows]
    pairs: list[Pair] = []
    cursor = 0
    for e in espn_rows:
        if is_admin_text(e.text):
            pairs.append(Pair(e, None, shift=0))
            continue
        lo, hi = max(0, cursor - window), min(len(nfl_rows), cursor + window + 1)
        best: Optional[tuple[tuple, int]] = None
        for k in range(lo, hi):
            if used[k] or admin_nfl[k]:
                continue
            n = nfl_rows[k]
            if _key(n) != _key(e):
                continue
            if e.clock is not None and n.clock is not None:
                dc = abs(e.clock - n.clock)
                if dc > clock_tolerance:
                    continue
            else:
                dc = 0
            sim = text_similarity(e.text, n.text)
            same_state = _state(n) == _state(e)
            if sim < MIN_SIMILARITY and not same_state:
                continue
            rank = (0 if dc == 0 else 1, -round(sim, 2), 0 if (n.down == e.down and n.possession == e.possession) else 1, abs(k - cursor))
            if best is None or rank < best[0]:
                best = (rank, k)
        if best is None:
            pairs.append(Pair(e, None))
            continue
        k = best[1]
        used[k] = True
        pairs.append(Pair(e, nfl_rows[k], shift=k - cursor))
        cursor = k + 1
    for k, n in enumerate(nfl_rows):
        if not used[k]:
            pairs.append(Pair(None, n))
    return pairs


def _value(p: StatePlay, f: str) -> Any:
    return getattr(p, f)


def agreement(pairs: list[Pair], fields: tuple[str, ...] = FIELDS, max_disagreements: int = MAX_DISAGREEMENTS) -> dict[str, Any]:
    """Per-field agreement over the matched pairs where both sides carry a value: ``n``,
    ``agree``, ``rate`` and the first ``max_disagreements`` disagreements (play ids, both
    values, the nflverse description). Also the alignment's own health: matched / unmatched
    counts and the share of matched pairs whose play ids agree."""
    out: dict[str, Any] = {}
    matched = [p for p in pairs if p.espn is not None and p.nfl is not None]
    for f in fields:
        n = agree = 0
        dis: list[dict[str, Any]] = []
        for p in matched:
            a, b = _value(p.espn, f), _value(p.nfl, f)
            if a is None or b is None:
                continue
            n += 1
            if a == b:
                agree += 1
            elif len(dis) < max_disagreements:
                dis.append({"espn_play": p.espn.play_id, "nfl_play": p.nfl.play_id, "period": p.nfl.period, "clock": p.nfl.clock, "espn": a, "nflverse": b, "text": p.nfl.text[:90]})
        out[f] = {"n": n, "agree": agree, "rate": round(agree / n, 4) if n else None, "disagreements": dis}
    # ESPN play id = <event id><nflverse play_id>; with the event id known the match is exact
    # (a bare suffix test would let ESPN play 140 claim nflverse play 40)
    id_ok = sum(1 for p in matched if p.nfl.play_id and p.espn.play_id.endswith(p.nfl.play_id) and (not p.espn.game_id.isdigit() or p.espn.play_id == p.espn.game_id + p.nfl.play_id))
    out["_alignment"] = {
        "matched": len(matched),
        "espn_unmatched": sum(1 for p in pairs if p.nfl is None and not is_admin_text(p.espn.text)), "nfl_unmatched": sum(1 for p in pairs if p.espn is None and not is_admin_text(p.nfl.text)),
        "espn_admin": sum(1 for p in pairs if p.nfl is None and is_admin_text(p.espn.text)), "nfl_admin": sum(1 for p in pairs if p.espn is None and is_admin_text(p.nfl.text)),
        "shifted": sum(1 for p in matched if p.shift != 0), "id_match_rate": round(id_ok / len(matched), 4) if matched else None,
    }
    return out


def merge_agreement(reports: list[dict[str, Any]], fields: tuple[str, ...] = FIELDS, max_disagreements: int = MAX_DISAGREEMENTS) -> dict[str, Any]:
    """Pool per-game ``agreement`` dicts into one (rates recomputed from the counts)."""
    out: dict[str, Any] = {}
    for f in fields:
        n = sum(r[f]["n"] for r in reports)
        agree = sum(r[f]["agree"] for r in reports)
        dis: list[dict[str, Any]] = []
        for r in reports:
            for d in r[f]["disagreements"]:
                if len(dis) < max_disagreements:
                    dis.append(dict(d, game=r.get("_game")))
        out[f] = {"n": n, "agree": agree, "rate": round(agree / n, 4) if n else None, "disagreements": dis}
    al = [r["_alignment"] for r in reports]
    matched = sum(a["matched"] for a in al)
    out["_alignment"] = {
        "games": len(reports), "matched": matched, "espn_unmatched": sum(a["espn_unmatched"] for a in al), "nfl_unmatched": sum(a["nfl_unmatched"] for a in al),
        "espn_admin": sum(a.get("espn_admin", 0) for a in al), "nfl_admin": sum(a.get("nfl_admin", 0) for a in al),
        "shifted": sum(a["shifted"] for a in al),
        "id_match_rate": round(sum((a["id_match_rate"] or 0) * a["matched"] for a in al) / matched, 4) if matched else None,
    }
    return out


def format_agreement(rep: dict[str, Any], title: str = "", fields: tuple[str, ...] = FIELDS) -> str:
    lines = [title] if title else []
    a = rep.get("_alignment", {})
    lines.append(f"aligned {a.get('matched', 0)} pairs ({a.get('shifted', 0)} out of step), unmatched ESPN {a.get('espn_unmatched', 0)} / nflverse {a.get('nfl_unmatched', 0)} (administrative rows skipped: {a.get('espn_admin', 0)} / {a.get('nfl_admin', 0)}), play-id agreement of aligned pairs {a.get('id_match_rate')}")
    lines.append("field            n   agree    rate")
    for f in fields:
        r = rep[f]
        rate = f"{r['rate']:.4f}" if r["rate"] is not None else "  -   "
        lines.append(f"{f:<14} {r['n']:>5} {r['agree']:>7}  {rate}")
    for f in fields:
        dis = rep[f]["disagreements"]
        if dis:
            lines.append(f"first {len(dis)} {f} disagreements (espn vs nflverse):")
            for d in dis:
                g = f"{d.get('game')} " if d.get("game") else ""
                lines.append(f"  {g}Q{d['period']} {(d['clock'] or 0) // 60}:{(d['clock'] or 0) % 60:02d}  espn={d['espn']} nfl={d['nflverse']}  [{d['espn_play']}/{d['nfl_play']}] {d['text']}")
    return "\n".join(lines)


# ---- ESPN win-probability alignment ---------------------------------------------------------

def wp_alignment_samples(summary: dict, wp_fn: Optional[Callable[..., float]] = None, model: Any = None, spread_home: Optional[float] = None) -> dict[str, Any]:
    """One sample per scoring play: ESPN's ``winprobability`` entry for the play and for its
    neighbours (``espn_prev`` / ``espn_next``), plus the model's WP on the state before the
    play (``pre``) and on the next play's start state (``post``). The neighbour pair is the
    model-free test: if ESPN's entry already moved with the score, it describes the
    post-play state. The model pair is secondary evidence only — ESPN's model reacts
    differently from ours, so ESPN's number can sit between our two states. Also counts the
    plays whose id has no WP entry (the timeline's index+1 fallback)."""
    if wp_fn is None:
        from ..models.wp import home_win_probability as wp_fn  # type: ignore[assignment]
    rows, texts, type_ids, meta = ordered_rows(summary)
    wp = summary.get("winprobability") or []
    wp_by_id: dict[str, Optional[float]] = {}
    for w in wp:
        try:
            wp_by_id[str(w.get("playId"))] = float(w.get("homeWinPercentage")) if w.get("homeWinPercentage") is not None else None
        except (TypeError, ValueError):
            wp_by_id[str(w.get("playId"))] = None
    if spread_home is None:
        for pc in summary.get("pickcenter") or []:
            sp = pc.get("spread")
            if sp is not None:
                fav_home = (pc.get("homeTeamOdds") or {}).get("favorite")
                spread_home = -abs(float(sp)) if fav_home else abs(float(sp))
                break
    ko_home = receive_2h_ko_home(rows, texts, type_ids)

    def wp_of(r: Optional[PlayRow], hs: int, as_: int, gsr: Optional[int]) -> Optional[float]:
        if gsr is None:
            return None
        try:
            if r is None:
                return float(wp_fn(home_score=hs, away_score=as_, game_seconds_remaining=gsr, possession=None, vegas_spread_home=spread_home or 0.0, receive_2h_ko_home=ko_home, model=model))
            return float(wp_fn(home_score=hs, away_score=as_, game_seconds_remaining=gsr, possession=r.possession, down=r.down, distance=r.distance, yardline_100=r.yardline_100, vegas_spread_home=spread_home or 0.0, receive_2h_ko_home=ko_home, model=model))
        except Exception:
            return None

    samples: list[dict[str, Any]] = []
    fallback = sum(1 for r in rows if r.play_id not in wp_by_id)
    for i, r in enumerate(rows):
        if (r.home_score_after, r.away_score_after) == (r.home_score, r.away_score):
            continue
        espn_p = wp_by_id.get(r.play_id)
        if espn_p is None:
            continue
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        pre = wp_of(r, r.home_score, r.away_score, r.game_seconds_remaining)
        post = wp_of(nxt, r.home_score_after, r.away_score_after, nxt.game_seconds_remaining if nxt is not None else 0)
        # nearest neighbours that have an entry (official-timeout rows often have none)
        espn_prev = next((wp_by_id[q.play_id] for q in reversed(rows[:i]) if wp_by_id.get(q.play_id) is not None), None)
        espn_next = next((wp_by_id[q.play_id] for q in rows[i + 1:] if wp_by_id.get(q.play_id) is not None), None)
        samples.append({
            "play_id": r.play_id, "period": r.period, "clock": r.clock_seconds, "score_before": f"{r.away_score}-{r.home_score}", "score_after": f"{r.away_score_after}-{r.home_score_after}",
            "espn": espn_p, "espn_prev": espn_prev, "espn_next": espn_next,
            "pre": round(pre, 4) if pre is not None else None, "post": round(post, 4) if post is not None else None, "text": texts[i][:80],
        })
    return {"samples": samples, "n_plays": len(rows), "wp_entries": len(wp), "fallback_plays": fallback, "fallback_share": round(fallback / len(rows), 4) if rows else None, "home": meta.get("home"), "away": meta.get("away")}


def classify_wp_alignment(samples: Iterable[Any], min_n: int = 5) -> dict[str, Any]:
    """``'post'`` when ESPN's entry on a scoring play has already moved with the score (the
    jump sits between the previous entry and this one), ``'pre'`` when the jump comes after
    it, ``'unknown'`` below ``min_n`` samples or on a split (share within 0.1 of 0.5).
    Samples are dicts with ``espn``, ``espn_prev``, ``espn_next`` (the model-free test) and
    optionally ``pre`` / ``post`` (the model's numbers, reported as secondary evidence:
    ``model_share_closer_to_post`` and the mean |delta| under each hypothesis)."""
    n = jump_before = 0
    d_before = d_after = 0.0
    nm = closer_post = 0
    d_pre = d_post = 0.0
    for s in samples:
        e = s.get("espn")
        if e is None:
            continue
        ep, en = s.get("espn_prev"), s.get("espn_next")
        if ep is not None and en is not None:
            a, b = abs(e - ep), abs(en - e)
            n += 1
            d_before += a
            d_after += b
            if a > b:
                jump_before += 1
        pre, post = s.get("pre"), s.get("post")
        if pre is not None and post is not None:
            nm += 1
            a, b = abs(e - pre), abs(e - post)
            d_pre += a
            d_post += b
            if b < a:
                closer_post += 1
    share = jump_before / n if n else None
    if n < min_n or share is None or abs(share - 0.5) < 0.1:
        label = "unknown"
    else:
        label = "post" if share > 0.5 else "pre"
    return {
        "alignment": label, "n": n, "share_jump_before": round(share, 4) if share is not None else None,
        "mean_jump_before": round(d_before / n, 4) if n else None, "mean_jump_after": round(d_after / n, 4) if n else None,
        "model_n": nm, "model_share_closer_to_post": round(closer_post / nm, 4) if nm else None,
        "mean_abs_delta_pre": round(d_pre / nm, 4) if nm else None, "mean_abs_delta_post": round(d_post / nm, 4) if nm else None,
    }


# ---- offline read-through cache for ESPN payloads -------------------------------------------

def cached_json(cache_dir: Optional[str | os.PathLike], key: str, fetch: Callable[[], Any], offline: bool = False) -> Any:
    """``<cache_dir>/<key>.json`` if present, else ``fetch()`` (written back). ``offline``
    raises instead of fetching so a rerun cannot silently change its inputs. The layout
    matches the replay's cache (``out/cache/history/<venue>/<key>.json``) so the two share
    summaries."""
    path = Path(cache_dir) / f"{key}.json" if cache_dir else None
    if path is not None and path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    if offline:
        raise FileNotFoundError(f"offline: {path} missing")
    data = fetch()
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    return data

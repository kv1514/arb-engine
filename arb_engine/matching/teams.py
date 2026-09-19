from __future__ import annotations

import os

import json
import re
from importlib import resources
from typing import Optional


def _load() -> dict:
    with resources.files("arb_engine.data").joinpath("nfl_teams.json").open("r", encoding="utf-8") as f:
        return json.load(f)["teams"]


NFL_TEAMS: dict[str, dict] = _load()

_INDEX: dict[str, str] = {}
for code, info in NFL_TEAMS.items():
    keys = [code, info["city"], info["nick"], f"{info['city']} {info['nick']}"] + list(info.get("aliases", []))
    for k in keys:
        _INDEX[re.sub(r"[^a-z0-9]", "", k.lower())] = code


def nfl_team_code(name: Optional[str]) -> Optional[str]:
    """Map any venue spelling ("Los Angeles R", "Rams", "lar", "LA Rams") to a canonical code."""
    if not name:
        return None
    key = re.sub(r"[^a-z0-9]", "", str(name).lower())
    if key in _INDEX:
        return _INDEX[key]
    # Substring match on longer aliases ("Spread: Bills (-1.5)" -> BUF). Longest alias
    # first so "Los Angeles Rams" beats "LA".
    for k in sorted(_INDEX, key=len, reverse=True):
        if len(k) >= 4 and k in key:
            return _INDEX[k]
    return None


def nfl_team_city(code: Optional[str]) -> str:
    """'BUF' -> 'Buffalo' (falls back to the code)."""
    info = NFL_TEAMS.get(code or "")
    return info["city"] if info else (code or "")


# ---- other team sports: data/<sport>_teams.json (built by scripts/build_teams.py --sport …) ----

TEAM_SPORTS = ("nfl", "ncaaf", "nba", "nhl")  # sports with a canonical team-code table

_TABLES: dict[str, dict] = {}
_SPORT_INDEX: dict[str, dict[str, str]] = {}


def _norm_team(s: str) -> str:
    s = str(s).lower().replace("&", " and ")
    s = re.sub(r"\bst\.?\b", "state", s)
    return re.sub(r"[^a-z0-9]", "", s)


def team_table(sport: str) -> dict:
    if sport == "nfl":
        return NFL_TEAMS
    if sport not in _TABLES:
        path = os.path.join(os.path.dirname(__file__), "..", "data", f"{sport}_teams.json")
        try:
            with open(path, encoding="utf-8") as f:
                _TABLES[sport] = json.load(f)
        except FileNotFoundError:
            _TABLES[sport] = {}
    return _TABLES[sport]


def _sport_index(sport: str) -> dict[str, str]:
    if sport not in _SPORT_INDEX:
        owners: dict[str, set[str]] = {}
        for code, info in sorted(team_table(sport).items()):
            for a in [code, info.get("name") or ""] + list(info.get("aliases") or []):
                if a:
                    owners.setdefault(_norm_team(a), set()).add(code)
        # An alias shared by two teams ("Los Angeles" = Lakers and Clippers) resolves to nothing
        # rather than to whichever sorts first.
        _SPORT_INDEX[sport] = {k: next(iter(v)) for k, v in owners.items() if len(v) == 1}
    return _SPORT_INDEX[sport]


def team_code(sport: str, name: Optional[str]) -> Optional[str]:
    """Canonical team code for any venue spelling. NFL keeps its fuzzy matcher; other sports
    (761 college programs) need exact code / alias hits — substring matching would mis-pair
    'Miami' with 'Miami (OH)' or 'Washington' with 'Washington State'."""
    if sport == "nfl":
        return nfl_team_code(name)
    if not name:
        return None
    table = team_table(sport)
    if not table:
        return None
    raw = str(name).strip()
    if raw.upper() in table:
        return raw.upper()
    return _sport_index(sport).get(_norm_team(raw))


def team_name(sport: str, code: Optional[str]) -> str:
    if sport == "nfl":
        return nfl_team_city(code)
    info = team_table(sport).get(code or "")
    return (info or {}).get("name") or (code or "")


def ncaaf_team_code(name: Optional[str]) -> Optional[str]:
    return team_code("ncaaf", name)


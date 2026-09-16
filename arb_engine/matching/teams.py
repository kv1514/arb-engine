from __future__ import annotations

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

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Optional

from .teams import nfl_team_code

# US/Eastern without pulling in zoneinfo data (DST second Sunday of March -> first Sunday of November).
def _is_dst(dt_utc: datetime) -> bool:
    y = dt_utc.year
    march = datetime(y, 3, 1, tzinfo=timezone.utc)
    second_sunday_march = march + timedelta(days=(6 - march.weekday()) % 7 + 7)
    nov = datetime(y, 11, 1, tzinfo=timezone.utc)
    first_sunday_nov = nov + timedelta(days=(6 - nov.weekday()) % 7)
    start = second_sunday_march.replace(hour=7)  # 2am EST = 07:00 UTC
    end = first_sunday_nov.replace(hour=6)       # 2am EDT = 06:00 UTC
    return start <= dt_utc < end


def et_date(dt: Optional[datetime]) -> Optional[str]:
    """Calendar date in US/Eastern (YYYY-MM-DD) — the date sports schedules are quoted in."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_utc = dt.astimezone(timezone.utc)
    offset = -4 if _is_dst(dt_utc) else -5
    return (dt_utc + timedelta(hours=offset)).strftime("%Y-%m-%d")


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    """Parse the ISO-ish timestamps the venues emit ('2026-09-20T17:00:00Z',
    '2026-09-20 17:00:00+00', '2026-09-15T22:47:42.498000851Z')."""
    if not s:
        return None
    s = str(s).strip()
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)  # trim nanoseconds
    s = s.replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    m = re.search(r"([+-]\d{2})$", s)
    if m:
        s = s + ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


_MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


def kalshi_ticker_date(ticker: str) -> Optional[str]:
    """'KXNFLGAME-26SEP27BALDAL-BAL' -> '2026-09-27' (the schedule date embedded in the ticker)."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", ticker or "")
    if not m or m.group(2) not in _MONTHS:
        return None
    return f"20{m.group(1)}-{_MONTHS[m.group(2)]:02d}-{int(m.group(3)):02d}"


def normalize_person(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\(.*?\)", " ", s)          # "K. Miyoshi (b. 2004)"
    s = re.sub(r"[^A-Za-z\s\-']", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def person_key(name: str) -> str:
    """'Xiaodi You' / 'X. You' / 'YOU' -> 'you' ; 'R. Pacheco Mendez' -> 'pacheco mendez'.

    Uses the surname (everything after the first token when the first token is a full
    first name or an initial), which is what all three venues agree on. First initials are
    dropped because Kalshi ticker suffixes ('-YOU') do not carry them.
    """
    s = normalize_person(name)
    if not s:
        return ""
    parts = s.replace("-", " ").split()
    if len(parts) == 1:
        return parts[0]
    return " ".join(parts[1:])


def person_keys(names: list[str]) -> list[str]:
    """Person keys for the participants of one event, guaranteed distinct: if two
    players share a surname, fall back to the full normalised name for both."""
    keys = [person_key(n) for n in names]
    if len(set(keys)) < len(keys):
        keys = [normalize_person(n) for n in names]
    return keys


def tennis_event_key(names: list[str], date_str: Optional[str]) -> str:
    keys = sorted(person_keys(names))
    return "tennis:" + "|".join(keys) + ":" + (date_str or "")


def nfl_event_key(team_names: list[str], date_str: Optional[str]) -> Optional[str]:
    codes = [nfl_team_code(n) for n in team_names]
    if any(c is None for c in codes):
        return None
    return "nfl:" + "|".join(sorted(codes)) + ":" + (date_str or "")  # type: ignore[arg-type]

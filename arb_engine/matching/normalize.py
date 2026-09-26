from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Optional

from .teams import canonical_from_ticker, kalshi_spelling, nfl_team_code, team_table

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


def team_event_key(sport: str, codes: list[str], date_str: Optional[str]) -> str:
    """Generic two-team key: '<sport>:<A>|<B>:<ET date>' with codes sorted."""
    return f"{sport}:" + "|".join(sorted(codes)) + ":" + (date_str or "")


def nfl_event_key(team_names: list[str], date_str: Optional[str]) -> Optional[str]:
    codes = [nfl_team_code(n) for n in team_names]
    if any(c is None for c in codes):
        return None
    return "nfl:" + "|".join(sorted(codes)) + ":" + (date_str or "")  # type: ignore[arg-type]


# ---- spreads / totals ---------------------------------------------------------------------

def fmt_line(x: float) -> str:
    """1.5 -> '1.5', 49.5 -> '49.5', 3.0 -> '3' (stable text for keys and labels)."""
    return f"{float(x):g}"


def spread_outcomes(fav: str, dog: str, line: float) -> tuple[str, str]:
    """Outcome keys for 'fav wins by more than line': ('BUF-1.5', 'DET+1.5')."""
    return f"{fav}-{fmt_line(line)}", f"{dog}+{fmt_line(line)}"


def spread_event_key(sport: str, codes: list[str], date_str: Optional[str], fav: str, line: float) -> str:
    return f"{sport}:" + "|".join(sorted(codes)) + f":{date_str or ''}:spread:{fav}-{fmt_line(line)}"


def total_event_key(sport: str, codes: list[str], date_str: Optional[str], line: float) -> str:
    return f"{sport}:" + "|".join(sorted(codes)) + f":{date_str or ''}:total:{fmt_line(line)}"


def game_event_key(event_key: str) -> str:
    """The game behind a line key: ``nfl:BUF|DET:2026-09-17:spread:BUF-1.5`` ->
    ``nfl:BUF|DET:2026-09-17``; a moneyline key is returned unchanged."""
    for tag in (":spread:", ":total:"):
        if tag in event_key:
            return event_key.split(tag, 1)[0]
    return event_key


def split_pair(pair: str, known: str) -> Optional[str]:
    """'DETBUF' with known 'BUF' -> 'DET' (the other code in a Kalshi/Rothera ticker pair)."""
    if pair.endswith(known) and len(pair) > len(known):
        return pair[: -len(known)]
    if pair.startswith(known) and len(pair) > len(known):
        return pair[len(known):]
    return None


def split_ticker_pair(pair: str, sport: str, known: Optional[str] = None) -> Optional[tuple[str, str]]:
    """Split an away+home ticker blob into canonical (away, home).

    Every cut whose two slices are teams (a canonical code, a Kalshi spelling or an exact
    alias: ``NEJAC`` is NE @ JAX) is a reading. ``known`` is the favourite's ticker code on
    a spread and keeps only the cuts that contain it (``BENCAPU`` given ``BEN`` vs ``BENC``).
    Two distinct readings are settled by the teams' own Kalshi spellings - ``TOWSDSU`` is
    Towson (Kalshi ``TOWS``) @ Delaware State, not ``TOW`` @ San Diego State - and are
    otherwise refused: ``MURMU`` is Methodist + Robert Morris or Murray State + Methodist.
    """
    blob = (pair or "").upper()
    if len(blob) < 4 or not team_table(sport):
        return None
    known_u = known.upper() if known else None
    readings: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for cut in range(2, len(blob) - 1):          # both codes at least two letters
        left, right = blob[:cut], blob[cut:]
        if known_u is not None and known_u not in (left, right):
            continue
        away, home = canonical_from_ticker(sport, left), canonical_from_ticker(sport, right)
        if away and home and away != home:
            readings.setdefault((away, home), []).append((left, right))
    if len(readings) > 1:
        spelled = [p for p, cuts in readings.items()
                   if any(kalshi_spelling(sport, p[0]) == l and kalshi_spelling(sport, p[1]) == r for l, r in cuts)]
        return spelled[0] if len(spelled) == 1 else None
    return next(iter(readings)) if readings else None


def ticker_pair(event_ticker: str) -> Optional[str]:
    """'KXNFLSPREAD-26SEP17DETBUF' -> 'DETBUF' (codes after the date block)."""
    m = re.search(r"-\d{2}[A-Z]{3}\d{2}([A-Z0-9]+)$", event_ticker or "")
    return m.group(1) if m else None


def strip_digits(s: str) -> str:
    return re.sub(r"\d+$", "", s or "")


def push_rule_for_line(line: float) -> str:
    """Half-point lines cannot push; integer lines can (venues settle pushes differently)."""
    return "no_push" if abs(float(line) * 2 - round(float(line) * 2)) < 1e-9 and round(float(line) * 2) % 2 == 1 else "push_possible"

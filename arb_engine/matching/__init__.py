"""Cross-venue identity: canonical team/player keys and event keys."""

from .teams import nfl_team_code, NFL_TEAMS
from .normalize import (
    normalize_person,
    person_key,
    person_keys,
    et_date,
    parse_iso,
    tennis_event_key,
    nfl_event_key,
    kalshi_ticker_date,
)
from .matcher import merge_snapshots, MergedEvent

__all__ = [
    "nfl_team_code", "NFL_TEAMS", "normalize_person", "person_key", "person_keys", "et_date", "parse_iso",
    "tennis_event_key", "nfl_event_key", "kalshi_ticker_date", "merge_snapshots", "MergedEvent",
]

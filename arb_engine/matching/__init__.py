"""Cross-venue identity: canonical team/player keys and event keys."""

from .teams import nfl_team_code, nfl_team_city, NFL_TEAMS
from .normalize import (
    normalize_person,
    person_key,
    person_keys,
    et_date,
    parse_iso,
    tennis_event_key,
    nfl_event_key,
    kalshi_ticker_date,
    fmt_line,
    spread_outcomes,
    spread_event_key,
    total_event_key,
    game_event_key,
    split_pair,
    split_ticker_pair,
    ticker_pair,
    strip_digits,
    push_rule_for_line,
)
from .matcher import merge_snapshots, MergedEvent

__all__ = [
    "nfl_team_code", "nfl_team_city", "NFL_TEAMS", "normalize_person", "person_key", "person_keys", "et_date", "parse_iso",
    "tennis_event_key", "nfl_event_key", "kalshi_ticker_date", "fmt_line", "spread_outcomes", "spread_event_key", "total_event_key", "game_event_key", "split_pair", "split_ticker_pair", "ticker_pair", "strip_digits", "push_rule_for_line", "merge_snapshots", "MergedEvent",
]

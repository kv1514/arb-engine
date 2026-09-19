"""Venue eligibility: which venues a US-resident account can *execute* on, as a table with a
verified date and a source per row (``data/venue_rules.json``).

Why a table and not a constant: nothing else in the engine asks whether the account can
hold the leg it sizes. Global Polymarket has barred US persons since the 2022 CFTC order,
yet its quotes are the best hedge on many NFL lines, so a scan "arb" or a maker rest whose
hedge sits there is unreachable — the price is a fair-value reference, not a leg. The
table makes that a checked fact with a re-verification date instead of tribal knowledge.

    executable_venues(settings)          -> {"kalshi", "robinhood"} by default
    executable_venues(settings, quotes=…) -> the same minus venues whose quote for this
                                            event carries meta.restricted (Gamma geoblock)
    stale_verification(days=30)          -> rows whose ``verified`` date is older than that

``EXECUTABLE_VENUES`` (env, or settings["executable_venues"]) overrides the table for an
account that really can trade elsewhere (non-US operator, or a future Polymarket US adapter).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

RULES_PATH = Path(__file__).parent / "data" / "venue_rules.json"
SETTING_KEY = "executable_venues"
UNRESTRICTED = {"all", "*"}  # the explicit way to lift the table (a None / unset value keeps it)
ENV_KEY = "EXECUTABLE_VENUES"
STALE_DAYS = 30


def _csv(v: Any) -> Optional[list[str]]:
    """'kalshi, robinhood' | ['kalshi'] | None -> lowercase list (None stays None)."""
    if v is None:
        return None
    if isinstance(v, str):
        return [x.strip().lower() for x in v.split(",") if x.strip()]
    return [str(x).strip().lower() for x in v if str(x).strip()]


try:  # P01's settings registry; keep importable without it.
    from .config import declare_setting as _declare_setting
except ImportError:  # pragma: no cover - depends on P01
    _declare_setting = None
if _declare_setting is not None:
    try:
        _declare_setting(SETTING_KEY, env=ENV_KEY, default=None, cast=_csv, doc="comma list of venues this account can execute on; overrides data/venue_rules.json")
    except Exception:  # never let a registry quirk break imports
        pass


def _setting(settings: Optional[Mapping[str, Any]], key: str, env: str) -> Any:
    """settings dict -> environment -> None (mirrors config.setting without requiring P01)."""
    if settings and settings.get(key) is not None:
        return settings[key]
    return os.environ.get(env) or None


@lru_cache(maxsize=1)
def load_rules(path: Optional[str] = None) -> dict[str, dict[str, Any]]:
    """The venue table keyed by venue. Cached: it is read on every scan/rest decision."""
    with open(path or RULES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    venues = data.get("venues", data)
    return {k.lower(): dict(v) for k, v in venues.items() if not k.startswith("_")}


def rule(venue: str) -> dict[str, Any]:
    return load_rules().get(venue.lower(), {})


def is_executable(venue: str, settings: Optional[Mapping[str, Any]] = None, home_state: Optional[str] = None) -> bool:
    return venue.lower() in executable_venues(settings, home_state=home_state)


def executable_venues(settings: Optional[Mapping[str, Any]] = None, home_state: Optional[str] = None, quotes: Optional[Iterable[Any]] = None, with_adapter_only: bool = True) -> set[str]:
    """Venues this account can execute on.

    ``settings["executable_venues"]`` / ``EXECUTABLE_VENUES`` replaces the table wholesale
    (an explicit operator statement). ``home_state`` drops venues whose table row lists that
    state. ``quotes`` (this event's OutcomeQuotes) drops a venue whose quote carries
    ``meta.restricted`` — Gamma's restricted-jurisdictions flag (P08 attaches it) — unless
    the override names that venue: the flag is set on every Polymarket sports market, so it
    can only confirm the table, never veto an explicit operator statement.
    """
    override = _csv(_setting(settings, SETTING_KEY, ENV_KEY))
    if override is not None and set(override) & UNRESTRICTED:  # "all" / "*": every venue with an adapter
        override = [v for v, r in load_rules().items() if r.get("adapter", True) or not with_adapter_only]
    if override is not None:
        out = set(override)
    else:
        out = {v for v, r in load_rules().items() if r.get("executable_for_us") is True and (r.get("adapter", True) or not with_adapter_only)}
    if home_state:
        hs = home_state.strip().upper()
        out = {v for v in out if hs not in {s.upper() for s in load_rules().get(v, {}).get("state_restrictions", [])}}
    if quotes is not None:
        # Gamma sets restricted=true on every sports market we have captured (it is the
        # venue-wide "restricted jurisdictions" flag, not a per-event geoblock), so it only
        # backs the table's default — an explicit operator override still wins.
        for q in quotes:
            meta = getattr(q, "meta", None) or {}
            v = str(getattr(q, "venue", "")).lower()
            if meta.get("restricted") is True and not (override is not None and v in override):
                out.discard(v)
    return out


def ineligible_reason(venue: str, settings: Optional[Mapping[str, Any]] = None, quote: Any = None) -> Optional[str]:
    """Human sentence for journals/alerts, or None when the venue is executable."""
    v = venue.lower()
    meta = (getattr(quote, "meta", None) or {}) if quote is not None else {}
    explicit = _csv(_setting(settings, SETTING_KEY, ENV_KEY))
    if meta.get("restricted") is True and not (explicit is not None and v in explicit):
        return f"{v}: market flagged restricted (restricted jurisdictions) by the venue"
    if v in executable_venues(settings):
        return None
    r = rule(v)
    src = r.get("source", "no source")
    if r.get("executable_for_us") is True and not r.get("adapter", True):
        return f"{v}: executable for US persons but no adapter in this repo ({src})"
    if r:
        return f"{v}: not executable for US persons per venue_rules.json ({src}, verified {r.get('verified', '?')})"
    return f"{v}: not in venue_rules.json"


def eligibility_note(venue: str, settings: Optional[Mapping[str, Any]] = None, quote: Any = None) -> str:
    """Short parenthetical for every hedge alert, so the operator never learns at the bank
    that the leg was unreachable."""
    reason = ineligible_reason(venue, settings, quote)
    return "executable for US accounts" if reason is None else "NOT EXECUTABLE — " + reason


def stale_verification(days: int = STALE_DAYS, today: Optional[_dt.date] = None, rules: Optional[Mapping[str, Mapping[str, Any]]] = None) -> dict[str, int]:
    """{venue: age_in_days} for rows verified more than ``days`` ago, or with no/invalid date."""
    today = today or _dt.date.today()
    out: dict[str, int] = {}
    for v, r in (rules or load_rules()).items():
        try:
            d = _dt.date.fromisoformat(str(r.get("verified", "")))
        except ValueError:
            out[v] = 10**6
            continue
        age = (today - d).days
        if age > days:
            out[v] = age
    return out


def check_table(rules: Optional[Mapping[str, Mapping[str, Any]]] = None) -> list[str]:
    """Structural problems with the table (missing verified/source, bad types). Empty = ok."""
    problems: list[str] = []
    for v, r in (rules or load_rules()).items():
        if not isinstance(r.get("executable_for_us"), bool):
            problems.append(f"{v}: executable_for_us must be a bool")
        if not r.get("verified"):
            problems.append(f"{v}: missing verified date")
        else:
            try:
                _dt.date.fromisoformat(str(r["verified"]))
            except ValueError:
                problems.append(f"{v}: verified is not an ISO date")
        if not str(r.get("source", "")).strip():
            problems.append(f"{v}: missing source")
    return problems

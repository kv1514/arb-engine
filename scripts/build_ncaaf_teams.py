#!/usr/bin/env python3
"""Build arb_engine/data/ncaaf_teams.json from ESPN's college-football team list, then learn the
venue codes that differ from ESPN's abbreviations: Kalshi ticker codes (from open KXNCAAFGAME
markets, resolved through their titles) and Robinhood/CDNA short names (from the catalogue when
cached). Re-run any time; manual aliases at the bottom survive.

    ARB_HTTP_TRANSPORT=curl python3 scripts/build_ncaaf_teams.py
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from arb_engine.venues.http import HttpClient  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "arb_engine", "data", "ncaaf_teams.json")
MANUAL_ALIASES = {  # canonical ESPN abbreviation -> extra spellings seen on venues
    "MIA": ["Miami FL", "Miami (FL)", "Miami Hurricanes"],
    "M-OH": ["Miami OH", "Miami (OH)", "Miami RedHawks"],
    "USC": ["Southern California", "USC Trojans"],
    "SC": ["South Carolina Gamecocks"],
    "LSU": ["Louisiana State"],
    "UCF": ["Central Florida"],
    "SMU": ["Southern Methodist"],
    "TCU": ["Texas Christian"],
    "BYU": ["Brigham Young"],
    "UNLV": ["Nevada-Las Vegas", "Nevada Las Vegas"],
    "ULM": ["Louisiana-Monroe", "Louisiana Monroe", "UL Monroe"],
    "UL": ["Louisiana-Lafayette", "Louisiana Lafayette", "Louisiana Ragin Cajuns", "UL Lafayette"],
    "UTSA": ["Texas-San Antonio", "Texas San Antonio"],
    "UTEP": ["Texas-El Paso", "Texas El Paso"],
    "FIU": ["Florida International"],
    "FAU": ["Florida Atlantic"],
    "UAB": ["Alabama-Birmingham", "Alabama Birmingham"],
    "UMASS": ["Massachusetts"],
    "UCONN": ["Connecticut"],
    "APP": ["Appalachian State", "App State"],
    "OKST": ["Oklahoma State"],
    "MSST": ["Mississippi State"],
    "MISS": ["Ole Miss", "Mississippi"],
    "SJSU": ["San Jose State", "San José State"],
    "HAW": ["Hawaii", "Hawai'i"],
    "NCSU": ["North Carolina State", "North Carolina St.", "NCST"],
    "UALB": ["University at Albany", "Albany", "ALBY"],
}
# Programs missing from ESPN's team list (added by hand): code -> (name, nick, aliases)
EXTRA_TEAMS = {
    "UTRGV": ("UT Rio Grande Valley", "Vaqueros", ["UTRGV", "UT Rio Grande Valley", "Texas-Rio Grande Valley", "Rio Grande Valley"]),
}


def norm(s: str) -> str:
    s = s.lower().replace("&", " and ")
    s = re.sub(r"\bst\.?\b", "state", s)
    s = re.sub(r"\bu\.?s\.?c\b", "usc", s)
    return re.sub(r"[^a-z0-9]", "", s)


def main() -> int:
    h = HttpClient(transport=os.environ.get("ARB_HTTP_TRANSPORT"))
    data = h.get("https://site.api.espn.com/apis/site/v2/sports/football/college-football/teams", {"limit": 1000}, headers={"Accept": "application/json"})
    teams = [x["team"] for x in data["sports"][0]["leagues"][0]["teams"]]
    out: dict[str, dict] = {}
    for t in teams:
        code = t.get("abbreviation")
        if not code:
            continue
        aliases = {t.get("displayName"), t.get("shortDisplayName"), t.get("location"), t.get("nickname"), f"{t.get('location')} {t.get('name')}".strip()}
        aliases = sorted(a for a in aliases if a)
        out[code] = {"name": t.get("location"), "nick": t.get("name"), "espn_id": t.get("id"), "aliases": aliases}
    for code, (name, nick, aliases) in EXTRA_TEAMS.items():
        out.setdefault(code, {"name": name, "nick": nick, "espn_id": None, "aliases": list(aliases)})
    for code, extra in MANUAL_ALIASES.items():
        if code in out:
            for a in extra:
                if a not in out[code]["aliases"]:
                    out[code]["aliases"].append(a)
    index = {}
    for code, info in out.items():
        for a in [code] + info["aliases"]:
            index.setdefault(norm(a), code)
    # Kalshi codes: open game markets, title "X wins".
    learned = 0
    try:
        k = h.get("https://api.elections.kalshi.com/trade-api/v2/markets", {"series_ticker": "KXNCAAFGAME", "status": "open", "limit": 1000})
        for m in k.get("markets", []):
            kcode = m["ticker"].rsplit("-", 1)[1]
            title = re.sub(r"\s+wins$", "", m.get("title", "")).strip()
            code = index.get(norm(kcode)) or index.get(norm(title))
            if code and kcode not in out[code]["aliases"] and kcode != code:
                out[code]["aliases"].append(kcode)
                out[code].setdefault("kalshi", kcode)
                learned += 1
            elif code and kcode == code:
                out[code].setdefault("kalshi", kcode)
            elif not code:
                print(f"unresolved kalshi: {kcode} {title!r}", file=sys.stderr)
    except Exception as e:
        print(f"kalshi codes skipped: {e!r}", file=sys.stderr)
    # Robinhood/CDNA short names from a cached catalogue, if present.
    cat = os.path.join(os.path.dirname(__file__), "..", "out", "cache", "robinhood_college-football_catalogue.json")
    if os.path.exists(cat):
        pp = json.load(open(cat))
        evs = pp.get("events") or {}
        evs = list(evs.values()) if isinstance(evs, dict) else evs
        for ev in evs:
            for c in (ev.get("eventContracts") or {}).values():
                if not str(c.get("symbol", "")).startswith(("NX.F.OPT.CFB", "KXNCAAFGAME", "NCAAFGAME")):
                    continue
                short, long_ = c.get("displayShortName"), c.get("displayLongName")
                if re.search(r"points|\bover\b|\bunder\b|[+-]\d", (long_ or "") + (short or ""), re.I):
                    continue  # spread / total contracts, not team names
                code = index.get(norm(short or "")) or index.get(norm(long_ or ""))
                if code and short and short != code and short not in out[code]["aliases"]:
                    out[code]["aliases"].append(short)
                    learned += 1
                elif not code:
                    print(f"unresolved robinhood: {short!r} {long_!r}", file=sys.stderr)
    for code, extra in MANUAL_ALIASES.items():
        if code in out:
            for a in extra:
                if a not in out[code]["aliases"]:
                    out[code]["aliases"].append(a)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=0, sort_keys=True)
    print(f"{len(out)} teams, {learned} venue aliases learned -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

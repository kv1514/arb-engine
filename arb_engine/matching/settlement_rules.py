"""Settlement-rule registry: how each venue settles ties, postponements, cancellations,
walkovers and retirements, keyed by (venue, sport, market_type, exchange).

Why a registry and not adapter constants: a Kalshi × Polymarket "hedge" is only a hedge in
the states where both venues pay the same. They do not on cancellations (Kalshi settles at a
"fair price" it determines, Polymarket pays 50-50), on postponements (Kalshi keeps a game
market open 48 h then fair-prices it, Polymarket keeps it open until played) and on tennis
walkovers. Rothera and CDNA publish nothing we can read, so their rows are marked
``unverified`` and flagged. Every row cites a text fixture under ``tests/fixtures/rules/``
by sha256 so a rule change on a venue is a test failure, not a silent drift.

Pure functions only; the scanner (P09) calls ``pair_flags`` / ``tennis_pair_flags`` through a
guarded import and keeps the arb definition itself unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "settlement_rules.json"
RULES_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "rules"

FIELDS = ("tie", "postponed", "cancelled", "walkover", "retirement", "ot_included")
STATUSES = ("verbatim", "derived", "unverified")
TIERS = ("tour", "challenger", "itf")

# Provisional walkover shares, used only for a tier the settled feed has not covered yet
# (Kalshi lists no ITF series, so ``itf`` stays provisional until Polymarket resolutions are
# tallied). Tour/challenger rows in the JSON carry script-derived values with ``n``.
PROVISIONAL_WALKOVER = {"tour": 0.03, "challenger": 0.09, "itf": 0.09}
DEFAULT_THIN_SPREAD = 0.03   # |yes_ask + no_ask - 1| above this = a thin, wide book
DEFAULT_THIN_SIZE = 20.0     # top-of-book contracts below this = thin

try:  # P01 settings helper; optional so this module imports on any branch.
    from ..config import declare_setting  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - P01 may not be present
    declare_setting = None
if declare_setting is not None:  # pragma: no cover - exercised only with P01
    declare_setting("tennis_thin_book_spread", env="ARB_TENNIS_THIN_SPREAD", default=DEFAULT_THIN_SPREAD, cast=float, doc="Tennis 'thin-book' flag when |yes_ask + no_ask - 1| exceeds this")
    declare_setting("tennis_thin_book_size", env="ARB_TENNIS_THIN_SIZE", default=DEFAULT_THIN_SIZE, cast=float, doc="Tennis 'thin-book' flag when the top-of-book size is below this many contracts")
    for _tier in TIERS:
        declare_setting(f"tennis_walkover_p_{_tier}", env=f"ARB_TENNIS_WALKOVER_P_{_tier.upper()}", default=None, cast=float, doc=f"Override the {_tier}-tier walkover probability from settlement_rules.json")


# ---- registry ------------------------------------------------------------------------------

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def load(path: Optional[str] = None) -> dict[str, Any]:
    """The registry JSON (cached). ``rules`` rows + ``tennis_walkover`` rows + ``version``."""
    with open(path or DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


def rules() -> list[dict[str, Any]]:
    return list(load().get("rules", []))


def _mt_matches(row_mt: Any, market_type: Optional[str]) -> bool:
    if row_mt in (None, "*"):
        return True
    if isinstance(row_mt, list):
        return market_type in row_mt
    return row_mt == market_type


def lookup(venue: str, sport: str, market_type: str = "moneyline", exchange: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The registry row for one quote source, or None. Robinhood's KalshiEX mirror is Kalshi's
    book *and* Kalshi's rules, so it resolves to the Kalshi row; a row with ``exchange: null``
    matches any exchange when no exchange-specific row exists."""
    venue, sport = (venue or "").lower(), (sport or "").lower()
    exchange = (exchange or "").lower() or None
    if venue == "robinhood" and exchange == "kalshi":
        return lookup("kalshi", sport, market_type, None)
    best, best_score = None, -1
    for row in rules():
        if row.get("venue") != venue or row.get("sport") != sport or not _mt_matches(row.get("market_type"), market_type):
            continue
        rx = row.get("exchange")
        if rx not in (None, exchange):
            continue
        score = (2 if rx == exchange and rx is not None else 0) + (1 if isinstance(row.get("market_type"), str) and row.get("market_type") != "*" else 0)
        if score > best_score:
            best, best_score = row, score
    return best


def quote_exchange(q: Any) -> Optional[str]:
    meta = getattr(q, "meta", None) or {}
    fp = getattr(q, "fee_params", None) or {}
    return meta.get("exchange") or fp.get("exchange")


def rule_for_quote(q: Any, sport: str, market_type: str = "moneyline") -> Optional[dict[str, Any]]:
    return lookup(getattr(q, "venue", ""), sport, market_type, quote_exchange(q))


def rule_key(row: Mapping[str, Any]) -> str:
    """Identity of the *book* a row describes (Kalshi mirrors collapse onto Kalshi)."""
    return f"{row.get('venue')}/{row.get('exchange') or '-'}"


# ---- tennis constants re-exported from the JSON -------------------------------------------

_TENNIS_KEYS = ("retirement", "walkover", "cancelled", "postponed")


def tennis_settlement(venue: str) -> dict[str, str]:
    """The four-key dict the adapters store in ``EventInfo.venues[venue]['settlement']``.
    Same shape/values as the adapters' local ``TENNIS_SETTLEMENT`` constants so P11/P12 can
    switch ``venues/kalshi.py`` to this without changing scanner flags."""
    row = lookup(venue, "tennis", "moneyline") or {}
    return {k: row.get(k) for k in _TENNIS_KEYS if row.get(k) is not None}


def _tennis_export() -> dict[str, dict[str, str]]:
    try:
        return {v: tennis_settlement(v) for v in ("kalshi", "polymarket")}
    except (OSError, ValueError):  # registry missing/corrupt: fall back to the adapters' literals
        return {}


TENNIS_SETTLEMENT: dict[str, dict[str, str]] = _tennis_export()
KALSHI_TENNIS_SETTLEMENT: dict[str, str] = TENNIS_SETTLEMENT.get("kalshi", {})
POLYMARKET_TENNIS_SETTLEMENT: dict[str, str] = TENNIS_SETTLEMENT.get("polymarket", {})


# ---- rule-text parsers -----------------------------------------------------------------------

_S = r"[^.]*?"  # within one sentence (rule texts avoid abbreviations with periods)


def _num(word: str) -> Optional[int]:
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "fourteen": 14}
    return int(word) if word.isdigit() else words.get(word.lower())


def parse_kalshi(rules_primary: Optional[str], rules_secondary: Optional[str]) -> dict[str, Any]:
    """Settlement fields from a Kalshi market's ``rules_primary`` + ``rules_secondary``.
    Fields the text does not state are None (a half-point spread has no tie clause; NBA
    markets say nothing about ties because the league has none)."""
    p, s = (rules_primary or ""), (rules_secondary or "")
    text = f"{p}\n{s}"
    out: dict[str, Any] = {k: None for k in FIELDS}
    if re.search(rf"\btie\b{_S}(?:\$0\.50|50c\b|50 cents|\$\.50)", text, re.I | re.S):
        out["tie"] = "half"
    m = re.search(rf"postponed{_S}within (\d+|two|one) hours?{_S}remain open", text, re.I | re.S)
    if m:
        out["postponed"] = f"open_{_num(m.group(1))}h"
    else:
        m = re.search(rf"postponed{_S}remain open{_S}within (\d+|two|one) weeks?", text, re.I | re.S)
        if m:
            out["postponed"] = f"open_{_num(m.group(1))}w"
    if re.search(rf"(?:cancel\w*|not started){_S}fair price", text, re.I | re.S):
        out["cancelled"] = "fair_price"
    if re.search(rf"walkover{_S}fair price", text, re.I | re.S):
        out["walkover"] = "fair_price"
    if re.search(r"after a ball has been played", p, re.I):
        # "X wins ... after a ball has been played": the tour records a retirement as a win for
        # the opponent, so the advancing player's YES pays (derived, matches Polymarket).
        out["retirement"] = "advancer"
    if re.search(r"\bovertime\b|\bincluding (?:any )?overtime", text, re.I):
        out["ot_included"] = True
    return out


def parse_polymarket(description: Optional[str]) -> dict[str, Any]:
    """Settlement fields from a Polymarket market ``description``. Handles the game template
    (postponed → stays open; cancelled → 50-50; tie → 50-50; "Overtime is included") and the
    tennis template in both its 7-day and 14-day forms plus an explicit deadline date."""
    d = description or ""
    out: dict[str, Any] = {k: None for k in FIELDS}
    if re.search(rf"\btie\b{_S}50-50", d, re.I | re.S):
        out["tie"] = "half"
    if re.search(rf"postponed{_S}remain open", d, re.I | re.S) or re.search(r"postponement alone will not trigger", d, re.I):
        out["postponed"] = "open_until_complete"
    m = re.search(r"(?:delayed (?:beyond|by more than)|within|after) (\d+) days", d, re.I) or re.search(r"\((\d+) days after the scheduled start\)", d, re.I)
    if m:
        out["postponed"] = f"50-50_after_{int(m.group(1))}d"
    elif out["postponed"] is None and re.search(r"not been (?:determined|completed) by [A-Z][a-z]+ \d{1,2}, \d{4}", d):
        out["postponed"] = "50-50_after_date"
    if re.search(rf"cancel\w*{_S}50-50", d, re.I | re.S):
        out["cancelled"] = "50-50"
    if re.search(rf"walkover{_S}50-50", d, re.I | re.S):
        out["walkover"] = "50-50"
    if re.search(rf"retire\w*{_S}(?:player who advances|advances)", d, re.I | re.S):
        out["retirement"] = "advancer"
    if re.search(r"overtime is included|including (?:any )?overtime", d, re.I):
        out["ot_included"] = True
    return out


# ---- pair flags -------------------------------------------------------------------------------

def _status_flags(row: Mapping[str, Any]) -> list[str]:
    status = row.get("status", "unverified")
    if status == "verbatim":
        # A verbatim row may still carry individual inferred fields (``derived_fields``):
        # surface those per field so the provenance is visible without demoting the row.
        return [f"settlement-rule-derived:{row.get('venue')}:{f}" for f in row.get("derived_fields") or ()]
    flags = [f"settlement-rule-{status}:{row.get('venue')}"]
    if row.get("tie") is not None:
        flags.append(f"tie-rule-{status}")
    return flags


def compare_rules(row_a: Optional[Mapping[str, Any]], row_b: Optional[Mapping[str, Any]], venue_a: str = "?", venue_b: str = "?") -> list[str]:
    """Flags for two registry rows: ``settlement-mismatch:<field>`` where both venues state a
    rule and it differs, ``settlement-unstated:<field>`` where only one side states it, plus
    the rows' own status flags. Two quotes on the same book never mismatch."""
    flags: list[str] = []
    if row_a is None:
        flags.append(f"settlement-rule-missing:{venue_a}")
    if row_b is None:
        flags.append(f"settlement-rule-missing:{venue_b}")
    if row_a is None or row_b is None:
        return flags
    if rule_key(row_a) == rule_key(row_b):
        return sorted(set(flags))  # same book, same rules: provenance is moot for the pair
    for row in (row_a, row_b):
        flags.extend(_status_flags(row))
    for f in FIELDS:
        va, vb = row_a.get(f), row_b.get(f)
        if va is None and vb is None:
            continue
        if va is None or vb is None:
            flags.append(f"settlement-unstated:{f}")
        elif va != vb:
            flags.append(f"settlement-mismatch:{f}")
    return sorted(set(flags))


def pair_flags(quote_a: Any, quote_b: Any, sport: str, market_type: str = "moneyline") -> list[str]:
    """Settlement flags for a two-leg cross-venue pair. Robinhood KX quotes resolve to the
    Kalshi row, so Kalshi × Robinhood-KX returns nothing (same book, same rules)."""
    ra, rb = rule_for_quote(quote_a, sport, market_type), rule_for_quote(quote_b, sport, market_type)
    name = lambda q: f"{getattr(q, 'venue', '?')}" + (f"/{quote_exchange(q)}" if quote_exchange(q) else "")  # noqa: E731
    return compare_rules(ra, rb, name(quote_a), name(quote_b))


# ---- tennis gates ------------------------------------------------------------------------------

def tennis_tier(ident: Optional[str]) -> str:
    """'tour' | 'challenger' | 'itf' from a Kalshi ticker (``KXATPCHALLENGERMATCH-…``), a
    Polymarket slug (``itf-…``, ``wta-…``) or a tournament name (``WTA 125K …``, ``W15 …``)."""
    s = (ident or "").upper()
    if not s:
        return "tour"
    if "CHALLENGER" in s or "CHALL-" in s or re.search(r"\b(?:WTA|ATP)?\s?125K?\b", s):
        return "challenger"
    if s.startswith("ITF") or "-ITF-" in s or re.search(r"\b[MW](?:15|25|35|50|60|75|100)\b", s):
        return "itf"
    return "tour"


def walkover_probability(tier: str, settings: Optional[Mapping[str, Any]] = None) -> tuple[float, str]:
    """(p_walkover, provenance) for a tier: a settings override, else the registry row
    (``derived`` when scripts/tennis_settlement_share.py produced it with ``n``), else the
    provisional constant."""
    settings = settings or {}
    override = settings.get(f"tennis_walkover_p_{tier}")
    if override is not None:
        return float(override), "settings"
    for row in load().get("tennis_walkover", []):
        if row.get("tier") == tier and row.get("p_walkover") is not None:
            return float(row["p_walkover"]), str(row.get("status", "provisional"))
    return PROVISIONAL_WALKOVER.get(tier, PROVISIONAL_WALKOVER["tour"]), "provisional"


def _legs(pair: Any) -> list[Any]:
    if isinstance(pair, Mapping):
        return list(pair.get("legs") or pair.get("quotes") or [])
    for attr in ("legs", "quotes"):
        v = getattr(pair, attr, None)
        if v:
            return [getattr(l, "quote", l) for l in v]
    return list(pair) if isinstance(pair, (list, tuple)) else []


def _margin(pair: Any) -> Optional[float]:
    v = pair.get("margin") if isinstance(pair, Mapping) else getattr(pair, "margin", None)
    return None if v is None else float(v)


def _top(q: Any) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """(ask, bid, ask_size) preferring the attached book over the summary quote."""
    book = getattr(q, "book", None)
    ask, bid, size = getattr(q, "ask", None), getattr(q, "bid", None), getattr(q, "ask_size", None)
    if book is not None:
        if getattr(book, "asks", None):
            ask, size = book.asks[0].price, book.asks[0].size
        if getattr(book, "bids", None):
            bid = book.bids[0].price
    return ask, bid, size


def _pair_tier(pair: Any, legs: Iterable[Any]) -> str:
    explicit = pair.get("tier") if isinstance(pair, Mapping) else getattr(pair, "tier", None)
    if explicit:
        return str(explicit)
    tiers = set()
    for q in legs:
        meta = getattr(q, "meta", None) or {}
        for ident in (meta.get("ticker"), meta.get("slug"), meta.get("symbol"), getattr(q, "venue_market_id", None), meta.get("tournament")):
            if ident and not str(ident).isdigit():
                tiers.add(tennis_tier(str(ident)))
    for t in ("itf", "challenger"):
        if t in tiers:
            return t
    return "tour"


def _setting_or(settings: dict, key: str, default: float) -> float:
    v = settings.get(key)
    return default if v is None or v == "" else float(v)


def tennis_pair_flags(pair: Any, settings: Optional[Mapping[str, Any]] = None) -> list[str]:
    """Tennis-specific gates for a two-leg pair (``{"legs": [quote_a, quote_b], "margin": m}``,
    or any object with ``.legs``/``.quotes`` and ``.margin``):

    * ``walkover-exposed`` — the favourite leg sits on Polymarket (pays 50¢ on a walkover while
      Kalshi fair-prices the other leg near its cost) and the margin does not cover the
      expected walkover loss ``p_walkover × (p_fav − 0.5)``.
    * ``tier:challenger`` / ``tier:itf`` — thinner, walkover-prone tiers.
    * ``thin-book`` — any leg with ``|yes_ask + no_ask − 1| > 0.03`` (= ask − bid on a folded
      quote) or a top-of-book size under 20 contracts.
    """
    settings = settings or {}
    legs = _legs(pair)
    flags: list[str] = []
    if not legs:
        return flags
    tier = _pair_tier(pair, legs)
    if tier != "tour":
        flags.append(f"tier:{tier}")
    # ``None`` means unset; an explicit 0 is a real choice (disable that gate), so no ``or``.
    spread_max = float(_setting_or(settings, "tennis_thin_book_spread", DEFAULT_THIN_SPREAD))
    size_min = float(_setting_or(settings, "tennis_thin_book_size", DEFAULT_THIN_SIZE))
    thin = False
    for q in legs:
        ask, bid, size = _top(q)
        if ask is not None and bid is not None and abs(ask + (1 - bid) - 1) > spread_max + 1e-12:
            thin = True
        if size is not None and size < size_min:
            thin = True
    if thin:
        flags.append("thin-book")
    priced = [(q, _top(q)[0]) for q in legs if _top(q)[0] is not None]
    if priced:
        fav, p_fav = max(priced, key=lambda t: t[1])
        if getattr(fav, "venue", "") == "polymarket" and p_fav > 0.5:
            p_wo, _src = walkover_probability(tier, settings)
            margin = _margin(pair) or 0.0
            if margin < p_wo * (p_fav - 0.5):
                flags.append("walkover-exposed")
    return flags


# ---- registry integrity ---------------------------------------------------------------------------

def verify(path: Optional[str] = None, fixture_dir: Optional[Path] = None) -> list[str]:
    """Problems with the registry: missing source/status, bad status, sha256 that does not
    match its fixture text. Empty list = healthy. Used by the tests and by --check tooling."""
    reg = load(path)
    fdir = Path(fixture_dir or RULES_FIXTURE_DIR)
    problems: list[str] = []
    for i, row in enumerate(reg.get("rules", [])):
        ident = f"rules[{i}] {row.get('venue')}/{row.get('sport')}/{row.get('market_type')}/{row.get('exchange')}"
        src = row.get("source") or {}
        if row.get("status") not in STATUSES:
            problems.append(f"{ident}: status {row.get('status')!r} not in {STATUSES}")
        for k in ("doc", "date", "fixture", "sha256"):
            if not src.get(k):
                problems.append(f"{ident}: source.{k} missing")
        fx = fdir / str(src.get("fixture") or "")
        if src.get("fixture") and not fx.exists():
            problems.append(f"{ident}: fixture {fx.name} missing")
        elif src.get("fixture"):
            got = sha256_text(fx.read_text(encoding="utf-8"))
            if got != src.get("sha256"):
                problems.append(f"{ident}: sha256 {src.get('sha256')} != {got} for {fx.name}")
        for f in FIELDS:
            if f not in row:
                problems.append(f"{ident}: field {f} missing")
    for i, row in enumerate(reg.get("tennis_walkover", [])):
        if row.get("tier") not in TIERS or row.get("status") not in ("derived", "provisional"):
            problems.append(f"tennis_walkover[{i}]: bad tier/status {row}")
        if row.get("status") == "derived" and not (row.get("n_markets") and row.get("n_markets") >= 200):
            problems.append(f"tennis_walkover[{i}]: derived rows need n_markets >= 200")
    return problems

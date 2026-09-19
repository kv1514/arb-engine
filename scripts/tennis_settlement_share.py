"""Share of Kalshi tennis markets that settled at a *fair price* (walkover / cancellation
before the first ball) rather than 0/1, per tier — the ``p_walkover`` inputs of the
``walkover-exposed`` gate in ``arb_engine/matching/settlement_rules.py``.

    python scripts/tennis_settlement_share.py                 # live: settled feed, 6 pages x 200 per series
    python scripts/tennis_settlement_share.py --pages 20      # deeper history
    python scripts/tennis_settlement_share.py --input raw.json  # offline: {"SERIES": [markets...]} or {"markets": [...]}
    python scripts/tennis_settlement_share.py --write         # update tennis_walkover rows in settlement_rules.json

Why: a Kalshi × Polymarket tennis hedge holds the favourite on Polymarket at ``p_fav`` and
receives 50¢ on a walkover, while the Kalshi leg fair-prices near cost, so the expected
walkover loss is ``p_walkover × (p_fav − 0.5)``. Kalshi reports those settlements as
``result: "scalar"`` with ``settlement_value_dollars`` strictly between 0 and 1 (e.g. the
Zidansek–Jeong WTA Seoul qualifier paid 0.85 / 0.15 on 2026-09-19), so the share is a plain
count over the settled feed. ``derived`` rows need at least 200 settled markets.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb_engine.matching.settlement_rules import DATA_PATH, tennis_tier  # noqa: E402

SERIES = ("KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH")
MIN_DERIVED = 200


def classify(m: dict) -> str:
    """'scalar' (fair-price settlement), 'binary' (0 or 1), or 'other' (void / unknown)."""
    r = (m.get("result") or "").lower()
    v = m.get("settlement_value_dollars")
    try:
        fv = float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        fv = None
    if r == "scalar" or (fv is not None and 0.0 < fv < 1.0):
        return "scalar"
    if r in ("yes", "no") or fv in (0.0, 1.0):
        return "binary"
    return "other"


def summarise(markets: Iterable[dict]) -> dict[str, dict[str, Any]]:
    """Per tier: settled markets, distinct matches, scalar/binary/other counts and p_scalar."""
    out: dict[str, dict[str, Any]] = {}
    for m in markets:
        if m.get("status") not in (None, "settled", "finalized", "closed"):
            continue
        tier = tennis_tier(m.get("ticker") or m.get("event_ticker") or "")
        t = out.setdefault(tier, {"n_markets": 0, "n_scalar": 0, "n_binary": 0, "n_other": 0, "matches": set()})
        t["n_markets"] += 1
        t["n_" + classify(m)] += 1
        t["matches"].add(m.get("event_ticker") or m.get("ticker"))
    for tier, t in out.items():
        t["n_matches"] = len(t.pop("matches"))
        t["p_scalar"] = round(t["n_scalar"] / t["n_markets"], 4) if t["n_markets"] else None
    return out


def flatten(raw: Any) -> list[dict]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        if isinstance(raw.get("markets"), list):
            return raw["markets"]
        return [m for v in raw.values() if isinstance(v, list) for m in v]
    return []


def fetch_settled(pages: int) -> list[dict]:
    from arb_engine.venues.kalshi import KalshiClient

    client = KalshiClient()
    out: list[dict] = []
    for s in SERIES:
        ms = client.markets(s, status="settled", limit=200, max_pages=pages)
        print(f"{s}: {len(ms)} settled markets", file=sys.stderr)
        out.extend(ms)
    return out


def rows_from_summary(summary: dict[str, dict[str, Any]], source: str) -> list[dict[str, Any]]:
    rows = []
    for tier, t in sorted(summary.items()):
        derived = t["n_markets"] >= MIN_DERIVED
        rows.append({"tier": tier, "p_walkover": t["p_scalar"] if derived else None, "n_markets": t["n_markets"], "n_matches": t["n_matches"], "n_scalar": t["n_scalar"], "status": "derived" if derived else "provisional", "source": source})
    return rows


def write_registry(rows: list[dict[str, Any]], path: Path = DATA_PATH) -> None:
    reg = json.loads(path.read_text(encoding="utf-8"))
    existing = {r["tier"]: r for r in reg.get("tennis_walkover", [])}
    for r in rows:
        if r["status"] != "derived":
            continue  # keep the provisional row and its documented default
        old = existing.get(r["tier"], {})
        existing[r["tier"]] = {**old, **r}
    reg["tennis_walkover"] = [existing[t] for t in sorted(existing)]
    path.write_text(json.dumps(reg, indent=1) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pages", type=int, default=6, help="pages of 200 settled markets per series (live mode)")
    ap.add_argument("--input", help="offline: JSON with the raw settled markets")
    ap.add_argument("--write", action="store_true", help="update tennis_walkover rows in settlement_rules.json")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    a = ap.parse_args(argv)
    if a.input:
        markets = flatten(json.loads(Path(a.input).read_text(encoding="utf-8")))
        source = f"scripts/tennis_settlement_share.py --input {Path(a.input).name}, {date.today().isoformat()}"
    else:
        markets = fetch_settled(a.pages)
        source = f"scripts/tennis_settlement_share.py on GET /markets?series_ticker={'|'.join(SERIES)}&status=settled, {a.pages} pages x 200 per series, {date.today().isoformat()}"
    summary = summarise(markets)
    if a.json:
        print(json.dumps(summary, indent=1))
    else:
        print(f"{'tier':<11}{'markets':>9}{'matches':>9}{'scalar':>8}{'binary':>8}{'other':>7}{'p_scalar':>10}")
        for tier, t in sorted(summary.items()):
            print(f"{tier:<11}{t['n_markets']:>9}{t['n_matches']:>9}{t['n_scalar']:>8}{t['n_binary']:>8}{t['n_other']:>7}{(t['p_scalar'] if t['p_scalar'] is not None else float('nan')):>10.4f}")
    if a.write:
        write_registry(rows_from_summary(summary, source))
        print(f"updated {DATA_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

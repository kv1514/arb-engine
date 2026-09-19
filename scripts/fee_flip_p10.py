"""Sign-flip audit for the opt-in exchange-fee models on the recorded fixture scans.

Scans the NFL and NCAAF fixtures (offline, the same adapters the tests use) under every
Rothera / CDNA fee model and counts, per fixture, how many events change margin sign and
how many Robinhood all-in rows move — and proves that only the rows routed to the exchange
whose model changed move at all. Writes the metrics-only JSON that
tests/test_fees.py replays (tests/fixtures/results/fee_flip_p10.json), so the numbers in
docs stay reproducible.

    python scripts/fee_flip_p10.py            # rewrite the results fixture
    python scripts/fee_flip_p10.py --print    # only print
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arb_engine.fees.robinhood import CDNA_FEE_MODELS, ROTHERA_FEE_MODELS  # noqa: E402
from arb_engine.scanner import ScanResult, scan  # noqa: E402

RESULTS = ROOT / "tests" / "fixtures" / "results" / "fee_flip_p10.json"
BASELINE = {"rothera_fee_model": "flat_001", "cdna_fee_model": "flat_001"}


def _fixture_adapters(sport: str) -> Callable[[], list]:
    if sport == "nfl":
        from tests.test_scanner import _adapters
    else:
        from tests.test_ncaaf import _adapters
    return _adapters


def _rows(res: ScanResult) -> dict[tuple[str, str, str], tuple[str | None, float | None]]:
    """(event, outcome, market_id) -> (exchange, all_in) for every Robinhood row."""
    out: dict[tuple[str, str, str], tuple[str | None, float | None]] = {}
    for e in res.events:
        for o in e.outcomes:
            for v in o.venues:
                if v.venue == "robinhood":
                    out[(e.event_key, o.outcome, v.market_id)] = (v.exchange, v.all_in)
    return out


def _sign(m: float | None) -> int:
    if m is None:
        return 0
    return 1 if m > 0 else -1


def compare(sport: str, settings: dict[str, Any], exchange: str) -> dict[str, Any]:
    """Baseline (flat_001 everywhere) vs ``settings`` on one fixture scan."""
    adapters = _fixture_adapters(sport)
    base = scan(sport, adapters(), settings=dict(BASELINE))
    alt = scan(sport, adapters(), settings={**BASELINE, **settings})
    base_rows, alt_rows = _rows(base), _rows(alt)
    assert set(base_rows) == set(alt_rows), "fee model must not add or drop rows"
    moved = [k for k in base_rows if base_rows[k][1] != alt_rows[k][1]]
    moved_other = [k for k in moved if base_rows[k][0] != exchange]
    base_m = {e.event_key: e.margin for e in base.events}
    alt_m = {e.event_key: e.margin for e in alt.events}
    flips = [k for k in base_m if _sign(base_m[k]) != _sign(alt_m.get(k))]
    improved = [k for k in base_m if base_m[k] is not None and alt_m.get(k) is not None and alt_m[k] > base_m[k] + 1e-12]
    return {
        "events": len(base.events),
        "robinhood_rows": len(base_rows),
        "rows_on_exchange": sum(1 for v in base_rows.values() if v[0] == exchange),
        "rows_moved": len(moved),
        "rows_moved_off_exchange": len(moved_other),
        "margin_sign_flips": len(flips),
        "margins_improved": len(improved),
        "arbs_before": len(base.arbs(include_thin=True)),
        "arbs_after": len(alt.arbs(include_thin=True)),
        "max_margin_before": max((m for m in base_m.values() if m is not None), default=None),
        "max_margin_after": max((m for m in alt_m.values() if m is not None), default=None),
    }


def fee_flip_metrics() -> dict[str, Any]:
    out: dict[str, Any] = {"item": "P10", "baseline": dict(BASELINE), "fixtures": {}}
    for sport in ("nfl", "ncaaf"):
        per: dict[str, Any] = {}
        for model in ROTHERA_FEE_MODELS:
            if model != BASELINE["rothera_fee_model"]:
                per[f"rothera:{model}"] = compare(sport, {"rothera_fee_model": model}, "rothera")
        for model in CDNA_FEE_MODELS:
            if model != BASELINE["cdna_fee_model"]:
                per[f"cdna:{model}"] = compare(sport, {"cdna_fee_model": model}, "cdna")
        out["fixtures"][sport] = per
    return out


def main(argv: list[str]) -> None:
    metrics = fee_flip_metrics()
    text = json.dumps(metrics, indent=1, sort_keys=True)
    if "--print" in argv:
        print(text)
        return
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(text + "\n")
    print(f"wrote {RESULTS}")
    print(text)


if __name__ == "__main__":
    main(sys.argv[1:])

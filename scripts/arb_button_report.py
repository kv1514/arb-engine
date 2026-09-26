#!/usr/bin/env python3
"""How the "Robinhood done" button is doing: every button issued, every practice and real tap.

    python3 scripts/arb_button_report.py [--journal out/orders/arb_button.jsonl] [--since 2026-09-26] [--games]

Reads out/orders/arb_button.jsonl (strategy/arbbutton.py). For each day:

* issued      - arbs pushed with a button (practice-script pairs are counted apart);
* auto        - the automatic practice tap ~10 s after each real arb (``arb_button_auto_practice_s``):
                was Kalshi's live price still the alert's, was Robinhood still at or under its max,
                would the Kalshi leg have filled in full - i.e. would the arb have locked had you
                acted on it - and what the simulated locked sets would have made;
* taps        - your own taps (practice, demo or live), same measures.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pct(a: int, b: int) -> str:
    return f"{a}/{b} ({a / b:.0%})" if b else "-"


def summarize(rows: list[dict]) -> dict:
    taps = [r for r in rows if r.get("event") in ("auto-practice", "tap") and r.get("status") != "expired"]
    n = len(taps)
    k_same = sum(1 for r in taps if r.get("kalshi_live_ask") is not None and abs(r["kalshi_live_ask"] - r.get("kalshi_alert_ask", -1)) < 1e-9)
    rh_ok = sum(1 for r in taps if r.get("rh_within_max"))
    full = sum(1 for r in taps if r.get("unhedged") == 0)
    locks = sum(1 for r in taps if r.get("would_lock"))
    profit = sum(r.get("profit_at_alert_rh_price") or 0 for r in taps if r.get("would_lock"))
    unhedged_ct = sum(r.get("unhedged") or 0 for r in taps if r.get("rh_within_max"))
    return {"taps": n, "kalshi_unchanged": k_same, "rh_within_max": rh_ok, "kalshi_filled_full": full, "would_lock": locks,
            "locked_profit": round(profit, 2), "unhedged_contracts": unhedged_ct}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Report on the Robinhood-done button (practice and real taps).")
    ap.add_argument("--journal", default=str(ROOT / "out/orders/arb_button.jsonl"))
    ap.add_argument("--since", help="YYYY-MM-DD (local)")
    ap.add_argument("--games", action="store_true", help="one line per auto-practice tap")
    a = ap.parse_args(argv)
    p = Path(a.journal)
    if not p.exists():
        print(f"no journal at {p} yet")
        return 0
    since = datetime.fromisoformat(a.since).timestamp() if a.since else 0
    rows = []
    for line in p.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if (r.get("ts") or 0) >= since:
            rows.append(r)
    by_day: dict[str, list] = defaultdict(list)
    for r in rows:
        by_day[datetime.fromtimestamp(r.get("ts") or 0).strftime("%Y-%m-%d")].append(r)
    for day in sorted(by_day):
        rs = by_day[day]
        issued = [r for r in rs if r.get("event") == "issued" and not r.get("practice")]
        issued_p = [r for r in rs if r.get("event") == "issued" and r.get("practice")]
        auto = summarize([r for r in rs if r.get("event") == "auto-practice"])
        mine = [r for r in rs if r.get("event") == "tap" and not r.get("practice")]
        script = summarize([r for r in rs if r.get("event") == "tap" and r.get("practice")])
        print(f"{day}: {len(issued)} arbs pushed with a button ({len({r.get('event_key') for r in issued})} games); {len(issued_p)} practice-script pairs")
        if auto["taps"]:
            print(f"  auto practice ~10 s after each arb: Kalshi price unchanged {_pct(auto['kalshi_unchanged'], auto['taps'])}, "
                  f"Robinhood still <= its max {_pct(auto['rh_within_max'], auto['taps'])}, Kalshi leg filled in full {_pct(auto['kalshi_filled_full'], auto['taps'])}")
            print(f"    -> would have locked {_pct(auto['would_lock'], auto['taps'])}: +${auto['locked_profit']:.2f} simulated; "
                  f"{auto['unhedged_contracts']} contracts would have been left unhedged on Robinhood")
        if mine:
            s = summarize(mine)
            print(f"  your taps: {len(mine)} ({', '.join(sorted({r.get('mode', '?') for r in mine}))}); locked {_pct(s['would_lock'], s['taps'])}, +${s['locked_profit']:.2f}")
        if script["taps"]:
            print(f"  practice-script taps: Kalshi unchanged {_pct(script['kalshi_unchanged'], script['taps'])}, Robinhood unchanged-or-better "
                  f"{_pct(script['rh_within_max'], script['taps'])}, Kalshi filled in full {_pct(script['kalshi_filled_full'], script['taps'])}")
        if a.games:
            for r in (x for x in rs if x.get("event") == "auto-practice"):
                print(f"    {datetime.fromtimestamp(r['ts']).strftime('%H:%M:%S')} {r.get('title')}: Kalshi {r.get('kalshi_alert_ask')}->{r.get('kalshi_live_ask')}, "
                      f"Robinhood {r.get('rh_alert_ask')}->{r.get('rh_live_ask')} (max {r.get('rh_max')}), filled {r.get('filled')}/{r.get('count')}, "
                      f"{'LOCK' if r.get('would_lock') else 'no lock'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Practise the "Robinhood done" button on live markets, right now - nothing is ever sent.

    python3 scripts/arb_button_practice.py --sport ncaaf [--pairs 5] [--wait 6] [--stake 100]
    python3 scripts/arb_button_practice.py --sport nfl --push              # also send the practice push to your phone
    python3 scripts/arb_button_practice.py --sport ncaaf --push --self-tap # ... and tap it through ntfy (end to end)
    python3 scripts/arb_button_practice.py --sport ncaaf --phone --pairs 1 # push one to your phone; wait for YOUR tap

For the Kalshi + Robinhood pairs closest to an arb in the games on right now, it does what an
ARB alert would: prices the set at the current asks, issues a button (practice mode), waits
``--wait`` seconds - the time you would spend buying the Robinhood leg - and taps. The tap
re-reads Kalshi's live order book and Robinhood's live quote, reports whether each still
matches the price it was issued at and whether the set still locks, and simulates the Kalshi
immediate-or-cancel buy against the real book at the button's limit. Every tap is journalled
to out/orders/arb_button.jsonl like a real one.

A pair that does not lock is still practised: its Kalshi limit is its own price at issue, so
the question answered is "would the bot have bought at the price it was shown?".
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arb_engine.fees.registry import fee_model_for_quote  # noqa: E402
from arb_engine.quant.arbitrage import Leg, evaluate, leg_all_in_cost  # noqa: E402
from arb_engine.scanner import _arb_to_dict, scan  # noqa: E402
from arb_engine.strategy.arbbutton import ArbButton  # noqa: E402


class _Printer:
    """Stands in for the Alerter when nothing is pushed."""
    ntfy = None

    def journal(self, *a, **k):
        pass

    def push(self, *a, **k):
        return False


def candidate_pairs(res, stake: float, top: int, mid: bool = False):
    """(set cost per contract, event key, title, sized dict, quotes) for the cheapest Kalshi +
    Robinhood (non-Kalshi book) pair of every game on the board, cheapest first."""
    out = []
    titles = {r.event_key: (r.title or r.event_key) for r in res.events}
    for ek, me in (res.merged or {}).items():
        ok = (lambda q: 0.10 <= q.ask <= 0.90) if mid else (lambda q: True)
        ks = [q for q in me.quotes_by_venue.get("kalshi", []) if q.ask is not None and q.ask_size and ok(q)]
        rs = [q for q in me.quotes_by_venue.get("robinhood", []) if q.ask is not None and q.ask_size and str(q.book_id) != "kalshi" and ok(q)]
        best = None
        for kq in ks:
            for rq in rs:
                if kq.outcome == rq.outcome:
                    continue
                legs = [Leg.from_quote(kq.outcome, kq, fee_model_for_quote(kq)), Leg.from_quote(rq.outcome, rq, fee_model_for_quote(rq))]
                per = float(sum(leg_all_in_cost(l, 1) for l in legs))
                n = int(min(stake / per, kq.ask_size or 0, rq.ask_size or 0))
                if n < 1:
                    continue
                if best is None or per < best[0]:
                    best = (per, legs, n)
        if best is None:
            continue
        per, legs, n = best
        sized = _arb_to_dict(evaluate(legs, n))
        out.append((per, ek, titles.get(ek, ek), sized, me.quotes_by_venue))
    return sorted(out, key=lambda x: x[0])[:top]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Practise the Robinhood-done button on live prices (paper: nothing is sent).")
    ap.add_argument("--sport", default="ncaaf")
    ap.add_argument("--pairs", type=int, default=5)
    ap.add_argument("--wait", type=float, default=6.0, help="seconds between issuing and tapping (buying Robinhood by hand)")
    ap.add_argument("--stake", type=float, default=100.0)
    ap.add_argument("--mid", action="store_true", help="only legs priced 10-90c (the contracts that actually move)")
    ap.add_argument("--live-only", action="store_true", help="only games in play")
    ap.add_argument("--push", action="store_true", help="send each practice push (with its button) to your ntfy topic")
    ap.add_argument("--self-tap", action="store_true", help="tap through ntfy yourself: POST the command and let the listener act")
    ap.add_argument("--phone", action="store_true", help="push the practice button to your phone and wait for your own tap on it (the phone round trip)")
    ap.add_argument("--tap-wait", type=float, default=900.0, help="with --phone: seconds to wait for your tap (the button lives this long)")
    ap.add_argument("--journal", default=str(ROOT / "out/orders/arb_button.jsonl"))
    a = ap.parse_args(argv)
    if a.phone:
        a.push = True
    from arb_engine.venues.kalshi import KalshiAdapter
    from arb_engine.venues.robinhood import RobinhoodAdapter

    ntfy = None
    if a.push or a.self_tap:
        topic = os.environ.get("ARB_ALERT_NTFY") or (ROOT / "out/run/ntfy_topic.txt").read_text().strip()
        ntfy = topic if topic.startswith("http") else f"https://ntfy.sh/{topic}"
        from arb_engine.strategy.alerts import Alerter

        alerts = Alerter(journal_path=str(ROOT / "out/practice_journal.jsonl"), quiet=True, desktop=False, webhook="", ntfy=ntfy, min_interval_s=0)
    else:
        alerts = _Printer()
    button = ArbButton("paper", alerts=alerts, cmd_url=(ntfy + "-cmd") if ntfy else "local", fee_for=fee_model_for_quote,
                       journal_path=a.journal, http_get=None if (a.self_tap or a.phone) else False,
                       ttl_s=max(180.0, a.tap_wait) if a.phone else 180.0)
    if a.self_tap or a.phone:
        button.start()                       # the real listener: one streaming subscription
        time.sleep(2)
    t0 = time.time()
    res = scan(a.sport, [KalshiAdapter(), RobinhoodAdapter()], settings={}, keep_merged=True, max_quote_age=120)
    if a.live_only:
        live = {r.event_key for r in res.events if r.live}
        res.merged = {k: v for k, v in (res.merged or {}).items() if k in live}
    pairs = candidate_pairs(res, a.stake, a.pairs, mid=a.mid)
    print(f"{a.sport}: {len(res.merged or {})} markets scanned in {time.time() - t0:.0f}s; practising the {len(pairs)} closest Kalshi + Robinhood pairs "
          f"(tap {a.wait:g}s after issue, paper)")
    rows = []
    for per, ek, title, sized, qbv in pairs:
        spec = button.register(ek, f"{title} [practice]", sized, qbv, practice=True)
        if spec is None:
            print(f"  {ek}: no button (not a Kalshi + Robinhood pair)")
            continue
        if a.push:
            from arb_engine.strategy import ticket

            body = ticket.arb_button_short(sized, spec, where="PRACTICE - not an alert", mode="paper")
            alerts.push("ARB FILL", body, event=ek, side=spec["token"], force=True, headline=f"PRACTICE button - {title}",
                        actions=[spec["action"], (f"Robinhood {spec['robinhood']['label']}", spec["robinhood"]["url"])])
        if a.phone:
            # Your tap on the phone reaches this process's listener like any real one.
            print(f"  pushed {title}: tap \"Robinhood done\" on your phone (waiting up to {a.tap_wait:.0f}s)", flush=True)
            deadline = time.time() + a.tap_wait
            while time.time() < deadline and spec["token"] not in button.results:
                time.sleep(0.5)
            rec = button.results.get(spec["token"])
            if rec is None:
                print(f"  {ek}: no tap from the phone within {a.tap_wait:.0f}s")
                continue
        else:
            time.sleep(a.wait)
        if a.self_tap and not a.phone:
            import urllib.request

            # The tap exactly as the phone sends it: one POST to the command topic. The listener
            # (the same streaming subscription the live processes run) picks it up.
            urllib.request.urlopen(urllib.request.Request(button.cmd_url, data=f"arb {spec['token']}".encode(), method="POST"), timeout=15).read()
            deadline = time.time() + 30
            while time.time() < deadline and spec["token"] not in button.results:
                time.sleep(0.5)
            rec = button.results.get(spec["token"])
            if rec is None:
                print(f"  {ek}: the tap did not come back through ntfy within 30 s")
                continue
        elif not a.phone:
            rec = button.fire(spec["token"])
        k, r = spec["kalshi"], spec["robinhood"]
        rows.append(rec)
        print(f"\n  {title}  ({ek})  set {per:.4f}/ct {'LOCKS' if per <= 1 else f'{(per - 1) * 100:.1f}c short of a lock'}")
        print(f"    issued : Kalshi {k['label']} {str(k['side']).upper()} {k['alert_ask']:.2f} (limit {k['limit']:.2f}) | Robinhood {r['label']} {str(r['side']).upper()} {r['alert_ask']:.2f} (max {r['max']:.2f}) | {spec['count']} ct")
        print(f"    tapped : {rec['tap_after_s']:.1f}s later, prices read in {rec.get('check_s', 0):.1f}s -> Kalshi {rec.get('kalshi_live_ask')} "
              f"({'same' if rec.get('kalshi_live_ask') == k['alert_ask'] else 'MOVED'}), Robinhood {rec.get('rh_live_ask')} "
              f"({'same' if rec.get('rh_live_ask') == r['alert_ask'] else 'MOVED'}), state {rec.get('rh_state')}")
        print(f"    result : {rec['status']} - Kalshi would fill {rec.get('filled', 0)} of {spec['count']} {rec.get('levels') or ''}"
              + (f"; unhedged {rec['unhedged']}" if rec.get("unhedged") else "") + (f"; {rec.get('kalshi_error') or rec.get('rh_error')}" if rec.get("kalshi_error") or rec.get("rh_error") else ""))
    if rows:
        same_k = sum(1 for x in rows if x.get("kalshi_live_ask") == x.get("kalshi_alert_ask"))
        same_r = sum(1 for x in rows if x.get("rh_live_ask") == x.get("rh_alert_ask"))
        full = sum(1 for x in rows if x.get("unhedged") == 0)
        print(f"\nsummary: {len(rows)} taps; Kalshi price unchanged {same_k}, Robinhood unchanged {same_r}; Kalshi leg would have filled in full {full}")
    button.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

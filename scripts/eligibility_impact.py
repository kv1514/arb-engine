#!/usr/bin/env python3
"""Eligibility-impact report: how many scan arbs and maker hedges relied on a venue leg this
account cannot execute (global Polymarket for a US person), before vs after the venue
eligibility table (arb_engine/compliance.py) is applied.

    python scripts/eligibility_impact.py --fixtures                      # offline: NFL + NCAAF fixture scans and maker discovery
    python scripts/eligibility_impact.py --db out/history.db --limit 5000  # recorded scans (after P07 records a week)
    python scripts/eligibility_impact.py --json out/scan_nfl.json         # a `scan --json` dump
    ... --out tests/fixtures/results/eligibility_p11.json                 # metrics-only JSON for the docs table

Method. A scan snapshot is one event at one timestamp with every venue's all-in ask per
outcome. "Before" = the cheapest all-in per outcome across every venue (what the scanner
printed); "after" = the same restricted to executable venues. An arb is a pre-game snapshot
whose best all-ins sum below 1. The share reported is arbs-before that had at least one
non-executable leg, and arbs-after / arbs-before. Maker hedges: MakerRunner.discover() with
hedge_venues=(robinhood, polymarket) vs the default; a watch is "non-executable" when its
hedge venue is not in the table. No network: fixtures or a recorded SQLite file only.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import asdict, is_dataclass
from typing import Any, Iterable, Mapping, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arb_engine import compliance  # noqa: E402

DEFAULT_OUT = os.path.join("tests", "fixtures", "results", "eligibility_p11.json")


# ---- snapshot rows ---------------------------------------------------------------------------
def rows_from_reports(reports: Iterable[Any], ts: float = 0.0) -> list[dict[str, Any]]:
    """EventReport dataclasses or their dicts (``scan --json``) -> snapshot rows."""
    rows = []
    for rep in reports:
        d = asdict(rep) if is_dataclass(rep) else dict(rep)
        outcomes: dict[str, dict[str, float]] = {}
        for o in d.get("outcomes", []):
            per_venue = {}
            for v in o.get("venues", []):
                if v.get("all_in") is not None and not v.get("stale") and not v.get("mirror_of"):
                    per_venue[v["venue"]] = float(v["all_in"])
            outcomes[o["outcome"]] = per_venue
        rows.append({"ts": ts, "event_key": d["event_key"], "market_type": d.get("market_type"), "live": bool(d.get("live")), "outcomes": outcomes})
    return rows


def rows_from_db(path: str, limit: Optional[int] = None, sport: Optional[str] = None) -> list[dict[str, Any]]:
    """store.py's scans + quotes tables -> snapshot rows, newest first, ``limit`` snapshots."""
    conn = sqlite3.connect(path)
    q = "SELECT ts, event_key, market_type, live FROM scans" + (" WHERE sport=?" if sport else "") + " ORDER BY ts DESC, event_key"
    if limit:
        q += f" LIMIT {int(limit)}"
    snaps = conn.execute(q, (sport,) if sport else ()).fetchall()
    rows = []
    for ts, key, mt, live in snaps:
        outcomes: dict[str, dict[str, float]] = {}
        for outcome, venue, all_in in conn.execute("SELECT outcome, venue, all_in FROM quotes WHERE ts=? AND event_key=? AND all_in IS NOT NULL", (ts, key)):
            outcomes.setdefault(outcome, {})[venue] = float(all_in)
        rows.append({"ts": ts, "event_key": key, "market_type": mt, "live": bool(live), "outcomes": outcomes})
    conn.close()
    return rows


# ---- metrics -----------------------------------------------------------------------------------
def _best_legs(outcomes: Mapping[str, Mapping[str, float]], venues: Optional[set[str]]) -> Optional[dict[str, tuple[str, float]]]:
    legs = {}
    for outcome, per_venue in outcomes.items():
        cands = [(c, v) for v, c in per_venue.items() if venues is None or v in venues]
        if not cands:
            return None
        c, v = min(cands)
        legs[outcome] = (v, c)
    return legs or None


def arb_impact(rows: Iterable[dict[str, Any]], executable: Optional[set[str]] = None, min_margin: float = 0.0) -> dict[str, Any]:
    executable = executable if executable is not None else compliance.executable_venues()
    n = arbs_before = arbs_non_exec = arbs_after = arbs_lost = 0
    by_venue: dict[str, int] = {}
    for r in rows:
        if r.get("live") or len(r.get("outcomes") or {}) < 2:
            continue
        before = _best_legs(r["outcomes"], None)
        if before is None:
            continue
        n += 1
        m_before = 1.0 - sum(c for _, c in before.values())
        if m_before <= min_margin:
            continue
        arbs_before += 1
        bad = sorted({v for v, _ in before.values() if v not in executable})
        if bad:
            arbs_non_exec += 1
            for v in bad:
                by_venue[v] = by_venue.get(v, 0) + 1
        after = _best_legs(r["outcomes"], executable)
        if after is not None and 1.0 - sum(c for _, c in after.values()) > min_margin:
            arbs_after += 1
        else:
            arbs_lost += 1
    return {
        "snapshots": n,
        "arbs_before": arbs_before,
        "arbs_before_with_non_executable_leg": arbs_non_exec,
        "share_before_non_executable": (arbs_non_exec / arbs_before) if arbs_before else None,
        "arbs_after": arbs_after,
        "arbs_lost": arbs_lost,
        "share_after_of_before": (arbs_after / arbs_before) if arbs_before else None,
        "non_executable_legs_by_venue": by_venue,
        "executable": sorted(executable),
    }


def hedge_impact(merged: list[Any], settings: Optional[dict[str, Any]] = None, size: float = 100) -> dict[str, Any]:
    """MakerRunner.discover() with every hedge venue vs the default (executable) list."""
    from arb_engine.strategy.alerts import Alerter
    from arb_engine.strategy.broker import PaperBroker
    from arb_engine.strategy.maker import MakerConfig, MakerRunner

    settings = settings or {}
    executable = compliance.executable_venues(settings)
    journal = os.path.join(os.environ.get("TMPDIR", "/tmp"), "eligibility_impact_journal.jsonl")

    def run(hedge_venues):
        cfg = MakerConfig(size=size, min_margin=-1.0, queue_ahead=False, hedge_venues=hedge_venues)
        r = MakerRunner(cfg, feed=None, broker=PaperBroker(), alerter=Alerter(journal_path=journal, quiet=True, desktop=False, webhook=""), settings=settings)
        return r.discover(merged)

    before = run(("robinhood", "polymarket"))
    after = run(MakerConfig().hedge_venues)
    non_exec = [w for w in before if w.hedge_venue not in executable]
    priceable_before = [w for w in before if w.desired_price is not None]
    priceable_after = [w for w in after if w.desired_price is not None]
    return {
        "watches_before": len(before),
        "watches_before_non_executable_hedge": len(non_exec),
        "share_before_non_executable": (len(non_exec) / len(before)) if before else None,
        "priceable_before": len(priceable_before),
        "priceable_before_non_executable_hedge": sum(1 for w in priceable_before if w.hedge_venue not in executable),
        "watches_after": len(after),
        "priceable_after": len(priceable_after),
        "watches_after_non_executable_hedge": sum(1 for w in after if w.hedge_venue not in executable),
        "by_hedge_venue_before": _count(w.hedge_venue for w in before),
        "by_hedge_venue_after": _count(w.hedge_venue for w in after),
    }


def _count(xs: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items()))


# ---- sources -----------------------------------------------------------------------------------
def fixture_sources() -> dict[str, tuple[list[dict[str, Any]], list[Any]]]:
    """{sport: (snapshot rows, merged events)} from the committed offline fixtures."""
    from arb_engine.matching.matcher import merge_snapshots
    from arb_engine.scanner import scan
    from tests.test_ncaaf import _adapters as ncaaf_adapters
    from tests.test_scanner import _adapters as nfl_adapters

    out = {}
    for sport, mk in (("nfl", nfl_adapters), ("ncaaf", ncaaf_adapters)):
        res = scan(sport, mk(), settings={})
        snaps = [a.fetch(sport) for a in mk()]
        merged = [me for me in merge_snapshots(snaps).values() if len(me.quotes_by_venue) >= 2]
        out[sport] = (rows_from_reports(res.events, ts=res.fetched_at), merged)
    return out


def report(sources: Mapping[str, tuple[list[dict[str, Any]], Optional[list[Any]]]], settings: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    settings = settings or {}
    executable = compliance.executable_venues(settings)
    out: dict[str, Any] = {"item": "P11", "executable_venues": sorted(executable), "sources": {}}
    for name, (rows, merged) in sources.items():
        entry = {"arbs": arb_impact(rows, executable)}
        if merged is not None:
            entry["maker_hedges"] = hedge_impact(merged, settings)
        out["sources"][name] = entry
    tot_before = sum(s["arbs"]["arbs_before"] for s in out["sources"].values())
    tot_bad = sum(s["arbs"]["arbs_before_with_non_executable_leg"] for s in out["sources"].values())
    tot_after = sum(s["arbs"]["arbs_after"] for s in out["sources"].values())
    out["summary"] = {"arbs_before": tot_before, "arbs_before_with_non_executable_leg": tot_bad, "share_before_non_executable": (tot_bad / tot_before) if tot_before else None, "arbs_after": tot_after, "share_after_of_before": (tot_after / tot_before) if tot_before else None}
    return out


def format_report(rep: Mapping[str, Any]) -> str:
    stale = compliance.stale_verification()   # printed, never written: it depends on today's date
    lines = [f"executable venues: {', '.join(rep['executable_venues'])}" + (f"  WARNING stale venue_rules.json rows (days): {stale}" if stale else "")]
    for name, s in rep["sources"].items():
        a = s["arbs"]
        pct = lambda x: "n/a" if x is None else f"{x:.0%}"  # noqa: E731
        lines.append(f"[{name}] snapshots={a['snapshots']} arbs before={a['arbs_before']} with non-executable leg={a['arbs_before_with_non_executable_leg']} ({pct(a['share_before_non_executable'])}) after={a['arbs_after']} ({pct(a['share_after_of_before'])} of before) legs by venue={a['non_executable_legs_by_venue']}")
        h = s.get("maker_hedges")
        if h:
            lines.append(f"[{name}] maker watches before={h['watches_before']} non-executable hedge={h['watches_before_non_executable_hedge']} ({pct(h['share_before_non_executable'])}; priceable {h['priceable_before_non_executable_hedge']}/{h['priceable_before']}) after={h['watches_after']} non-executable={h['watches_after_non_executable_hedge']} priceable={h['priceable_after']} by venue before={h['by_hedge_venue_before']} after={h['by_hedge_venue_after']}")
    s = rep["summary"]
    lines.append(f"summary: arbs before={s['arbs_before']} non-executable={s['arbs_before_with_non_executable_leg']} after={s['arbs_after']}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fixtures", action="store_true", help="use the committed NFL/NCAAF fixture scans (offline)")
    p.add_argument("--db", help="recorded SQLite file (store.py) to read scans/quotes from")
    p.add_argument("--json", action="append", default=[], help="a `scan --json` dump (repeatable)")
    p.add_argument("--sport", default=None, help="filter --db rows by sport")
    p.add_argument("--limit", type=int, default=20000, help="max --db snapshots (newest first)")
    p.add_argument("--out", default=None, help=f"write metrics JSON here (e.g. {DEFAULT_OUT})")
    args = p.parse_args(argv)
    sources: dict[str, tuple[list[dict[str, Any]], Optional[list[Any]]]] = {}
    if args.fixtures or not (args.db or args.json):
        sources.update(fixture_sources())
    if args.db:
        sources[f"db:{os.path.basename(args.db)}"] = (rows_from_db(args.db, args.limit, args.sport), None)
    for path in args.json:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        events = payload.get("events", payload) if isinstance(payload, dict) else payload
        sources[f"json:{os.path.basename(path)}"] = (rows_from_reports(events, ts=float(payload.get("fetched_at", 0) if isinstance(payload, dict) else 0)), None)
    rep = report(sources)
    print(format_report(rep))
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=1, sort_keys=True)
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

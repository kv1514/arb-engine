#!/usr/bin/env python3
"""Every causal property of the microdata / evaluator experiment, checked on synthetic data.

One row per audited property, each decided by a live check rather than by reading the code.
Nothing here reads a recorded database, opens a fold, talks to a venue or places an order,
and a green matrix says the accounting is causal - never that the strategy is profitable or
that anything is safe to trade.

    python3 audit/property_matrix.py            # exit 0 when every property holds
"""
import json
import math
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arb_engine.fees.base import ZeroFees                                             # noqa: E402
from arb_engine.quant import microdata as md                                          # noqa: E402
from arb_engine.quant.paperexec import ioc_round_trip, two_leg_arb                    # noqa: E402
from scripts import microstructure_eval as ev                                         # noqa: E402

EV = "nfl:A|B:2026-09-27"
KF = {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1, "series": "KXNFLGAME"}
RESULTS: list[tuple[str, bool, str]] = []


def obs(t, book, outcome, mid=None, venue=None, side="yes", tie=0.5, size=100, bid=None, ask=None,
        event=EV, **kw):
    if bid is None or ask is None:
        bid, ask = round(mid - .01, 4), round(mid + .01, 4)
    r = {"event_key": event, "obs_ts": float(t), "req_ts": float(t), "quote_time": float(t), "refreshed": 1,
         "in_play": True, "source": "fast", "venue": venue or book, "book_id": book,
         "venue_market_id": f"{book}-{outcome}-{side}", "outcome": outcome, "side": side,
         "bid": bid, "ask": ask, "bid_size": size, "ask_size": size, "tie_payout": tie}
    r.update(kw)
    return r


def identical():
    return mock.patch("arb_engine.quant.microdata.settlement_relation", return_value="identical")


def build(rows, **kw):
    with identical():
        return md.build(rows, sample=kw.pop("sample", "all"), horizons=kw.pop("horizons", (5,)),
                        fee_for_row=kw.pop("fee_for_row", lambda r: None), **kw)


def check(name, where, ok, note=""):
    RESULTS.append((name, bool(ok), where + (f" - {note}" if note else "")))


def two_books(n=40):
    out = []
    for t in range(n):
        out.append(obs(t, "kalshi", "A", .40, fee_params=dict(KF)))
        out.append(obs(t, "kalshi", "B", .60, fee_params=dict(KF)))
        out.append(obs(t, "rothera", "A", .41, venue="robinhood", tie=0.0))
        out.append(obs(t, "rothera", "B", .59, venue="robinhood", tie=1.0))
    return out


# 1. atomic instants -------------------------------------------------------------------
rows = two_books()
a, b = md.features_at(rows, 39.0), md.features_at(list(reversed(rows)), 39.0)
check("all rows at equal observation time enter state atomically", "microdata.py:488 (build installs the whole batch, then evaluates)",
      sorted((list(k), sorted(v.items())) for k, v in a.items()) == sorted((list(k), sorted(v.items())) for k, v in b.items())
      and len(a) == 4)

# 2. permutation / name / venue order ---------------------------------------------------
import random                                                                          # noqa: E402

shuffled = list(rows)
random.Random(11).shuffle(shuffled)
canon = lambda s: json.dumps(sorted((json.dumps(x, sort_keys=True, default=str) for x in s)))   # noqa: E731
perm_ok = canon(build(rows)) == canon(build(shuffled)) == canon(build(list(reversed(rows))))


def leader_case(up, down):
    out = []
    for t in range(0, 61):
        step = min(max(t - 10, 0), 30) / 30.0
        out.append(obs(t, "rothera", "A", .50, venue="robinhood"))
        out.append(obs(t, "rothera", "B", .50, venue="robinhood"))
        out.append(obs(t, up, "A", .47 + .06 * step, venue="kalshi"))
        out.append(obs(t, down, "A", .53 - .06 * step, venue="polymarket"))
    return out


def h3_trades(rows_):
    sel, _ = ev.select_trades(build(rows_, sample="unconditional", horizons=()), ev._h3_direction, 60.0)
    return [(s["t"], d, tuple(bc["key"])) for s, d, bc in sel]


name_ok = h3_trades(leader_case("alpha", "zulu")) == h3_trades(leader_case("zulu", "alpha")) == []
check("permutation / book-name / venue order does not change results",
      "microdata.py:210 choose_leader; microstructure_eval.py:509 select_trades; :1009 arb_scan",
      perm_ok and name_ok, "M1 fixed: tied contradicting leaders are no leader")

# 3. future append ----------------------------------------------------------------------
future = rows + [obs(t, "kalshi", o, m, fee_params=dict(KF)) for t in range(80, 100) for o, m in (("A", .9), ("B", .1))]
before = {(s["t"], s["book_id"], s["outcome"]): json.dumps(s, sort_keys=True, default=str) for s in build(rows) if s["t"] < 33}
after = {(s["t"], s["book_id"], s["outcome"]): json.dumps(s, sort_keys=True, default=str) for s in build(future) if s["t"] < 33}
check("appending future rows leaves all prior features and labels unchanged",
      "microdata.py:481 (streaming build); microdata.py:609 _complement (pushed rows only)",
      before == after and before)

# 4. features_at refuses the future -------------------------------------------------------
try:
    md.features_at(rows + [obs(99, "kalshi", "A", .4)], 39.0)
    raised = False
except ValueError:
    raised = True
check("features_at(t) raises if any source input has obs_ts > t", "microdata.py:431-437 features_at", raised)

# 5. late receipt -------------------------------------------------------------------------
p = md._Prints([{"ticker": "K", "ts": 10.0, "price": .6, "count": 5, "taker_side": "yes", "obs_ts": 40.0}])
check("late receipt time prevents historical prints from appearing before visibility",
      "microdata.py:334 (_Prints.at bisects on obs_ts)",
      p.at("K", 39.9, "yes", .5).get("flow_60") == 0 and p.at("K", 40.0, "yes", .5)["flow_60"] == 5)

# 6. stale by observation time, not quote time ---------------------------------------------
stale_rows = two_books(20) + [obs(19.0, "rothera", "A", .48, venue="robinhood", quote_time=19.0, obs_ts=19.0)]
old_quote = obs(30.0, "kalshi", "A", .40, quote_time=5.0, fee_params=dict(KF))     # 25 s venue lag
fresh_quote = obs(30.0, "kalshi", "A", .40, quote_time=29.0, fee_params=dict(KF))
check("stale rows are excluded using observation time, not exchange quote time",
      "microdata.py:118-131 is_observation; microdata.py:84 fresh_limit (compared against obs times)",
      md.is_observation(old_quote) is False and md.is_observation(fresh_quote) is True)

# 7. decision points ----------------------------------------------------------------------
bad = [obs(1, "kalshi", "A", .4, refreshed=0), obs(1, "kalshi", "A", .4, in_play=False),
       obs(1, "kalshi", "A", None, bid=.6, ask=.4), obs(1, "kalshi", "A", None, bid=0.0, ask=.4),
       obs(1, "kalshi", "A", None, bid=.4, ask=1.0)]
check("decision points require refreshed, two-sided, valid, in-play rows",
      "microdata.py:118-131 is_observation", not any(md.is_observation(r) for r in bad))

# 8. trigger cooldown per book-contract -----------------------------------------------------
ramp = []
for t in range(0, 200):
    m = .30 + .002 * t
    for book, venue in (("kalshi", "kalshi"), ("rothera", "robinhood")):
        ramp.append(obs(t, book, "A", m, venue=venue, fee_params=dict(KF)))
trig = [s for s in build(ramp, sample="trigger", horizons=()) if s["kind"] == "trigger"]
per_book = {}
for s in trig:
    per_book.setdefault(s["book_id"], []).append(s["t"])
gaps_ok = all(b - a >= md.TRIGGER_COOLDOWN_S - 1e-9 for ts in per_book.values() for a, b in zip(ts, ts[1:]))
check("trigger cooldown is per book-contract", "microdata.py:496-501 (last_trigger keyed by contract_key)",
      gaps_ok and len(per_book) == 2, f"{ {k: len(v) for k, v in per_book.items()} }")

# 9. labels use the first refreshed observation in the window --------------------------------
lab_rows = [obs(t, "kalshi", o, m, fee_params=dict(KF)) for t in range(0, 20) for o, m in (("A", .40), ("B", .60))]
lab_rows.append(obs(5.5, "kalshi", "A", .90, refreshed=0))          # carried: never a mark
s0 = next(s for s in build(lab_rows, sample="unconditional") if s["t"] == 0.0 and s["outcome"] == "A")
check("labels use the FIRST refreshed observation in the specified future window",
      "microdata.py:580-587 (bisect over the de-duplicated refreshed series)", abs(s0["dmid_5_fwd"]) < 1e-12)

# 10. missing labels excluded and counted -----------------------------------------------------
samples = [dict(s) for s in build(lab_rows, sample="unconditional") if s["outcome"] == "A"]
m = ev.candidate_metrics(samples, 5, seed=1, draws=10)
check("missing labels are excluded AND counted",
      "microstructure_eval.py:605-611 candidate_metrics (labelled / missing_labels)",
      m["missing_labels"] > 0 and m["labelled"] + m["missing_labels"] == m["attempted_orders"])

# 11. short labels use an executable complement -------------------------------------------------
s_any = next(s for s in build(two_books(), sample="unconditional") if s["outcome"] == "A" and s["t"] > 5)
bc = ev.bought_contract(s_any, -1)
check("short labels use an executable complement",
      "microstructure_eval.py:450-460 bought_contract -> microdata.py:609 _complement",
      bc is not None and bc["key"][2] == "B" and bc["key"][1] == s_any["book_id"] and not bc["self"])

# 12. same-book rows collapse ----------------------------------------------------------------
direct = obs(5, "kalshi", "A", .40, venue="kalshi")
resale = obs(5, "kalshi", "A", .30, venue="robinhood", venue_market_id="RH-KX")
got1, _ = md.resolve_instant([(0, direct), (0, resale)])
got2, _ = md.resolve_instant([(0, resale), (0, direct)])
check("same-book rows collapse correctly", "microdata.py:368-386 resolve_instant (direct route outranks a resale)",
      got1[1]["venue"] == got2[1]["venue"] == "kalshi" and md.contract_key(direct) == md.contract_key(resale))

# 13. cross-book needs settlement identity AND tie payout -----------------------------------------
mixed = [obs(t, "kalshi", "A", .40, tie=.5, fee_params=dict(KF)) for t in range(40)]
mixed += [obs(t, "rothera", "A", .41, venue="robinhood", tie=0.0) for t in range(40)]
with mock.patch("arb_engine.quant.microdata.settlement_relation", return_value="identical"):
    s_tie = next(x for x in md.build(mixed, sample="unconditional", horizons=(), fee_for_row=lambda r: None)
                 if x["book_id"] == "kalshi" and x["t"] == 35.0)
same_tie = [obs(t, "kalshi", "A", .40, tie=.5, fee_params=dict(KF)) for t in range(40)]
same_tie += [obs(t, "rothera", "A", .41, venue="robinhood", tie=.5) for t in range(40)]
with mock.patch("arb_engine.quant.microdata.settlement_relation", return_value="unverified"):
    s_rel = next(x for x in md.build(same_tie, sample="unconditional", horizons=(), fee_for_row=lambda r: None)
                 if x["book_id"] == "kalshi" and x["t"] == 35.0)
with identical():
    s_ok = next(x for x in md.build(same_tie, sample="unconditional", horizons=(), fee_for_row=lambda r: None)
                if x["book_id"] == "kalshi" and x["t"] == 35.0)
check("cross-book matching requires settlement identity AND tie payout",
      "microdata.py:542-545 (tie_match and settle == 'identical')",
      s_tie["cross"] == {} and s_tie["cross_excluded"].get("tie_mismatch")
      and s_rel["cross"] == {} and s_rel["cross_excluded"].get("settlement_unverified")
      and list(s_ok["cross"]) == ["rothera"], "equal ties + verbatim rules is the only registered comparison")

# 14. unknown tie excluded from guaranteed claims ---------------------------------------------
legs_a = [obs(t, "kalshi", "A", None, bid=.44, ask=.45) for t in range(20)]
legs_b = [obs(t, "rothera", "B", None, bid=.49, ask=.50, venue="robinhood") for t in range(20)]
unknown = two_leg_arb(legs_a, legs_b, 0.0, .45, .50, 10, ZeroFees(), ZeroFees(), latency_a_s=1, latency_b_s=1,
                      tie_payouts=(.5, None), settlement_compatible=True, book_id_a="kalshi", book_id_b="rothera")
check("unknown tie identity is excluded from guaranteed claims",
      "paperexec.py:285-287 (unknown-tie); :213-219 ArbResult.guaranteed",
      unknown.excluded == "unknown-tie" and not unknown.guaranteed and unknown.pnl is None)

# 15. H4 guaranteed vs speculation ---------------------------------------------------------------
recs = [{"game": "g", "t": 1.0, "margin": .05, "tie_safe": True, "settlement": "identical", "class": "guaranteed-eligible",
         "guaranteed_result": True, "excluded": None, "legs_filled": 2, "matched": 10, "unwound": 0, "unresolved": 0,
         "pnl_win": .05, "pnl_tie": .01, "pnl_worst": .01, "pnl_ev": .05},
        {"game": "g", "t": 2.0, "margin": .05, "tie_safe": False, "settlement": "unverified", "class": "speculation",
         "guaranteed_result": False, "excluded": None, "legs_filled": 2, "matched": 10, "unwound": 0, "unresolved": 0,
         "pnl_win": .40, "pnl_tie": -.60, "pnl_worst": -.60, "pnl_ev": .39}]
rep = ev.h4_report(recs, seed=1, draws=10)
check("H4 guaranteed and speculative results are never mixed",
      "microstructure_eval.py:1060-1071 h4_report (worst case vs win case, separate blocks)",
      rep["guaranteed"]["trades"] == 1 and abs(rep["guaranteed"]["mean_ret"]["point"] - .01) < 1e-9
      and rep["speculation"]["trades"] == 1 and abs(rep["speculation"]["mean_ret"]["point"] - .40) < 1e-9)

# 16. paper execution off the decision time, and never before the fill -------------------------------
late = [{"obs_ts": 2.5, "refreshed": 1, "bid": .49, "ask": .50, "bid_size": 100, "ask_size": 100,
         "book_id": "kalshi", "venue_market_id": "K", "side": "yes"},
        {"obs_ts": 31.5, "refreshed": 1, "bid": .60, "ask": .61, "bid_size": 100, "ask_size": 100,
         "book_id": "kalshi", "venue_market_id": "K", "side": "yes"}]
horizon_from_decision = ioc_round_trip(late, 0.0, .50, 10, ZeroFees(), latency_s=1.0, horizon_s=30.0)
one_row = ioc_round_trip([{"obs_ts": 3.0, "refreshed": 1, "bid": .70, "ask": .50, "bid_size": 100, "ask_size": 100,
                           "book_id": "kalshi", "venue_market_id": "K", "side": "yes"}],
                         0.0, .50, 10, ZeroFees(), latency_s=1.0, horizon_s=2.0, entry_tol_s=5.0)
check("paper execution uses DECISION time plus latency, not observed fill time",
      "paperexec.py:167-181 (t_out = decision + latency + horizon; exits strictly after the fill)",
      [t for t, _, _, _ in horizon_from_decision.exits] == [31.5] and one_row.exits == [] and one_row.unresolved == 10,
      "M4 fixed: the filling observation is never also the exit")

# 17. determinism of a whole evaluation ------------------------------------------------------------
SPEC = {"version": 99, "primary": "H3@5s", "horizons": [5], "bootstrap": 20, "seed": 7, "signal_cooldown_s": 60,
        "latencies_s": [1], "haircuts": [1.0], "robust": {"latency_s": 1, "haircut": 1.0},
        "manual_leg_latency_s": 2, "secondary": [], "lock_watch_s": 20}
ev_rows, finals = [], {}
for gi, (h, aw) in enumerate((("A", "B"), ("C", "D"))):
    key = f"nfl:{h}|{aw}:2026-09-27"
    finals[key] = (h, aw, h)
    for t in range(0, 60):
        wave = .04 * math.sin(t / 7.0 + gi)
        ev_rows.append(obs(t, "kalshi", h, .50 + wave, event=key, fee_params=dict(KF)))
        ev_rows.append(obs(t, "kalshi", aw, .50 - wave, event=key, fee_params=dict(KF)))
        ev_rows.append(obs(t, "rothera", h, .49 + wave, event=key, venue="robinhood", exchange="rothera", tie=0.0))
        ev_rows.append(obs(t, "rothera", aw, .49 - wave, event=key, venue="robinhood", exchange="rothera", tie=0.0))
data = {"espn": [], "prints": [], "finals": finals, "event_keys": sorted(finals)}
run = lambda rs: json.dumps(ev.evaluate({**data, "rows": rs}, dict(SPEC), 1.0, False), sort_keys=True, default=str)   # noqa: E731
base = run(list(ev_rows))
sh = list(ev_rows)
random.Random(3).shuffle(sh)
check("evaluator output is deterministic across runs and across input permutations",
      "microdata.py:417-428 observation_instants; microstructure_eval.py:435 select, :463 select_trades, :980 arb_scan",
      base == run(list(ev_rows)) == run(sh) == run(list(reversed(ev_rows))))


def main() -> int:
    width = max(len(n) for n, _, _ in RESULTS)
    print(f"{'property'.ljust(width)}  verdict  enforced at")
    print("-" * (width + 60))
    for name, ok, where in RESULTS:
        print(f"{name.ljust(width)}  {'HOLDS ' if ok else 'FAILS '}  {where}")
    bad = [n for n, ok, _ in RESULTS if not ok]
    print()
    print(f"{len(RESULTS) - len(bad)}/{len(RESULTS)} properties hold on synthetic fixtures.")
    print("This is an accounting check. It is not evidence of profitability and says nothing about live safety.")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

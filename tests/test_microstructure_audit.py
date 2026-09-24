"""Adversarial checks for the microstructure audit (docs/MODEL.md, "Microstructure experiment").

Each class pins one audit finding: simultaneous observations, normalized NO settlement,
the null test, the effective-spec hash, the test-open ledger, symmetric decisions through
executable complements, decision-time book identity, metric accounting and partial hedges.
"""
import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arb_engine.fees.base import ZeroFees

EV = "nfl:A|B:2026-09-27"


def obs(t, book, outcome, bid, ask, venue=None, side="yes", size=100, tie=None, **kw):
    r = {"event_key": EV, "obs_ts": float(t), "req_ts": float(t), "refreshed": 1, "in_play": True, "source": "fast",
         "venue": venue or book, "book_id": book, "venue_market_id": f"{book}-{outcome}-{side}", "outcome": outcome, "side": side,
         "bid": bid, "ask": ask, "bid_size": size, "ask_size": size, "quote_time": float(t)}
    if tie is not None:
        r["tie_payout"] = tie
    r.update(kw)
    return r


class AtomicObservationTests(unittest.TestCase):
    """1. Rows with one obs_ts are one batch: cross-book features never depend on arrival order."""

    def _rows(self, a="alpha", z="zulu"):
        out = []
        for t in range(0, 40):
            ma = .40 if t < 30 else .48          # book a jumps at t=30, book z does not
            out.append(obs(t, a, "A", round(ma - .01, 2), round(ma + .01, 2), tie=.5))
            out.append(obs(t, z, "A", .39, .41, tie=.5))
        return out

    def _cross(self, samples, book, t):
        s = next(x for x in samples if x["book_id"] == book and x["t"] == t and x["kind"] == "unconditional")
        return s["leader_book"], sorted(s["cross"])

    def test_permutation_and_book_name_order_do_not_matter(self):
        from arb_engine.quant.microdata import build

        with mock.patch("arb_engine.quant.microdata.settlement_relation", return_value="identical"):
            base = build(self._rows(), sample="unconditional", horizons=(), fee_for_row=lambda r: None)
            rows = self._rows()
            random.Random(7).shuffle(rows)
            shuffled = build(rows, sample="unconditional", horizons=(), fee_for_row=lambda r: None)
            # Names that sort the other way round: the moving book now sorts last.
            renamed = build(self._rows(a="zz_mover", z="aa_still"), sample="unconditional", horizons=(), fee_for_row=lambda r: None)
        strip = lambda xs: json.dumps(sorted(xs, key=lambda x: (x["t"], x["book_id"], x["kind"])), sort_keys=True, default=str)  # noqa: E731
        self.assertEqual(strip(base), strip(shuffled))
        # At t=30 both books were observed at the same instant: each sees the other.
        self.assertEqual(self._cross(base, "zulu", 30.0), ("alpha", ["alpha"]))
        self.assertEqual(self._cross(base, "alpha", 30.0), ("zulu", ["zulu"]))
        self.assertEqual(self._cross(renamed, "aa_still", 30.0), ("zz_mover", ["zz_mover"]))
        self.assertEqual(self._cross(renamed, "zz_mover", 30.0), ("aa_still", ["aa_still"]))

    def test_appending_future_rows_changes_no_past_sample(self):
        from arb_engine.quant.microdata import build

        rows = self._rows()
        past = [r for r in rows if r["obs_ts"] <= 25]
        with mock.patch("arb_engine.quant.microdata.settlement_relation", return_value="identical"):
            full = [x for x in build(rows, sample="all", horizons=(), fee_for_row=lambda r: None) if x["t"] <= 25]
            part = build(past, sample="all", horizons=(), fee_for_row=lambda r: None)
        self.assertEqual(json.dumps(full, sort_keys=True, default=str), json.dumps(part, sort_keys=True, default=str))


class NormalizedSettlementTests(unittest.TestCase):
    """2. Adapter rows are normalized (a NO row names the team it pays on); never invert them."""

    def _adapter_rows(self):
        from arb_engine.models import VenueSnapshot
        from arb_engine.store import Store
        from arb_engine.venues.robinhood import RobinhoodAdapter

        ad = RobinhoodAdapter(http=object())
        snap = VenueSnapshot(venue="robinhood", fetched_at=1000.0)
        contracts = [{"id": "c-det", "symbol": "NFLGAME-26SEP27DETBUF-DET", "displayShortName": "DET", "displayLongName": "Detroit Lions",
                      "exchange": "EXCHANGE_SOURCE_ROTHERA"},
                     {"id": "c-buf", "symbol": "NFLGAME-26SEP27DETBUF-BUF", "displayShortName": "BUF", "displayLongName": "Buffalo Bills",
                      "exchange": "EXCHANGE_SOURCE_ROTHERA"}]
        quotes = {"c-det": {"yes_ask_price": .41, "yes_bid_price": .40, "no_ask_price": .60, "no_bid_price": .59, "ask_size": 50, "bid_size": 50},
                  "c-buf": {"yes_ask_price": .60, "yes_bid_price": .59, "no_ask_price": .41, "no_bid_price": .40, "ask_size": 50, "bid_size": 50}}
        states = {"E1": {"gameStart": "2026-09-27T17:00:00Z"}}
        ad.ingest(snap, "nfl", "nfl", [{"event": {"id": "E1", "urlSlugs": ["det-buf"]}, "contracts": contracts}], quotes, states, emit_no_side=True)
        l1 = Store.l1_from_quotes({"robinhood": snap.quotes}, req_ts=1000.0, obs_ts=1000.0)
        ev = snap.quotes[0].event_key
        rows = [dict(r, event_key=ev) for r in l1["rows"]]
        # A Kalshi YES on DET for the same game: $0.50 on a tie (half), its own book.
        rows.append({"event_key": ev, "venue": "kalshi", "book_id": "kalshi", "venue_market_id": "KX-DET", "outcome": "DET", "side": "yes",
                     "bid": .40, "ask": .41, "obs_ts": 1000.0, "refreshed": 1})
        return ev, rows

    def test_adapter_yes_and_no_rows_across_win_loss_and_tie(self):
        from arb_engine.quant.microdata import contract_key, settlement_values

        ev, rows = self._adapter_rows()
        no_det = next(r for r in rows if r["side"] == "no" and r.get("no_of") == "DET")
        self.assertEqual(no_det["outcome"], "BUF")                       # the adapter already normalized it
        keys = {"yes_det": contract_key(next(r for r in rows if r["side"] == "yes" and r["outcome"] == "DET" and r["book_id"] == "rothera")),
                "no_det": contract_key(no_det), "kalshi_det": contract_key(rows[-1])}
        expect = {"DET": {"yes_det": 1.0, "no_det": 0.0, "kalshi_det": 1.0},     # DET wins
                  "BUF": {"yes_det": 0.0, "no_det": 1.0, "kalshi_det": 0.0},     # DET loses
                  None: {"yes_det": 0.0, "no_det": 1.0, "kalshi_det": 0.5}}      # tie: Rothera YES $0, its NO $1, Kalshi YES $0.50
        for winner, want in expect.items():
            got = settlement_values(rows, {ev: ("BUF", "DET", winner)})
            self.assertEqual({k: got.get(v) for k, v in keys.items()}, want, winner)

    def test_books_with_different_tie_payouts_never_overwrite_each_other(self):
        from arb_engine.quant.microdata import settlement_values

        ev, rows = self._adapter_rows()
        for order in (rows, list(reversed(rows))):
            got = settlement_values(order, {ev: ("BUF", "DET", None)})
            self.assertEqual(got[(ev, "rothera", "DET", "yes")], 0.0)
            self.assertEqual(got[(ev, "kalshi", "DET", "yes")], 0.5)


class NullTestTests(unittest.TestCase):
    """3. The p-value is a game-level sign-flip test with a real null distribution."""

    def test_alternative_and_null(self):
        from scripts.microstructure_eval import sign_flip_p

        pos = {f"g{i}": [.02, .01] for i in range(10)}
        self.assertAlmostEqual(sign_flip_p(pos), 1 / 1024)                 # exact: only the observed signs reach T
        self.assertEqual(sign_flip_p({f"g{i}": [-.02] for i in range(10)}), 1.0)
        sym = {"a": [.03], "b": [-.03], "c": [.01], "d": [-.01]}
        self.assertGreater(sign_flip_p(sym), .4)
        self.assertEqual(sign_flip_p({}), 1.0)
        big = {f"g{i}": [.01] for i in range(20)}                          # Monte Carlo branch, fixed seed
        self.assertEqual(sign_flip_p(big, seed=1, draws=500), sign_flip_p(big, seed=1, draws=500))
        self.assertAlmostEqual(sign_flip_p(big, seed=1, draws=500), 1 / 501)

    def test_size_under_the_null(self):
        from scripts.microstructure_eval import sign_flip_p

        rng = random.Random(3)
        rejections = 0
        for _ in range(300):
            games = {f"g{i}": [rng.gauss(0, 1) for _ in range(rng.randint(1, 4))] for i in range(10)}
            rejections += sign_flip_p(games) <= .10
        self.assertLessEqual(rejections / 300, .14)                        # nominal .10; exact test, sampling noise allowed


class SpecHashTests(unittest.TestCase):
    """4. Every behaviour-changing input moves the effective-spec digest."""

    MAN = {"discovery_dates": ["2026-09-20"], "test_from": "2026-10-08", "spec": {"horizons": [30]}}

    def _digest(self, **kw):
        from scripts.microstructure_eval import effective_spec, spec_hash

        return spec_hash(effective_spec(kw.pop("manifest", self.MAN), **kw))

    def test_each_mutation_changes_the_digest(self):
        from arb_engine.fees import kalshi as kfees
        from scripts.microstructure_eval import CODE_FILES, effective_spec, spec_hash

        base = self._digest()
        self.assertEqual(base, self._digest())
        self.assertNotEqual(base, self._digest(runtime={"latency_override": 3.0}))                        # --latency
        self.assertNotEqual(base, self._digest(manifest={**self.MAN, "test_from": "2026-10-09"}))          # fold policy
        self.assertNotEqual(base, self._digest(manifest={**self.MAN, "spec": {"horizons": [15]}}))         # configuration
        self.assertNotEqual(base, self._digest(frozen={"coef": {"B3_ridge_dmid30": {"30": [0, .1]}}}))     # frozen models
        with mock.patch.object(kfees.KalshiFees, "fee", lambda self, p, c, role="taker": 0):             # fee implementation
            self.assertNotEqual(base, self._digest())
        with mock.patch.dict(os.environ, {"ROBINHOOD_ROTHERA_FEE_MODEL": "quadratic"}):                   # fee setting
            self.assertNotEqual(base, self._digest())
        import arb_engine.matching.settlement_rules as sr
        real = sr.lookup
        with mock.patch.object(sr, "lookup", lambda *a, **k: {**(real(*a, **k) or {}), "tie": "half"}):   # settlement registry
            self.assertNotEqual(base, self._digest())
        with tempfile.TemporaryDirectory() as tmp:                                                        # code: matching logic
            for f in CODE_FILES:
                (Path(tmp) / f).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / f).write_text("x")
            a = spec_hash(effective_spec(self.MAN, root=Path(tmp)))
            (Path(tmp) / "arb_engine/matching/normalize.py").write_text("y")
            self.assertNotEqual(a, spec_hash(effective_spec(self.MAN, root=Path(tmp))))
            (Path(tmp) / "arb_engine/data/settlement_rules.json").unlink()                                 # a missing file counts
            self.assertIn("MISSING", effective_spec(self.MAN, root=Path(tmp))["code"].values())


def _db(tmp, games, n=200):
    import sqlite3

    db = Path(tmp) / "h.db"
    con = sqlite3.connect(db)
    con.execute("create table inplay_ticks (ts real, event_key text, live integer, l1_json text, source text)")
    con.execute("create table espn_ticks (ts real, event_key text, status text, period integer, clock integer, home text, away text, home_score integer, away_score integer, last_play_type text)")
    for g in games:
        a, b = g.split(":")[1].split("|")
        for t in range(n):
            mid = .5 + .06 * ((t // 60) % 2)
            rows = [{"venue": v, "book_id": bk, "outcome": o, "side": "yes", "obs_ts": 1000.0 + t, "refreshed": 1, "venue_market_id": f"{bk}-{o}",
                     "bid": round((mid if o == a else 1 - mid) - .01, 3), "ask": round((mid if o == a else 1 - mid) + .01, 3), "bid_size": 50,
                     "ask_size": 50, "fee_params": {}, "exchange": "rothera" if bk == "rothera" else None, "tie_payout": 0.0 if bk == "rothera" else .5}
                    for v, bk in (("kalshi", "kalshi"), ("robinhood", "rothera")) for o in (a, b)]
            con.execute("insert into inplay_ticks values (?,?,?,?,?)", (1000.0 + t, g, 1, json.dumps({"rows": rows}), "fast"))
    con.commit()
    con.close()
    return db


def _run(argv):
    import contextlib
    import io
    from scripts import microstructure_eval as ev

    with contextlib.redirect_stdout(io.StringIO()):
        return ev.main(argv)


class TestOpenLedgerTests(unittest.TestCase):
    """5. Test-open intent is durable, written before test data is read, and independent of --log."""

    def test_intent_precedes_reading_and_another_log_cannot_reset_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp, ["nfl:A|B:2026-10-11"])
            man = Path(tmp) / "m.json"
            man.write_text(json.dumps({"discovery_dates": ["2026-09-20"], "test_from": "2026-10-08",
                                       "spec": {"horizons": [30], "bootstrap": 50, "seed": 1}}))
            frozen = Path(tmp) / "frozen.json"
            frozen.write_text(json.dumps({"coef": {"B3_ridge_dmid30": {"30": [0, 0]}, "B4_ridge_gap": {"30": [0, 0]}}}))
            fspec = Path(tmp) / "fz" / "frozen_spec.json"
            common = ["--db", str(db), "--manifest", str(man), "--frozen", str(frozen), "--frozen-spec", str(fspec)]
            self.assertEqual(_run(common + ["--fold", "discovery", "--freeze-spec", str(fspec), "--log", str(Path(tmp) / "a.jsonl")]), 0)
            ledger = fspec.parent / "test_open_ledger.jsonl"
            self.assertEqual(json.loads(fspec.read_text())["test_ledger"], ledger.name)
            # The data read fails: the intent was already on disk, and the failure is recorded.
            with mock.patch("arb_engine.quant.microdata.load_db", side_effect=OSError("disk gone")):
                with self.assertRaises(OSError):
                    _run(common + ["--fold", "test", "--log", str(Path(tmp) / "a.jsonl")])
            events = [json.loads(x) for x in ledger.read_text().splitlines()]
            self.assertEqual([e["event"] for e in events], ["intent", "failed"])
            self.assertEqual(events[0]["run_id"], events[1]["run_id"])
            self.assertFalse((Path(tmp) / "a.jsonl").read_text().count('"fold":"test"'))   # the audit log never saw a finished test run
            # A later spec, re-frozen, with a brand-new --log: the ledger still knows the first opening.
            frozen.write_text(json.dumps({"coef": {"B3_ridge_dmid30": {"30": [0, .2]}, "B4_ridge_gap": {"30": [0, 0]}}}))
            self.assertEqual(_run(common + ["--fold", "discovery", "--freeze-spec", str(fspec), "--log", str(Path(tmp) / "b.jsonl")]), 0)
            with self.assertRaises(RuntimeError):
                _run(common + ["--fold", "test", "--log", str(Path(tmp) / "fresh.jsonl")])
            out = Path(tmp) / "r.json"
            self.assertEqual(_run(common + ["--fold", "test", "--log", str(Path(tmp) / "fresh.jsonl"), "--results", str(out),
                                            "--reopen-test", "models refit after a data fix"]), 0)
            self.assertTrue(json.loads(out.read_text())["exploratory"])
            events = [json.loads(x) for x in ledger.read_text().splitlines()]
            self.assertEqual([e["event"] for e in events], ["intent", "failed", "intent", "completed"])
            self.assertEqual(events[3]["results_sha256"], __import__("hashlib").sha256(out.read_bytes()).hexdigest())

    def test_a_latency_override_is_part_of_the_frozen_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp, ["nfl:A|B:2026-10-11"], n=100)
            man = Path(tmp) / "m.json"
            man.write_text(json.dumps({"test_from": "2026-10-08", "spec": {"horizons": [30], "bootstrap": 50, "seed": 1}}))
            frozen = Path(tmp) / "frozen.json"
            frozen.write_text(json.dumps({"coef": {"B3_ridge_dmid30": {"30": [0, 0]}, "B4_ridge_gap": {"30": [0, 0]}}}))
            fspec = Path(tmp) / "frozen_spec.json"
            common = ["--db", str(db), "--manifest", str(man), "--frozen", str(frozen), "--frozen-spec", str(fspec), "--log", str(Path(tmp) / "l.jsonl")]
            _run(common + ["--fold", "discovery", "--freeze-spec", str(fspec)])
            with self.assertRaises(SystemExit):
                _run(common + ["--fold", "test", "--latency", "0.5"])          # not the spec that was frozen
            self.assertFalse((Path(tmp) / "test_open_ledger.jsonl").exists())


def _sample(t, outcome, dmid, book="kalshi", comp=None, missing=None, kind="trigger", **kw):
    s = {"t": float(t), "kind": kind, "event_key": EV, "book_id": book, "outcome": outcome, "side": "yes", "venue": "kalshi" if book == "kalshi" else "robinhood",
         "ask": .51, "mid": .50, "dmid_30": dmid, "complement": comp, "complement_missing": missing}
    s.update(kw)
    return s


class SymmetricDecisionTests(unittest.TestCase):
    """6. A fall is traded through a real executable complement; mirrors are one trade."""

    def test_complement_found_by_payoff_not_by_name(self):
        from arb_engine.quant.microdata import build

        rows = []
        for t in range(0, 12):
            rows += [obs(t, "kalshi", "A", .49, .51, tie=.5), obs(t, "kalshi", "B", .48, .50, tie=.5),                  # Kalshi: YES-B complements YES-A
                     obs(t, "rothera", "A", .49, .51, venue="robinhood", tie=0.0), obs(t, "rothera", "B", .48, .50, venue="robinhood", tie=0.0)]
        s = {(x["book_id"], x["outcome"]): x for x in build(rows, sample="unconditional", horizons=(), fee_for_row=lambda r: None) if x["t"] == 10.0}
        self.assertEqual(s[("kalshi", "A")]["complement"]["key"], [EV, "kalshi", "B", "yes"])
        self.assertEqual(s[("kalshi", "A")]["complement"]["ask"], .50)
        # Rothera YES-B pays $0 on a tie, like YES-A: not the complement of YES-A.
        self.assertIsNone(s[("rothera", "A")]["complement"])
        self.assertEqual(s[("rothera", "A")]["complement_missing"], "tie-inexact")
        # With the Rothera NO of A (recorded on B, side no, $1 on a tie) it is.
        rows += [obs(t, "rothera", "B", .48, .50, venue="robinhood", side="no", tie=1.0, no_of="A") for t in range(0, 12)]
        s = {(x["book_id"], x["outcome"], x["side"]): x for x in build(rows, sample="unconditional", horizons=(), fee_for_row=lambda r: None) if x["t"] == 10.0}
        self.assertEqual(s[("rothera", "A", "yes")]["complement"]["key"], [EV, "rothera", "B", "no"])
        # A stale complement is not a complement.
        late = [r for r in rows if not (r["book_id"] == "kalshi" and r["outcome"] == "B" and r["obs_ts"] > 5)]
        s = {(x["book_id"], x["outcome"]): x for x in build(late, sample="unconditional", horizons=(), fee_for_row=lambda r: None) if x["t"] == 10.0}
        self.assertEqual(s[("kalshi", "A")]["complement_missing"], "stale")

    def test_mirrors_are_one_trade_and_missing_complements_are_counted(self):
        from scripts.microstructure_eval import _dir_h1, select_trades

        comp_of_b = {"key": [EV, "kalshi", "A", "yes"], "venue": "kalshi", "bid": .50, "ask": .52}
        up_a = _sample(10, "A", .06)                                     # A rose: buy A
        down_b = _sample(10, "B", -.06, comp=comp_of_b)                 # its mirror, B fell: buy B's complement = A
        down_r = _sample(10, "A", -.06, book="rothera", missing="tie-inexact")
        trades, st = select_trades([down_b, up_a, down_r], _dir_h1, 60.0)
        self.assertEqual(len(trades), 1)                                 # one economic trade: long A on Kalshi
        self.assertEqual(tuple(trades[0][2]["key"]), (EV, "kalshi", "A", "yes"))
        self.assertEqual(st["same_exposure_dropped"], 1)
        self.assertEqual(st["complement_unavailable"], {"tie-inexact": 1})
        _, d, bc = select_trades([down_b], _dir_h1, 60.0)[0][0]
        self.assertEqual((d, bc["ask"], bc["self"]), (-1, .52, False))  # the complement's own ask, not 1 - A's bid

    def test_h3_is_symmetric(self):
        from scripts.microstructure_eval import _h3_direction

        base = {"kind": "unconditional", "dmid_30": 0.0}
        self.assertEqual(_h3_direction({**base, "leader_dmid_30": .08, "gap_leader": .05}), 1)
        self.assertEqual(_h3_direction({**base, "leader_dmid_30": -.08, "gap_leader": -.05}), -1)
        self.assertEqual(_h3_direction({**base, "leader_dmid_30": -.08, "gap_leader": .05}), 0)      # gap against the leader's move
        self.assertEqual(_h3_direction({**base, "dmid_30": -.05, "leader_dmid_30": -.08, "gap_leader": -.05}), 0)   # follower already moved

    def test_a_complement_trade_executes_on_the_complements_own_book(self):
        from scripts.microstructure_eval import ExecCtx, realize

        rows = [obs(t, "kalshi", "B", .47, .49, size=3) for t in range(0, 60)] + [obs(t, "kalshi", "A", .50, .52) for t in range(0, 60)]
        ctx = ExecCtx(rows, {}, lambda r: ZeroFees(), n=10)
        s = _sample(5, "A", -.06, comp={"key": [EV, "kalshi", "B", "yes"], "venue": "kalshi", "bid": .47, "ask": .49})
        tr = realize(ctx, s, -1, {"key": (EV, "kalshi", "B", "yes"), "venue": "kalshi", "ask": .49, "mid": .48, "self": False}, [30], 1.0, 1.0, reuse=True)
        e = tr["exec_30"]
        self.assertEqual((e["filled"], e["requested"]), (3, 10))         # B's own displayed depth, not A's
        self.assertAlmostEqual(e["entry_notional"], 3 * .49)
        self.assertEqual(tr["bought"], [EV, "kalshi", "B", "yes"])


class BookIdentityTests(unittest.TestCase):
    """8. arb_scan pins each leg's book at decision time."""

    def test_arb_scan_passes_decision_time_books(self):
        from scripts import microstructure_eval as ev

        rows = []
        for t in range(0, 30):
            rows += [obs(t, "kalshi", "A", .43, .44, tie=.5), obs(t, "rothera", "B", .43, .44, venue="robinhood", tie=1.0, exchange="rothera")]
        seen = []
        real = __import__("arb_engine.quant.paperexec", fromlist=["two_leg_arb"]).two_leg_arb

        def spy(*a, **k):
            seen.append((k.get("book_id_a"), k.get("book_id_b")))
            return real(*a, **k)
        with mock.patch("arb_engine.quant.paperexec.two_leg_arb", spy):
            recs = ev.arb_scan(rows, lambda r: ZeroFees(), 1, 5)
        self.assertTrue(recs)
        self.assertEqual(set(seen), {("kalshi", "rothera")})

    def test_same_book_is_rejected_with_no_future_quotes(self):
        from arb_engine.quant.paperexec import two_leg_arb

        r = two_leg_arb([], [], 0, .40, .50, 10, ZeroFees(), ZeroFees(), book_id_a="kalshi", book_id_b="kalshi")
        self.assertEqual((r.excluded, r.legs[0].filled, r.legs[1].filled), ("same-book", 0, 0))


class MetricAccountingTests(unittest.TestCase):
    """9. Registered grid, manual Robinhood latency, fee share, unsupported metrics."""

    def test_report_carries_the_grid_manual_latency_and_explicit_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _db(tmp, ["nfl:A|B:2026-09-27", "nfl:C|D:2026-09-27"])
            man = Path(tmp) / "m.json"
            man.write_text(json.dumps({"test_from": "2026-10-08", "spec": {"horizons": [30], "bootstrap": 50, "seed": 1}}))
            frozen = Path(tmp) / "frozen.json"
            frozen.write_text(json.dumps({"coef": {"B3_ridge_dmid30": {"30": [0, 0]}, "B4_ridge_gap": {"30": [0, 0]}}}))
            out = Path(tmp) / "r.json"
            self.assertEqual(_run(["--db", str(db), "--manifest", str(man), "--fold", "validation", "--frozen", str(frozen),
                                   "--log", str(Path(tmp) / "l.jsonl"), "--results", str(out)]), 0)
            r = json.loads(out.read_text())
        ex = r["execution"]
        self.assertEqual(ex["registered_grid"], ["L1_h1", "L1_h0.5", "L3_h1", "L3_h0.5"])
        self.assertEqual(ex["venue_latency"], {"robinhood": 15.0})
        self.assertEqual(ex["cadence_s"], 1.0)
        b1 = r["horizons"]["30"]["B1_buy_any"]
        self.assertEqual(set(b1["grid"]), set(ex["registered_grid"]))
        self.assertIn("fee_share_of_notional", b1)
        self.assertIn("unsupported", b1)
        for name, inp in r["decision_inputs"].items():
            self.assertIn("missing", inp, name)
            self.assertIn("robust_l3_h05", inp, name)
        h3 = r["horizons"]["30"]["H3_leadlag"]
        self.assertEqual(h3["attempted_orders"], 0)                       # Kalshi vs Rothera: no settlement identity
        self.assertEqual(h3["unsupported"]["mean_ret"], "no resolved trade")
        self.assertIn("complement_unavailable", h3["selection"])
        self.assertEqual(r["primary"]["test"], "game-level sign-flip, one-sided (mean > 0)")

    def test_legacy_cadence_marks_fast_latencies_unsupported(self):
        from scripts.microstructure_eval import _cadence, ExecCtx

        rows = [obs(t, "kalshi", "A", .49, .51) for t in range(0, 60, 5)]
        self.assertEqual(_cadence(ExecCtx(rows, {}, lambda r: ZeroFees(), 10)), 5.0)   # > 1 s + 2 s tolerance: L1 cannot fill


class PartialHedgeTests(unittest.TestCase):
    """10. Every contract a hedge fills stays in the books, locked or not."""

    def test_partial_hedge_locks_what_it_filled_and_exits_the_rest(self):
        from scripts.microstructure_eval import h3_lock_trades

        rows = []
        for t in range(0, 700):
            a_mid = .60 if t < 100 else .80
            rows.append(obs(t, "kalshi", "KC", round(a_mid - .01, 2), round(a_mid + .01, 2), tie=.5))
            # DEN offered cheap enough to lock only at t=150..151, and only 4 contracts deep.
            den_ask, den_size = (.21, 4) if 150 <= t <= 151 else (.60, 100)
            rows.append(obs(t, "kalshi", "DEN", .19, den_ask, size=den_size, tie=.5))
        s = [{"kind": "unconditional", "t": 10.0, "event_key": EV, "book_id": "kalshi", "outcome": "KC", "side": "yes", "venue": "kalshi",
              "ask": .61, "mid": .60, "dmid_30": 0.0, "leader_dmid_30": .08, "gap_leader": .06}]
        out = h3_lock_trades(s, rows, lambda r: ZeroFees(), latency_s=1, watch_s=600, n=10)
        inv = out["inventory"]
        self.assertEqual((out["filled"], inv["partial_hedges"], inv["locked_contracts"], inv["unhedged_contracts_at_watch_end"]), (1, 1, 4, 6))
        self.assertEqual(inv["partly_locked"], 1)
        # 4 locked sets pay $1; 6 unhedged KC sold to the 0.79 bid after the watch; 10 KC bought at 0.61, 4 DEN at 0.21.
        want = (4 * 1.0 + 6 * .79 - 10 * .61 - 4 * .21) / 10
        self.assertAlmostEqual(out["rets"][EV][0], want)
        self.assertEqual(out["locked_only"], {})                         # not a full lock


if __name__ == "__main__":
    unittest.main()

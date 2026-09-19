import argparse
import contextlib
import io
import json
import os
import unittest

from arb_engine.store import Store
from arb_engine.tickreplay import FreshnessTracker, fallback_gate, format_replay, game_state_from, load_fixture, merged_event_from, replay_both, replay_ticks, resolve_event

from .helpers import FIXTURES

T0 = 1_800_000_000.0
KEY = "nfl:DEN|KC:2026-09-21"


def _db(name):
    path = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"arb_tickreplay_{name}_{os.getpid()}.db")
    if os.path.exists(path):
        os.unlink(path)
    return path


class FixtureTests(unittest.TestCase):
    def test_fixture_loads_into_both_tables(self):
        path = _db("load")
        st = Store(path)
        key = load_fixture(st, str(FIXTURES / "ticks/synthetic_40.json"))
        self.assertEqual(key, KEY)
        self.assertEqual(len(st.tick_rows(key)), 40)
        self.assertEqual(len(st.espn_tick_rows(key)), 40)
        t = st.tick_rows(key)[0]
        self.assertEqual((t["home"], t["away"], t["live"], t["kalshi_home_ask"], t["robinhood_away_bid"], t["robinhood_book_id"]), ("KC", "DEN", 1, 0.35, 0.64, "rothera"))
        me = merged_event_from(t)
        self.assertEqual(me.info.outcomes, ["DEN", "KC"])
        self.assertEqual(me.quotes_by_venue["kalshi"][0].fee_params["fee_type"], "quadratic_with_maker_fees")
        rh = next(q for q in me.quotes_by_venue["robinhood"] if q.outcome == "KC")
        self.assertEqual((rh.fee_params["exchange"], rh.book_id, rh.ask, rh.quote_time), ("rothera", "rothera", 0.36, T0 - 1.0))
        gs = game_state_from(json.loads(st.espn_tick_rows(key)[23]["situation_json"]))
        self.assertEqual((gs.home, gs.home_score, gs.period, gs.review_pending, gs.suspect, gs.last_play_id), ("KC", 21, 3, True, True, "p110"))
        an = st.anomaly_counts()
        self.assertEqual(an["episodes"], {"score-before-lastplay": 1, "review-pending": 1, "suspect": 1, "score-decrease": 1})
        self.assertEqual((an["suspect"], an["review_pending"]), (1, 4))
        self.assertEqual(resolve_event(st, "den|kc"), KEY)
        with self.assertRaises(ValueError):
            resolve_event(st, "nope")
        st.close()
        os.unlink(path)


class ReplayTests(unittest.TestCase):
    def test_gates_change_steal_count_and_clv_on_the_synthetic_game(self):
        path = _db("gates")
        st = Store(path)
        key = load_fixture(st, str(FIXTURES / "ticks/synthetic_40.json"))
        res = replay_both(st, key, offsets=(60, 120))
        on, off = res["on"], res["off"]
        self.assertEqual((on["ticks"], on["views"], on["settled"], on["winner"]), (40, 40, True, "DEN"))
        # Gates off: the stale-feed episode (4 ticks) and the review window (4 ticks) both
        # look like Kansas City steals; gates on keeps only the Q4 Denver steal on Robinhood.
        self.assertGreater(off["steals"], on["steals"])
        self.assertEqual(on["steals"], 3)
        self.assertEqual(off["steals"], 11)
        self.assertEqual(on["gated"], 8)
        self.assertEqual(off["gated"], 0)
        # The in-play item's live gates (FeedFreshness at the tape clock) — the same 3/11/8 the
        # fallback gate gave, plus clock-frozen from its frozen-poll rule.
        self.assertEqual(on["gate_source"], "evaluate_inplay")
        self.assertEqual(set(on["gate_reasons"]), {"feed-stale", "clock-frozen", "score-pending", "review-pending", "suspect"})
        self.assertEqual({(o["outcome"], o["venue"]) for o in on["observations"]}, {("DEN", "robinhood")})
        self.assertIn(("KC", "kalshi"), {(o["outcome"], o["venue"]) for o in off["observations"]})
        p_on, p_off = on["policies"], off["policies"]
        self.assertGreater(p_on["every"]["clv_bid_60_mean"], 0)
        self.assertLess(p_off["every"]["clv_bid_60_mean"], 0)
        self.assertGreater(p_on["every"]["pnl_settle_mean"], 0)
        self.assertLess(p_off["every"]["pnl_settle_mean"], 0)
        self.assertEqual((p_on["first"]["n"], p_off["first"]["n"]), (1, 2))
        self.assertAlmostEqual(p_on["first"]["pnl_settle_sum"], 1.0 - on["observations"][0]["all_in"], places=4)
        text = format_replay(res, offsets=(60, 120))
        self.assertIn("gate source evaluate_inplay", text)
        self.assertIn("winner DEN", text)
        self.assertIn("feed-stale x3", text)
        # Single-policy run through the path-taking entry point.
        one = replay_ticks(path, "DEN|KC", gates=False, offsets=(60,))
        self.assertEqual(one["steals"], 11)
        st.close()
        os.unlink(path)

    def test_clv_rung_skips_ticks_without_the_venue_quote(self):
        # Drop Kalshi from the clean fixture's tick at +60 s after its first STEAL: the +60 s
        # rung must come from the next Kalshi-quoting tick, not read as missing.
        from arb_engine.tickreplay import _bid_at

        path = _db("rung")
        st = Store(path)
        key = load_fixture(st, str(FIXTURES / "ticks/synthetic_clean_10.json"))
        base = replay_ticks(st, key, gates=False, offsets=(60,))
        first = base["observations"][0]
        ticks = st.tick_rows(key)
        t_rung = first["ts"] + 60
        rung_tick = next(tk for tk in ticks if tk["ts"] >= t_rung)
        before = _bid_at(st, ticks, key, first["venue"], first["outcome"], t_rung)
        self.assertEqual(before, first["bid_60"])
        self.assertIsNotNone(before)
        # Blank that tick's quote for the venue (flat columns and l1_json) as a lost poll would.
        sets = {f"{first['venue']}_{side}_{col}": None for side in ("home", "away") for col in ("bid", "ask")}
        l1 = json.loads(rung_tick.get("l1_json") or "{}")
        l1.pop(first["venue"], None)
        st.conn.execute(f"UPDATE inplay_ticks SET {', '.join(f'{k}=?' for k in sets)}, l1_json=? WHERE event_key=? AND ts=?", [*sets.values(), json.dumps(l1), key, rung_tick["ts"]])
        st.conn.commit()
        ticks = st.tick_rows(key)
        nxt = next(tk for tk in ticks if tk["ts"] > rung_tick["ts"])
        expect, _ = st._tick_quote(nxt, first["venue"], first["outcome"])
        self.assertIsNotNone(expect)
        self.assertEqual(_bid_at(st, ticks, key, first["venue"], first["outcome"], t_rung), expect)
        self.assertIsNone(_bid_at(st, ticks, key, first["venue"], first["outcome"], t_rung, max_ticks=1))
        again = replay_ticks(st, key, gates=False, offsets=(60,))
        self.assertEqual(again["observations"][0]["bid_60"], expect)
        st.close()
        os.unlink(path)

    def test_no_gate_triggers_gives_identical_results(self):
        path = _db("clean")
        st = Store(path)
        key = load_fixture(st, str(FIXTURES / "ticks/synthetic_clean_10.json"))
        res = replay_both(st, key, offsets=(60, 120))
        on, off = res["on"], res["off"]
        self.assertEqual(on["gated"], 0)
        self.assertEqual(on["gate_reasons"], {})
        self.assertEqual(on["steals"], off["steals"])
        self.assertEqual(on["observations"], off["observations"])
        self.assertEqual(on["policies"], off["policies"])
        self.assertEqual(on["steals"], 3)
        st.close()
        os.unlink(path)


class GateTests(unittest.TestCase):
    def test_fallback_gate_reasons(self):
        fresh = {"last_state_change_ts": 100.0, "last_score_change_ts": None, "mids": {"kalshi": {"KC": 0.30}}, "mids_at_state_change": {"kalshi": {"KC": 0.35}}, "flags": {}}
        self.assertEqual(fallback_gate(130.0, fresh), ["feed-stale"])
        self.assertEqual(fallback_gate(110.0, fresh), [])                              # not stale yet
        fresh["mids"]["kalshi"]["KC"] = 0.345
        self.assertEqual(fallback_gate(130.0, fresh), [])                              # feed quiet, market quiet
        fresh.update({"last_score_change_ts": 125.0, "last_play_id": "p9", "play_id_at_score": "p9"})
        self.assertEqual(fallback_gate(130.0, fresh), ["score-pending"])
        fresh["last_play_id"] = "p10"
        self.assertEqual(fallback_gate(130.0, fresh), [])
        fresh.update({"last_play_id": None, "play_id_at_score": None})                 # timer fallback without play ids
        self.assertEqual(fallback_gate(130.0, fresh), ["score-pending"])
        self.assertEqual(fallback_gate(150.0, fresh), [])
        fresh["flags"] = {"suspect": True, "review_pending": True}
        self.assertEqual(fallback_gate(150.0, fresh), ["suspect", "review-pending"])

    def test_tracker_follows_state_and_score_changes(self):
        from arb_engine.venues.espn import GameState

        path = _db("tracker")
        st = Store(path)
        load_fixture(st, str(FIXTURES / "ticks/synthetic_40.json"))
        ticks = st.tick_rows(KEY)
        tr = FreshnessTracker()
        f0 = tr.update(T0, GameState(event_id="1", home="KC", away="DEN", home_score=14, away_score=17, status="live", period=3, clock_seconds_remaining_in_period=500), merged_event_from(ticks[0]))
        self.assertEqual(f0["last_state_change_ts"], T0)
        self.assertIsNone(f0["last_score_change_ts"])
        f1 = tr.update(T0 + 10, GameState(event_id="1", home="KC", away="DEN", home_score=14, away_score=17, status="live", period=3, clock_seconds_remaining_in_period=500), merged_event_from(ticks[1]))
        self.assertEqual(f1["last_state_change_ts"], T0)                              # same hash: unchanged
        gs = GameState(event_id="1", home="KC", away="DEN", home_score=14, away_score=24, status="live", period=3, clock_seconds_remaining_in_period=380)
        gs.last_play_id = "p100"
        f2 = tr.update(T0 + 20, gs, merged_event_from(ticks[16]))
        self.assertEqual((f2["last_state_change_ts"], f2["last_score_change_ts"], f2["play_id_at_score"]), (T0 + 20, T0 + 20, "p100"))
        self.assertEqual(f2["mids"]["kalshi"]["KC"], 0.20)
        st.close()
        os.unlink(path)


class CliPluginTests(unittest.TestCase):
    def test_backtest_ticks_and_stats_convergence_commands(self):
        from arb_engine.cli_plugins import record_flags

        path = _db("cli")
        st = Store(path)
        load_fixture(st, str(FIXTURES / "ticks/synthetic_40.json"))
        st.record_steal(T0 + 320, KEY, "DEN", "robinhood", ask=0.80, all_in=0.82, fair=0.89)
        st.update_ladder(now=T0 + 4000)
        st.close()
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="cmd")
        stt = sub.add_parser("stats")
        stt.add_argument("--db")
        calls = []
        stt.set_defaults(func=lambda args: calls.append("original") or 0)
        handlers = record_flags.register(sub, {"stats": stt})
        self.assertEqual(set(handlers), {"backtest-ticks", "stats", "clv", "event-study"})
        args = p.parse_args(["backtest-ticks", "--db", path, "--game", "DEN|KC", "--gates", "both"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = handlers["backtest-ticks"](args, {})
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("gates  policy   steals  gated", text)
        self.assertIn("  on     every ", text)
        self.assertIn("  off    every ", text)
        # stats without --convergence falls through to the original handler; with it, prints the ladder.
        args = p.parse_args(["stats", "--db", path])
        handlers["stats"](args, {})
        self.assertEqual(calls, ["original"])
        args = p.parse_args(["stats", "--db", path, "--convergence"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            handlers["stats"](args, {})
        text = out.getvalue()
        self.assertIn("1 STEAL observations over 1 game(s)", text)
        self.assertIn("ratios withheld", text)
        self.assertIn("score-decrease", text)
        args = p.parse_args(["clv", "--db", path, "--min-games", "1"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            handlers["clv"](args, {})
        self.assertIn("5-8%      no     1      1        1", out.getvalue())
        self.assertIn("0/0", out.getvalue())   # mid at +60 s sits exactly as far from fair as the entry: neither toward nor away
        os.unlink(path)

    def test_event_study_command_binds_specs_to_games_offline(self):
        import shutil

        from arb_engine.cli_plugins import record_flags
        from arb_engine.venues.trades import Trade, TradesClient, _cache_key

        class NoNetwork:
            def get_json(self, *a, **k):
                raise AssertionError("event-study must run from the cache")

        # Two games: the trimmed P03 fixture and a copy under another label; the Kalshi tape
        # of each is its own before/after rows, pre-populated in the cache so no fetch happens.
        with open(FIXTURES / "trades/replay_rows_trim.json", encoding="utf-8") as f:
            g1 = json.load(f)["games"][0]
        g2 = dict(g1, final="KC 30-27 DEN", espn_event_id="401999999", home="KC", away="DEN")
        rows_path = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"arb_es_rows_{os.getpid()}.json")
        with open(rows_path, "w", encoding="utf-8") as f:
            json.dump({"games": [g1, g2]}, f)
        cache = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"arb_es_cache_{os.getpid()}")
        shutil.rmtree(cache, ignore_errors=True)
        client = TradesClient(http=NoNetwork(), cache_dir=cache, offline=True)
        tss = [r["ts"] for r in g1["rows"]]
        t0, t1 = min(tss) - 3600, max(tss) + 1800
        for ticker in ("KXNFLGAME-DETBUF-DET", "KXNFLGAME-DENKC-KC"):
            tape = [Trade("kalshi", ticker, r["ts"] - 1, r["kalshi_before_p"], 1) for r in g1["rows"]] + [Trade("kalshi", ticker, r["ts"] + 60, r["kalshi_after_p"], 1) for r in g1["rows"]]
            client._save(_cache_key("kalshi", ticker, t0, t1), "kalshi", ticker, t0, t1, tape)
        p = argparse.ArgumentParser()
        handlers = record_flags.register(p.add_subparsers(dest="cmd"), {})
        out_json = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"arb_es_out_{os.getpid()}.json")
        base = ["event-study", "--rows", rows_path, "--cache-dir", cache, "--offline", "--json", out_json]
        # Unqualified spec on a two-game file: refused rather than scoring both games on one tape.
        with self.assertRaises(SystemExit) as cm:
            handlers["event-study"](p.parse_args(base + ["--trades", "kalshi=KXNFLGAME-DETBUF-DET"]), {})
        self.assertIn("add @<game>", str(cm.exception))
        with self.assertRaises(SystemExit):
            handlers["event-study"](p.parse_args(base + ["--trades", "kalshi=KXNFLGAME-DETBUF-DET@nomatch"]), {})
        with self.assertRaises(SystemExit):
            handlers["event-study"](p.parse_args(base + ["--trades", "kalshi=KXNFLGAME-DETBUF-DET@40"]), {})   # matches both ESPN ids (401872932, 401999999)
        with self.assertRaises(SystemExit):
            handlers["event-study"](p.parse_args(base + ["--trades", "draftkings=X@DET"]), {})
        # Qualified specs: each game scored on its own tape; a game without a spec is skipped.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = handlers["event-study"](p.parse_args(base + ["--trades", "kalshi=KXNFLGAME-DETBUF-DET@DET|BUF"]), {})
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("DET 24-21 BUF: 4 events", text)
        self.assertIn("KC 30-27 DEN: no --trades spec names it, skipped", text)
        with open(out_json, encoding="utf-8") as f:
            one = json.load(f)
        self.assertEqual({e["game"] for e in one["events"]}, {"DET 24-21 BUF"})
        n_one = one["summary"]["n_events"]
        self.assertGreaterEqual(one["summary"]["venues"]["kalshi"]["n"], 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            handlers["event-study"](p.parse_args(base + ["--trades", "kalshi=KXNFLGAME-DETBUF-DET@DET|BUF", "--trades", "kalshi=KXNFLGAME-DENKC-KC:away@401999999"]), {})
        with open(out_json, encoding="utf-8") as f:
            two = json.load(f)
        self.assertEqual({e["game"] for e in two["events"]}, {"DET 24-21 BUF", "KC 30-27 DEN"})
        self.assertEqual(two["summary"]["n_events"], 2 * n_one)
        self.assertIn("kalshi: n=", out.getvalue())
        # --limit 1 makes an unqualified spec unambiguous again.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            handlers["event-study"](p.parse_args(base + ["--limit", "1", "--trades", "kalshi=KXNFLGAME-DETBUF-DET"]), {})
        self.assertIn("DET 24-21 BUF: 4 events", out.getvalue())
        self.assertEqual(record_flags.parse_trade_spec("polymarket=123:away@KC"), {"venue": "polymarket", "market": "123", "is_home": False, "game": "KC"})
        shutil.rmtree(cache, ignore_errors=True)
        os.unlink(rows_path)
        os.unlink(out_json)


if __name__ == "__main__":
    unittest.main()

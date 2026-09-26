"""strategy/arbbutton.py: the "Robinhood done" button - issue, tap, verify live prices, buy (or practice)."""
import json
import os
import unittest
from unittest import mock

from arb_engine.fees.registry import fee_model_for_quote
from arb_engine.matching.matcher import MergedEvent
from arb_engine.models import EventInfo, OutcomeQuote
from arb_engine.scanner import analyze_event
from arb_engine.strategy.arbbutton import ArbButton, all_in, max_price

KEY = "nfl:DEN|KC:2026-09-27"
T0 = 1_800_000_000.0
KFEE = {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}


def _me(kc=0.55, den=0.36, rh_book="rothera"):
    info = EventInfo(event_key=KEY, sport="nfl", market_type="moneyline", outcomes=["DEN", "KC"], labels={"DEN": "Denver", "KC": "Kansas City"})
    k = [OutcomeQuote("kalshi", "KXNFLGAME-26SEP27DENKC-KC", KEY, "KC", outcome_label="Kansas City", ask=kc, bid=kc - .01, ask_size=500, ts=T0, fee_params=KFEE,
                      meta={"ticker": "KXNFLGAME-26SEP27DENKC-KC", "side": "yes"}, url="https://kalshi.com/markets/kxnflgame/kxnflgame-26sep27denkc")]
    r = [OutcomeQuote("robinhood", "c-den", KEY, "DEN", outcome_label="Denver", ask=den, bid=den - .01, ask_size=500, ts=T0, quote_time=T0,
                      fee_params={"exchange": rh_book}, meta={"contract_id": "c-den", "side": "yes", "exchange": rh_book},
                      book_id=rh_book, url="https://robinhood.com/us/en/prediction-markets/nfl/events/den-kc/")]
    return MergedEvent(KEY, info, {"kalshi": k, "robinhood": r})


def _sized(me, budget=100.0):
    rep = analyze_event(me, {}, now=T0, executable_venues={"kalshi", "robinhood"}, budget=budget)
    return rep.sized_arb


class _Book:
    """Kalshi orderbook answer: YES asks are the NO bids mirrored."""

    def __init__(self, yes_asks):
        self.yes_asks, self.calls = yes_asks, 0

    def orderbook(self, ticker, depth=10):
        self.calls += 1
        return {"orderbook_fp": {"yes_dollars": [], "no_dollars": [[str(round(1 - p, 4)), str(s)] for p, s in self.yes_asks]}}


class _RH:
    def __init__(self, ask=0.36, bid=0.35):
        self.ask, self.bid = ask, bid

    def quotes(self, ids):
        return {i: {"yes_ask_price": self.ask, "yes_bid_price": self.bid, "state": "active"} for i in ids}


class _Alerts:
    ntfy = "https://ntfy.sh/t"

    def __init__(self):
        self.pushes, self.events = [], []

    def push(self, title, msg, **kw):
        self.pushes.append((title, msg, kw))
        return True

    def journal(self, kind, **data):
        self.events.append((kind, data))


def _button(mode="paper", book=None, rh=None, **kw):
    path = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"button_{os.getpid()}_{mode}.jsonl")
    if os.path.exists(path):
        os.remove(path)
    clock = kw.pop("clock", lambda: T0)
    b = ArbButton(mode, alerts=_Alerts(), cmd_url="https://ntfy.sh/t-cmd", fee_for=fee_model_for_quote, data_client=book or _Book([(0.55, 60), (0.56, 100)]),
                  robinhood=rh or _RH(), journal_path=path, http_get=False, clock=clock, **kw)
    return b, path


class IssueTests(unittest.TestCase):
    def test_a_kalshi_plus_robinhood_arb_gets_a_button_with_both_limits(self):
        me = _me()
        sized = _sized(me)
        b, path = _button()
        spec = b.register(KEY, "DEN @ KC", sized, me.quotes_by_venue, now=T0)
        self.assertIsNotNone(spec)
        n = spec["count"]
        k, r = spec["kalshi"], spec["robinhood"]
        self.assertEqual((k["ticker"], k["side"], r["contract_id"], r["side"]), ("KXNFLGAME-26SEP27DENKC-KC", "yes", "c-den", "yes"))
        self.assertEqual((k["alert_ask"], r["alert_ask"]), (0.55, 0.36))
        # Robinhood may cost up to half the room; the Kalshi limit still locks at that price ...
        kf, rf = fee_model_for_quote(me.quotes_by_venue["kalshi"][0]), fee_model_for_quote(me.quotes_by_venue["robinhood"][0])
        self.assertGreaterEqual(r["max"], r["alert_ask"])
        self.assertLessEqual(all_in(kf, k["limit"], n) + all_in(rf, r["max"], n), 1.0 + 1e-12)
        # ... and one cent more on Kalshi would not.
        self.assertGreater(all_in(kf, round(k["limit"] + .01, 2), n) + all_in(rf, r["max"], n), 1.0)
        self.assertEqual(spec["action"], {"action": "http", "label": "Robinhood done - buy Kalshi", "url": "https://ntfy.sh/t-cmd",
                                          "method": "POST", "body": f"arb {spec['token']}", "clear": True})
        self.assertEqual(json.loads(open(path).readline())["event"], "issued")

    def test_no_button_without_a_kalshi_and_a_non_kalshi_robinhood_leg(self):
        b, _ = _button()
        kx = _me(rh_book="kalshi")                          # a Robinhood KX quote is Kalshi's own book
        self.assertIsNone(b.register(KEY, "t", _sized(kx) or {"legs": []}, kx.quotes_by_venue, now=T0))
        self.assertIsNone(b.register(KEY, "t", {"legs": [{"venue": "kalshi"}], "contracts": 10}, {}, now=T0))
        off, _ = _button(mode="off")
        me = _me()
        self.assertIsNone(off.register(KEY, "t", _sized(me), me.quotes_by_venue, now=T0))

    def test_max_price_is_the_last_locking_cent(self):
        from arb_engine.fees.base import ZeroFees

        self.assertEqual(max_price(ZeroFees(), 0.36, 10, 0.50), 0.64)
        self.assertIsNone(max_price(ZeroFees(), 0.60, 10, 0.45))


class TapTests(unittest.TestCase):
    def _spec(self, b):
        me = _me()
        return b.register(KEY, "DEN @ KC", _sized(me), me.quotes_by_venue, now=T0)

    def test_practice_tap_checks_live_prices_and_walks_the_real_book(self):
        b, path = _button(book=_Book([(0.55, 60), (0.56, 1000)]))
        spec = self._spec(b)
        n, lim = spec["count"], spec["kalshi"]["limit"]
        rec = b.fire(spec["token"], now=T0 + 8)
        self.assertEqual(rec["status"], "simulated")
        self.assertEqual((rec["kalshi_live_ask"], rec["rh_live_ask"]), (0.55, 0.36))
        self.assertTrue(rec["still_locks_live"])
        self.assertEqual(rec["filled"], n)                  # 60 at 55c, the rest at 56c (<= the limit)
        self.assertEqual(rec["levels"][0], (0.55, 60))
        self.assertTrue(all(px <= lim for px, _ in rec["levels"]))
        self.assertEqual(rec["unhedged"], 0)
        title, body, kw = b.alerts.pushes[-1]
        self.assertEqual(title, "ARB FILL")
        self.assertTrue(kw["force"])
        self.assertTrue(kw["headline"].startswith("PRACTICE OK - DEN @ KC"))
        self.assertIn(f"Kalshi: would buy {n} Kansas City YES at 55¢-56¢", body)
        self.assertIn("Live now: Kalshi Kansas City 55¢ (alert 55¢), Robinhood Denver 36¢ (alert 36¢)", body)
        self.assertIn("Practice: no order was sent.", body)
        self.assertIsNone(b.fire(spec["token"], now=T0 + 9))   # single use
        self.assertIsNone(b.fire("not-a-token", now=T0 + 9))   # another process's (or nobody's) token
        events = [json.loads(x)["event"] for x in open(path)]
        self.assertEqual(events, ["issued", "tap"])

    def test_a_moved_kalshi_price_is_not_bought_and_the_robinhood_leg_is_flagged(self):
        b, _ = _button(book=_Book([(0.62, 500)]), rh=_RH(ask=0.37, bid=0.34))
        spec = self._spec(b)
        rec = b.fire(spec["token"], now=T0 + 8)
        self.assertEqual((rec["filled"], rec["unhedged"]), (0, spec["count"]))
        self.assertFalse(rec["still_locks_live"])
        title, body, kw = b.alerts.pushes[-1]
        self.assertTrue(kw["headline"].startswith("PRACTICE MISSED"))
        self.assertIn(f"above the {round(spec['kalshi']['limit'] * 100)}¢ limit", body)
        self.assertIn(f"Unhedged: {spec['count']} Denver YES on Robinhood - sell them (bid 34¢) or wait", body)

    def test_thin_book_is_a_partial_and_late_tap_expires(self):
        b, _ = _button(book=_Book([(0.55, 7)]))
        spec = self._spec(b)
        rec = b.fire(spec["token"], now=T0 + 5)
        self.assertEqual((rec["filled"], rec["unhedged"]), (7, spec["count"] - 7))
        self.assertTrue(b.alerts.pushes[-1][2]["headline"].startswith("PRACTICE PARTIAL"))
        spec2 = self._spec(b)
        rec2 = b.fire(spec2["token"], now=T0 + 181)
        self.assertEqual(rec2["status"], "expired")
        self.assertEqual(b._data.calls, 1)                  # an expired tap never reads a price

    def test_the_command_topic_is_polled_and_only_own_tokens_act(self):
        b, _ = _button()
        spec = self._spec(b)
        lines = [json.dumps({"id": "m1", "event": "open"}), json.dumps({"id": "m2", "event": "message", "message": "arb deadbeefdeadbeef"}),
                 json.dumps({"id": "m3", "event": "message", "message": f"arb {spec['token']}"}), json.dumps({"id": "m4", "event": "message", "message": "hello"})]
        b.http_get = lambda url: "\n".join(lines) if "since=100" in url else ""
        since, results = b.poll_once("100")
        self.assertEqual(since, "m4")
        self.assertEqual([r["token"] for r in results], [spec["token"]])

    def test_the_listener_streams_one_subscription_and_survives_errors(self):
        import threading
        b, path = _button()
        spec = self._spec(b)
        calls = []
        done = threading.Event()

        def stream(since):
            calls.append(since)
            if len(calls) == 1:
                raise OSError("HTTP Error 429: Too Many Requests")      # ntfy says slow down: back off, reconnect
            yield {"id": "k1", "event": "keepalive"}
            yield {"id": "m9", "event": "message", "message": f"arb {spec['token']}"}
            done.set()
            b.stop()
        b.stream, b.http_get = stream, None
        b._loop_backoff = 0
        with mock.patch.object(b._stop, "wait", lambda t: b._stop.is_set()):
            b._loop()
        self.assertTrue(done.is_set())
        self.assertEqual(len(calls), 2)                          # one subscription at a time, reopened after the error
        self.assertEqual(calls[1], calls[0])                     # nothing was consumed before the error
        events = [json.loads(x)["event"] for x in open(path)]
        self.assertEqual(events, ["issued", "listen-error", "tap"])

    def test_auto_practice_simulates_without_pushing_or_using_the_token(self):
        b, path = _button(book=_Book([(0.55, 500)]), rh=_RH(ask=0.37, bid=0.36))
        spec = self._spec(b)
        rec = b.auto_practice(spec["token"], now=T0 + 10)
        self.assertEqual((rec["event"], rec["status"]), ("auto-practice", "simulated"))
        self.assertEqual(rec["rh_live_ask"], 0.37)
        self.assertEqual(rec["rh_within_max"], 0.37 <= spec["robinhood"]["max"] + 1e-9)
        self.assertEqual(rec["would_lock"], rec["rh_within_max"] and rec["unhedged"] == 0)
        self.assertEqual(b.alerts.pushes, [])                # never pushed
        self.assertFalse(spec.get("used"))
        self.assertIsNotNone(b.fire(spec["token"], now=T0 + 12))   # your own tap still works
        # In live mode the automatic tap still only simulates.
        sent = []

        class _Ex:
            def plan(self, *a, **k):
                sent.append(a)

            def execute(self, *a, **k):
                sent.append("execute")
        with mock.patch.dict(os.environ, {"ARB_LIVE_TRADING": "1"}):
            lb, _ = _button(mode="live", executor=_Ex(), book=_Book([(0.55, 500)]))
            lspec = self._spec(lb)
            self.assertEqual(lb.auto_practice(lspec["token"], now=T0 + 10)["status"], "simulated")
        self.assertEqual(sent, [])

    def test_live_needs_the_switch_and_demo_sends_an_ioc_at_the_limit(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARB_LIVE_TRADING", None)
            with self.assertRaises(RuntimeError):
                ArbButton("live", cmd_url="https://ntfy.sh/t-cmd")
        sent = []

        class _Ex:
            def plan(self, ticker, action, side, count, price, **kw):
                sent.append((ticker, action, side, count, price, kw["time_in_force"]))
                return "plan"

            def execute(self, plan, confirm=False):
                return {"status": "SUBMITTED", "response": {"order": {"order_id": "o1", "fill_count": "40.00"}}}
        b, _ = _button(mode="demo", executor=_Ex())
        spec = self._spec(b)
        rec = b.fire(spec["token"], now=T0 + 6)
        self.assertEqual(sent, [("KXNFLGAME-26SEP27DENKC-KC", "buy", "yes", spec["count"], spec["kalshi"]["limit"], "immediate_or_cancel")])
        self.assertEqual((rec["status"], rec["order_id"], rec["filled"]), ("SUBMITTED", "o1", 40))


class AlerterIntegrationTests(unittest.TestCase):
    def test_the_push_carries_the_button_once_a_loop_starts_it(self):
        from arb_engine.strategy.arbalert import ArbAlerter

        class _A(_Alerts):
            def alert(self, kind, msg, **data):
                self.pushes.append((kind, msg, data))

            def info(self, *a, **k):
                pass
        me = _me()
        arbs = ArbAlerter(_A(), {}, bankroll=500, executable_venues={"kalshi", "robinhood"})
        rep = arbs.analyse(me, T0)
        arbs.handle(me, rep, "DEN @ KC", T0)
        self.assertNotIn("Robinhood done", json.dumps(arbs.alerts.pushes[-1][2]["ntfy_actions"]))   # tests and one-off runs: no button
        arbs.start_button()
        self.assertEqual(arbs.button.auto_practice_s, 10.0)
        arbs.button.journal_path = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"button_int_{os.getpid()}.jsonl")   # never the real journal
        arbs.button.http_get = False                       # no poller in a test
        arbs.button.auto_practice_s = None                 # nor a timer
        arbs.button.live_prices = lambda spec: {"kalshi_asks": [(0.55, 500.0)], "rh_ask": 0.36, "rh_bid": 0.35, "rh_state": "active", "at": T0 + 1}
        arbs.last.clear()
        arbs.handle(me, rep, "DEN @ KC", T0 + 1)
        kind, full, data = arbs.alerts.pushes[-1]
        self.assertEqual(kind, "BIG ARB")
        self.assertEqual(data["ntfy_actions"][0]["action"], "http")
        self.assertEqual([a[0] for a in data["ntfy_actions"][1:]], ["Robinhood Denver", "Kalshi Kansas City"])
        self.assertEqual(data["ntfy_click"], "https://robinhood.com/us/en/prediction-markets/nfl/events/den-kc/")
        body = data["ntfy_body"].splitlines()
        self.assertTrue(body[0].startswith("1) Robinhood: buy "))
        self.assertIn('2) Tap "Robinhood done": the bot buys', body[1])
        self.assertIn("Practice mode", data["ntfy_body"])
        self.assertIn("+ Kalshi taker fee:", full)          # the journal keeps the full ticket

    def _started(self, live):
        from arb_engine.strategy.arbalert import ArbAlerter

        class _A(_Alerts):
            def alert(self, kind, msg, **data):
                self.pushes.append((kind, msg, data))

            def info(self, *a, **k):
                pass
        me = _me()
        arbs = ArbAlerter(_A(), {}, bankroll=500, executable_venues={"kalshi", "robinhood"})
        arbs.start_button()
        arbs.button.http_get, arbs.button.auto_practice_s = False, None
        arbs.button.journal_path = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"button_gate_{os.getpid()}.jsonl")
        arbs.button.live_prices = lambda spec: dict(live, at=T0)
        return arbs, me, arbs.analyse(me, T0)

    def test_an_arb_gone_from_the_live_book_is_journalled_not_pushed(self):
        # Kalshi's /markets list said 0.55; its order book already asks 0.62 (the list trails the
        # book by 5-10 s in play). Nothing locks there, so no push and no button.
        arbs, me, rep = self._started({"kalshi_asks": [(0.62, 500.0)], "rh_ask": 0.36, "rh_bid": 0.35, "rh_state": "active"})
        out = arbs.handle(me, rep, "DEN @ KC", T0)
        kind, text, data = arbs.alerts.pushes[-1]
        self.assertEqual(kind, "ARB GONE")
        self.assertEqual(out[-1][0], "ARB GONE")
        self.assertNotIn("ntfy_actions", data)
        self.assertIn("GONE on the live book - not pushed: Kalshi Kansas City YES 0.62 on the book (list said 0.55", text)
        self.assertEqual(arbs.button.pending, {})           # withdrawn: no tap, no practice tap
        # The throttle was not used up; the same stale list price is not re-checked for 5 s ...
        self.assertNotIn(me.event_key, arbs.last)
        n = len(arbs.alerts.pushes)
        arbs.handle(me, rep, "DEN @ KC", T0 + 3)
        self.assertEqual(len(arbs.alerts.pushes), n)
        # ... and once the book agrees the arb goes out with its button.
        arbs.button.live_prices = lambda spec: {"kalshi_asks": [(0.55, 500.0)], "rh_ask": 0.36, "rh_bid": 0.35, "rh_state": "active", "at": T0 + 6}
        arbs.handle(me, rep, "DEN @ KC", T0 + 6)
        kind, _, data = arbs.alerts.pushes[-1]
        self.assertEqual(kind, "BIG ARB")
        self.assertEqual(data["ntfy_actions"][0]["action"], "http")

    def test_thin_book_at_the_limit_is_gone_but_an_unreadable_book_still_pushes(self):
        # Two contracts at the list price is not the ticket's size.
        arbs, me, rep = self._started({"kalshi_asks": [(0.55, 2.0), (0.70, 500.0)], "rh_ask": 0.36, "rh_bid": 0.35, "rh_state": "active"})
        arbs.handle(me, rep, "DEN @ KC", T0)
        self.assertEqual(arbs.alerts.pushes[-1][0], "ARB GONE")
        self.assertIn("fills 2/", arbs.alerts.pushes[-1][1])
        # A read that fails is not evidence the arb is gone: push as before.
        arbs, me, rep = self._started({"kalshi_asks": [], "kalshi_error": "HTTPError(429)", "rh_ask": 0.36, "rh_state": "active"})
        arbs.handle(me, rep, "DEN @ KC", T0)
        self.assertEqual(arbs.alerts.pushes[-1][0], "BIG ARB")

    def test_the_book_check_can_be_turned_off(self):
        from arb_engine.strategy.arbalert import ArbAlerter
        arbs, me, rep = self._started({"kalshi_asks": [(0.62, 500.0)], "rh_ask": 0.36, "rh_state": "active"})
        arbs.confirm_book = False
        arbs.handle(me, rep, "DEN @ KC", T0)
        self.assertEqual(arbs.alerts.pushes[-1][0], "BIG ARB")


if __name__ == "__main__":
    unittest.main()

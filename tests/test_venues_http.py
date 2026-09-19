"""HttpClient transport policy: the ESPN 403 host / user-agent fallback, its 60 s failure
cache and the reason carried by the final HttpError. Offline: the urllib transport is
replaced by a scripted one that records (host, User-Agent) per attempt."""

import unittest
from unittest import mock

from arb_engine.venues import http as H
from arb_engine.venues.http import DEFAULT_UA, PLAIN_UA, HttpClient, HttpError


class ScriptedClient(HttpClient):
    """``script`` is consumed one (status, body) per transport call; the last entry repeats.
    The transport is pinned to urllib so an exported ``ARB_HTTP_TRANSPORT=curl`` cannot route
    the calls past the override."""

    def __init__(self, script, **kw):
        kw.setdefault("transport", "urllib")
        super().__init__(retries=0, **kw)
        self.script = list(script)
        self.attempts: list[tuple[str, str]] = []

    def _via_urllib(self, method, url, hdrs, data):
        self.attempts.append((url.split("/")[2], hdrs.get("User-Agent", "")))
        status, body = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return status, body


ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"


class FallbackTests(unittest.TestCase):
    def test_403_then_200_uses_the_web_host(self):
        c = ScriptedClient([(403, "denied"), (200, '{"events": []}')])
        self.assertEqual(c.get(ESPN, params={"dates": "20260920"}), {"events": []})
        self.assertEqual(c.attempts, [("site.api.espn.com", DEFAULT_UA), ("site.web.api.espn.com", DEFAULT_UA)])
        self.assertEqual(c.fallback_log[0], "site.api.espn.com (arb-engine/0.1 (+https://github.com/kv1514/arb-engine)): HTTP 403")
        self.assertTrue(c.fallback_log[-1].endswith("ok"))

    def test_fallback_keeps_path_and_query(self):
        seen = []

        class C(ScriptedClient):
            def _via_urllib(self, method, url, hdrs, data):
                seen.append(url)
                return super()._via_urllib(method, url, hdrs, data)

        c = C([(403, ""), (200, "{}")])
        c.get(ESPN, params={"dates": "20260920", "limit": 300})
        self.assertEqual(seen[1], "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates=20260920&limit=300")

    def test_ua_fallback_order_and_reason(self):
        c = ScriptedClient([(403, "a"), (403, "b"), (403, "c")])
        with self.assertRaises(HttpError) as cm:
            c.get(ESPN)
        self.assertEqual([a[0] for a in c.attempts], ["site.api.espn.com", "site.web.api.espn.com", "site.api.espn.com"])
        self.assertEqual([a[1] for a in c.attempts], [DEFAULT_UA, DEFAULT_UA, PLAIN_UA])
        e = cm.exception
        self.assertEqual(e.status, 403)
        self.assertEqual(e.url, ESPN)
        self.assertIn("site.web.api.espn.com", e.reason)
        self.assertIn(PLAIN_UA, e.reason)
        self.assertIn("[site.api.espn.com", str(e))  # the reason is part of the message an 'espn: none' row prints

    def test_plain_ua_succeeds_after_two_403s(self):
        c = ScriptedClient([(403, ""), (403, ""), (200, '{"ok": 1}')])
        self.assertEqual(c.get(ESPN), {"ok": 1})
        self.assertEqual(c.attempts[-1], ("site.api.espn.com", PLAIN_UA))

    def test_failure_cached_for_60s_then_retried(self):
        c = ScriptedClient([(403, ""), (200, "{}"), (200, "{}"), (200, "{}")])
        with mock.patch.object(H.time, "monotonic", return_value=1000.0):
            c.get(ESPN)
            self.assertEqual(len(c.attempts), 2)
            c.get(ESPN)  # within the TTL: the failing (host, UA) is skipped, straight to the web host
            self.assertEqual(c.attempts[2], ("site.web.api.espn.com", DEFAULT_UA))
            self.assertEqual(len(c.attempts), 3)
            self.assertTrue(any("skipped" in line for line in c.fallback_log))
        with mock.patch.object(H.time, "monotonic", return_value=1000.0 + H.FALLBACK_TTL_S):
            c.get(ESPN)  # TTL elapsed: the original host is tried again
            self.assertEqual(c.attempts[3], ("site.api.espn.com", DEFAULT_UA))

    def test_all_candidates_cached_tries_the_original(self):
        c = ScriptedClient([(403, ""), (403, ""), (403, ""), (200, "{}")])
        with mock.patch.object(H.time, "monotonic", return_value=5.0):
            with self.assertRaises(HttpError):
                c.get(ESPN)
            self.assertEqual(len(c.attempts), 3)
            self.assertEqual(c.get(ESPN), {})
            self.assertEqual(len(c.attempts), 4)  # one attempt, on the original host

    def test_non_espn_hosts_have_no_fallback(self):
        c = ScriptedClient([(403, "no")])
        with self.assertRaises(HttpError) as cm:
            c.get("https://api.elections.kalshi.com/trade-api/v2/markets")
        self.assertEqual(len(c.attempts), 1)
        self.assertIsNone(cm.exception.reason)

    def test_other_statuses_do_not_fall_back(self):
        c = ScriptedClient([(404, "missing")])
        with self.assertRaises(HttpError) as cm:
            c.get(ESPN)
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(len(c.attempts), 1)

    def test_http_error_reason_optional(self):
        e = HttpError(500, "u", "body")
        self.assertIsNone(e.reason)
        self.assertEqual(str(e), "HTTP 500 for u: body")
        self.assertEqual(str(HttpError(403, "u", "b", reason="why")), "HTTP 403 for u: b [why]")


if __name__ == "__main__":
    unittest.main()

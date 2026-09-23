"""Alerts + journal for the maker runner.

A hedge alert is the one thing that must not be missed: the resting Kalshi leg filled and
the other leg (usually on Robinhood, which has no API) has to be bought by hand within the
window in which the hedge price still locks the margin. So every alert goes to stdout with
a bell, to a JSONL journal, to a macOS notification when available, to an optional
webhook (``ARB_ALERT_WEBHOOK``, JSON POST) and to an optional ntfy topic
(``ARB_ALERT_NTFY`` = a topic name on ntfy.sh or a full ``https://host/topic`` URL): the
phone gets a push for ARB / LAG / HEDGE NOW / TAKER ARB / EXCHANGE PAUSED by default
(``ARB_ALERT_NTFY_KINDS`` widens or narrows that; STEAL is opt-in after its first live
Sunday lost), with one push per (title, event, side) per ``ARB_ALERT_MIN_INTERVAL_S``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional


# Structured STEAL fields an in-play caller may pass to ``alert`` (all optional). They are
# journalled under ``steal`` so the JSONL stays greppable per field, and forwarded to
# ``store.record_steal`` when the alerter was given a store — the observation ladder in
# ``arb_engine.store`` then measures whether the price converged toward fair or ran away.
STEAL_FIELDS = ("outcome", "venue", "ask", "bid", "all_in", "fair", "edge", "model_p", "market_p", "espn_p", "gated", "gated_reasons", "suggested_contracts", "state_hash", "period")
# Extra structured fields stored in the observation's ``extra_json`` (LAG signals: which venue
# led and by how much) so the ladder can be split by signal kind.
STEAL_EXTRA_FIELDS = ("signal_kind", "leader", "lead_move", "follower_move")


NTFY_DEFAULT_URL = "https://ntfy.sh"
NTFY_DEFAULT_KINDS = ("ARB", "ARB CLOSE", "LAG", "EXEC ERROR", "HEDGE NOW", "TAKER ARB", "EXCHANGE PAUSED", "HEDGE VENUE NOT EXECUTABLE", "FINAL")
# Kinds throttled per game rather than per (game, side): the maker rates every spread and
# total line of a game in one pass, and one push per game per minute (the best-margin line
# comes first, the watches are ranked) beats eight in three seconds.
NTFY_GAME_LEVEL = ("TAKER ARB", "ARB CLOSE", "EXEC ERROR")
NTFY_PRIORITY = {"HEDGE NOW": "5", "EXCHANGE PAUSED": "5", "EXEC ERROR": "5", "ARB": "4", "LAG": "4", "TAKER ARB": "4", "ARB CLOSE": "3", "STEAL": "3", "LOCK NOW": "3", "FINAL": "2"}
NTFY_TAGS = {"HEDGE NOW": "rotating_light", "EXEC ERROR": "warning", "ARB": "moneybag", "ARB CLOSE": "eyes", "LAG": "hourglass_flowing_sand", "TAKER ARB": "moneybag", "STEAL": "chart_with_upwards_trend", "LOCK NOW": "lock", "EXCHANGE PAUSED": "pause_button", "FINAL": "checkered_flag"}

try:  # settings registry; the module must import without it
    from ..config import declare_setting as _declare_setting  # type: ignore
except Exception:  # pragma: no cover
    _declare_setting = None
if _declare_setting is not None:
    for _k, _env, _default, _cast, _doc in (
        ("alert_ntfy", "ARB_ALERT_NTFY", None, str, "ntfy topic name (on ntfy.sh) or full https://host/topic URL that receives ARB / LAG / HEDGE NOW pushes"),
        ("alert_ntfy_kinds", "ARB_ALERT_NTFY_KINDS", ",".join(NTFY_DEFAULT_KINDS), str, "comma list of alert titles that are pushed to ntfy (add STEAL / LOCK NOW to opt in)"),
        ("alert_min_interval_s", "ARB_ALERT_MIN_INTERVAL_S", 60.0, float, "seconds between two pushes for the same (title, event, side); HEDGE NOW is never throttled"),
    ):
        try:
            _declare_setting(_k, env=_env, default=_default, cast=_cast, doc=_doc)
        except Exception:  # pragma: no cover
            pass


def ntfy_url(topic_or_url: Optional[str]) -> Optional[str]:
    """'arb-abc123' -> https://ntfy.sh/arb-abc123; a full URL passes through; None stays None."""
    if not topic_or_url:
        return None
    t = topic_or_url.strip()
    return t if t.startswith(("http://", "https://")) else f"{NTFY_DEFAULT_URL}/{t}"


class Alerter:
    def __init__(self, journal_path: str | os.PathLike = "out/maker_journal.jsonl", webhook: Optional[str] = None, quiet: bool = False, desktop: bool = True, store: Any = None, ntfy: Optional[str] = None, ntfy_kinds: Optional[Iterable[str]] = None, min_interval_s: Optional[float] = None, transport: Optional[Callable[[str, bytes, dict[str, str]], None]] = None):
        self.journal_path = Path(journal_path)
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self.webhook = webhook if webhook is not None else os.environ.get("ARB_ALERT_WEBHOOK")
        self.ntfy = ntfy_url(ntfy if ntfy is not None else os.environ.get("ARB_ALERT_NTFY"))
        kinds = ntfy_kinds if ntfy_kinds is not None else (os.environ.get("ARB_ALERT_NTFY_KINDS") or ",".join(NTFY_DEFAULT_KINDS)).split(",")
        self.ntfy_kinds = {k.strip().upper() for k in kinds if k.strip()}
        try:
            self.min_interval_s = float(min_interval_s if min_interval_s is not None else (os.environ.get("ARB_ALERT_MIN_INTERVAL_S") or 60.0))
        except (TypeError, ValueError):
            self.min_interval_s = 60.0
        self._transport = transport  # tests inject one; None = urllib with a curl fallback
        self._last_push: dict[tuple[str, str, str], float] = {}
        self.quiet = quiet
        self.desktop = desktop and sys.platform == "darwin" and shutil.which("osascript") is not None
        self.store = store  # optional arb_engine.store.Store (record_steal on structured STEALs)
        self.events: list[dict[str, Any]] = []
        self.steals: list[dict[str, Any]] = []

    def journal(self, kind: str, **data: Any) -> dict[str, Any]:
        rec = {"ts": time.time(), "kind": kind, **data}
        self.events.append(rec)
        try:
            with open(self.journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass
        return rec

    def info(self, msg: str, **data: Any) -> None:
        self.journal("info", msg=msg, **data)
        if not self.quiet:
            print(time.strftime("%H:%M:%S"), msg)

    def alert(self, title: str, msg: str, **data: Any) -> None:
        """Loud: bell + notification + webhook + journal.

        Structured STEAL fields (``STEAL_FIELDS``: outcome, venue, ask, all_in, fair, edge,
        model_p, market_p, espn_p, gated, gated_reasons, suggested_contracts, state_hash,
        period) are journalled under ``steal`` and recorded as a STEAL observation when a
        store is attached; every other kwarg is journalled as before.
        """
        # A structured STEAL is identified by its (outcome, venue) pair; other alerts (maker
        # fills carry ``side``/``price``) keep every kwarg as plain journal data.
        headline = data.pop("ntfy_title", None)   # what the phone shows in bold; ``title`` stays the kind
        steal = {k: data.pop(k) for k in STEAL_FIELDS + STEAL_EXTRA_FIELDS if k in data} if "outcome" in data and "venue" in data else {}
        if steal:
            ts = data.pop("ts", None)
            steal.setdefault("ts", ts if ts is not None else time.time())
            steal.setdefault("event_key", data.get("event") or data.get("event_key"))
            steal.setdefault("gated", title.upper().startswith("GATED"))
            if steal.get("edge") is None and steal.get("fair") is not None and steal.get("all_in") is not None:
                steal["edge"] = steal["fair"] - steal["all_in"]
            self.steals.append(steal)
            data["steal"] = steal
            if self.store is not None and hasattr(self.store, "record_steal"):
                try:
                    self.store.record_steal(**{k: v for k, v in steal.items() if k in ("ts", "event_key") or k in STEAL_FIELDS or k in STEAL_EXTRA_FIELDS})
                except Exception as e:
                    self.journal("record_steal_error", error=repr(e))
        self.journal("alert", title=title, msg=msg, **data)
        if not self.quiet:
            print("\a" + time.strftime("%H:%M:%S"), f"*** {title} ***", msg, flush=True)
        if self.desktop:
            try:
                safe = lambda s: s.replace('"', "'")  # noqa: E731
                subprocess.run(["osascript", "-e", f'display notification "{safe(msg)[:200]}" with title "{safe(title)[:60]}" sound name "Glass"'], capture_output=True, timeout=5)
            except Exception:
                pass
        if self.webhook:
            try:
                self._post(self.webhook, json.dumps({"title": title, "text": msg, **data}, default=str).encode(), {"Content-Type": "application/json"})
            except Exception as e:
                self.journal("webhook_error", error=str(e))
        if self.ntfy:
            self.push(title, msg, event=data.get("event") or data.get("event_key"), side=(steal.get("outcome") if steal else data.get("outcome")) or data.get("watch") or "", headline=headline)

    # ---- ntfy --------------------------------------------------------------------------------
    def push(self, title: str, msg: str, event: Any = None, side: Any = None, force: bool = False, headline: Optional[str] = None) -> bool:
        """One ntfy push for ``title`` unless its kind is not subscribed or the same
        (title, event, side) was pushed less than ``min_interval_s`` ago (HEDGE NOW always goes;
        ``NTFY_GAME_LEVEL`` kinds throttle per (title, event))."""
        if not self.ntfy:
            return False
        kind = title.upper()
        if not force and kind not in self.ntfy_kinds:
            return False
        key = (kind, str(event or ""), "" if kind in NTFY_GAME_LEVEL and event else str(side or ""))
        now = time.time()
        if not force and kind != "HEDGE NOW" and now - self._last_push.get(key, -1e18) < self.min_interval_s:
            self.journal("ntfy_throttled", title=title, event=event, side=side)
            return False
        self._last_push[key] = now
        headers = {"Title": (headline or title)[:120], "Priority": NTFY_PRIORITY.get(kind, "3"), "Tags": NTFY_TAGS.get(kind, "bell"), "Content-Type": "text/plain; charset=utf-8"}
        try:
            self._post(self.ntfy, msg[:3500].encode("utf-8"), headers)
            self.journal("ntfy", title=title, event=event, side=side)
            return True
        except Exception as e:
            self.journal("ntfy_error", error=str(e))
            return False

    def _post(self, url: str, body: bytes, headers: dict[str, str]) -> None:
        if self._transport is not None:
            self._transport(url, body, headers)
            return
        try:
            import urllib.request

            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            curl = shutil.which("curl")
            if not curl:
                raise
            cmd = [curl, "-sS", "-m", "6", "-X", "POST", "--data-binary", "@-"]
            for k, v in headers.items():
                cmd += ["-H", f"{k}: {v}"]
            subprocess.run(cmd + [url], input=body, capture_output=True, timeout=8, check=True)

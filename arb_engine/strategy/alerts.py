"""Alerts + journal for the maker runner.

A hedge alert is the one thing that must not be missed: the resting Kalshi leg filled and
the other leg (usually on Robinhood, which has no API) has to be bought by hand within the
window in which the hedge price still locks the margin. So every alert goes to stdout with
a bell, to a JSONL journal, to a macOS notification when available, and to an optional
webhook (``ARB_ALERT_WEBHOOK``, JSON POST).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional


class Alerter:
    def __init__(self, journal_path: str | os.PathLike = "out/maker_journal.jsonl", webhook: Optional[str] = None, quiet: bool = False, desktop: bool = True):
        self.journal_path = Path(journal_path)
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self.webhook = webhook if webhook is not None else os.environ.get("ARB_ALERT_WEBHOOK")
        self.quiet = quiet
        self.desktop = desktop and sys.platform == "darwin" and shutil.which("osascript") is not None
        self.events: list[dict[str, Any]] = []

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
        """Loud: bell + notification + webhook + journal."""
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
                import urllib.request

                req = urllib.request.Request(self.webhook, data=json.dumps({"title": title, "text": msg, **data}, default=str).encode(), headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=5).read()
            except Exception as e:
                self.journal("webhook_error", error=str(e))

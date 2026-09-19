"""SQLite recorder for scans and in-play ticks (``--record``), so the STEAL signal, arb
frequency and venue lag can be measured after the fact instead of guessed."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
  ts REAL, sport TEXT, event_key TEXT, market_type TEXT, title TEXT, venues TEXT,
  gross_sum REAL, margin REAL, fillable INTEGER, live INTEGER, sized_contracts REAL, sized_profit REAL, flags TEXT
);
CREATE INDEX IF NOT EXISTS scans_event ON scans(event_key, ts);
CREATE TABLE IF NOT EXISTS quotes (
  ts REAL, event_key TEXT, outcome TEXT, venue TEXT, exchange TEXT, ask REAL, bid REAL, ask_size REAL, all_in REAL, max_buy REAL, max_buy_maker REAL
);
CREATE INDEX IF NOT EXISTS quotes_event ON quotes(event_key, ts);
CREATE TABLE IF NOT EXISTS inplay_ticks (
  ts REAL, event_key TEXT, live INTEGER, game_line TEXT, home_score INTEGER, away_score INTEGER, period INTEGER,
  model_p REAL, market_p REAL, espn_p REAL, blend_p REAL, disagreement REAL, actions TEXT, view TEXT
);
CREATE INDEX IF NOT EXISTS ticks_event ON inplay_ticks(event_key, ts);
"""


class Store:
    def __init__(self, path: str = "out/history.db"):
        import os

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)

    def record_scan(self, result: Any) -> int:
        """Persist a ScanResult (every event + every venue quote). Returns rows written."""
        ts = float(getattr(result, "fetched_at", None) or time.time())
        n = 0
        with self.conn:
            for ev in result.events:
                sized = ev.sized_arb or {}
                self.conn.execute("INSERT INTO scans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (ts, result.sport, ev.event_key, ev.market_type, ev.title, ",".join(ev.venues), ev.gross_sum, ev.margin, int(ev.fillable), int(ev.live), sized.get("contracts"), sized.get("profit"), ",".join(ev.flags)))
                for o in ev.outcomes:
                    for v in o.venues:
                        self.conn.execute("INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?,?)", (ts, ev.event_key, o.outcome, v.venue, v.exchange, v.ask, v.bid, v.ask_size, v.all_in, v.max_buy_price, v.max_buy_maker))
                        n += 1
                n += 1
        return n

    def record_tick(self, view: Any) -> None:
        gs = view.game_state or {}
        home_o = None
        if gs:
            home_o = gs.get("home")
        side = next((s for s in view.sides if home_o and s.outcome == home_o), view.sides[0] if view.sides else None)
        with self.conn:
            self.conn.execute("INSERT INTO inplay_ticks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (time.time(), view.event_key, int(view.live), view.game_line, gs.get("home_score"), gs.get("away_score"), gs.get("period"), side.model_p if side else None, side.market_p if side else None, side.espn_p if side else None, side.fair if side else None, view.disagreement, "\n".join(view.actions), json.dumps(asdict(view), default=str)))

    def arb_stats(self, sport: Optional[str] = None) -> dict[str, Any]:
        q = "SELECT market_type, COUNT(*), SUM(fillable), AVG(margin), MAX(margin) FROM scans" + (" WHERE sport=?" if sport else "") + " GROUP BY market_type"
        rows = self.conn.execute(q, (sport,) if sport else ()).fetchall()
        return {r[0]: {"events": r[1], "fillable_arbs": r[2] or 0, "avg_margin": r[3], "max_margin": r[4]} for r in rows}

    def close(self) -> None:
        self.conn.close()

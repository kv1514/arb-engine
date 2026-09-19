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
  gross_sum REAL, margin REAL, fillable INTEGER, live INTEGER, sized_contracts REAL, sized_profit REAL, flags TEXT,
  start_time TEXT
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
        # Older files predate start_time; add it in place.
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(scans)")}
        if "start_time" not in cols:
            self.conn.execute("ALTER TABLE scans ADD COLUMN start_time TEXT")

    def record_scan(self, result: Any) -> int:
        """Persist a ScanResult (every event + every venue quote). Returns rows written."""
        ts = float(getattr(result, "fetched_at", None) or time.time())
        n = 0
        with self.conn:
            for ev in result.events:
                sized = ev.sized_arb or {}
                self.conn.execute("INSERT INTO scans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (ts, result.sport, ev.event_key, ev.market_type, ev.title, ",".join(ev.venues), ev.gross_sum, ev.margin, int(ev.fillable), int(ev.live), sized.get("contracts"), sized.get("profit"), ",".join(ev.flags), ev.start_time))
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

    # Hours-to-kickoff buckets for the frequency question ("when do arbs exist?").
    BUCKETS = ((0, 1, "<1h"), (1, 6, "1-6h"), (6, 24, "6-24h"), (24, 72, "1-3d"), (72, 24 * 365, "3d+"))

    def arb_frequency(self, sport: Optional[str] = None, min_margin: float = 0.0) -> dict[str, Any]:
        """How often a fillable arb above ``min_margin`` was present, by market type and hours to
        kickoff: rows = scan snapshots of one event; an *episode* is a run of consecutive scans
        of the same event where the arb persisted (so duration = episodes' length in scans)."""
        from .matching.normalize import parse_iso

        q = "SELECT ts, event_key, market_type, margin, fillable, live, start_time, sized_profit FROM scans" + (" WHERE sport=?" if sport else "") + " ORDER BY event_key, ts"
        rows = self.conn.execute(q, (sport,) if sport else ()).fetchall()
        by_bucket: dict[str, dict[str, Any]] = {}
        episodes: list[dict[str, Any]] = []
        cur: dict[str, Any] = {}
        prev_key = None
        for ts, key, mtype, margin, fillable, live, start, profit in rows:
            st = parse_iso(start) if start else None
            hours = (st.timestamp() - ts) / 3600.0 if st else None
            label = next((b[2] for b in self.BUCKETS if hours is not None and b[0] <= hours < b[1]), "live/unknown" if live or hours is None else "3d+")
            b = by_bucket.setdefault(f"{mtype}:{label}", {"market_type": mtype, "bucket": label, "snapshots": 0, "arb_snapshots": 0, "max_margin": None, "profit_sum": 0.0})
            b["snapshots"] += 1
            is_arb = bool(fillable) and (margin or 0) > min_margin and not live
            if is_arb:
                b["arb_snapshots"] += 1
                b["max_margin"] = margin if b["max_margin"] is None else max(b["max_margin"], margin)
                b["profit_sum"] += profit or 0.0
            # Episodes: consecutive arb snapshots of the same event.
            if key != prev_key and cur:
                episodes.append(cur)
                cur = {}
            if is_arb:
                if cur and cur.get("event_key") == key:
                    cur["end"], cur["scans"], cur["max_margin"] = ts, cur["scans"] + 1, max(cur["max_margin"], margin)
                else:
                    if cur:
                        episodes.append(cur)
                    cur = {"event_key": key, "market_type": mtype, "start": ts, "end": ts, "scans": 1, "max_margin": margin, "bucket": label}
            elif cur and cur.get("event_key") == key:
                episodes.append(cur)
                cur = {}
            prev_key = key
        if cur:
            episodes.append(cur)
        for b in by_bucket.values():
            b["arb_share"] = round(b["arb_snapshots"] / b["snapshots"], 4) if b["snapshots"] else None
        return {"snapshots": len(rows), "buckets": sorted(by_bucket.values(), key=lambda b: (b["market_type"], [x[2] for x in self.BUCKETS].index(b["bucket"]) if b["bucket"] in [x[2] for x in self.BUCKETS] else 99)), "episodes": episodes, "episode_scans_mean": round(sum(e["scans"] for e in episodes) / len(episodes), 2) if episodes else None, "episode_seconds_mean": round(sum(e["end"] - e["start"] for e in episodes) / len(episodes), 1) if episodes else None}

    def close(self) -> None:
        self.conn.close()

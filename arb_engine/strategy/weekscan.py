"""Look for arbitrage all week: every game, every market, between and during games.

The live slate watches in-play moneylines of the games being played. Everything else - every
upcoming game's moneyline, spread and total, days before kickoff, and the spread / total
lines while a game is on - is this module's, so a weekday injury report or line move that one
venue prices before the other is caught too.

Two speeds:

* **Full sweep** every ``full_every_s`` (120 s): ``scanner.scan`` over each sport's whole
  catalogue on the executable venues (NFL ~1,600 markets in ~6 s, college ~8,700 in ~35 s).
  Every arb is alerted; every market within ``watch_margin`` (3c) of locking joins the watch.
* **Fast watch** every ``fast_every_s`` (5 s): only the watched markets, refreshed with the
  fast lane's cheap calls (Kalshi ``/markets?tickers=``, Robinhood's quotes API), re-analysed
  and alerted - short-lived moves are seen within seconds, not at the next sweep. The watch is
  capped at the ``max_watch`` markets closest to locking and forgets one that has stayed
  outside the band for ``drop_after_s``.

Alerts go through ``strategy/arbalert.ArbAlerter`` - the same tiers, stake, legging and tickets
as game day. A pre-game arb re-pushes at most every ``prematch_every_s`` (10 min) unless it
grew by a cent (a lock that sits for an hour is one message, not 120). In-play moneylines are
skipped (the live slate owns them). Arbs and watched markets are recorded to the history
database (``scans`` / ``quotes``) for the weekday backtests; the rest of a sweep is not, which
keeps the file from growing by millions of rows a day.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Optional

from ..matching.matcher import MergedEvent
from .arbalert import ArbAlerter
from .fastlane import FastLane


class WeekScanner:
    def __init__(self, sports: Iterable[str], adapters: list[Any], alerts: Any, settings: Optional[dict[str, Any]] = None,
                 store: Any = None, bankroll: Optional[float] = None, executable_venues: Optional[set[str]] = None,
                 full_every_s: float = 120.0, fast_every_s: float = 5.0, watch_margin: float = 0.03, max_watch: int = 60,
                 drop_after_s: float = 600.0, prematch_every_s: float = 600.0, scan_fn: Optional[Callable] = None,
                 fastlane: Optional[FastLane] = None, clock: Callable[[], float] = time.time, sweep_max_age_s: float = 60.0) -> None:
        self.sports, self.adapters, self.alerts, self.settings = list(sports), list(adapters), alerts, settings or {}
        self.store, self.exec = store, executable_venues or {"kalshi", "robinhood"}
        self.full_every_s, self.fast_every_s = full_every_s, fast_every_s
        self.watch_margin, self.max_watch, self.drop_after_s = watch_margin, max_watch, drop_after_s
        self.clock = clock
        self.arbs = ArbAlerter(alerts, self.settings, bankroll, executable_venues=self.exec, throttle_s=prematch_every_s, push_near=False)
        if scan_fn is None:
            from ..scanner import scan as scan_fn
        self.scan_fn = scan_fn
        if fastlane is None:
            by = {getattr(a, "venue", ""): a for a in self.adapters}
            fastlane = FastLane(kalshi_client=getattr(by.get("kalshi"), "client", None), robinhood=by.get("robinhood"), clock=clock)
        self.fastlane = fastlane
        # event_key -> {"sport", "me", "title", "margin", "outside_since"}
        self.watch: dict[str, dict[str, Any]] = {}
        self.last_full = -1e18
        # A sweep's prices older than this are not priced (a venue refresh that failed leaves
        # cached prices carrying their real age - venues/robinhood.py - and they drop out here).
        self.sweep_max_age_s = sweep_max_age_s
        self.stats = {"sweeps": 0, "fast_steps": 0, "arbs": 0, "near": 0, "errors": []}
        # run() sweeps on a background thread so the fast watch never waits for a slow sweep:
        # _lock guards the watch and the alerter, _lane_lock the fast lane (seed vs step).
        import threading

        self._lock = threading.RLock()
        self._lane_lock = threading.Lock()

    # ---- helpers -------------------------------------------------------------------------
    @staticmethod
    def _skip(rep: Any) -> bool:
        """In-play moneylines belong to the live slate."""
        return bool(rep.live) and rep.market_type == "moneyline"

    def _where(self, rep: Any) -> str:
        return ("in-play " + (rep.market_type or "line")) if rep.live else "pre-game"

    def _alert(self, me: MergedEvent, rep: Any, now: float) -> list[tuple[str, str]]:
        self.arbs.observe(me, now)
        out = self.arbs.handle(me, rep, rep.title or me.event_key, now, where=self._where(rep))
        for kind, _ in out:
            self.stats["near" if kind == "ARB CLOSE" else "arbs"] += 1
        return out

    def _consider(self, sport: str, me: MergedEvent, rep: Any, now: float) -> None:
        """Keep a market on the fast watch while it is within the band (or an arb)."""
        m = rep.margin
        inside = m is not None and m >= -self.watch_margin
        w = self.watch.get(me.event_key)
        if inside:
            self.watch[me.event_key] = {"sport": sport, "me": me, "title": rep.title or me.event_key, "margin": m, "outside_since": None}
        elif w is not None:
            w["me"], w["margin"] = me, m
            w["outside_since"] = w["outside_since"] or now
            if now - w["outside_since"] >= self.drop_after_s:
                del self.watch[me.event_key]

    def _trim(self) -> None:
        if len(self.watch) > self.max_watch:
            keep = sorted(self.watch, key=lambda k: -(self.watch[k]["margin"] if self.watch[k]["margin"] is not None else -9))[: self.max_watch]
            self.watch = {k: self.watch[k] for k in keep}

    # ---- the two speeds ------------------------------------------------------------------
    def _fetch(self, now: float) -> list[tuple[str, Any]]:
        """The slow part of a sweep - every catalogue over the network - holding no lock."""
        out = []
        for sport in self.sports:
            try:
                out.append((sport, self.scan_fn(sport, self.adapters, settings=self.settings, executable_venues=self.exec, keep_merged=True,
                                                now=now, max_quote_age=self.sweep_max_age_s)))
            except Exception as e:   # one sport failing never stops the other
                self.stats["errors"].append(f"{sport}: {e!r}")
        return out

    def _process(self, results: list[tuple[str, Any]], now: float) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        with self._lock:
            for sport, res in results:
                keep = []
                for rep in res.events:
                    me = (res.merged or {}).get(rep.event_key)
                    if me is None or self._skip(rep):
                        continue
                    if (rep.arb or {}).get("is_arb"):
                        # A sweep's prices come from a catalogue pass that can take half a minute
                        # and read two venues seconds apart: an arb found here is only a lead. It
                        # goes on the fast watch, whose next step re-reads both venues' live
                        # quotes (sized to the stake) and pushes it only if it still holds.
                        self.stats["pending"] = self.stats.get("pending", 0) + 1
                        got = []
                    else:
                        got = self._alert(me, rep, now)
                    found += got
                    self._consider(sport, me, rep, now)
                    if got or rep.event_key in self.watch:
                        keep.append(rep)
                if self.store is not None and keep:
                    try:
                        self.store.record_scan(SimpleNamespace(sport=sport, fetched_at=now, events=keep))
                    except Exception as e:
                        self.stats["errors"].append(f"record: {e!r}")
            self._trim()
            seed = {k: w["me"].quotes_by_venue for k, w in self.watch.items()}
        with self._lane_lock:
            self.fastlane.seed(seed)
        return found

    def full_sweep(self, now: Optional[float] = None) -> dict[str, Any]:
        now = self.clock() if now is None else now
        t_start = time.monotonic()
        found = self._process(self._fetch(now), now)
        self.last_full = now
        self.stats["sweeps"] += 1
        self.stats["last_sweep_s"] = time.monotonic() - t_start
        self.stats["fast_since_sweep"] = 0
        return {"found": found, "watching": len(self.watch)}

    def fast_step(self, now: Optional[float] = None) -> list[tuple[str, str]]:
        with self._lock:
            keys = list(self.watch)
        if not keys:
            return []
        with self._lane_lock:
            refreshed, errors = self.fastlane.step(keys, None if now is None else now)
        now = self.clock() if now is None else now
        self.stats["errors"].extend(errors)
        with self._lock:
            return self._fast_process(refreshed, now)

    def _fast_process(self, refreshed: dict[str, Any], now: float) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for key, by in refreshed.items():
            w = self.watch.get(key)
            if w is None:
                continue
            me = MergedEvent(key, w["me"].info, by)
            try:
                rep = self.arbs.analyse(me, now, max_quote_age=max(15.0, 3 * self.fast_every_s))
            except Exception as e:
                self.stats["errors"].append(f"analyse {key}: {e!r}")
                continue
            if self._skip(rep):
                del self.watch[key]
                continue
            found += self._alert(me, rep, now)
            self._consider(w["sport"], me, rep, now)
        self.stats["fast_steps"] += 1
        self.stats["fast_since_sweep"] = self.stats.get("fast_since_sweep", 0) + 1
        return found

    def step(self, now: Optional[float] = None) -> list[tuple[str, str]]:
        t = self.clock() if now is None else now
        if t - self.last_full >= self.full_every_s:
            return self.full_sweep(now)["found"]
        return self.fast_step(now)

    def run(self, duration: Optional[float] = None, printer: Callable[[str], None] = print) -> None:
        """Full sweeps on a background thread every ``full_every_s``; fast steps here every
        ``fast_every_s`` whatever the sweep is doing."""
        import threading

        end = None if duration is None else self.clock() + duration
        stop = threading.Event()
        self.arbs.start_button()
        printer(f"week scan: {', '.join(self.sports)}; full sweep every {self.full_every_s:g}s (background), watch within "
                f"{self.watch_margin * 100:.0f}c every {self.fast_every_s:g}s (max {self.max_watch}); in-play moneylines are the live slate's")

        def show(found: list[tuple[str, str]]) -> None:
            for kind, text in found:
                if kind in ("BIG ARB", "ARB"):
                    printer(f"{time.strftime('%H:%M:%S')} *** {kind} *** " + text.replace("\n", "\n    "))

        def sweeper() -> None:
            while not stop.is_set():
                t0 = self.clock()
                fast_before = self.stats.get("fast_since_sweep", 0)
                try:
                    show(self.full_sweep()["found"])
                except Exception as e:   # the scanner must outlive any single bad sweep
                    printer(f"{time.strftime('%H:%M:%S')} week scan sweep error: {e!r}")
                best = max((w["margin"] for w in list(self.watch.values()) if w["margin"] is not None), default=None)
                printer(f"{time.strftime('%H:%M:%S')} sweep {self.stats['sweeps']} ({self.stats.get('last_sweep_s', 0):.0f}s, "
                        f"{fast_before} fast checks since the last one): watching {len(self.watch)} markets within "
                        f"{self.watch_margin * 100:.0f}c" + (f", closest {best * 100:+.2f}c" if best is not None else "")
                        + f"; {self.stats['arbs']} arb alerts so far")
                stop.wait(max(0.0, self.full_every_s - (self.clock() - t0)))

        th = threading.Thread(target=sweeper, name="weekscan-sweep", daemon=True)
        th.start()
        try:
            while end is None or self.clock() < end:
                t0 = self.clock()
                try:
                    show(self.fast_step())
                except Exception as e:
                    printer(f"{time.strftime('%H:%M:%S')} week scan fast-watch error: {e!r}")
                time.sleep(max(0.0, self.fast_every_s - (self.clock() - t0)))
        finally:
            stop.set()

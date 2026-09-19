"""Slate-wide in-play scanner: every live (or about-to-start) game at once.

`InplayWatcher` follows one event with your lots. This runs the same evaluation across the
whole ESPN scoreboard: one venue fetch per tick, one ESPN scoreboard call, a summary refresh
per live game every ``refresh_summary_every`` seconds, then ``evaluate_inplay`` per game with
no lots — so the output is the fair value per side (model / market / ESPN), the cheapest venue
all-in, and STEAL flags wherever a side is below fair by ``steal_edge`` with the model agreeing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ..matching.matcher import MergedEvent, merge_snapshots
from ..venues.espn import ESPNClient, ESPNFeed, GameState
from .alerts import Alerter
from .inplay import InplayView, evaluate_inplay


@dataclass
class SlateTick:
    at: float
    views: list[InplayView]
    games: int                 # scoreboard games considered (live + pre within window)
    missing: list[str] = field(default_factory=list)   # games with no merged venue event
    errors: list[str] = field(default_factory=list)


class LiveSlate:
    def __init__(self, adapters: Iterable[Any], feed: Optional[ESPNFeed] = None, settings: Optional[dict[str, Any]] = None, sport: str = "nfl", steal_edge: float = 0.03, target_margin: float = 0.0, pre_hours: float = 1.0, alerter: Optional[Alerter] = None, store: Any = None, model: Any = None, refresh_summary_every: float = 30.0, contracts: float = 100):
        self.adapters = list(adapters)
        self.feed = feed or ESPNFeed(ESPNClient(sport=sport))
        self.settings = settings or {}
        self.sport = sport
        self.steal_edge = steal_edge
        self.target_margin = target_margin
        self.pre_hours = pre_hours
        self.alerts = alerter or Alerter()
        self.store = store
        self.model = model
        self.refresh_summary_every = refresh_summary_every
        self.contracts = contracts
        self._enriched: dict[str, tuple[float, GameState]] = {}   # event_id -> (when, enriched state)
        self._seen_steal: set[str] = set()

    # -- pieces ----------------------------------------------------------------------------
    def wanted_games(self, games: list[GameState], now: Optional[float] = None) -> list[GameState]:
        now = now or time.time()
        out = []
        for g in games:
            if g.status == "live":
                out.append(g)
            elif g.status == "pre" and g.start_time is not None:
                st = g.start_time if g.start_time.tzinfo else g.start_time.replace(tzinfo=timezone.utc)
                hours = (st.timestamp() - now) / 3600.0
                if -0.25 <= hours <= self.pre_hours:
                    out.append(g)
        return out

    def state_for(self, g: GameState, now: Optional[float] = None) -> GameState:
        """Live games get the ESPN summary (win probability, odds, 2H kickoff) at most every
        ``refresh_summary_every`` seconds; between refreshes the enriched fields are carried over."""
        now = now or time.time()
        if g.status != "live":
            return g
        when, cached = self._enriched.get(g.event_id, (0.0, None))
        if now - when >= self.refresh_summary_every:
            try:
                g = self.feed.enrich(g)
                self._enriched[g.event_id] = (now, g)
                return g
            except Exception:
                pass
        if cached is not None:
            for k in ("espn_home_wp", "espn_wp_series", "vegas_spread_home", "vegas_total", "odds_provider", "receive_2h_ko_home"):
                if getattr(g, k, None) in (None, []) and getattr(cached, k, None) not in (None, []):
                    setattr(g, k, getattr(cached, k))
        return g

    def merged_events(self, errors: list[str]) -> dict[str, MergedEvent]:
        snaps = []
        for ad in self.adapters:
            try:
                snaps.append(ad.fetch(self.sport))
            except Exception as e:
                errors.append(f"{getattr(ad, 'venue', ad.__class__.__name__)}: {e!r}")
        return merge_snapshots(snaps) if snaps else {}

    # -- one pass ----------------------------------------------------------------------------
    def tick(self, now: Optional[float] = None) -> SlateTick:
        now = now or time.time()
        errors: list[str] = []
        games = self.wanted_games(self.feed.games(), now)
        out = SlateTick(at=now, views=[], games=len(games), errors=errors)
        if not games:
            return out
        merged = self.merged_events(errors)
        for g in games:
            me = merged.get(g.event_key or "")
            if me is None or me.info.market_type != "moneyline":
                out.missing.append(f"{g.away} @ {g.home} ({g.event_key})")
                continue
            gs = self.state_for(g, now)
            try:
                view = evaluate_inplay(me, [], self.settings, self.steal_edge, self.target_margin, game_state=gs, model=self.model)
            except Exception as e:
                errors.append(f"{g.away} @ {g.home}: {e!r}")
                continue
            out.views.append(view)
            for act in view.actions:
                if act.startswith("STEAL"):
                    key = f"{view.event_key}|{act.split(' on ')[0]}"
                    if key not in self._seen_steal:
                        self._seen_steal.add(key)
                        self.alerts.alert("STEAL", f"{view.title}: {act}", event=view.event_key, game_state=view.game_state)
            if self.store is not None:
                try:
                    self.store.record_tick(view)
                except Exception as e:
                    errors.append(f"record: {e!r}")
        return out

    def run(self, interval: float = 10.0, duration: float = 6 * 3600, max_iterations: Optional[int] = None, printer=print) -> None:
        t_end = time.time() + duration
        n = 0
        while time.time() < t_end and (max_iterations is None or n < max_iterations):
            t0 = time.time()
            try:
                tick = self.tick(t0)
                printer(format_tick(tick))
            except Exception as e:
                printer(f"{time.strftime('%H:%M:%S')} tick failed: {e!r}")
            n += 1
            time.sleep(max(1.0, interval - (time.time() - t0)))


def _p(x: Optional[float]) -> str:
    return "  -  " if x is None else f"{x:.3f}"


def format_view(v: InplayView) -> str:
    # Pre-game lines already start with "PRE …"; live lines are "Q3 04:12 · …", so tag those.
    head = v.game_line or f"{'LIVE' if v.live else 'PRE'} {v.title}"
    if v.live and not head.startswith("LIVE"):
        head = "LIVE " + head
    parts = [head]
    for sv in v.sides:
        parts.append(f"    {sv.label:<16} fair {_p(sv.fair)} [mkt {_p(sv.market_p)} model {_p(sv.model_p)} espn {_p(sv.espn_p)}]  best {sv.best_venue or '-':<10} ask {_p(sv.best_ask)} all-in {_p(sv.best_all_in)}  edge {'' if sv.steal_edge is None else f'{sv.steal_edge*100:+.1f}%'}{'  STEAL' if sv.steal else ''}")
    for a in v.actions:
        if a.startswith("STEAL"):
            parts.append(f"    -> {a}")
    if v.disagreement is not None and v.disagreement > 0.05:
        parts.append(f"    sources disagree by {v.disagreement:.2f}")
    return "\n".join(parts)


def format_tick(t: SlateTick) -> str:
    stamp = datetime.fromtimestamp(t.at).strftime("%H:%M:%S")
    if not t.games:
        return f"{stamp} no live games (and none starting within the window)"
    lines = [f"{stamp} {len(t.views)} game(s) priced, {len(t.missing)} without venue quotes"]
    for v in sorted(t.views, key=lambda x: (not x.live, x.title)):
        lines.append(format_view(v))
    for m in t.missing:
        lines.append(f"    no quotes: {m}")
    for e in t.errors:
        lines.append(f"    error: {e}")
    return "\n".join(lines)

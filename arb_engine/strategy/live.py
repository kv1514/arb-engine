"""Slate-wide in-play scanner: every live (or about-to-start) game at once.

`InplayWatcher` follows one event with your lots. This runs the same evaluation across the
whole ESPN scoreboard: one venue fetch per tick, one ESPN scoreboard call, a summary refresh
per live game every ``refresh_summary_every`` seconds, then ``evaluate_inplay`` per game with
no lots — so the output is the fair value per side (model / market / ESPN), the cheapest venue
all-in, and STEAL flags wherever a side is below fair by ``steal_edge`` with the model agreeing.

Per game the slate keeps a ``FeedFreshness`` (the execution gates in ``strategy/inplay.py``
need the previous poll), sizes STEALs against one bankroll for the whole slate (a tick with
five simultaneous STEALs cannot deploy the bankroll five times: ``slate_cap`` scales the
suggested stakes proportionally), and, when a recorder is attached, writes every ESPN state,
every priced tick with its venue L1 and freshness, every STEAL / GATED STEAL, the ladder
update and one pre-game line anchor per event. Each recorder hook is optional (``hasattr``),
so an older store that only has ``record_tick(view)`` still works.

Every evaluation runs on the resolved executable venue set (``executable_venues`` kwarg, else
``scanner.resolve_executable_venues(settings)``: Polymarket is signal-only for a US account),
so a STEAL line names a venue the account can buy on; a cheaper non-executable ask is printed
as ``signal only``. ``quiet=True`` (``--quiet`` / ``INPLAY_QUIET``) prints one summary line per
tick plus the STEAL / LOCK / GATED lines instead of every game.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ..matching.matcher import MergedEvent, merge_snapshots
from ..venues.espn import ESPNClient, ESPNFeed, GameState
from .alerts import Alerter
from .inplay import FeedFreshness, InplayView, SideView, _call_optional, call_store, evaluate_inplay, record_view, resolve_executable, setting, steal_record

try:  # the settings registry (config.declare_setting); this module must import without it
    from ..config import declare_setting as _declare_setting  # type: ignore
except Exception:  # pragma: no cover
    _declare_setting = None
if _declare_setting is not None:
    try:
        _declare_setting("inplay_quiet", env="INPLAY_QUIET", default=False, cast=lambda s: str(s).strip().lower() in ("1", "true", "yes", "on"), doc="live slate: print only STEAL / LOCK / GATED lines and a one-line summary per tick")
    except Exception:  # pragma: no cover
        pass


@dataclass
class SlateTick:
    at: float
    views: list[InplayView]
    games: int                 # scoreboard games considered (live + pre within window)
    missing: list[str] = field(default_factory=list)   # games with no merged venue event
    errors: list[str] = field(default_factory=list)
    stake_scale: float = 1.0   # < 1 when the slate cap scaled this tick's STEAL stakes
    quiet: bool = False        # how the slate wants this tick printed (format_tick honours it)


class LiveSlate:
    def __init__(self, adapters: Iterable[Any], feed: Optional[ESPNFeed] = None, settings: Optional[dict[str, Any]] = None, sport: str = "nfl", steal_edge: float = 0.03, target_margin: float = 0.0, pre_hours: float = 1.0, alerter: Optional[Alerter] = None, store: Any = None, model: Any = None, refresh_summary_every: float = 30.0, contracts: float = 100, bankroll: Optional[float] = None, kelly_fraction: float = 0.25, interval: float = 10.0, slate_cap: Optional[float] = None, executable_venues: Optional[Iterable[str]] = None, quiet: Optional[bool] = None):
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
        self.bankroll, self.kelly_fraction = bankroll, kelly_fraction
        self.interval = interval
        if slate_cap is None:
            slate_cap = setting(self.settings, "inplay_slate_cap")
        self.slate_cap = float(slate_cap) if slate_cap is not None else bankroll   # dollars per tick across every STEAL
        self.executable_venues = resolve_executable(self.settings, executable_venues)   # None = unrestricted; resolved once, passed to every evaluation
        self.quiet = bool(quiet) if quiet is not None else bool(setting(self.settings, "inplay_quiet", False))
        self._enriched: dict[str, tuple[float, GameState]] = {}   # event_id -> (when, enriched state)
        self._seen_steal: set[str] = set()
        self._fresh: dict[str, FeedFreshness] = {}                # event_key -> per-event poll memory
        self._pregame_recorded: set[str] = set()

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
            # A suspect state (P02's StateGuard nulled the WP because the score moved before the
            # last play did) must not get the stale pre-play WP copied back over the hole.
            suspect = bool(getattr(g, "suspect", False))
            for k in ("espn_home_wp", "espn_wp_series", "vegas_spread_home", "vegas_total", "odds_provider", "receive_2h_ko_home"):
                if suspect and k in ("espn_home_wp", "espn_wp_series"):
                    continue
                if getattr(g, k, None) in (None, []) and getattr(cached, k, None) not in (None, []):
                    setattr(g, k, getattr(cached, k))
        return g

    def freshness_for(self, event_key: str) -> FeedFreshness:
        f = self._fresh.get(event_key)
        if f is None:
            f = self._fresh[event_key] = FeedFreshness.from_settings(self.settings, interval_s=self.interval)
        return f

    def merged_events(self, errors: list[str]) -> dict[str, MergedEvent]:
        snaps = []
        for ad in self.adapters:
            try:
                snaps.append(ad.fetch(self.sport))
            except Exception as e:
                errors.append(f"{getattr(ad, 'venue', ad.__class__.__name__)}: {e!r}")
        return merge_snapshots(snaps) if snaps else {}

    def apply_slate_cap(self, views: list[InplayView]) -> float:
        """Scale every STEAL's suggested stake so the tick's total stays within ``slate_cap``
        (proportionally: each side keeps its share of the Kelly total). Returns the scale."""
        if not self.slate_cap or self.slate_cap <= 0:
            return 1.0
        sized: list[tuple[InplayView, SideView]] = [(v, s) for v in views for s in v.sides if s.steal and s.suggested_contracts and s.kelly_stake]
        total = sum(s.kelly_stake * (s.suggested_contracts / s.kelly_contracts if s.kelly_contracts else 1.0) for _, s in sized)
        if total <= self.slate_cap or total <= 0:
            return 1.0
        scale = self.slate_cap / total
        for v, s in sized:
            old = s.suggested_contracts
            s.suggested_contracts = int(old * scale)
            s.kelly_stake = round(s.kelly_stake * scale, 2)
            v.actions = [re.sub(rf"→ buy {old} contracts \(", f"→ buy {s.suggested_contracts} contracts (slate cap {scale:.0%}: ", a) if f" {s.label} all-in " in a else a for a in v.actions]
        return scale

    def record_pregame(self, g: GameState, me: MergedEvent, now: float) -> None:
        """One CLV anchor per event before kickoff: the sportsbook moneylines (P02's pickcenter
        parse) and the Kalshi mid for the home side, via ``store.record_pregame_line``."""
        if self.store is None or g.status != "pre" or not g.event_key or g.event_key in self._pregame_recorded or not hasattr(self.store, "record_pregame_line"):
            return
        ml_h, ml_a = getattr(g, "sportsbook_ml_home", None), getattr(g, "sportsbook_ml_away", None)
        kq = next((q for q in me.quotes_by_venue.get("kalshi", []) if q.outcome == g.home), None)
        k_mid = kq.mid if kq is not None else None
        if ml_h is None and k_mid is None:
            return
        call_store(self.store, "record_pregame_line", g.event_key, ml_h, ml_a, k_mid, now)  # (event_key, ml_home, ml_away, kalshi_mid, ts)
        self._pregame_recorded.add(g.event_key)

    # -- one pass ----------------------------------------------------------------------------
    def tick(self, now: Optional[float] = None) -> SlateTick:
        now = now or time.time()
        errors: list[str] = []
        games = self.wanted_games(self.feed.games(), now)
        out = SlateTick(at=now, views=[], games=len(games), errors=errors, quiet=self.quiet)
        if not games:
            return out
        merged = self.merged_events(errors)
        priced: list[tuple[GameState, MergedEvent, InplayView, FeedFreshness]] = []
        for g in games:
            me = merged.get(g.event_key or "")
            if me is None or me.info.market_type != "moneyline":
                out.missing.append(f"{g.away} @ {g.home} ({g.event_key})")
                continue
            gs = self.state_for(g, now)
            fresh = self.freshness_for(me.event_key)
            try:
                view = evaluate_inplay(me, [], self.settings, self.steal_edge, self.target_margin, game_state=gs, model=self.model, bankroll=self.bankroll, kelly_fraction=self.kelly_fraction, freshness=fresh, now=now, executable_venues=self.executable_venues)
            except Exception as e:
                errors.append(f"{g.away} @ {g.home}: {e!r}")
                continue
            out.views.append(view)
            priced.append((gs, me, view, fresh))
        out.stake_scale = self.apply_slate_cap(out.views)
        for gs, me, view, fresh in priced:
            for act in view.actions:
                if act.startswith("STEAL"):
                    key = f"{view.event_key}|{act.split(' on ')[0]}"
                    if key not in self._seen_steal:
                        self._seen_steal.add(key)
                        sv = next((s for s in view.sides if f" {s.label} all-in " in act), None)
                        extra = {"steal": steal_record(view, sv, act, now)} if sv is not None else {}
                        _call_optional(self.alerts.alert, "STEAL", f"{view.title}: {act}", event=view.event_key, game_state=view.game_state, **extra)
                elif act.startswith("GATED"):
                    self.alerts.info(f"{view.title}: {act}", event=view.event_key, gated=view.gated_reasons)
            try:
                self.record_pregame(gs, me, now)
            except Exception as e:
                errors.append(f"record_pregame_line: {e!r}")
            for err in record_view(self.store, view, me, gs, fresh, now, update_ladder=False):
                errors.append(f"record: {err}")
        if priced and self.store is not None:
            try:
                call_store(self.store, "update_ladder", now)   # once per tick, after every game's STEALs are in
            except Exception as e:
                errors.append(f"record: update_ladder: {e!r}")
        return out

    def run(self, interval: Optional[float] = None, duration: float = 6 * 3600, max_iterations: Optional[int] = None, printer=print) -> None:
        interval = self.interval if interval is None else float(interval)
        self.interval = interval
        for f in self._fresh.values():
            f.interval_s = interval
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
    if v.gated_reasons:
        head += f"  [gated: {', '.join(v.gated_reasons)}]"
    parts = [head]
    for sv in v.sides:
        flag = "  STEAL" if sv.steal else ("  GATED" if sv.steal_gated else ("  SIGNAL ONLY" if sv.best_ineligible else ""))
        # Beside the headline: the executable best when the headline is signal-only, or the
        # cheaper non-executable ask when the headline is the executable one.
        beside = ""
        if sv.best_ineligible and sv.exec_venue:
            beside = f"  exec {sv.exec_venue} all-in {_p(sv.exec_all_in)} ({sv.exec_edge*100:+.1f}%)" if sv.exec_edge is not None else f"  exec {sv.exec_venue} all-in {_p(sv.exec_all_in)}"
        elif sv.signal_venue and not sv.best_ineligible:
            beside = f"  ({sv.signal_venue} {_p(sv.signal_all_in)} signal only)"
        parts.append(f"    {sv.label:<16} fair {_p(sv.fair)} [mkt {_p(sv.market_p)} model {_p(sv.model_p)} espn {_p(sv.espn_p)}]  best {sv.best_venue or '-':<10} ask {_p(sv.best_ask)} all-in {_p(sv.best_all_in)}  edge {'' if sv.steal_edge is None else f'{sv.steal_edge*100:+.1f}%'}{flag}{f' → {sv.suggested_contracts} ct' if sv.suggested_contracts else ''}{beside}")
    for a in v.actions:
        if a.startswith("STEAL") or a.startswith("GATED STEAL") or a.startswith("signal only"):
            parts.append(f"    -> {a}")
    if v.disagreement is not None and v.disagreement > 0.05:
        parts.append(f"    sources disagree by {v.disagreement:.2f}" + (" — STEAL gated" if v.disagreement_gated else ""))
    return "\n".join(parts)


def _signal_lines(v: InplayView) -> list[str]:
    """The lines a quiet tick keeps for one game: STEAL / LOCK NOW / GATED actions, each
    prefixed with the game line so the reader knows which game without the full block."""
    head = v.game_line or v.title
    if v.live and not head.startswith("LIVE"):
        head = "LIVE " + head
    return [f"    {head}  ->  {a}" for a in v.actions if a.startswith(("STEAL", "LOCK NOW", "GATED"))]


def format_tick(t: SlateTick, quiet: Optional[bool] = None) -> str:
    """One tick as text. ``quiet`` (default: the tick's own flag, set by the slate from
    ``--quiet``) keeps the summary line, the STEAL / LOCK / GATED lines and the errors only."""
    quiet = t.quiet if quiet is None else bool(quiet)
    stamp = datetime.fromtimestamp(t.at).strftime("%H:%M:%S")
    if not t.games:
        return f"{stamp} no live games (and none starting within the window)"
    views = sorted(t.views, key=lambda x: (not x.live, x.title))
    summary = f"{stamp} {len(t.views)} game(s) priced, {len(t.missing)} without venue quotes" + (f", stakes scaled to {t.stake_scale:.0%} by the slate cap" if t.stake_scale < 1 else "")
    if quiet:
        n_live = sum(1 for v in views if v.live)
        n_steal = sum(1 for v in views for s in v.sides if s.steal)
        n_gated = sum(1 for v in views for s in v.sides if s.steal_gated or s.lock_gated)
        n_lock = sum(1 for v in views for s in v.sides if s.lock_available and not s.lock_gated)
        n_signal = sum(1 for v in views for s in v.sides if s.best_ineligible)
        lines = [summary + f"; {n_live} live, {n_steal} STEAL, {n_lock} LOCK, {n_gated} gated, {n_signal} signal-only"]
        for v in views:
            lines.extend(_signal_lines(v))
    else:
        lines = [summary]
        for v in views:
            lines.append(format_view(v))
        for m in t.missing:
            lines.append(f"    no quotes: {m}")
    for e in t.errors:
        lines.append(f"    error: {e}")
    return "\n".join(lines)

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
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ..matching.matcher import MergedEvent, merge_snapshots
from ..venues.espn import ESPNClient, ESPNFeed, GameState
from .alerts import Alerter
from .inplay import FeedFreshness, InplayView, SideView, _call_optional, call_store, evaluate_inplay, record_view, resolve_executable, setting, steal_record
from .fastlane import FastLane
from .leadlag import LagSignal, LeadLagTracker
from .lagexec import LagExecutor
from . import ticket
from .paperlag import LagPaperBook
from .laglock import LagLockBook

try:  # the settings registry (config.declare_setting); this module must import without it
    from ..config import declare_setting as _declare_setting  # type: ignore
except Exception:  # pragma: no cover
    _declare_setting = None
if _declare_setting is not None:
    try:
        _declare_setting("inplay_quiet", env="INPLAY_QUIET", default=False, cast=lambda s: str(s).strip().lower() in ("1", "true", "yes", "on"), doc="live slate: print only STEAL / LOCK / GATED lines and a one-line summary per tick")
        _declare_setting("inplay_idle_every_s", env="INPLAY_IDLE_EVERY_S", default=60.0, cast=float, doc="live slate: seconds between ticks while no game is live or within the pre-game window (a recorder can then run all week)")
        _declare_setting("arb_near_margin", env="ARB_NEAR_MARGIN", default=0.03, cast=float, doc="live slate: how far below a lock (dollars per contract, fees in) still earns an ARB CLOSE alert - the buffer that says 'this pair is about to cross'")
        _declare_setting("arb_push_min_margin", env="ARB_PUSH_MIN_MARGIN", default=0.01, cast=float, doc="live slate: smallest ARB margin (dollars per contract, fees in) that is pushed; smaller ones are journalled as ARB SMALL. Replayed by hand (a person on the Robinhood leg), arbs under 1c lost money at every leg speed tested")
        _declare_setting("arb_big_margin", env="ARB_BIG_MARGIN", default=0.03, cast=float, doc="live slate: ARB margin from which the push is titled BIG ARB at top priority (replayed: arbs of 3c+ made money when legged by hand)")
        _declare_setting("lag_lock_watch_s", env="LAG_LOCK_WATCH_S", default=600.0, cast=float, doc="LAG lock watch: seconds after a LAG position fills during which the other outcome is watched for a price that locks the pair")
        _declare_setting("lag_lock_tie_safe", env="LAG_LOCK_TIE_SAFE", default=True, cast=lambda s: str(s).strip().lower() in ("1", "true", "yes", "on"), doc="LAG lock watch: only lock pairs that pay at least $1 on a tie (a Kalshi YES + Rothera YES pays $0.50)")
        _declare_setting("arb_near_every_s", env="ARB_NEAR_EVERY_S", default=300.0, cast=float, doc="live slate: seconds before the same event may send another ARB CLOSE unless the gap shrank by a cent")
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
    lags: list[str] = field(default_factory=list)   # LAG signal texts this tick (lead-lag, market vs market)
    arbs: list[str] = field(default_factory=list)   # fresh two-leg ARB texts this tick (fees, depth, quote age <= 10 s)
    paper: list[str] = field(default_factory=list)  # paper LAG fills / expiries this tick


class LiveSlate:
    def __init__(self, adapters: Iterable[Any], feed: Optional[ESPNFeed] = None, settings: Optional[dict[str, Any]] = None, sport: str = "nfl", steal_edge: float = 0.03, target_margin: float = 0.0, pre_hours: float = 1.0, alerter: Optional[Alerter] = None, store: Any = None, model: Any = None, refresh_summary_every: float = 30.0, contracts: float = 100, bankroll: Optional[float] = None, kelly_fraction: float = 0.25, interval: float = 10.0, slate_cap: Optional[float] = None, executable_venues: Optional[Iterable[str]] = None, quiet: Optional[bool] = None, fast: float = 0.0, lag_executor: Optional[LagExecutor] = None):
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
        # Market-vs-market signals: the venue that has not repriced yet (LAG) and fresh two-leg
        # arbs (ARB) — both need no ESPN state, so they run on every priced event, in play or not.
        self.leadlag = LeadLagTracker.from_settings(self.settings, executable=self.executable_venues, fresh_s=max(10.0, float(interval)))
        self._arb_last: dict[str, float] = {}                     # event_key -> last ARB alert time
        self._near_last: dict[str, tuple[float, float]] = {}       # event_key -> (last ARB CLOSE time, margin then)
        self.near_margin = float(setting(self.settings, "arb_near_margin", 0.03) or 0.0)
        self.near_every = float(setting(self.settings, "arb_near_every_s", 300.0) or 300.0)
        self.arb_push_min = float(setting(self.settings, "arb_push_min_margin", 0.01) or 0.0)
        self.arb_big = float(setting(self.settings, "arb_big_margin", 0.03) or 0.03)
        # Fast lane: 1 s top-of-book refreshes of Kalshi + Robinhood for the live games between
        # full ticks (``fast`` seconds; 0 = off). Built from the adapters this slate already has.
        self.fast = float(fast or 0.0)
        kalshi_client = next((getattr(a, "client", None) for a in self.adapters if getattr(a, "venue", "") == "kalshi"), None)
        robinhood = next((a for a in self.adapters if getattr(a, "venue", "") == "robinhood"), None)
        self.fastlane = FastLane(kalshi_client=kalshi_client, robinhood=robinhood)
        self._live_priced: dict[str, tuple[GameState, MergedEvent, InplayView]] = {}   # from the last full tick
        self._sig_lock = threading.RLock()   # market_signals / seed run from the fast thread and the tick thread
        self._stop = threading.Event()
        # Paper fills for every LAG, judged on the next quotes (strategy/paperlag.py): the
        # fill-adjusted P&L the replay cannot give. Rows in the store's lag_paper table.
        self.paper = LagPaperBook(store=self.store, alerter=self.alerts) if self.store is not None else LagPaperBook(store=None, alerter=self.alerts)
        self.lag_executor = lag_executor   # strategy/lagexec.py: off | intent | demo | live
        # strategy/laglock.py: filled LAG positions (paper and executed) watch the other
        # outcome for a price that locks the pair.
        self.laglock = LagLockBook(store=self.store, watch_s=float(setting(self.settings, "lag_lock_watch_s", 600.0) or 600.0),
                                   executable=self.executable_venues, executor=lag_executor, alerter=self.alerts, settings=self.settings,
                                   require_tie_safe=bool(setting(self.settings, "lag_lock_tie_safe", True)), fresh_s=max(10.0, float(interval)))
        self.idle_every = float(setting(self.settings, "inplay_idle_every_s", 60.0) or 60.0)   # tick cadence with nothing to price
        self._final_seen: set[str] = set()
        self._counts: dict[str, dict[str, int]] = {}   # event_key -> {"lag": n, "arb": n}
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
        pinned = now
        now = now or time.time()
        errors: list[str] = []
        games = self.wanted_games(self.feed.games(), now)
        out = SlateTick(at=now, views=[], games=len(games), errors=errors, quiet=self.quiet)
        if not games:
            return out
        merged = self.merged_events(errors)
        if pinned is None:
            # The catalogue fetch takes seconds; quotes are stamped as they arrive, so the
            # tick decides once they are all in hand, not when it started fetching.
            now = max(now, time.time())
            out.at = now
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
            self.market_signals(me, view, out, now)
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
        for gs, me, view, _ in priced:
            if getattr(gs, "status", "") == "final" and gs.home_score is not None and gs.away_score is not None:
                try:
                    winner = None if gs.home_score == gs.away_score else (gs.home if gs.home_score > gs.away_score else gs.away)
                    self.paper.settle(me.event_key, winner, now)
                except Exception as e:
                    errors.append(f"paperlag settle: {e!r}")
                if me.event_key not in self._final_seen:
                    self._final_seen.add(me.event_key)
                    self.final_summary(gs, me, now)
        with self._sig_lock:
            self._live_priced = {me.event_key: (gs, me, view) for gs, me, view, _ in priced if view.live}
            if self.fast:
                self.fastlane.seed({k: me.quotes_by_venue for k, (_, me, _) in self._live_priced.items()})
        return out

    def fast_step(self, now: Optional[float] = None) -> SlateTick:
        """One fast-lane step: refresh Kalshi + Robinhood top of book for the live games from
        the last full tick, run the market-vs-market signals (LAG, ARB) on the fresh quotes
        and record the L1 so the observation ladder gets 1 s resolution. The ESPN state,
        model and STEAL logic are the full tick's business."""
        pinned = now   # None in production: the lane then reads the clock around each request
        now = now or time.time()
        with self._sig_lock:
            live = dict(self._live_priced)
        out = SlateTick(at=now, views=[], games=len(live), quiet=True)
        if not live:
            return out
        refreshed, errs = self.fastlane.step(list(live), pinned)
        if pinned is None:
            # Signals are decided when the quotes are in hand (their obs_ts), not when the
            # step began; judging them at the start would call every fresh quote "future".
            now = self.fastlane.clock()
            out.at = now
        out.errors.extend(errs)
        if self.store is not None:
            try:   # one ticker per step (~150 ms): the 1 s quote refresh is never held up
                self.fastlane.poll_trades(self.store, now, per_step=1)
            except Exception as e:
                out.errors.append(f"trade prints: {e!r}")
        for key, (gs, me, view) in live.items():
            by = refreshed.get(key)
            if not by:
                continue
            me2 = MergedEvent(me.event_key, me.info, by)
            self.market_signals(me2, view, out, now)
            if self.store is not None:
                try:
                    call_store(self.store, "record_tick", view, quotes_by_venue=by, freshness=None, ts=now, source="fast")
                except Exception as e:
                    out.errors.append(f"record: {e!r}")
        if self.store is not None and (out.lags or out.arbs):
            try:
                call_store(self.store, "update_ladder", now)
            except Exception as e:
                out.errors.append(f"record: update_ladder: {e!r}")
        return out

    def final_summary(self, gs: GameState, me: MergedEvent, now: float) -> None:
        """One FINAL line per game (journal + ntfy when subscribed): score, how many LAG / ARB
        signals it produced and what the paper book made of the LAGs."""
        c = self._counts.get(me.event_key, {"lag": 0, "arb": 0})
        ps = self.paper.summary(me.event_key)
        pnl = ps.get("pnl_settle") or {}
        ls = self.laglock.summary(me.event_key)
        text = (f"FINAL {gs.away} {gs.away_score}-{gs.home_score} {gs.home}: {c['lag']} LAG, {c['arb']} ARB; paper LAG {ps.get('filled', 0)} filled / {ps.get('expired', 0)} expired"
                + (f", settled {pnl.get('wins', 0)}/{pnl.get('n', 0)} wins, {pnl.get('mean', 0):+.3f}/ct" if pnl else "")
                + (f"; LAG locks {ls['locked']}/{ls['positions']}" + (f" (+${ls['locked_dollars']:.2f}, median {ls['median_seconds_to_lock']:.0f}s)" if ls.get("locked") else "") if ls["positions"] else ""))
        try:
            _call_optional(self.alerts.alert, "FINAL", text, event=me.event_key)
        except Exception:
            self.alerts.info(text, event=me.event_key)

    def market_signals(self, me: MergedEvent, view: InplayView, out: SlateTick, now: float) -> None:
        """LAG (lead-lag) and fresh ARB signals for one event: alerted, journalled and, for
        LAG, recorded as a ``signal_kind="lag"`` observation so the store's ladder measures
        convergence. Failures here never stop the tick. Serialised: the fast-lane thread and
        the full tick both call it."""
        with self._sig_lock:
            self._market_signals(me, view, out, now)

    def _market_signals(self, me: MergedEvent, view: InplayView, out: SlateTick, now: float) -> None:
        try:
            for line in self.paper.observe(me.event_key, me.quotes_by_venue, now):
                out.paper.append(f"{view.title}: {line}")
        except Exception as e:
            out.errors.append(f"paperlag: {e!r}")
        others = {o: [x for x in me.info.outcomes if x != o] for o in me.info.outcomes}
        try:
            for o in self.paper.orders:
                if o.event_key == me.event_key and o.filled_at is not None and len(others.get(o.outcome, [])) == 1:
                    self.laglock.open(f"paper:{o.key}", o.event_key, o.outcome, others[o.outcome][0], o.follower, o.contracts,
                                      o.fill_price if o.fill_price is not None else o.price, o.all_in, now, "paper", getattr(o, "tie_value", None))
            for line in self.laglock.observe(me.event_key, me.quotes_by_venue, now):
                out.paper.append(f"{view.title}: {line}")
        except Exception as e:
            out.errors.append(f"laglock: {e!r}")
        try:
            for sig in self.leadlag.observe(me.event_key, view.title, list(me.info.outcomes), dict(me.info.labels or {}), me.quotes_by_venue, self.settings, now, self.bankroll, self.kelly_fraction):
                text = sig.text()   # starts with "NFL - DEN @ KC - LAG: ..."
                out.lags.append(text)
                self._counts.setdefault(me.event_key, {"lag": 0, "arb": 0})["lag"] += 1
                try:
                    self.paper.open(sig, now)
                except Exception as e:
                    out.errors.append(f"paperlag open: {e!r}")
                if self.lag_executor is not None:
                    try:
                        rec = self.lag_executor.on_signal(sig, me.quotes_by_venue, now)
                        if rec is not None:
                            out.paper.append(f"lag-exec {rec.get('status')}: {rec.get('ticker')} {rec.get('side')} {rec.get('count')} @ {rec.get('price')}" + (f" — {rec['reason']}" if rec.get("reason") else ""))
                            line = self.lag_executor.describe(rec)
                            if line:   # the LAG push below says what the bot did, not only what it saw
                                text = f"{text}\n{line}"
                            filled = _fill_count(rec)
                            if rec.get("status") == "SUBMITTED" and filled and len(others.get(sig.outcome, [])) == 1:
                                self.laglock.open(f"exec:{rec.get('order_id') or now}", sig.event_key, sig.outcome, others[sig.outcome][0], sig.follower,
                                                  filled, sig.follower_ask, _all_in_at(sig, me.quotes_by_venue, filled, self.settings), now,
                                                  self.lag_executor.mode, getattr(sig, "tie_value", None))
                    except Exception as e:
                        out.errors.append(f"lag-exec: {e!r}")
                # ``signal_kind`` (not ``kind``: Alerter.journal's first positional is ``kind``) lands in
                # the observation's extra_json so the ladder can be split STEAL vs LAG.
                extra = {"outcome": sig.outcome, "venue": sig.follower, "ask": sig.follower_ask, "all_in": sig.follower_all_in, "fair": sig.leader_mid, "edge": sig.edge, "market_p": sig.leader_mid, "suggested_contracts": sig.suggested_contracts, "period": view.game_state.get("period") if isinstance(view.game_state, dict) else None, "signal_kind": "lag", "leader": sig.leader, "lead_move": sig.lead_move, "follower_move": sig.follower_move, "ts": now}
                sport = ticket.SPORT_NAMES.get(str(me.event_key).split(":")[0], "")
                _call_optional(self.alerts.alert, "LAG", text, event=view.event_key, ntfy_title=f"LAG {sport}".strip(), **extra)
        except Exception as e:
            out.errors.append(f"leadlag: {e!r}")
        try:
            from ..scanner import analyze_event

            # The bankroll is passed in as the budget so the sized legs (and their fees) are
            # the order that can be paid for, not a reference 100-lot scaled afterwards.
            rep = analyze_event(me, self.settings, contracts=self.contracts, target_margin=self.target_margin, max_quote_age=max(10.0, float(self.interval)), now=now, executable_venues=self.executable_venues, budget=self.bankroll or None)
            arb = rep.arb or {}
            stale = "stale-quote" in (rep.flags or [])
            if arb.get("is_arb") and rep.fillable and not stale:
                sized = rep.sized_arb or arb
                note = (f"depth-capped" if not self.bankroll else f"bankroll {ticket.money(self.bankroll)}")
                first, why, maxp = self._legging(me, rep, sized, now)
                margin = float(sized.get("margin") or 0.0)
                kind = "BIG ARB" if margin >= self.arb_big else ("ARB" if margin >= self.arb_push_min else "ARB SMALL")
                text = ticket.arb_ticket(view.title, sized, size_note=note, sport=rep.sport or me.event_key, header=kind,
                                         first=first, first_reason=why, max_prices=maxp, now=now)
                out.arbs.append(text)
                self._counts.setdefault(me.event_key, {"lag": 0, "arb": 0})["arb"] += 1
                if now - self._arb_last.get(me.event_key, -1e18) >= 30.0:
                    self._arb_last[me.event_key] = now
                    # ARB SMALL is journalled, not pushed (not a default ntfy kind): replayed by
                    # hand, arbs under arb_push_min_margin lost money.
                    _call_optional(self.alerts.alert, kind, text, event=view.event_key, ntfy_title=f"{kind} {ticket.SPORT_NAMES.get(rep.sport or '', (rep.sport or '').upper())}".strip(),
                                   margin=sized.get("margin"), legs=sized.get("legs"), contracts=sized.get("contracts"), cost=sized.get("total_cost"), profit=sized.get("profit"))
            elif not stale and arb.get("margin") is not None and self.near_margin > 0 and -self.near_margin <= float(arb["margin"]) < 0:
                # Nearly a lock: alert once per ``arb_near_every_s`` and again whenever the gap
                # shrank by a cent, so the phone says "get ready" before the pair crosses.
                m = float(arb["margin"])
                last_t, last_m = self._near_last.get(me.event_key, (-1e18, -1.0))
                if now - last_t >= self.near_every or m >= last_m + 0.01:
                    self._near_last[me.event_key] = (now, m)
                    text = ticket.near_arb_ticket(view.title, rep, m, sport=rep.sport or me.event_key, bankroll=self.bankroll or None)
                    out.arbs.append(text)
                    _call_optional(self.alerts.alert, "ARB CLOSE", text, event=view.event_key, ntfy_title=f"ARB CLOSE {ticket.SPORT_NAMES.get(rep.sport or '', (rep.sport or '').upper())}".strip(), margin=m)
        except Exception as e:
            out.errors.append(f"arb: {e!r}")

    def _legging(self, me: MergedEvent, rep: Any, sized: dict, now: float) -> tuple[Optional[int], str, dict]:
        """Which leg to buy first and how far each other leg may move and still lock.

        First: the leg whose venue has moved least over the lead-lag window - the stale price,
        the one about to catch up and disappear (on the first Sunday the lagging venue caught
        up in a median 23 s). Without move history: the leg with the thinner displayed size.
        Other legs: ``VenuePrice.max_buy_price`` - the most that leg may cost while the rest
        stay at their asks and the set still locks."""
        legs = list(sized.get("legs") or [])
        if len(legs) < 2:
            return None, "", {}
        moves = []
        for l in legs:
            h = self.leadlag._hist.get((me.event_key, l.get("venue")))
            mv = self.leadlag._move_over_window(h, now) if h else None
            moves.append(abs(mv) if mv is not None else None)
        first, why = None, ""
        if all(m is not None for m in moves) and max(moves) - min(moves) >= 0.01:
            first = min(range(len(legs)), key=lambda i: moves[i])
            other = max(range(len(legs)), key=lambda i: moves[i])
            why = (f"{str(legs[first].get('venue')).upper()} has not followed {str(legs[other].get('venue')).upper()}'s "
                   f"{moves[other] * 100:.0f}c move yet: its price is the one about to go")
        else:
            sizes = [l.get("ask_size") for l in legs]
            if all(x is not None for x in sizes) and len(set(sizes)) > 1:
                first = min(range(len(legs)), key=lambda i: sizes[i])
                why = f"thinner book ({float(sizes[first]):g} offered)"
        maxp: dict[int, float] = {}
        for i, l in enumerate(legs):
            for o in rep.outcomes or []:
                if o.outcome != l.get("outcome"):
                    continue
                for v in o.venues:
                    if v.venue == l.get("venue") and v.market_id == l.get("market_id") and v.max_buy_price is not None:
                        maxp[i] = v.max_buy_price
        return first, why, maxp

    def run(self, interval: Optional[float] = None, duration: float = 6 * 3600, max_iterations: Optional[int] = None, printer=print) -> None:
        interval = self.interval if interval is None else float(interval)
        self.interval = interval
        for f in self._fresh.values():
            f.interval_s = interval
        t_end = time.time() + duration
        n = 0
        fast_thread = None
        if self.fast:
            self._stop.clear()
            fast_thread = threading.Thread(target=self._fast_loop, args=(printer,), name="fastlane", daemon=True)
            fast_thread.start()
        tick = None
        while time.time() < t_end and (max_iterations is None or n < max_iterations):
            t0 = time.time()
            tick = None
            try:
                tick = self.tick(t0)
                printer(format_tick(tick))
            except Exception as e:
                printer(f"{time.strftime('%H:%M:%S')} tick failed: {e!r}")
            n += 1
            idle = not self._live_priced and not (tick.views if "tick" in locals() and tick is not None else [])
            pause = self.idle_every if idle else interval
            time.sleep(max(1.0, pause - (time.time() - t0)))
        self._stop.set()
        if fast_thread is not None:
            fast_thread.join(timeout=self.fast * 3 + 5)

    def _fast_loop(self, printer=print) -> None:
        """The fast lane's own thread: a step every ``self.fast`` seconds while there are live
        games from the last full tick; a step that overruns just shortens the pause. Only
        signal / error lines are printed (the full tick prints the summaries)."""
        last_err = ""
        while not self._stop.is_set():
            s0 = time.time()
            try:
                with self._sig_lock:
                    have = bool(self._live_priced)
                if have:
                    ft = self.fast_step(s0)
                    for line in ft.arbs:
                        printer(f"{time.strftime('%H:%M:%S')} *** ARB *** {line}")
                    for line in ft.lags:
                        printer(f"{time.strftime('%H:%M:%S')} LAG {line}")
                    for line in ft.paper:
                        printer(f"{time.strftime('%H:%M:%S')} {line}")
                    for e in ft.errors:
                        if e != last_err:   # a venue that keeps failing is printed once, not every second
                            printer(f"{time.strftime('%H:%M:%S')} fast: {e}")
                        last_err = e
            except Exception as e:
                printer(f"{time.strftime('%H:%M:%S')} fast step failed: {e!r}")
            self._stop.wait(max(0.05, self.fast - (time.time() - s0)))


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
        lines = [summary + f"; {n_live} live, {n_steal} STEAL, {n_lock} LOCK, {n_gated} gated, {n_signal} signal-only, {len(t.lags)} LAG, {len(t.arbs)} ARB"]
        for v in views:
            lines.extend(_signal_lines(v))
    else:
        lines = [summary]
        for v in views:
            lines.append(format_view(v))
        for m in t.missing:
            lines.append(f"    no quotes: {m}")
    for a in t.arbs:
        lines.append(f"    *** ARB *** {a}")
    for l in t.lags:
        lines.append(f"    LAG {l}")
    for l in t.paper:
        lines.append(f"    {l}")
    for e in t.errors:
        lines.append(f"    error: {e}")
    return "\n".join(lines)


def _fill_count(rec: dict) -> int:
    try:
        return int(float(rec.get("fill_count")))
    except (TypeError, ValueError):
        return 0


def _all_in_at(sig: Any, quotes_by_venue: dict, n: int, settings: Optional[dict]) -> float:
    """The executed LAG's per-contract cost at the size that filled (fees round per order)."""
    from ..fees.registry import fee_model_for_quote

    q = next((x for x in quotes_by_venue.get(sig.follower, []) if x.outcome == sig.outcome and (x.meta or {}).get("side") != "no"), None)
    try:
        if q is not None and n:
            return float(sig.follower_ask) + float(fee_model_for_quote(q, settings).fee(sig.follower_ask, n, "taker")) / n
    except Exception:
        pass
    return float(sig.follower_all_in)

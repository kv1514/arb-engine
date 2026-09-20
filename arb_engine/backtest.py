"""Replay a finished game play by play with public price history and score every
fair-value source against the real outcome.

    arb-engine backtest --espn 401872932            # DET @ BUF, 2026-09-17
    arb-engine backtest --week 1 --bar-mode both --slices --placebo --json out/w1.json
    arb-engine backtest --pool out/w1.json,out/w2.json   # strata + intervals over weeks

Per play (ESPN wall-clock time): reconstruct the game state, score our WP model on the
state *before* the play (``model_p``) and *after* it (``model_after_p``), read ESPN's WP,
and read each venue's market at that minute (Kalshi bid/ask candles, Robinhood 5-minute
trade bars, Polymarket 1-minute mids). Report log-loss / Brier per source over the in-play
window, the blend, disagreement, and how many minutes showed a fee-adjusted arbitrage.

Honesty rules (why the numbers look different from a naive replay)
------------------------------------------------------------------
* **Two market alignments.** ``kalshi_before_p`` is the last candle ending at or before the
  play (what the market knew when the model saw the pre-play state); ``kalshi_after_p`` is
  the first candle ending after it (the market has seen the play). The model-vs-market claim
  is a range between them; ``bar_mode`` picks which one feeds ``kalshi_p`` / the blend
  (default ``before``).
* **Play classes.** Every row carries ``play_class`` (scrimmage / kickoff / try / timeout /
  kneel / end_period / ot / try_synth / kickoff_pending_synth). The headline is the
  in-play *scrimmage* subset; synthetic rows (ESPN folds the try into the TD) exist so the
  try / kickoff rules have rows to score and never enter the headline. Timeout rows are
  scored on the post-timeout state.
* **Sport-aware gate.** ``in_play = period and (gsr is None or gsr > 0) and not decided``.
  College OT rows have no game clock: they stay in with ``model_p=None`` and ESPN / market
  scored (the OT slice reports model n=0 until a college OT rule exists).
* **Timeouts** come from the play text, not the 3/3 default.
* **Spread fallback** replaces ``spread or 0.0``: pickcenter -> de-vigged sportsbook
  moneylines -> a spread implied by the pre-kickoff Kalshi price -> None (logged), with the
  Q1/Q2 log-loss of the line-less subset reported with and without the fallback.
* **Fees** in ``simulate_steal`` use the series' ``fee_multiplier`` (0.5 on some series).
* **Placebos.** ``simulate_steal`` reports the pre-play-fair / before-ask pairing and the
  post-play-fair / after-ask pairing (the executable one) next to a +1-candle shift and a
  within-week game-label shuffle, so an ROI that survives only the leaky pairing is named.

Persisted replay JSON (``--json``), read back by ``load_results`` / ``--pool`` and by the
event study: ``{season, week, sport, bar_mode, fit, simulations, report, skipped,
games: [ReplayResult]}`` where each game's ``rows[]`` carries at least
``ts, period, clock, home_score, away_score, possession, play_class, slice, in_play,
synthetic, model_p, model_after_p, espn_p, kalshi_before_p, kalshi_after_p,
kalshi_before_spread, kalshi_after_spread, kalshi_p, market_p, blend_p, blend_after_p``
(``kalshi_p`` is the ``bar_mode`` alignment). Unknown keys are ignored on load.
"""

from __future__ import annotations

import inspect
import json
import logging
import math
import random
import statistics
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

from .fees.kalshi import KalshiFees
from .fees.robinhood import RobinhoodFees
from .matching.normalize import parse_iso
from .matching.teams import team_code, team_name, team_table
from .quant.arbitrage import Leg, evaluate
from .quant.inplay_fair import blended_fair, market_confidence_from_spread
from .venues.espn import ESPNClient, normalize_home_spread
from .venues.history import Bar, HistoryClient, OfflineCacheMiss, PlayRow, bar_at, espn_timeline

log = logging.getLogger(__name__)

# Settings keys this module owns (config.declare_setting arrives with the CLI registry item).
try:  # pragma: no cover - only once that item exists
    from .config import declare_setting as _declare_setting  # type: ignore[attr-defined]
except ImportError:
    _declare_setting = None
HISTORY_CACHE_DIR_DEFAULT = "out/cache/history"
if _declare_setting is not None:  # pragma: no cover
    try:
        _declare_setting("history_cache_dir", env="ARB_HISTORY_CACHE_DIR", default=HISTORY_CACHE_DIR_DEFAULT, cast=str, doc="read-through cache for backtest history (kalshi candles, polymarket, robinhood, ESPN)")
        _declare_setting("backtest_bar_mode", env="ARB_BACKTEST_BAR_MODE", default="before", cast=str, doc="market alignment for the replay headline: before | after")
    except Exception:
        pass


@dataclass
class ReplayRow:
    ts: float
    period: int
    clock: Optional[int]
    home_score: int
    away_score: int
    possession: Optional[str]
    model_p: Optional[float]
    espn_p: Optional[float]
    kalshi_p: Optional[float]
    robinhood_p: Optional[float]
    polymarket_p: Optional[float]
    market_p: Optional[float]
    blend_p: Optional[float]
    disagreement: Optional[float]
    kalshi_arb_margin: Optional[float]
    cross_arb_margin: Optional[float]
    text: str = ""
    kalshi_spread: Optional[float] = None  # ask - bid of the Kalshi candle used (book width), bar_mode alignment
    kalshi_home_ask: Optional[float] = None  # executable prices at the bar_mode alignment
    kalshi_away_ask: Optional[float] = None
    # --- alignment-explicit market columns ---
    kalshi_before_p: Optional[float] = None
    kalshi_after_p: Optional[float] = None
    kalshi_before_spread: Optional[float] = None
    kalshi_after_spread: Optional[float] = None
    kalshi_before_home_ask: Optional[float] = None
    kalshi_before_away_ask: Optional[float] = None
    kalshi_after_home_ask: Optional[float] = None
    kalshi_after_away_ask: Optional[float] = None
    kalshi_shift_home_ask: Optional[float] = None  # the candle after the after-bar (placebo)
    kalshi_shift_away_ask: Optional[float] = None
    # --- post-play scoring ---
    model_after_p: Optional[float] = None
    market_after_p: Optional[float] = None
    blend_after_p: Optional[float] = None
    model_p_zero_spread: Optional[float] = None  # model with spread 0 (only filled for line-less games)
    # --- labels ---
    play_class: Optional[str] = None
    slice: Optional[str] = None
    down: Optional[int] = None
    lead_bucket: Optional[str] = None
    quarter: Optional[int] = None
    secs_bucket: Optional[str] = None
    pickcenter_spread: Optional[float] = None
    home_timeouts: Optional[int] = None
    away_timeouts: Optional[int] = None
    gsr: Optional[int] = None
    in_play: bool = True
    synthetic: bool = False
    extra_model_p: dict[str, Optional[float]] = field(default_factory=dict)


@dataclass
class ReplayResult:
    espn_event_id: str
    home: str
    away: str
    home_won: bool
    final: str
    kickoff: Optional[str]
    n_plays: int
    n_inplay: int
    metrics: dict[str, dict[str, float]]
    arb_minutes: dict[str, Any]
    rows: list[ReplayRow] = field(default_factory=list)
    pregame_kalshi_p: Optional[float] = None
    spread_home: Optional[float] = None
    spread_source: Optional[str] = None
    kalshi_fee_multiplier: float = 1.0
    sport: str = "nfl"
    bar_mode: str = "before"


def _scores(pred: list[float], y: int) -> dict[str, float]:
    ps = [min(max(p, 1e-6), 1 - 1e-6) for p in pred]
    if not ps:
        return {"n": 0}
    ll = -sum(math.log(p) if y else math.log(1 - p) for p in ps) / len(ps)
    br = sum((p - y) ** 2 for p in ps) / len(ps)
    return {"n": len(ps), "log_loss": round(ll, 4), "brier": round(br, 4), "mean_p_home": round(sum(ps) / len(ps), 4)}


# Kalshi's ticker codes where they differ from the standard NFL abbreviations.
KALSHI_TICKER_CODES = {"JAX": "JAC"}
KALSHI_GAME_SERIES = {"nfl": "KXNFLGAME", "ncaaf": "KXNCAAFGAME"}
BAR_MODES = ("before", "after")
HEADLINE_CLASSES = ("scrimmage",)  # the in-play headline subset
MIN_CELL = 50  # strata cells with fewer rows are reported with n only
MODEL_PLAY_CLASS = {"try_synth": "try", "kickoff_pending_synth": "kickoff", "ot": None}  # what the model's play_class kwarg understands


# ---- labels and the gate -------------------------------------------------------------------

def slice_label(period: int, gsr: Optional[int], overtime: bool = False) -> Optional[str]:
    """q1 / q2 / q3 / q4_early (>= 5:00 left) / q4_late / ot."""
    if overtime or (period and period > 4):
        return "ot"
    if not period:
        return None
    if period < 4:
        return f"q{period}"
    return "q4_late" if (gsr is not None and gsr < 300) else "q4_early"


def lead_bucket(home_score: int, away_score: int) -> str:
    d = abs(home_score - away_score)
    if d == 0:
        return "tie"
    if d <= 3:
        return "1-3"
    if d <= 8:
        return "4-8"
    if d <= 16:
        return "9-16"
    return "17+"


def secs_bucket(gsr: Optional[int]) -> Optional[str]:
    if gsr is None:
        return "ot"
    for hi, label in ((300, "0-5m"), (900, "5-15m"), (1800, "15-30m"), (2700, "30-45m")):
        if gsr < hi:
            return label
    return "45-60m"


def season_of(summary: Optional[dict], kickoff: Optional[datetime]) -> Optional[int]:
    """The football *season* a game belongs to, which is not the kickoff's calendar year:
    week 18, the playoffs and the bowls are played in January. ESPN's header carries the
    season; without it, January / February games belong to the previous year."""
    try:
        y = int(((summary or {}).get("header") or {}).get("season", {}).get("year"))
        if y > 1900:
            return y
    except (TypeError, ValueError, AttributeError):
        pass
    if kickoff is None:
        return None
    return kickoff.year - 1 if kickoff.month <= 2 else kickoff.year


def in_play_gate(period: int, gsr: Optional[int], home_score: int, away_score: int, play_class: Optional[str] = None, last: bool = False) -> bool:
    """Whether the score can still change: a period exists, the clock (if the sport has one)
    is running, and the game is not decided (0:00 with a margin, or the final row)."""
    if not period:
        return False
    if gsr is not None and gsr <= 0 and home_score != away_score:
        return False
    if last and play_class == "end_period":
        return False
    return True


# ---- model plumbing -----------------------------------------------------------------------

def _accepted_kwargs(fn: Any) -> Optional[set[str]]:
    """Parameter names ``fn`` accepts (None = accepts anything)."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return None
    return set(sig.parameters)


def _call_model(fn: Any, accepted: Optional[set[str]], **kw: Any) -> Optional[float]:
    if fn is None:
        return None
    if accepted is not None:
        kw = {k: v for k, v in kw.items() if k in accepted}
    try:
        return float(fn(**kw))
    except Exception:
        return None


def _spread_from_pregame_probability_local(p_home: float, model: Any = None) -> Optional[float]:
    """Home spread (ESPN sign, negative = home favoured) whose neutral pre-game model
    probability equals ``p_home``, by bisection over [-30, 30]. Local fallback for the
    rules-table version in ``models.wp``."""
    try:
        from .models.wp import home_win_probability
    except Exception:
        return None
    if p_home is None or not (0 < p_home < 1):
        return None

    def f(spread: float) -> float:
        return home_win_probability(home_score=0, away_score=0, game_seconds_remaining=3600, possession=None, vegas_spread_home=spread, model=model, monotone=False) - p_home

    lo, hi = -30.0, 30.0
    flo, fhi = f(lo), f(hi)
    if flo * fhi > 0:
        return None
    for _ in range(40):
        mid = (lo + hi) / 2
        fm = f(mid)
        if abs(fm) < 1e-6:
            return round(mid, 2)
        if (fm > 0) == (flo > 0):
            lo, flo = mid, fm
        else:
            hi = mid
    return round((lo + hi) / 2, 2)


def _sportsbook_home_probability(pc: dict) -> Optional[float]:
    """De-vigged P(home) from the pickcenter moneylines: the lines module's
    ``sportsbook_probs_from_moneylines`` when it exists, else power de-vig locally."""
    ml = pc.get("moneyline") or {}
    h = ((ml.get("home") or {}).get("close") or {}).get("odds") or (pc.get("homeTeamOdds") or {}).get("moneyLine")
    a = ((ml.get("away") or {}).get("close") or {}).get("odds") or (pc.get("awayTeamOdds") or {}).get("moneyLine")
    try:
        hml, aml = float(str(h).replace("+", "")), float(str(a).replace("+", ""))
    except (TypeError, ValueError):
        return None
    try:
        from .quant.odds import sportsbook_probs_from_moneylines  # type: ignore[attr-defined]

        got = sportsbook_probs_from_moneylines(hml, aml)
        if isinstance(got, dict):
            v = got.get("home") if "home" in got else got.get("p_home")
            return float(v) if v is not None else None
        if isinstance(got, (tuple, list)) and got:
            return float(got[0])
    except Exception:
        pass
    try:
        from .quant.odds import american_to_decimal, devig_power, implied_from_decimal

        probs = devig_power([implied_from_decimal(american_to_decimal(hml)), implied_from_decimal(american_to_decimal(aml))])
        return float(probs[0])
    except Exception:
        return None


def resolve_spread(summary: dict, home: str, pregame_kalshi_p: Optional[float], model: Any = None, sport: str = "nfl") -> tuple[Optional[float], str]:
    """R05a fallback chain for the pre-game home spread: pickcenter -> de-vigged sportsbook
    moneyline -> pre-kickoff Kalshi price -> None (logged). Returns (spread, source)."""
    pcs = summary.get("pickcenter") or []
    for pc in pcs:
        sp = normalize_home_spread(pc, home, sport)
        if sp is not None:
            return float(sp), "pickcenter"
    try:
        from .models.wp import spread_from_pregame_probability as _s_from_p  # type: ignore[attr-defined]
    except Exception:
        _s_from_p = _spread_from_pregame_probability_local
    for pc in pcs:
        p = _sportsbook_home_probability(pc)
        if p is not None:
            try:
                sp = _s_from_p(p, model=model)
            except TypeError:
                sp = _s_from_p(p)
            if sp is not None:
                return float(sp), "sportsbook_moneyline"
    if pregame_kalshi_p is not None:
        try:
            sp = _s_from_p(pregame_kalshi_p, model=model)
        except TypeError:
            sp = _s_from_p(pregame_kalshi_p)
        if sp is not None:
            return float(sp), "pregame_kalshi"
    log.warning("no pre-game spread for %s (no pickcenter, moneyline or pre-kickoff Kalshi price): model runs at spread 0", home)
    return None, "none"


class GameReplayer:
    def __init__(self, espn: Optional[ESPNClient] = None, history: Optional[HistoryClient] = None, model: Any = None, sport: str = "nfl", bar_mode: str = "before", extra_models: Optional[dict[str, Any]] = None, spread_scale: Optional[float] = None, spread_clamp: Optional[float] = None):
        self.sport = sport
        self.espn = espn or ESPNClient(sport=sport)
        self.history = history or HistoryClient()
        self.model = model
        self.bar_mode = bar_mode if bar_mode in BAR_MODES else "before"
        self.extra_models = extra_models or {}
        self.spread_scale = spread_scale
        self.spread_clamp = spread_clamp

    def kalshi_tickers(self, home: str, away: str, kickoff) -> tuple[str, str]:
        """KX{NFL|NCAAF}GAME-{YYMONDD}{AWAY}{HOME}-{TEAM} using the ET date of kickoff (Kalshi's convention)."""
        from .matching.normalize import et_date

        d = et_date(kickoff)
        y, m, dd = d.split("-")
        mon = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"][int(m) - 1]
        sport = getattr(self, "sport", "nfl")
        if sport == "nfl":
            home_k, away_k = KALSHI_TICKER_CODES.get(home, home), KALSHI_TICKER_CODES.get(away, away)
        else:
            table = team_table(sport)
            home_k, away_k = (table.get(home) or {}).get("kalshi") or home, (table.get(away) or {}).get("kalshi") or away
        series = KALSHI_GAME_SERIES.get(sport, f"KX{sport.upper()}GAME")
        code = f"{y[2:]}{mon}{dd}{away_k}{home_k}"
        return f"{series}-{code}-{home_k}", f"{series}-{code}-{away_k}"

    def _spread_for_model(self, spread: Optional[float]) -> float:
        s = float(spread or 0.0)
        if self.spread_scale is not None:
            s *= float(self.spread_scale)
        if self.spread_clamp is not None:
            s = max(-abs(self.spread_clamp), min(abs(self.spread_clamp), s))
        return s

    def replay(self, espn_event_id: str, rh_contracts: Optional[dict[str, str]] = None, pm_tokens: Optional[dict[str, str]] = None, pre_minutes: int = 30, post_minutes: int = 10, kalshi_tickers: Optional[dict[str, str]] = None, summary: Optional[dict] = None) -> ReplayResult:
        summary = summary or self.history.espn_summary(self.espn, espn_event_id)
        plays, meta = espn_timeline(summary, sport=self.sport)
        home, away = team_code(self.sport, meta["home"]) or meta["home"], team_code(self.sport, meta["away"]) or meta["away"]
        if not plays:
            raise RuntimeError("no plays with wall-clock timestamps in the ESPN summary")
        kickoff = meta.get("kickoff")
        t0 = min(plays[0].ts, kickoff.timestamp() if kickoff else plays[0].ts) - pre_minutes * 60
        t1 = plays[-1].ts + post_minutes * 60
        # Venue histories.
        tickers = kalshi_tickers or dict(zip(("home", "away"), self.kalshi_tickers(home, away, kickoff or plays[0].ts)))
        kh = self._safe(lambda: self.history.kalshi_candles(tickers["home"], int(t0), int(t1))) or []
        ka = self._safe(lambda: self.history.kalshi_candles(tickers["away"], int(t0), int(t1))) or []
        fee_mult = self._safe(lambda: self.history.kalshi_fee_multiplier(tickers["home"]))
        fee_mult = float(fee_mult) if fee_mult is not None else 1.0
        rh: dict[str, list[Bar]] = {}
        if rh_contracts:
            start_iso = (kickoff - timedelta(minutes=pre_minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z") if kickoff else None
            rh = self._safe(lambda: self.history.robinhood_bars(list(rh_contracts.values()), start_iso)) or {}
        pm: dict[str, list[Bar]] = {}
        if pm_tokens:
            for side, tok in pm_tokens.items():
                pm[side] = self._safe(lambda tok=tok: self.history.polymarket_history(tok, int(t0), int(t1))) or []
        # Pre-kickoff Kalshi price (last candle ending before kickoff) and the spread chain.
        pregame_kalshi_p = None
        if kickoff:
            pb_h = bar_at(kh, kickoff.timestamp(), "kalshi_before")
            pb_a = bar_at(ka, kickoff.timestamp(), "kalshi_before")
            pregame_kalshi_p = pb_h.mid if pb_h and pb_h.mid is not None else ((1 - pb_a.mid) if pb_a and pb_a.mid is not None else None)
        spread_home, spread_source = resolve_spread(summary, home, pregame_kalshi_p, self.model, self.sport)
        pickcenter_spread = spread_home if spread_source == "pickcenter" else None

        home_won = meta["home_score"] > meta["away_score"]
        y = 1 if home_won else 0
        kfee = KalshiFees.from_series({"fee_type": "quadratic_with_maker_fees", "fee_multiplier": fee_mult})
        rfee = RobinhoodFees(exchange="rothera")
        rows: list[ReplayRow] = []
        k_arb_minutes: set[int] = set()
        x_arb_minutes: set[int] = set()
        best_k, best_x = None, None
        try:
            from .models.wp import home_win_probability
        except Exception:
            home_win_probability = None  # type: ignore[assignment]
        accepted = _accepted_kwargs(home_win_probability) if home_win_probability else None
        first_ko_home = None  # receive_2h_ko_home: the team that kicked off receives the 2H kickoff
        first_real = next((p for p in plays if not p.synthetic), None)
        if first_real is not None and first_real.play_class == "kickoff" and first_real.period == 1 and first_real.possession in ("home", "away"):
            # Kickoff rows carry the receiver: the home team receives the 2H kick iff the away team received the opening one.
            first_ko_home = first_real.possession == "away"
        season = season_of(summary, kickoff)
        spread_model = self._spread_for_model(spread_home)
        n_all = len(plays)

        def model_at(p: PlayRow, after: bool, spread: float, mdl: Any) -> Optional[float]:
            gsr = p.gsr_after if after else p.game_seconds_remaining
            if gsr is None:
                return None  # no game clock (college OT): no model
            timeouts = {"home_timeouts": p.home_timeouts, "away_timeouts": p.away_timeouts} if p.home_timeouts is not None and p.away_timeouts is not None else {}
            state = dict(home_score=p.home_score_after, away_score=p.away_score_after, possession=p.possession_after, down=p.down_after, distance=p.distance_after, yardline_100=p.yardline_100_after) if after else dict(home_score=p.home_score, away_score=p.away_score, possession=p.possession, down=p.down, distance=p.distance, yardline_100=p.yardline_100)
            if after:
                # A scoring play's after-state is the kickoff-pending state (history.espn_timeline
                # builds it that way), so give the model the same class hint the synthetic
                # kickoff_pending row gets; every other after-state is a plain scrimmage state.
                play_class = "kickoff" if (p.scoring_play and p.down_after is None) else None
            else:
                play_class = MODEL_PLAY_CLASS.get(p.play_class or "", p.play_class)
            return _call_model(home_win_probability, accepted, game_seconds_remaining=gsr, vegas_spread_home=spread, receive_2h_ko_home=first_ko_home, model=mdl, play_class=play_class, overtime=p.overtime, season=season, **timeouts, **state)

        for idx, p in enumerate(plays):
            in_play = in_play_gate(p.period, p.game_seconds_remaining, p.home_score, p.away_score, p.play_class, last=(idx == n_all - 1))
            model_p = model_at(p, False, spread_model, self.model)
            model_after_p = model_at(p, True, spread_model, self.model)
            model_zero = model_at(p, False, 0.0, self.model) if spread_source not in ("pickcenter",) and spread_home is not None else None
            extra = {name: model_at(p, False, spread_model, m) for name, m in self.extra_models.items()}
            # Both Kalshi alignments; ``bar_mode`` decides which one is the headline.
            bh_b, ba_b = bar_at(kh, p.ts, "kalshi_before"), bar_at(ka, p.ts, "kalshi_before")
            bh_a, ba_a = bar_at(kh, p.ts, "kalshi"), bar_at(ka, p.ts, "kalshi")
            bh_s = bar_at(kh, bh_a.ts + 1, "kalshi") if bh_a else None
            ba_s = bar_at(ka, ba_a.ts + 1, "kalshi") if ba_a else None
            k_before, k_before_spread = self._kalshi_mid(bh_b, ba_b)
            k_after, k_after_spread = self._kalshi_mid(bh_a, ba_a)
            bh, ba = (bh_b, ba_b) if self.bar_mode == "before" else (bh_a, ba_a)
            kalshi_p, k_spread = (k_before, k_before_spread) if self.bar_mode == "before" else (k_after, k_after_spread)
            rb_h = bar_at(rh.get(rh_contracts.get("home", ""), []), p.ts, "start") if rh_contracts else None
            rb_a = bar_at(rh.get(rh_contracts.get("away", ""), []), p.ts, "start") if rh_contracts else None
            robinhood_p = rb_h.close if rb_h and rb_h.close is not None else ((1 - rb_a.close) if rb_a and rb_a.close is not None else None)
            pb_h = bar_at(pm.get("home", []), p.ts, "start") if pm else None
            pb_a = bar_at(pm.get("away", []), p.ts, "start") if pm else None
            polymarket_p = pb_h.close if pb_h and pb_h.close is not None else ((1 - pb_a.close) if pb_a and pb_a.close is not None else None)
            mids = [v for v in (kalshi_p, robinhood_p, polymarket_p) if v is not None]
            market_p = sum(mids) / len(mids) if mids else None
            mids_after = [v for v in (k_after, robinhood_p, polymarket_p) if v is not None]
            market_after_p = sum(mids_after) / len(mids_after) if mids_after else None
            b = blended_fair({home: market_p, away: (1 - market_p)} if market_p is not None else None, model_p, p.espn_home_wp, home, away, live=True, market_confidence=market_confidence_from_spread(k_spread), sport=self.sport)
            b_after = blended_fair({home: market_after_p, away: (1 - market_after_p)} if market_after_p is not None else None, model_after_p, p.espn_home_wp, home, away, live=True, market_confidence=market_confidence_from_spread(k_after_spread), sport=self.sport)
            # Arbitrage checks at this minute (executable prices: the after bar).
            k_margin = x_margin = None
            if not p.synthetic and bh_a and ba_a and bh_a.ask is not None and ba_a.ask is not None:
                k_margin = evaluate([Leg(home, "kalshi", bh_a.ask, kfee), Leg(away, "kalshi", ba_a.ask, kfee)], 100).margin
                if k_margin > 0:
                    k_arb_minutes.add(int(p.ts // 60))
                    best_k = max(best_k or -1, k_margin)
            if not p.synthetic and bh_a and ba_a and rb_h and rb_a and bh_a.ask is not None and ba_a.ask is not None and rb_h.close is not None and rb_a.close is not None:
                m1 = evaluate([Leg(home, "kalshi", bh_a.ask, kfee), Leg(away, "robinhood", rb_a.close, rfee)], 100).margin
                m2 = evaluate([Leg(home, "robinhood", rb_h.close, rfee), Leg(away, "kalshi", ba_a.ask, kfee)], 100).margin
                x_margin = max(m1, m2)
                if x_margin > 0:
                    x_arb_minutes.add(int(p.ts // 60))
                    best_x = max(best_x or -1, x_margin)
            rows.append(ReplayRow(
                ts=p.ts, period=p.period, clock=p.clock_seconds, home_score=p.home_score, away_score=p.away_score, possession=p.possession,
                model_p=model_p, espn_p=p.espn_home_wp, kalshi_p=kalshi_p, robinhood_p=robinhood_p, polymarket_p=polymarket_p, market_p=market_p, blend_p=b.home_p, disagreement=b.disagreement,
                kalshi_arb_margin=k_margin, cross_arb_margin=x_margin, text=p.text, kalshi_spread=k_spread, kalshi_home_ask=bh.ask if bh else None, kalshi_away_ask=ba.ask if ba else None,
                kalshi_before_p=k_before, kalshi_after_p=k_after, kalshi_before_spread=k_before_spread, kalshi_after_spread=k_after_spread,
                kalshi_before_home_ask=bh_b.ask if bh_b else None, kalshi_before_away_ask=ba_b.ask if ba_b else None, kalshi_after_home_ask=bh_a.ask if bh_a else None, kalshi_after_away_ask=ba_a.ask if ba_a else None,
                kalshi_shift_home_ask=bh_s.ask if bh_s else None, kalshi_shift_away_ask=ba_s.ask if ba_s else None,
                model_after_p=model_after_p, market_after_p=market_after_p, blend_after_p=b_after.home_p, model_p_zero_spread=model_zero,
                play_class=p.play_class, slice=slice_label(p.period, p.game_seconds_remaining, p.overtime), down=p.down, lead_bucket=lead_bucket(p.home_score, p.away_score), quarter=p.period or None, secs_bucket=secs_bucket(p.game_seconds_remaining),
                pickcenter_spread=pickcenter_spread, home_timeouts=p.home_timeouts, away_timeouts=p.away_timeouts, gsr=p.game_seconds_remaining, in_play=in_play, synthetic=p.synthetic, extra_model_p=extra,
            ))
        real = [r for r in rows if not r.synthetic]
        inplay = [r for r in real if r.in_play]
        scrim = [r for r in inplay if r.play_class in HEADLINE_CLASSES]
        keys = ("model", "model_after", "espn", "kalshi", "kalshi_before", "kalshi_after", "robinhood", "polymarket", "market", "blend", "blend_after")
        metrics = {k: _scores([getattr(r, f"{k}_p") for r in real if getattr(r, f"{k}_p") is not None], y) for k in keys}
        metrics["_inplay_only"] = {k: _scores([getattr(r, f"{k}_p") for r in inplay if getattr(r, f"{k}_p") is not None], y) for k in keys}
        metrics["_scrimmage"] = {k: _scores([getattr(r, f"{k}_p") for r in scrim if getattr(r, f"{k}_p") is not None], y) for k in keys}
        return ReplayResult(
            espn_event_id=espn_event_id, home=home, away=away, home_won=home_won, final=f"{away} {meta['away_score']}-{meta['home_score']} {home}",
            kickoff=kickoff.isoformat() if kickoff else None, n_plays=len(real), n_inplay=len(inplay), metrics=metrics,
            arb_minutes={"kalshi_book_minutes": len(k_arb_minutes), "kalshi_best_margin": best_k, "cross_kalshi_robinhood_minutes": len(x_arb_minutes), "cross_best_margin": best_x, "note": "Robinhood history is trade prices (no ask); cross-venue counts are indicative only", "kalshi_tickers": tickers, "kalshi_candles": len(kh) + len(ka), "robinhood_bars": sum(len(v) for v in rh.values()), "polymarket_points": sum(len(v) for v in pm.values()), "synthetic_rows": len(rows) - len(real)},
            rows=rows, pregame_kalshi_p=round(pregame_kalshi_p, 4) if pregame_kalshi_p is not None else None, spread_home=spread_home, spread_source=spread_source, kalshi_fee_multiplier=fee_mult, sport=self.sport, bar_mode=self.bar_mode,
        )

    @staticmethod
    def _kalshi_mid(bh: Optional[Bar], ba: Optional[Bar]) -> tuple[Optional[float], Optional[float]]:
        """P(home) from the home candle (or 1 - away) and the tighter book width."""
        p = bh.mid if bh and bh.mid is not None else ((1 - ba.mid) if ba and ba.mid is not None else None)
        spread = None
        for bar in (bh, ba):
            if bar and bar.ask is not None and bar.bid is not None:
                w = round(bar.ask - bar.bid, 4)
                spread = w if spread is None else min(spread, w)
        return p, spread

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except OfflineCacheMiss:
            raise
        except Exception:
            return None


def summarize(res: ReplayResult) -> str:
    lines = [f"{res.final}  (ESPN {res.espn_event_id}, kickoff {res.kickoff}, {res.n_plays} plays, {res.n_inplay} in play, spread {res.spread_home} [{res.spread_source}], pre-kickoff Kalshi {res.pregame_kalshi_p}, bar mode {res.bar_mode})"]
    lines.append("source          n    log-loss  brier   mean P(home)   [in-play only: log-loss  brier]   [scrimmage: log-loss]")
    for k in ("model", "model_after", "espn", "kalshi_before", "kalshi_after", "robinhood", "polymarket", "market", "blend", "blend_after"):
        m = res.metrics.get(k, {})
        ip = res.metrics.get("_inplay_only", {}).get(k, {})
        sc = res.metrics.get("_scrimmage", {}).get(k, {})
        if m.get("n"):
            lines.append(f"{k:<14} {m['n']:>4}  {m['log_loss']:.4f}   {m['brier']:.4f}   {m['mean_p_home']:.3f}          {ip.get('log_loss', float('nan')):.4f}   {ip.get('brier', float('nan')):.4f}              {sc.get('log_loss', float('nan')):.4f}")
    a = res.arb_minutes
    lines.append(f"arb minutes: Kalshi book alone {a['kalshi_book_minutes']} (best {a['kalshi_best_margin']}), Kalshi x Robinhood {a['cross_kalshi_robinhood_minutes']} (best {a['cross_best_margin']}) — {a['note']}")
    lines.append(f"data: kalshi candles {a['kalshi_candles']}, robinhood bars {a['robinhood_bars']}, polymarket points {a['polymarket_points']}, synthetic rows {a.get('synthetic_rows', 0)}, fee multiplier {res.kalshi_fee_multiplier}")
    # A few sample rows across the game.
    real = [r for r in res.rows if not r.synthetic]
    step = max(1, len(real) // 8)
    lines.append("time   Q  clock  score     poss   class      model  espn   k.bef  k.aft  rh    pm    blend  dis")
    for r in real[::step]:
        t = datetime.fromtimestamp(r.ts, tz=timezone.utc).strftime("%H:%M")
        f = lambda x: "  -  " if x is None else f"{x:.3f}"  # noqa: E731
        lines.append(f"{t}  {r.period}  {(r.clock or 0) // 60:02d}:{(r.clock or 0) % 60:02d}  {r.away_score:>2}-{r.home_score:<2}   {str(r.possession or '-'):<5}  {str(r.play_class or '-'):<9}  {f(r.model_p)}  {f(r.espn_p)}  {f(r.kalshi_before_p)}  {f(r.kalshi_after_p)}  {f(r.robinhood_p)}  {f(r.polymarket_p)}  {f(r.blend_p)}  {f(r.disagreement)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# Many games: resolve venue ids automatically, pool the scores, fit the blend weights.

GAMMA = "https://gamma-api.polymarket.com"


# Polymarket's slug codes where they differ from the standard NFL abbreviations.
POLYMARKET_SLUG_CODES = {"LAR": "la", "JAX": "jax", "WSH": "was", "LV": "lv", "LAC": "lac"}


def _polymarket_event(http: Any, slug: str) -> Optional[dict]:
    try:
        events = http.get(f"{GAMMA}/events", {"slug": slug})
    except Exception:
        return None
    return (events or [None])[0]


def resolve_polymarket_tokens(http: Any, away: str, home: str, kickoff: Any, away_name: Optional[str] = None, home_name: Optional[str] = None, sport: str = "nfl") -> Optional[dict[str, str]]:
    """Moneyline CLOB token ids for an NFL game from its Gamma event slug
    ``nfl-{away}-{home}-{UTC date}`` (works for closed games; ``/markets?slug=`` does not).
    Falls back to ``/public-search`` with the team names when the codes differ."""
    import json as _json
    import re as _re

    if kickoff is None:
        return None
    if isinstance(kickoff, datetime):
        ko = kickoff
    elif isinstance(kickoff, str):
        ko = parse_iso(kickoff)
        if ko is None:
            return None
    else:
        ko = datetime.fromtimestamp(float(kickoff), tz=timezone.utc)
    date = ko.astimezone(timezone.utc).strftime("%Y-%m-%d")
    prefix = "cfb" if sport == "ncaaf" else "nfl"
    slug = f"{prefix}-{POLYMARKET_SLUG_CODES.get(away, away.lower())}-{POLYMARKET_SLUG_CODES.get(home, home.lower())}-{date}"
    ev = _polymarket_event(http, slug) if sport == "nfl" else None  # college slugs use Polymarket's own codes: search
    if ev is None and (away_name or home_name):
        try:
            # NFL: nicknames ("Lions Bills"); college: the schools' names ("Syracuse Pittsburgh") —
            # Polymarket's search does not match "Syracuse Orange Pittsburgh Panthers".
            q = f"{(away_name or away).split()[-1]} {(home_name or home).split()[-1]}" if sport == "nfl" else f"{team_name(sport, away)} {team_name(sport, home)}"
            found = http.get(f"{GAMMA}/public-search", {"q": q, "limit_per_type": 10})
        except Exception:
            found = None
        for cand in (found or {}).get("events") or []:
            cs = str(cand.get("slug") or "")
            if _re.match("^" + prefix + r"-[a-z0-9]+-[a-z0-9]+-" + _re.escape(date) + "$", cs):
                slug = cs
                ev = _polymarket_event(http, cs) or cand
                break
    if not ev:
        return None
    for _ev in (ev,):
        for m in _ev.get("markets") or []:
            if m.get("slug") != slug and (m.get("sportsMarketType") or "").lower() != "moneyline":
                continue
            try:
                outs = _json.loads(m.get("outcomes") or "[]")
                toks = _json.loads(m.get("clobTokenIds") or "[]")
            except Exception:
                continue
            if len(outs) != 2 or len(toks) != 2:
                continue
            codes = [team_code(sport, o) for o in outs]
            if home in codes and away in codes:
                return {"home": str(toks[codes.index(home)]), "away": str(toks[codes.index(away)])}
    return None


def week_games(espn: ESPNClient, season: int, week: int, history: Optional[HistoryClient] = None) -> list[dict[str, Any]]:
    """Finished games of a week: ``[{id, name, date}]`` (through the history cache when given)."""
    sb = history.espn_scoreboard_week(espn, season, week) if history is not None else espn.scoreboard_week(season, week)
    out = []
    for ev in sb.get("events") or []:
        st = ((ev.get("status") or {}).get("type") or {}).get("name") or ""
        if st == "STATUS_FINAL":
            out.append({"id": str(ev.get("id")), "name": ev.get("name"), "date": ev.get("date")})
    return out


SOURCES = ("model", "espn", "kalshi", "robinhood", "polymarket", "market", "blend")
ALIGNED_SOURCES = ("model", "model_after", "espn", "kalshi_before", "kalshi_after", "blend", "blend_after")
TIGHT_BOOK = 0.04  # Kalshi ask - bid at or under this is a "real" two-sided market


def _pred(r: ReplayRow, k: str) -> Optional[float]:
    if k == "kalshi_tight":
        return r.kalshi_p if r.kalshi_spread is not None and r.kalshi_spread <= TIGHT_BOOK else None
    if k == "kalshi_wide":
        return r.kalshi_p if r.kalshi_spread is not None and r.kalshi_spread > TIGHT_BOOK else None
    if k == "model_when_tight":  # the model on exactly the plays kalshi_tight covers (fair comparison)
        return r.model_p if r.kalshi_spread is not None and r.kalshi_spread <= TIGHT_BOOK else None
    if k.startswith("model:"):
        return (r.extra_model_p or {}).get(k[6:])
    return getattr(r, f"{k}_p", None)


POOLED_KEYS = SOURCES + ("model_after", "kalshi_before", "kalshi_after", "blend_after", "kalshi_tight", "model_when_tight", "kalshi_wide")


def _row_ok(r: ReplayRow, inplay_only: bool, play_classes: Optional[tuple[str, ...]], synthetic: bool) -> bool:
    if r.synthetic and not synthetic:
        return False
    if inplay_only and not r.in_play:
        return False
    if play_classes is not None and r.play_class not in play_classes:
        return False
    return True


def _extra_model_keys(results: list[ReplayResult]) -> list[str]:
    names: dict[str, None] = {}
    for res in results:
        for r in res.rows:
            for k in (r.extra_model_p or {}):
                names.setdefault(k, None)
    return [f"model:{k}" for k in names]


def pooled_metrics(results: list[ReplayResult], inplay_only: bool = False, play_classes: Optional[tuple[str, ...]] = None, synthetic: bool = False, keys: Optional[tuple[str, ...]] = None) -> dict[str, dict[str, float]]:
    """Log-loss / Brier over every play of every game, per source (plus Kalshi split by book
    width and any extra ``--models`` exports). Synthetic rows are excluded unless asked for."""
    out: dict[str, dict[str, float]] = {}
    for k in tuple(keys or POOLED_KEYS) + tuple(_extra_model_keys(results)):
        ll = br = 0.0
        n = 0
        games = 0
        for res in results:
            y = 1 if res.home_won else 0
            seen = False
            for r in res.rows:
                if not _row_ok(r, inplay_only, play_classes, synthetic):
                    continue
                p = _pred(r, k)
                if p is None:
                    continue
                p = min(max(p, 1e-6), 1 - 1e-6)
                ll += -(math.log(p) if y else math.log(1 - p))
                br += (p - y) ** 2
                n += 1
                seen = True
            games += 1 if seen else 0
        out[k] = {"n": n, "log_loss": round(ll / n, 4) if n else None, "brier": round(br / n, 4) if n else None, "games": games}
    return out


# ---- strata tables, class gaps, intervals --------------------------------------------------

def _cell(results: list[ReplayResult], pick: Callable[[ReplayRow], bool], keys: tuple[str, ...], min_n: int) -> dict[str, Any]:
    """One strata cell: n per source and log-loss/Brier only when n >= min_n."""
    cell: dict[str, Any] = {}
    n_rows = 0
    for res in results:
        for r in res.rows:
            if pick(r):
                n_rows += 1
    cell["n"] = n_rows
    for k in keys:
        ll = br = 0.0
        n = 0
        for res in results:
            y = 1 if res.home_won else 0
            for r in res.rows:
                if not pick(r):
                    continue
                p = _pred(r, k)
                if p is None:
                    continue
                p = min(max(p, 1e-6), 1 - 1e-6)
                ll += -(math.log(p) if y else math.log(1 - p))
                br += (p - y) ** 2
                n += 1
        cell[k] = {"n": n, "log_loss": round(ll / n, 4) if n >= min_n else None, "brier": round(br / n, 4) if n >= min_n else None}
    return cell


def strata_tables(results: list[ReplayResult], min_n: int = MIN_CELL, keys: tuple[str, ...] = ALIGNED_SOURCES) -> dict[str, Any]:
    """Per-slice (in-play, real rows) and per-play-class (in-play, synthetic included) tables
    with n and per-source log-loss + Brier; cells under ``min_n`` rows show n only."""
    slices: dict[str, Any] = {}
    for s in ("q1", "q2", "q3", "q4_early", "q4_late", "ot"):
        slices[s] = _cell(results, lambda r, s=s: r.in_play and not r.synthetic and r.slice == s, keys, min_n)
    classes: dict[str, Any] = {}
    present = sorted({r.play_class or "none" for res in results for r in res.rows if r.in_play})
    for c in present:
        classes[c] = _cell(results, lambda r, c=c: r.in_play and (r.play_class or "none") == c, keys, min_n)
    leads: dict[str, Any] = {}
    for lb in ("tie", "1-3", "4-8", "9-16", "17+"):
        leads[lb] = _cell(results, lambda r, lb=lb: r.in_play and not r.synthetic and r.lead_bucket == lb, keys, min_n)
    return {"min_n": min_n, "sources": list(keys), "by_slice": slices, "by_play_class": classes, "by_lead": leads}


def class_gaps(results: list[ReplayResult], edges: tuple[float, ...] = (0.03, 0.05, 0.08), contracts: int = 10, fee_model: Any = None) -> dict[str, Any]:
    """Per play class: mean |model - kalshi_before| and how many rows would qualify for a
    STEAL at each edge (blend AND model above the before-bar all-in + edge, the watcher's
    agreement rule). Says which classes generate the signals before P05 changes them."""
    out: dict[str, Any] = {}
    for res in results:
        kfee = fee_model or KalshiFees.from_series({"fee_type": "quadratic_with_maker_fees", "fee_multiplier": res.kalshi_fee_multiplier})
        for r in res.rows:
            if not r.in_play:
                continue
            c = r.play_class or "none"
            d = out.setdefault(c, {"n": 0, "n_gap": 0, "abs_gap_sum": 0.0, "steal": {str(e): 0 for e in edges}})
            d["n"] += 1
            if r.model_p is not None and r.kalshi_before_p is not None:
                d["n_gap"] += 1
                d["abs_gap_sum"] += abs(r.model_p - r.kalshi_before_p)
            if r.model_p is None or r.blend_p is None:
                continue
            for side, ask, fair_m, fair_b in (("home", r.kalshi_before_home_ask, r.model_p, r.blend_p), ("away", r.kalshi_before_away_ask, 1 - r.model_p, 1 - r.blend_p)):
                if ask is None or not (0 < ask < 1):
                    continue
                all_in = ask + float(kfee.fee(ask, contracts, "taker")) / contracts
                for e in edges:
                    if fair_m - all_in >= e and fair_b - all_in >= e:
                        d["steal"][str(e)] += 1
    for c, d in out.items():
        d["mean_abs_gap"] = round(d["abs_gap_sum"] / d["n_gap"], 4) if d["n_gap"] else None
        d.pop("abs_gap_sum")
    return dict(sorted(out.items()))


def _bootstrap_paired_local(diffs_by_game: list[float], B: int = 1000, seed: int = 0) -> dict[str, Any]:
    """Game-cluster bootstrap of a mean per-game difference (fallback for quant.calibration)."""
    n = len(diffs_by_game)
    if n == 0:
        return {"mean": None, "lo90": None, "hi90": None, "frac_positive": None, "n_games": 0, "sd_per_game": None}
    rng = random.Random(seed)
    means = []
    for _ in range(B):
        s = [diffs_by_game[rng.randrange(n)] for _ in range(n)]
        means.append(sum(s) / n)
    means.sort()
    mean = sum(diffs_by_game) / n
    return {"mean": round(mean, 4), "lo90": round(means[int(0.05 * B)], 4), "hi90": round(means[min(B - 1, int(0.95 * B))], 4), "frac_positive": round(sum(1 for m in means if m > 0) / B, 3), "n_games": n, "sd_per_game": round(statistics.pstdev(diffs_by_game), 4) if n > 1 else None}


def bootstrap_paired(diffs_by_game: list[float], B: int = 1000, seed: int = 0) -> dict[str, Any]:
    try:
        from .quant.calibration import bootstrap_paired as _bp  # type: ignore[attr-defined]

        got = _bp(diffs_by_game, B=B, seed=seed)
        if isinstance(got, dict) and "mean" in got:
            return got
    except Exception:
        pass
    return _bootstrap_paired_local(diffs_by_game, B, seed)


def games_needed(delta: float, sd_per_game: Optional[float], power: float = 0.8, alpha: float = 0.1) -> Optional[int]:
    """Games for a paired test to detect a per-game mean difference ``delta`` (fallback)."""
    try:
        from .quant.calibration import games_needed as _gn  # type: ignore[attr-defined]

        v = _gn(delta, sd_per_game, power=power, alpha=alpha)
        if v is not None:
            return int(v)
    except Exception:
        pass
    if not sd_per_game or not delta or sd_per_game != sd_per_game or delta != delta:  # None, 0 or NaN (one game)
        return None
    nd = statistics.NormalDist()
    z = nd.inv_cdf(1 - alpha / 2) + nd.inv_cdf(power)
    return int(math.ceil((z * sd_per_game / abs(delta)) ** 2))


def _per_game_loss_diff(results: list[ReplayResult], a: str, b: str, inplay_only: bool = True, play_classes: Optional[tuple[str, ...]] = HEADLINE_CLASSES) -> list[float]:
    """Mean log-loss(a) - log-loss(b) per game over rows where both sources exist."""
    diffs = []
    for res in results:
        y = 1 if res.home_won else 0
        tot = 0.0
        n = 0
        for r in res.rows:
            if not _row_ok(r, inplay_only, play_classes, False):
                continue
            pa, pb = _pred(r, a), _pred(r, b)
            if pa is None or pb is None:
                continue
            pa, pb = min(max(pa, 1e-6), 1 - 1e-6), min(max(pb, 1e-6), 1 - 1e-6)
            la = -(math.log(pa) if y else math.log(1 - pa))
            lb = -(math.log(pb) if y else math.log(1 - pb))
            tot += la - lb
            n += 1
        if n:
            diffs.append(tot / n)
    return diffs


INTERVAL_PAIRS = (("blend", "model"), ("model", "kalshi_before"), ("model", "kalshi_after"), ("model_after", "kalshi_after"), ("espn", "model"))


def interval_report(results: list[ReplayResult], pairs: tuple[tuple[str, str], ...] = INTERVAL_PAIRS, B: int = 1000, seed: int = 0, fit: Optional[dict[str, Any]] = None, play_classes: Optional[tuple[str, ...]] = HEADLINE_CLASSES) -> dict[str, Any]:
    """Game-cluster 90% intervals on per-game log-loss differences (negative = first source
    better) plus the games needed to resolve each at 80% power."""
    out: dict[str, Any] = {}
    for a, b in pairs:
        d = _per_game_loss_diff(results, a, b, play_classes=play_classes)
        bs = bootstrap_paired(d, B=B, seed=seed)
        bs["games_needed_at_observed"] = games_needed(bs.get("mean") or 0.0, bs.get("sd_per_game"))
        out[f"{a}_minus_{b}"] = bs
    if fit and fit.get("n") and fit.get("best") and fit.get("current"):
        d = _fit_loss_diffs(results, fit["best"], fit["current"], play_classes=play_classes)
        bs = bootstrap_paired(d, B=B, seed=seed)
        bs["games_needed_at_observed"] = games_needed(bs.get("mean") or 0.0, bs.get("sd_per_game"))
        out["best_grid_minus_current"] = bs
    return out


def _blend(wm: float, wo: float, we: float, a: float, b: float, c: float, pool: str) -> float:
    tot = wm + wo + we
    if tot <= 0:
        return 0.5
    if pool == "logit":
        lg = lambda p: math.log(min(max(p, 1e-6), 1 - 1e-6) / (1 - min(max(p, 1e-6), 1 - 1e-6)))  # noqa: E731
        z = (wm * lg(a) + wo * lg(b) + we * lg(c)) / tot
        return 1 / (1 + math.exp(-z))
    return (wm * a + wo * b + we * c) / tot


def _fit_loss_diffs(results: list[ReplayResult], best: dict[str, Any], cur: dict[str, Any], play_classes: Optional[tuple[str, ...]] = HEADLINE_CLASSES) -> list[float]:
    pool = str(best.get("pool") or "linear")
    diffs = []
    for res in results:
        y = 1 if res.home_won else 0
        tot = 0.0
        n = 0
        for r in res.rows:
            if not _row_ok(r, True, play_classes, False) or r.market_p is None or r.model_p is None or r.espn_p is None:
                continue
            pb = min(max(_blend(best["market"], best["model"], best["espn"], r.market_p, r.model_p, r.espn_p, pool), 1e-6), 1 - 1e-6)
            pc = min(max(_blend(cur["market"], cur["model"], cur["espn"], r.market_p, r.model_p, r.espn_p, "linear"), 1e-6), 1 - 1e-6)
            tot += -(math.log(pb) if y else math.log(1 - pb)) + (math.log(pc) if y else math.log(1 - pc))
            n += 1
        if n:
            diffs.append(tot / n)
    return diffs


def fit_blend_weights(results: list[ReplayResult], step: float = 0.05, inplay_only: bool = True, sport: str = "nfl", pool: str = "linear", play_classes: Optional[tuple[str, ...]] = None) -> dict[str, Any]:
    """Grid-search (market, model, espn) weights on the simplex that minimise pooled log-loss
    over plays where all three sources exist. ``pool`` is 'linear' (probability average) or
    'logit' (average of log-odds). Reports the sport's current weights too, and whether the
    live blend can pool in logit space (``blended_fair`` accepting ``pool=``)."""
    from .quant.inplay_fair import DEFAULT_WEIGHTS, SPORT_WEIGHTS

    rows = []
    for res in results:
        y = 1 if res.home_won else 0
        for r in res.rows:
            if not _row_ok(r, inplay_only, play_classes, False):
                continue
            if r.market_p is not None and r.model_p is not None and r.espn_p is not None:
                rows.append((r.market_p, r.model_p, r.espn_p, y))
    if not rows:
        return {"n": 0}

    def loss(wm: float, wo: float, we: float, pl: str = pool) -> float:
        s = 0.0
        for a, b, c, y in rows:
            p = min(max(_blend(wm, wo, we, a, b, c, pl), 1e-6), 1 - 1e-6)
            s += -(math.log(p) if y else math.log(1 - p))
        return s / len(rows)

    grid = []
    k = int(round(1 / step))
    for i in range(k + 1):
        for j in range(k + 1 - i):
            wm, wo = i * step, j * step
            we = max(0.0, 1 - wm - wo)
            grid.append((loss(wm, wo, we), round(wm, 2), round(wo, 2), round(we, 2)))
    grid.sort()
    d = SPORT_WEIGHTS.get(sport, DEFAULT_WEIGHTS)
    cur = loss(d.get("market", 0.5), d.get("model", 0.35), d.get("espn", 0.15), "linear")
    accepted = _accepted_kwargs(blended_fair)
    return {
        "n": len(rows),
        "games": len(results),
        "pool": pool,
        "logit_pool_available_live": bool(accepted is None or "pool" in accepted),
        "best": {"market": grid[0][1], "model": grid[0][2], "espn": grid[0][3], "log_loss": round(grid[0][0], 4), "pool": pool},
        "current": {"market": d.get("market"), "model": d.get("model"), "espn": d.get("espn"), "log_loss": round(cur, 4)},
        "corners": {"market": round(loss(1, 0, 0), 4), "model": round(loss(0, 1, 0), 4), "espn": round(loss(0, 0, 1), 4)},
        "top5": [{"market": g[1], "model": g[2], "espn": g[3], "log_loss": round(g[0], 4)} for g in grid[:5]],
    }


def walk_forward(weeks: list[list[ReplayResult]], sport: str = "nfl", step: float = 0.05, play_classes: Optional[tuple[str, ...]] = HEADLINE_CLASSES) -> dict[str, Any]:
    """Fit the blend on weeks 1..n-1 and score week n with those weights (out of sample)
    against the current default. Needs >= 2 weeks."""
    out: dict[str, Any] = {"weeks": [], "n_weeks": len(weeks)}
    for n in range(1, len(weeks)):
        train = [res for wk in weeks[:n] for res in wk]
        test = weeks[n]
        fit = fit_blend_weights(train, step=step, sport=sport, play_classes=play_classes)
        if not fit.get("n"):
            out["weeks"].append({"week_index": n, "n_train": 0})
            continue
        diffs = _fit_loss_diffs(test, fit["best"], fit["current"], play_classes=play_classes)
        oos = fit_blend_weights(test, step=1.0, sport=sport, play_classes=play_classes)  # only for the current-weight loss on the test week
        out["weeks"].append({"week_index": n, "n_train": fit["n"], "n_test": oos.get("n", 0), "weights": {k: fit["best"][k] for k in ("market", "model", "espn")}, "train_log_loss": fit["best"]["log_loss"], "test_current_log_loss": oos.get("current", {}).get("log_loss"), "test_fitted_minus_current": bootstrap_paired(diffs) if diffs else None})
    return out


def replay_week(season: int, week: int, replayer: Optional[GameReplayer] = None, http: Any = None, polymarket: bool = True, limit: Optional[int] = None, progress: Any = None, sport: str = "nfl", bar_mode: Optional[str] = None) -> tuple[list[ReplayResult], list[dict[str, Any]]]:
    """Replay every finished game of a week (Kalshi + Polymarket + ESPN + model; Robinhood
    history needs contract ids, which the catalogue only holds for open events)."""
    rep = replayer or GameReplayer(sport=sport, bar_mode=bar_mode or "before")
    http = http or rep.history.cached_http("gamma")
    games = week_games(rep.espn, season, week, history=rep.history)
    if limit:
        games = games[:limit]
    results: list[ReplayResult] = []
    skipped: list[dict[str, Any]] = []
    for g in games:
        try:
            summary = rep.history.espn_summary(rep.espn, g["id"])
            _, meta = espn_timeline(summary, sport=rep.sport, synthetic=False)
            home, away = team_code(rep.sport, meta["home"]) or meta["home"], team_code(rep.sport, meta["away"]) or meta["away"]
            names = str(g.get("name") or "").split(" at ")
            pm = resolve_polymarket_tokens(http, away, home, meta.get("kickoff"), away_name=names[0] if len(names) == 2 else None, home_name=names[1] if len(names) == 2 else None, sport=rep.sport) if polymarket else None
            res = rep.replay(g["id"], pm_tokens=pm, summary=summary)
            results.append(res)
            if progress:
                progress(f"{res.final:<22} plays={res.n_plays:<4} kalshi={res.arb_minutes['kalshi_candles']:<4} pm={res.arb_minutes['polymarket_points']:<4} model={res.metrics['model'].get('log_loss')} kalshi_before={res.metrics['kalshi_before'].get('log_loss')} kalshi_after={res.metrics['kalshi_after'].get('log_loss')} spread={res.spread_home}[{res.spread_source}]")
        except OfflineCacheMiss:
            raise
        except Exception as e:  # keep going; report at the end
            skipped.append({"id": g["id"], "name": g["name"], "error": repr(e)})
            if progress:
                progress(f"skip {g['name']}: {e!r}")
    return results, skipped


# ---------------------------------------------------------------------------------------------
# Strategy simulation on the replay: the watcher's STEAL / LOCK rules against Kalshi's asks.


@dataclass
class SimTrade:
    game: str
    side: str
    kind: str  # steal | lock
    period: int
    clock: Optional[int]
    ask: float
    all_in: float
    fair: float
    contracts: int


PAIRINGS = {
    # pairing -> (fair column by source, home-ask column, away-ask column)
    "primary": ({"blend": "blend_p", "model": "model_p", "market": "market_p"}, "kalshi_home_ask", "kalshi_away_ask"),
    "pre_before": ({"blend": "blend_p", "model": "model_p", "market": "market_p"}, "kalshi_before_home_ask", "kalshi_before_away_ask"),
    "post_after": ({"blend": "blend_after_p", "model": "model_after_p", "market": "market_after_p"}, "kalshi_after_home_ask", "kalshi_after_away_ask"),
    "pre_after": ({"blend": "blend_p", "model": "model_p", "market": "market_p"}, "kalshi_after_home_ask", "kalshi_after_away_ask"),  # the leaky pairing, reported for comparison
    "shift": ({"blend": "blend_after_p", "model": "model_after_p", "market": "market_after_p"}, "kalshi_shift_home_ask", "kalshi_shift_away_ask"),  # placebo: one candle later than the after bar
}


def shuffled_labels(results: list[ReplayResult], seed: int = 0) -> list[ReplayResult]:
    """Placebo: the same rows with the games' outcomes permuted (within the week)."""
    rng = random.Random(seed)
    labels = [res.home_won for res in results]
    rng.shuffle(labels)
    out = []
    for res, won in zip(results, labels):
        d = {f.name: getattr(res, f.name) for f in fields(ReplayResult)}
        d["home_won"] = won
        out.append(ReplayResult(**d))
    return out


def simulate_steal(results: list[ReplayResult], edges: tuple[float, ...] = (0.02, 0.04, 0.06, 0.10), contracts: int = 10, lock: bool = True, source: str = "blend", target_margin: float = 0.0, fee_model: Any = None, lock_fraction: float = 0.0, pairing: str = "primary", fee_multiplier: Optional[float] = None, placebo: Optional[str] = None, seed: int = 0, play_classes: Optional[tuple[str, ...]] = None, filters: Optional[Iterable[str]] = None, agreement_gap: float = 0.12) -> dict[str, Any]:
    """Replay the in-play rules against Kalshi's candle asks. One STEAL entry per game: buy
    ``contracts`` of a side when ``fair - all_in >= edge``; then LOCK the other side when its
    all-in leaves ``target_margin`` on the pair **and** the guaranteed profit is at least
    ``lock_fraction`` of the expected profit of holding; otherwise hold to settlement.

    ``filters`` names live STEAL gates to replay: ``"agreement"`` skips the STEAL entry on a
    row where ``strategy.inplay.agreement_gate`` would fire (model vs market further apart
    than ``agreement_gap`` with ESPN's win probability on the market's side; locks are not
    gated, as in play). Skipped rows are counted per edge as ``filtered``, so the with /
    without runs measure what the gate costs or saves. The live default of 0.12 is a
    *provisional* risk control chosen from one college slate (PUR @ UCLA, model 0.93 vs
    market and ESPN 0.77, blend still +11 %), not from a replay: the committed results
    fixtures are metrics-only and the week-1 cache is synthetic, so the number is to be
    measured on the first recorded Sunday (``live --record`` then ``backtest-ticks``) before
    anyone trusts it.

    ``pairing`` picks which fair meets which ask: ``pre_before`` (pre-play fair vs the
    candle before the play), ``post_after`` (post-play fair vs the candle after: the
    executable number), ``pre_after`` (the leaky pairing), ``shift`` (placebo: post-play
    fair vs one candle later still) or ``primary`` (the row's ``bar_mode`` columns).
    ``placebo='shuffle'`` permutes the games' outcomes. Kalshi taker fees on every buy use
    the series' ``fee_multiplier`` per game unless ``fee_model`` / ``fee_multiplier`` is
    given. Depth is unknown (candles), so keep ``contracts`` small."""
    fair_cols, ask_h, ask_a = PAIRINGS.get(pairing, PAIRINGS["primary"])
    fair_col = fair_cols.get(source, fair_cols["blend"])
    filters = tuple(filters or ())
    unknown = [f for f in filters if f not in ("agreement",)]
    if unknown:
        raise ValueError(f"unknown simulate_steal filter(s) {unknown}; known: agreement")
    if placebo == "shuffle":
        results = shuffled_labels(results, seed)
    out: dict[str, Any] = {"contracts": contracts, "source": source, "lock": lock, "target_margin": target_margin, "lock_fraction": lock_fraction, "pairing": pairing, "placebo": placebo, "by_edge": {}}
    if filters:
        # Only a filtered run carries the filter keys: the committed results fixtures are
        # unfiltered and byte-stable, so an unfiltered run must not change shape.
        out["filters"], out["agreement_gap"] = list(filters), (agreement_gap if "agreement" in filters else None)
    if "agreement" in filters:
        from .strategy.inplay import agreement_gate   # the live rule itself, so replay and watcher never drift
        model_col, market_col = fair_cols["model"], fair_cols["market"]
    if pairing == "pre_before" and any(res.bar_mode == "after" for res in results):
        # blend_p was built on the after candle: this pairing is then trading before-asks with after information.
        out["note"] = "results replayed with bar_mode=after: the pre-play blend already contains the after candle, so pre_before is contaminated; rerun with --bar-mode before"
    for edge in edges:
        trades: list[SimTrade] = []
        games = []
        filtered = 0
        for res in results:
            y = res.home_won
            kfee = fee_model or KalshiFees.from_series({"fee_type": "quadratic_with_maker_fees", "fee_multiplier": fee_multiplier if fee_multiplier is not None else res.kalshi_fee_multiplier})
            pos: dict[str, Optional[tuple[float, int]]] = {"home": None, "away": None}  # (cost incl. fees, contracts)
            for r in res.rows:
                if r.synthetic or not r.in_play:
                    continue
                if play_classes is not None and r.play_class not in play_classes:
                    continue
                fair_home = getattr(r, fair_col, None)
                if fair_home is None:
                    continue
                # ESPN's post-play number is the row's only espn column; the gate compares it with
                # the same-alignment model and market fairs the pairing trades on.
                gate = "agreement" in filters and agreement_gate(getattr(r, model_col, None), getattr(r, market_col, None), r.espn_p, agreement_gap)
                for side, ask, fair in (("home", getattr(r, ask_h, None), fair_home), ("away", getattr(r, ask_a, None), 1.0 - fair_home)):
                    if ask is None or not (0 < ask < 1):
                        continue
                    other = "away" if side == "home" else "home"
                    fee = float(kfee.fee(ask, contracts, "taker"))
                    all_in = ask + fee / contracts
                    if pos[side] is None and pos[other] is None and fair - all_in >= edge:
                        if gate:
                            filtered += 1
                            continue
                        pos[side] = (all_in * contracts, contracts)
                        trades.append(SimTrade(res.final, side, "steal", r.period, r.clock, ask, round(all_in, 4), round(fair, 4), contracts))
                    elif lock and pos[side] is None and pos[other] is not None and pos[other][0] + all_in * contracts <= contracts * (1.0 - target_margin):
                        guaranteed = contracts - pos[other][0] - all_in * contracts
                        ev_hold = (1.0 - fair) * contracts - pos[other][0]  # fair of the held side = 1 - fair(this side)
                        if guaranteed < lock_fraction * ev_hold:
                            continue
                        pos[side] = (all_in * contracts, contracts)
                        trades.append(SimTrade(res.final, side, "lock", r.period, r.clock, ask, round(all_in, 4), round(fair, 4), contracts))
            if pos["home"] or pos["away"]:
                cost = sum(p[0] for p in pos.values() if p)
                payout = float(contracts) if ((pos["home"] and y) or (pos["away"] and not y)) else 0.0
                games.append({"game": res.final, "cost": round(cost, 2), "payout": payout, "pnl": round(payout - cost, 2), "locked": bool(pos["home"] and pos["away"])})
        cost = sum(g["cost"] for g in games)
        pnl = sum(g["pnl"] for g in games)
        pnls = [g["pnl"] for g in games]
        bs = bootstrap_paired(pnls, B=500, seed=seed) if len(pnls) >= 2 else None
        out["by_edge"][str(edge)] = {
            "edge": edge, "games_traded": len(games), "steals": sum(1 for t in trades if t.kind == "steal"), "locks": sum(1 for t in trades if t.kind == "lock"),
            "wins": sum(1 for g in games if g["pnl"] > 0), "losses": sum(1 for g in games if g["pnl"] < 0),
            "cost": round(cost, 2), "pnl": round(pnl, 2), "roi": round(pnl / cost, 4) if cost else None,
            "pnl_per_game_90": {"lo": bs["lo90"], "hi": bs["hi90"]} if bs else None,
            "games": games, "trades": [asdict(t) for t in trades],
        }
        if filters:
            out["by_edge"][str(edge)]["filtered"] = filtered
    return out


def simulate_pairings(results: list[ReplayResult], edges: tuple[float, ...] = (0.03, 0.05, 0.08), contracts: int = 10, source: str = "blend", lock: bool = False, placebos: bool = True, seed: int = 0) -> list[dict[str, Any]]:
    """The honest pair of simulations plus placebos: pre-play fair vs before ask,
    post-play fair vs after ask (executable), the leaky pre/after pairing, the +1-candle
    shift and the game-label shuffle."""
    sims = [simulate_steal(results, edges=edges, contracts=contracts, source=source, lock=lock, pairing="pre_before", seed=seed), simulate_steal(results, edges=edges, contracts=contracts, source=source, lock=lock, pairing="post_after", seed=seed)]
    if placebos:
        sims.append(simulate_steal(results, edges=edges, contracts=contracts, source=source, lock=lock, pairing="pre_after", seed=seed))
        sims.append(simulate_steal(results, edges=edges, contracts=contracts, source=source, lock=lock, pairing="shift", seed=seed))
        sims.append(simulate_steal(results, edges=edges, contracts=contracts, source=source, lock=lock, pairing="post_after", placebo="shuffle", seed=seed))
    return sims


def summarize_sim(sim: dict[str, Any]) -> str:
    lock_desc = f"lock when guaranteed ≥ {sim.get('lock_fraction', 0):.0%} of hold EV" if sim["lock"] else "no lock (hold to settlement)"
    tag = f"pairing={sim.get('pairing', 'primary')}" + (f", placebo={sim['placebo']}" if sim.get("placebo") else "") + (f", filters={','.join(sim['filters'])} (gap {sim.get('agreement_gap')})" if sim.get("filters") else "")
    lines = [f"STEAL/LOCK simulation on Kalshi asks ({sim['contracts']} contracts per entry, fair = {sim['source']}, {lock_desc}, taker fees, {tag}):"]
    if sim.get("note"):
        lines.append(f"  note: {sim['note']}")
    lines.append("  edge   games  steals  locks  wins  losses      cost       pnl     roi    pnl/game 90%" + ("  filtered" if sim.get("filters") else ""))
    for k, v in sim["by_edge"].items():
        roi = f"{v['roi']*100:+.1f}%" if v["roi"] is not None else "  -  "
        ci = v.get("pnl_per_game_90")
        ci_s = f"[{ci['lo']:+.2f}, {ci['hi']:+.2f}]" if ci and ci.get("lo") is not None else "-"
        lines.append(f"  {v['edge']:<6.2f} {v['games_traded']:>5} {v['steals']:>7} {v['locks']:>6} {v['wins']:>5} {v['losses']:>7}  {v['cost']:>9.2f} {v['pnl']:>9.2f}  {roi:>7}  {ci_s}" + (f"  {v.get('filtered', 0):>8}" if sim.get("filters") else ""))
    return "\n".join(lines)


# ---- persisted results: load, pool, report ---------------------------------------------------

def _from_dict(cls: Any, d: dict) -> Any:
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})


def load_results(path: str) -> tuple[list[ReplayResult], dict[str, Any]]:
    """Read a ``--json`` file back into ReplayResults (unknown keys ignored; missing new
    columns default). Returns (results, header without games)."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    games = data.get("games") or []
    results = []
    for g in games:
        rows = [_from_dict(ReplayRow, r) for r in (g.get("rows") or [])]
        gd = dict(g)
        gd["rows"] = rows
        results.append(_from_dict(ReplayResult, gd))
    header = {k: v for k, v in data.items() if k != "games"}
    return results, header


def pool_weeks(paths: list[str], sport: str = "nfl", B: int = 1000, seed: int = 0) -> dict[str, Any]:
    """Strata, intervals and games-needed over >= 2 persisted weeks, plus the walk-forward
    blend fit (weeks 1..n-1 -> week n)."""
    weeks: list[list[ReplayResult]] = []
    headers = []
    for p in paths:
        res, hdr = load_results(p)
        weeks.append(res)
        headers.append({"path": p, "week": hdr.get("week"), "season": hdr.get("season"), "games": len(res)})
    allres = [r for wk in weeks for r in wk]
    fit = fit_blend_weights(allres, sport=sport, play_classes=HEADLINE_CLASSES) if allres else {"n": 0}
    return {
        "weeks": headers, "n_games": len(allres), "n_weeks": len(weeks),
        "pooled_inplay": pooled_metrics(allres, inplay_only=True, play_classes=HEADLINE_CLASSES),
        "strata": strata_tables(allres),
        "class_gaps": class_gaps(allres),
        "fit": fit,
        "intervals": interval_report(allres, B=B, seed=seed, fit=fit),
        "walk_forward": walk_forward(weeks, sport=sport) if len(weeks) >= 2 else {"n_weeks": len(weeks), "weeks": []},
    }


def week_report(results: list[ReplayResult], fit: Optional[dict[str, Any]] = None, sims: Optional[list[dict[str, Any]]] = None, B: int = 1000, seed: int = 0, slices: bool = True) -> dict[str, Any]:
    """Metrics-only report of a replayed week (no per-row data): pooled numbers under both
    alignments, strata tables, class gaps, intervals and the simulations' by-edge totals."""
    rep: dict[str, Any] = {
        "n_games": len(results),
        "n_rows": sum(r.n_plays for r in results),
        "n_inplay": sum(r.n_inplay for r in results),
        "spread_sources": dict(sorted({s: sum(1 for r in results if r.spread_source == s) for s in {r.spread_source for r in results}}.items())),
        "pooled_all": pooled_metrics(results),
        "pooled_inplay": pooled_metrics(results, inplay_only=True),
        "pooled_scrimmage": pooled_metrics(results, inplay_only=True, play_classes=HEADLINE_CLASSES),
        "lineless_q1q2": lineless_report(results),
        "fit": fit,
        "intervals": interval_report(results, B=B, seed=seed, fit=fit),
    }
    if slices:
        rep["strata"] = strata_tables(results)
        rep["class_gaps"] = class_gaps(results)
    if sims:
        rep["simulations"] = [{"pairing": s.get("pairing"), "placebo": s.get("placebo"), "source": s.get("source"), "lock": s.get("lock"), "by_edge": {e: {k: v for k, v in d.items() if k not in ("games", "trades")} for e, d in s["by_edge"].items()}} for s in sims]
    return rep


def lineless_report(results: list[ReplayResult]) -> dict[str, Any]:
    """Q1/Q2 log-loss of the model on games without a pickcenter line, with the fallback
    spread vs at spread 0 (what the old replay fed)."""
    with_fb: list[float] = []
    zero: list[float] = []
    ys: list[int] = []
    games = 0
    for res in results:
        if res.spread_source == "pickcenter":
            continue
        games += 1
        y = 1 if res.home_won else 0
        for r in res.rows:
            if r.synthetic or not r.in_play or r.slice not in ("q1", "q2") or r.model_p is None or r.model_p_zero_spread is None:
                continue
            with_fb.append(r.model_p)
            zero.append(r.model_p_zero_spread)
            ys.append(y)

    def ll(ps: list[float]) -> Optional[float]:
        if not ps:
            return None
        return round(-sum(math.log(min(max(p, 1e-6), 1 - 1e-6)) if y else math.log(1 - min(max(p, 1e-6), 1 - 1e-6)) for p, y in zip(ps, ys)) / len(ps), 4)

    return {"games": games, "n": len(with_fb), "log_loss_spread_zero": ll(zero), "log_loss_with_fallback": ll(with_fb)}


def summarize_many(results: list[ReplayResult], skipped: list[dict[str, Any]], fit: Optional[dict[str, Any]] = None, sims: Optional[list[dict[str, Any]]] = None, report: Optional[dict[str, Any]] = None) -> str:
    lines = [f"{len(results)} games replayed, {sum(r.n_plays for r in results)} plays" + (f", {len(skipped)} skipped" if skipped else "")]
    for res in results:
        m = lambda k: _fmt((res.metrics.get(k) or {}).get("log_loss"))  # noqa: E731
        lines.append(f"  {res.final:<22} {res.n_plays:>4} plays  model {m('model')}  espn {m('espn')}  kalshi {m('kalshi_before')}-{m('kalshi_after')}  poly {m('polymarket')}  blend {m('blend')}  spread {res.spread_home} [{res.spread_source}]  kalshi-arb-min {res.arb_minutes.get('kalshi_book_minutes')}")
    for title, kw in (("all plays", {}), ("in play only (score can still change)", {"inplay_only": True}), ("in play, scrimmage plays only (headline)", {"inplay_only": True, "play_classes": HEADLINE_CLASSES})):
        pm = pooled_metrics(results, **kw)
        lines.append(f"pooled — {title}:")
        lines.append("  source            games     n   log-loss   brier")
        for k, m in pm.items():
            label = {"kalshi_tight": f"kalshi ≤{TIGHT_BOOK*100:.0f}¢ book", "model_when_tight": "model (same plays)", "kalshi_wide": f"kalshi >{TIGHT_BOOK*100:.0f}¢ book"}.get(k, k)
            lines.append(f"  {label:<18} {m['games']:>4} {m['n']:>6}   {_fmt(m['log_loss'])}   {_fmt(m['brier'])}")
    pm = pooled_metrics(results, inplay_only=True, play_classes=HEADLINE_CLASSES)
    kb, ka, mo = pm["kalshi_before"]["log_loss"], pm["kalshi_after"]["log_loss"], pm["model"]["log_loss"]
    if kb is not None and ka is not None and mo is not None:
        lines.append(f"model-vs-market (in-play scrimmage): model {mo:.4f} vs Kalshi {min(kb, ka):.4f}-{max(kb, ka):.4f} (before / after the play); the truth is inside that range")
    if fit and fit.get("n"):
        b, c, co = fit["best"], fit["current"], fit["corners"]
        lines.append(f"blend weights ({fit.get('pool', 'linear')} pool, in play, {fit['n']} plays with all three sources): best market {b['market']} / model {b['model']} / espn {b['espn']} -> {b['log_loss']}; current {c['market']} / {c['model']} / {c['espn']} -> {c['log_loss']}; market-only {co['market']}, model-only {co['model']}, espn-only {co['espn']}")
    if report:
        iv = report.get("intervals") or {}
        for k, v in iv.items():
            if v and v.get("mean") is not None:
                lines.append(f"  {k:<32} mean {v['mean']:+.4f}  90% [{v['lo90']:+.4f}, {v['hi90']:+.4f}]  games {v['n_games']}  games needed {v.get('games_needed_at_observed')}")
        ll = report.get("lineless_q1q2") or {}
        if ll.get("n"):
            lines.append(f"  line-less games {ll['games']}: Q1/Q2 model log-loss at spread 0 {ll['log_loss_spread_zero']} -> with fallback spread {ll['log_loss_with_fallback']} ({ll['n']} rows)")
        st = report.get("strata")
        if st:
            lines.append(f"per-slice (in play; cells under {st['min_n']} rows show n only):")
            lines.append("  slice      n   " + "  ".join(f"{s:>13}" for s in st["sources"]))
            for name, cell in st["by_slice"].items():
                lines.append(f"  {name:<8} {cell['n']:>5}  " + "  ".join(f"{_fmt(cell[s]['log_loss']):>13}" for s in st["sources"]))
            lines.append("per-play-class (in play, synthetic rows included):")
            lines.append("  class                  n   " + "  ".join(f"{s:>13}" for s in st["sources"]))
            for name, cell in st["by_play_class"].items():
                lines.append(f"  {name:<20} {cell['n']:>5}  " + "  ".join(f"{_fmt(cell[s]['log_loss']):>13}" for s in st["sources"]))
        cg = report.get("class_gaps")
        if cg:
            lines.append("class               n   |model-kalshi_before|   STEAL-qualifying rows at 3% / 5% / 8%")
            for c, d in cg.items():
                st_ = d["steal"]
                lines.append(f"  {c:<18} {d['n']:>5}   {_fmt(d['mean_abs_gap']):>18}   {st_.get('0.03', 0)} / {st_.get('0.05', 0)} / {st_.get('0.08', 0)}")
    for sim in sims or []:
        lines.append(summarize_sim(sim))
    for s in skipped:
        lines.append(f"  skipped {s['name']}: {s['error']}")
    return "\n".join(lines)


def _fmt(v: Any) -> str:
    return f"{v:.4f}" if isinstance(v, (int, float)) and v is not None else "  -   "


def dump_results_json(obj: Any) -> str:
    """Canonical JSON for the committed metrics-only results fixtures (byte-for-byte diffs)."""
    return json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n"

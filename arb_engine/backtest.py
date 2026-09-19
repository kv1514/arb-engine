"""Replay a finished NFL game play by play with public price history and score every
fair-value source against the real outcome.

    arb-engine backtest --espn 401872932            # DET @ BUF, 2026-09-17

Per play (ESPN wall-clock time): reconstruct the game state, score our WP model, read
ESPN's WP, and read each venue's market at that minute (Kalshi bid/ask candles, Robinhood
5-minute trade bars, Polymarket 1-minute mids). Report log-loss / Brier per source over the
in-play window, the blend, disagreement, and how many minutes showed a fee-adjusted
arbitrage (Kalshi book alone, and Kalshi vs Robinhood using RH's last trade as its ask —
an approximation, RH history has no bid/ask).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any, Optional

from .fees.kalshi import KalshiFees
from .fees.robinhood import RobinhoodFees
from .matching.normalize import kalshi_ticker_date
from .matching.teams import nfl_team_code
from .quant.arbitrage import Leg, evaluate
from .quant.inplay_fair import blended_fair
from .venues.espn import ESPNClient
from .venues.history import Bar, HistoryClient, PlayRow, bar_at, espn_timeline


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


def _scores(pred: list[float], y: int) -> dict[str, float]:
    ps = [min(max(p, 1e-6), 1 - 1e-6) for p in pred]
    if not ps:
        return {"n": 0}
    ll = -sum(math.log(p) if y else math.log(1 - p) for p in ps) / len(ps)
    br = sum((p - y) ** 2 for p in ps) / len(ps)
    return {"n": len(ps), "log_loss": round(ll, 4), "brier": round(br, 4), "mean_p_home": round(sum(ps) / len(ps), 4)}


class GameReplayer:
    def __init__(self, espn: Optional[ESPNClient] = None, history: Optional[HistoryClient] = None, model: Any = None):
        self.espn = espn or ESPNClient()
        self.history = history or HistoryClient()
        self.model = model

    def kalshi_tickers(self, home: str, away: str, kickoff) -> tuple[str, str]:
        """KXNFLGAME-{YYMONDD}{AWAY}{HOME}-{TEAM} using the ET date of kickoff (Kalshi's convention)."""
        from .matching.normalize import et_date

        d = et_date(kickoff)
        y, m, dd = d.split("-")
        mon = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"][int(m) - 1]
        code = f"{y[2:]}{mon}{dd}{away}{home}"
        return f"KXNFLGAME-{code}-{home}", f"KXNFLGAME-{code}-{away}"

    def replay(self, espn_event_id: str, rh_contracts: Optional[dict[str, str]] = None, pm_tokens: Optional[dict[str, str]] = None, pre_minutes: int = 30, post_minutes: int = 10, kalshi_tickers: Optional[dict[str, str]] = None) -> ReplayResult:
        summary = self.espn.summary(espn_event_id)
        plays, meta = espn_timeline(summary)
        home, away = nfl_team_code(meta["home"]) or meta["home"], nfl_team_code(meta["away"]) or meta["away"]
        if not plays:
            raise RuntimeError("no plays with wall-clock timestamps in the ESPN summary")
        kickoff = meta.get("kickoff")
        t0 = min(plays[0].ts, kickoff.timestamp() if kickoff else plays[0].ts) - pre_minutes * 60
        t1 = plays[-1].ts + post_minutes * 60
        # Pre-game spread from ESPN pickcenter if present (home line, negative = home favoured).
        spread_home = None
        for pc in summary.get("pickcenter") or []:
            sp = pc.get("spread")
            if sp is not None:
                fav_home = (pc.get("homeTeamOdds") or {}).get("favorite")
                spread_home = -abs(float(sp)) if fav_home else abs(float(sp))
                break
        # Venue histories.
        tickers = kalshi_tickers or dict(zip(("home", "away"), self.kalshi_tickers(home, away, kickoff or plays[0].ts)))
        kh = self._safe(lambda: self.history.kalshi_candles(tickers["home"], int(t0), int(t1)))
        ka = self._safe(lambda: self.history.kalshi_candles(tickers["away"], int(t0), int(t1)))
        rh: dict[str, list[Bar]] = {}
        if rh_contracts:
            start_iso = (kickoff - timedelta(minutes=pre_minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z") if kickoff else None
            rh = self._safe(lambda: self.history.robinhood_bars(list(rh_contracts.values()), start_iso)) or {}
        pm: dict[str, list[Bar]] = {}
        if pm_tokens:
            for side, tok in pm_tokens.items():
                pm[side] = self._safe(lambda tok=tok: self.history.polymarket_history(tok, int(t0), int(t1))) or []

        home_won = meta["home_score"] > meta["away_score"]
        y = 1 if home_won else 0
        kfee = KalshiFees.from_series({"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1})
        rfee = RobinhoodFees(exchange="rothera")
        rows: list[ReplayRow] = []
        preds: dict[str, list[float]] = {k: [] for k in ("model", "espn", "kalshi", "robinhood", "polymarket", "market", "blend")}
        k_arb_minutes: set[int] = set()
        x_arb_minutes: set[int] = set()
        best_k, best_x = None, None
        try:
            from .models.wp import home_win_probability
        except Exception:
            home_win_probability = None  # type: ignore[assignment]
        first_ko_home = None  # receive_2h_ko_home: the team that kicked off receives the 2H kickoff
        if plays and plays[0].possession in ("home", "away"):
            first_ko_home = plays[0].possession == "away"  # away received the opening kickoff -> home kicked -> home receives 2H
        for p in plays:
            if p.game_seconds_remaining is None:
                continue
            model_p = None
            if home_win_probability is not None:
                try:
                    model_p = home_win_probability(home_score=p.home_score, away_score=p.away_score, game_seconds_remaining=p.game_seconds_remaining, possession=p.possession, down=p.down, distance=p.distance, yardline_100=p.yardline_100, vegas_spread_home=spread_home or 0.0, receive_2h_ko_home=first_ko_home, model=self.model)
                except Exception:
                    model_p = None
            bh, ba = bar_at(kh or [], p.ts, "kalshi"), bar_at(ka or [], p.ts, "kalshi")
            kalshi_p = bh.mid if bh and bh.mid is not None else ((1 - ba.mid) if ba and ba.mid is not None else None)
            rb_h = bar_at(rh.get(rh_contracts.get("home", ""), []), p.ts, "start") if rh_contracts else None
            rb_a = bar_at(rh.get(rh_contracts.get("away", ""), []), p.ts, "start") if rh_contracts else None
            robinhood_p = rb_h.close if rb_h and rb_h.close is not None else ((1 - rb_a.close) if rb_a and rb_a.close is not None else None)
            pb_h = bar_at(pm.get("home", []), p.ts, "start") if pm else None
            pb_a = bar_at(pm.get("away", []), p.ts, "start") if pm else None
            polymarket_p = pb_h.close if pb_h and pb_h.close is not None else ((1 - pb_a.close) if pb_a and pb_a.close is not None else None)
            mids = [v for v in (kalshi_p, robinhood_p, polymarket_p) if v is not None]
            market_p = sum(mids) / len(mids) if mids else None
            b = blended_fair({home: market_p, away: (1 - market_p) if market_p is not None else None} if market_p is not None else None, model_p, p.espn_home_wp, home, away, live=True)
            # Arbitrage checks at this minute.
            k_margin = x_margin = None
            if bh and ba and bh.ask is not None and ba.ask is not None:
                k_margin = evaluate([Leg(home, "kalshi", bh.ask, kfee), Leg(away, "kalshi", ba.ask, kfee)], 100).margin
                if k_margin > 0:
                    k_arb_minutes.add(int(p.ts // 60))
                    best_k = max(best_k or -1, k_margin)
            if bh and ba and rb_h and rb_a and bh.ask is not None and ba.ask is not None and rb_h.close is not None and rb_a.close is not None:
                m1 = evaluate([Leg(home, "kalshi", bh.ask, kfee), Leg(away, "robinhood", rb_a.close, rfee)], 100).margin
                m2 = evaluate([Leg(home, "robinhood", rb_h.close, rfee), Leg(away, "kalshi", ba.ask, kfee)], 100).margin
                x_margin = max(m1, m2)
                if x_margin > 0:
                    x_arb_minutes.add(int(p.ts // 60))
                    best_x = max(best_x or -1, x_margin)
            rows.append(ReplayRow(ts=p.ts, period=p.period, clock=p.clock_seconds, home_score=p.home_score, away_score=p.away_score, possession=p.possession, model_p=model_p, espn_p=p.espn_home_wp, kalshi_p=kalshi_p, robinhood_p=robinhood_p, polymarket_p=polymarket_p, market_p=market_p, blend_p=b.home_p, disagreement=b.disagreement, kalshi_arb_margin=k_margin, cross_arb_margin=x_margin, text=p.text))
            for k, v in (("model", model_p), ("espn", p.espn_home_wp), ("kalshi", kalshi_p), ("robinhood", robinhood_p), ("polymarket", polymarket_p), ("market", market_p), ("blend", b.home_p)):
                if v is not None:
                    preds[k].append(v)
        # Decided plays (score can no longer change) are trivial for every source; report both.
        inplay = [r for r in rows if r.period and (r.period < 4 or (r.clock or 0) > 0)]
        metrics = {k: _scores(v, y) for k, v in preds.items()}
        metrics["_inplay_only"] = {k: _scores([getattr(r, f"{k}_p") for r in inplay if getattr(r, f"{k}_p") is not None], y) for k in ("model", "espn", "kalshi", "robinhood", "polymarket", "market", "blend")}
        return ReplayResult(
            espn_event_id=espn_event_id, home=home, away=away, home_won=home_won, final=f"{away} {meta['away_score']}-{meta['home_score']} {home}",
            kickoff=kickoff.isoformat() if kickoff else None, n_plays=len(rows), n_inplay=len(inplay), metrics=metrics,
            arb_minutes={"kalshi_book_minutes": len(k_arb_minutes), "kalshi_best_margin": best_k, "cross_kalshi_robinhood_minutes": len(x_arb_minutes), "cross_best_margin": best_x, "note": "Robinhood history is trade prices (no ask); cross-venue counts are indicative only", "kalshi_tickers": tickers, "kalshi_candles": len(kh or []) + len(ka or []), "robinhood_bars": sum(len(v) for v in rh.values()), "polymarket_points": sum(len(v) for v in pm.values())},
            rows=rows,
        )

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except Exception:
            return None


def summarize(res: ReplayResult) -> str:
    lines = [f"{res.final}  (ESPN {res.espn_event_id}, kickoff {res.kickoff}, {res.n_plays} plays, {res.n_inplay} in play)"]
    lines.append("source        n    log-loss  brier   mean P(home)   [in-play only: log-loss  brier]")
    for k in ("model", "espn", "kalshi", "robinhood", "polymarket", "market", "blend"):
        m = res.metrics.get(k, {})
        ip = res.metrics.get("_inplay_only", {}).get(k, {})
        if m.get("n"):
            lines.append(f"{k:<12} {m['n']:>4}  {m['log_loss']:.4f}   {m['brier']:.4f}   {m['mean_p_home']:.3f}          {ip.get('log_loss', float('nan')):.4f}   {ip.get('brier', float('nan')):.4f}")
    a = res.arb_minutes
    lines.append(f"arb minutes: Kalshi book alone {a['kalshi_book_minutes']} (best {a['kalshi_best_margin']}), Kalshi x Robinhood {a['cross_kalshi_robinhood_minutes']} (best {a['cross_best_margin']}) — {a['note']}")
    lines.append(f"data: kalshi candles {a['kalshi_candles']}, robinhood bars {a['robinhood_bars']}, polymarket points {a['polymarket_points']}")
    # A few sample rows across the game.
    step = max(1, len(res.rows) // 8)
    lines.append("time   Q  clock  score     poss   model  espn   kalshi  rh    pm    blend  dis")
    for r in res.rows[::step]:
        from datetime import datetime, timezone
        t = datetime.fromtimestamp(r.ts, tz=timezone.utc).strftime("%H:%M")
        f = lambda x: "  -  " if x is None else f"{x:.3f}"  # noqa: E731
        lines.append(f"{t}  {r.period}  {(r.clock or 0) // 60:02d}:{(r.clock or 0) % 60:02d}  {r.away_score:>2}-{r.home_score:<2}   {str(r.possession or '-'):<5}  {f(r.model_p)}  {f(r.espn_p)}  {f(r.kalshi_p)}  {f(r.robinhood_p)}  {f(r.polymarket_p)}  {f(r.blend_p)}  {f(r.disagreement)}")
    return "\n".join(lines)

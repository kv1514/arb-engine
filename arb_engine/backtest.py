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
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .fees.kalshi import KalshiFees
from .fees.robinhood import RobinhoodFees
from .matching.normalize import kalshi_ticker_date
from .matching.teams import nfl_team_code, team_code, team_name, team_table
from .quant.arbitrage import Leg, evaluate
from .quant.inplay_fair import blended_fair, market_confidence_from_spread
from .venues.espn import ESPNClient
from .matching.normalize import parse_iso
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
    kalshi_spread: Optional[float] = None  # ask - bid of the Kalshi candle used (book width)
    kalshi_home_ask: Optional[float] = None  # executable prices at the candle close after the play
    kalshi_away_ask: Optional[float] = None


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


# Kalshi's ticker codes where they differ from the standard NFL abbreviations.
KALSHI_TICKER_CODES = {"JAX": "JAC"}
KALSHI_GAME_SERIES = {"nfl": "KXNFLGAME", "ncaaf": "KXNCAAFGAME"}


class GameReplayer:
    def __init__(self, espn: Optional[ESPNClient] = None, history: Optional[HistoryClient] = None, model: Any = None, sport: str = "nfl"):
        self.sport = sport
        self.espn = espn or ESPNClient(sport=sport)
        self.history = history or HistoryClient()
        self.model = model

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

    def replay(self, espn_event_id: str, rh_contracts: Optional[dict[str, str]] = None, pm_tokens: Optional[dict[str, str]] = None, pre_minutes: int = 30, post_minutes: int = 10, kalshi_tickers: Optional[dict[str, str]] = None) -> ReplayResult:
        summary = self.espn.summary(espn_event_id)
        plays, meta = espn_timeline(summary)
        home, away = team_code(self.sport, meta["home"]) or meta["home"], team_code(self.sport, meta["away"]) or meta["away"]
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
            k_spread = None
            for bar in (bh, ba):
                if bar and bar.ask is not None and bar.bid is not None:
                    k_spread = round(bar.ask - bar.bid, 4) if k_spread is None else min(k_spread, round(bar.ask - bar.bid, 4))
            b = blended_fair({home: market_p, away: (1 - market_p) if market_p is not None else None} if market_p is not None else None, model_p, p.espn_home_wp, home, away, live=True, market_confidence=market_confidence_from_spread(k_spread), sport=self.sport)
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
            rows.append(ReplayRow(ts=p.ts, period=p.period, clock=p.clock_seconds, home_score=p.home_score, away_score=p.away_score, possession=p.possession, model_p=model_p, espn_p=p.espn_home_wp, kalshi_p=kalshi_p, robinhood_p=robinhood_p, polymarket_p=polymarket_p, market_p=market_p, blend_p=b.home_p, disagreement=b.disagreement, kalshi_arb_margin=k_margin, cross_arb_margin=x_margin, text=p.text, kalshi_spread=k_spread, kalshi_home_ask=bh.ask if bh else None, kalshi_away_ask=ba.ask if ba else None))
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


def week_games(espn: ESPNClient, season: int, week: int) -> list[dict[str, Any]]:
    """Finished games of a week: ``[{id, name, date}]``."""
    sb = espn.scoreboard_week(season, week)
    out = []
    for ev in sb.get("events") or []:
        st = ((ev.get("status") or {}).get("type") or {}).get("name") or ""
        if st == "STATUS_FINAL":
            out.append({"id": str(ev.get("id")), "name": ev.get("name"), "date": ev.get("date")})
    return out


SOURCES = ("model", "espn", "kalshi", "robinhood", "polymarket", "market", "blend")
TIGHT_BOOK = 0.04  # Kalshi ask - bid at or under this is a "real" two-sided market


def _pred(r: ReplayRow, k: str) -> Optional[float]:
    if k == "kalshi_tight":
        return r.kalshi_p if r.kalshi_spread is not None and r.kalshi_spread <= TIGHT_BOOK else None
    if k == "kalshi_wide":
        return r.kalshi_p if r.kalshi_spread is not None and r.kalshi_spread > TIGHT_BOOK else None
    if k == "model_when_tight":  # the model on exactly the plays kalshi_tight covers (fair comparison)
        return r.model_p if r.kalshi_spread is not None and r.kalshi_spread <= TIGHT_BOOK else None
    return getattr(r, f"{k}_p")


POOLED_KEYS = SOURCES + ("kalshi_tight", "model_when_tight", "kalshi_wide")


def pooled_metrics(results: list[ReplayResult], inplay_only: bool = False) -> dict[str, dict[str, float]]:
    """Log-loss / Brier over every play of every game, per source (plus Kalshi split by book width)."""
    out: dict[str, dict[str, float]] = {}
    for k in POOLED_KEYS:
        ll = br = 0.0
        n = 0
        for res in results:
            y = 1 if res.home_won else 0
            for r in res.rows:
                if inplay_only and not (r.period and (r.period < 4 or (r.clock or 0) > 0)):
                    continue
                p = _pred(r, k)
                if p is None:
                    continue
                p = min(max(p, 1e-6), 1 - 1e-6)
                ll += -(math.log(p) if y else math.log(1 - p))
                br += (p - y) ** 2
                n += 1
        out[k] = {"n": n, "log_loss": round(ll / n, 4) if n else None, "brier": round(br / n, 4) if n else None, "games": sum(1 for res in results if any(_pred(r, k) is not None for r in res.rows))}
    return out


def fit_blend_weights(results: list[ReplayResult], step: float = 0.05, inplay_only: bool = True) -> dict[str, Any]:
    """Grid-search (market, model, espn) weights on the simplex that minimise pooled log-loss
    over plays where all three sources exist. Reports the current default too."""
    from .quant.inplay_fair import DEFAULT_WEIGHTS

    rows = []
    for res in results:
        y = 1 if res.home_won else 0
        for r in res.rows:
            if inplay_only and not (r.period and (r.period < 4 or (r.clock or 0) > 0)):
                continue
            if r.market_p is not None and r.model_p is not None and r.espn_p is not None:
                rows.append((r.market_p, r.model_p, r.espn_p, y))
    if not rows:
        return {"n": 0}

    def loss(wm: float, wo: float, we: float) -> float:
        tot = wm + wo + we
        s = 0.0
        for a, b, c, y in rows:
            p = min(max((wm * a + wo * b + we * c) / tot, 1e-6), 1 - 1e-6)
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
    d = DEFAULT_WEIGHTS
    cur = loss(d.get("market", 0.5), d.get("model", 0.35), d.get("espn", 0.15))
    return {
        "n": len(rows),
        "games": len(results),
        "best": {"market": grid[0][1], "model": grid[0][2], "espn": grid[0][3], "log_loss": round(grid[0][0], 4)},
        "current": {"market": d.get("market"), "model": d.get("model"), "espn": d.get("espn"), "log_loss": round(cur, 4)},
        "corners": {"market": round(loss(1, 0, 0), 4), "model": round(loss(0, 1, 0), 4), "espn": round(loss(0, 0, 1), 4)},
        "top5": [{"market": g[1], "model": g[2], "espn": g[3], "log_loss": round(g[0], 4)} for g in grid[:5]],
    }


def replay_week(season: int, week: int, replayer: Optional[GameReplayer] = None, http: Any = None, polymarket: bool = True, limit: Optional[int] = None, progress: Any = None, sport: str = "nfl") -> tuple[list[ReplayResult], list[dict[str, Any]]]:
    """Replay every finished game of a week (Kalshi + Polymarket + ESPN + model; Robinhood
    history needs contract ids, which the catalogue only holds for open events)."""
    rep = replayer or GameReplayer(sport=sport)
    http = http or rep.history.http
    games = week_games(rep.espn, season, week)
    if limit:
        games = games[:limit]
    results: list[ReplayResult] = []
    skipped: list[dict[str, Any]] = []
    for g in games:
        try:
            summary = rep.espn.summary(g["id"])
            _, meta = espn_timeline(summary)
            home, away = team_code(rep.sport, meta["home"]) or meta["home"], team_code(rep.sport, meta["away"]) or meta["away"]
            names = str(g.get("name") or "").split(" at ")
            pm = resolve_polymarket_tokens(http, away, home, meta.get("kickoff"), away_name=names[0] if len(names) == 2 else None, home_name=names[1] if len(names) == 2 else None, sport=rep.sport) if polymarket else None
            res = rep.replay(g["id"], pm_tokens=pm)
            results.append(res)
            if progress:
                progress(f"{res.final:<22} plays={res.n_plays:<4} kalshi={res.arb_minutes['kalshi_candles']:<4} pm={res.arb_minutes['polymarket_points']:<4} model={res.metrics['model'].get('log_loss')} kalshi_ll={res.metrics['kalshi'].get('log_loss')}")
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


def simulate_steal(results: list[ReplayResult], edges: tuple[float, ...] = (0.02, 0.04, 0.06, 0.10), contracts: int = 10, lock: bool = True, source: str = "blend", target_margin: float = 0.0, fee_model: Any = None, lock_fraction: float = 0.0) -> dict[str, Any]:
    """Replay the in-play rules against Kalshi's candle-close asks (the bar *after* each play,
    so the market has already seen it). One STEAL entry per game: buy ``contracts`` of a side
    when ``fair - all_in >= edge``; then LOCK the other side when its all-in leaves
    ``target_margin`` on the pair **and** the guaranteed profit is at least ``lock_fraction``
    of the expected profit of holding (``fair_held * N - cost``); otherwise hold to
    settlement. Kalshi taker fees on every buy, none at settlement. Depth is unknown
    (candles), so keep ``contracts`` small."""
    kfee = fee_model or KalshiFees.from_series({"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1})
    out: dict[str, Any] = {"contracts": contracts, "source": source, "lock": lock, "target_margin": target_margin, "lock_fraction": lock_fraction, "by_edge": {}}
    for edge in edges:
        trades: list[SimTrade] = []
        games = []
        for res in results:
            y = res.home_won
            pos: dict[str, Optional[tuple[float, int]]] = {"home": None, "away": None}  # (cost incl. fees, contracts)
            for r in res.rows:
                if not (r.period and (r.period < 4 or (r.clock or 0) > 0)):
                    continue
                fair_home = r.blend_p if source == "blend" else (r.model_p if source == "model" else r.market_p)
                if fair_home is None:
                    continue
                for side, ask, fair in (("home", r.kalshi_home_ask, fair_home), ("away", r.kalshi_away_ask, 1.0 - fair_home)):
                    if ask is None or not (0 < ask < 1):
                        continue
                    other = "away" if side == "home" else "home"
                    fee = float(kfee.fee(ask, contracts, "taker"))
                    all_in = ask + fee / contracts
                    if pos[side] is None and pos[other] is None and fair - all_in >= edge:
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
        out["by_edge"][str(edge)] = {
            "edge": edge, "games_traded": len(games), "steals": sum(1 for t in trades if t.kind == "steal"), "locks": sum(1 for t in trades if t.kind == "lock"),
            "wins": sum(1 for g in games if g["pnl"] > 0), "losses": sum(1 for g in games if g["pnl"] < 0),
            "cost": round(cost, 2), "pnl": round(pnl, 2), "roi": round(pnl / cost, 4) if cost else None,
            "games": games, "trades": [asdict(t) for t in trades],
        }
    return out


def summarize_sim(sim: dict[str, Any]) -> str:
    lock_desc = f"lock when guaranteed ≥ {sim.get('lock_fraction', 0):.0%} of hold EV" if sim["lock"] else "no lock (hold to settlement)"
    lines = [f"STEAL/LOCK simulation on Kalshi asks ({sim['contracts']} contracts per entry, fair = {sim['source']}, {lock_desc}, taker fees):"]
    lines.append("  edge   games  steals  locks  wins  losses      cost       pnl     roi")
    for k, v in sim["by_edge"].items():
        roi = f"{v['roi']*100:+.1f}%" if v["roi"] is not None else "  -  "
        lines.append(f"  {v['edge']:<6.2f} {v['games_traded']:>5} {v['steals']:>7} {v['locks']:>6} {v['wins']:>5} {v['losses']:>7}  {v['cost']:>9.2f} {v['pnl']:>9.2f}  {roi:>7}")
    return "\n".join(lines)




def summarize_many(results: list[ReplayResult], skipped: list[dict[str, Any]], fit: Optional[dict[str, Any]] = None, sims: Optional[list[dict[str, Any]]] = None) -> str:
    lines = [f"{len(results)} games replayed, {sum(r.n_plays for r in results)} plays" + (f", {len(skipped)} skipped" if skipped else "")]
    for res in results:
        lines.append(f"  {res.final:<22} {res.n_plays:>4} plays  model {_fmt(res.metrics['model'].get('log_loss'))}  espn {_fmt(res.metrics['espn'].get('log_loss'))}  kalshi {_fmt(res.metrics['kalshi'].get('log_loss'))}  poly {_fmt(res.metrics['polymarket'].get('log_loss'))}  blend {_fmt(res.metrics['blend'].get('log_loss'))}  kalshi-arb-min {res.arb_minutes['kalshi_book_minutes']}")
    for title, inplay in (("all plays", False), ("in play only (score can still change)", True)):
        pm = pooled_metrics(results, inplay_only=inplay)
        lines.append(f"pooled — {title}:")
        lines.append("  source            games     n   log-loss   brier")
        for k in POOLED_KEYS:
            m = pm[k]
            label = {"kalshi_tight": f"kalshi ≤{TIGHT_BOOK*100:.0f}¢ book", "model_when_tight": "model (same plays)", "kalshi_wide": f"kalshi >{TIGHT_BOOK*100:.0f}¢ book"}.get(k, k)
            lines.append(f"  {label:<18} {m['games']:>4} {m['n']:>6}   {_fmt(m['log_loss'])}   {_fmt(m['brier'])}")
    if fit and fit.get("n"):
        b, c, co = fit["best"], fit["current"], fit["corners"]
        lines.append(f"blend weights (in play, {fit['n']} plays with all three sources): best market {b['market']} / model {b['model']} / espn {b['espn']} -> {b['log_loss']}; current {c['market']} / {c['model']} / {c['espn']} -> {c['log_loss']}; market-only {co['market']}, model-only {co['model']}, espn-only {co['espn']}")
    for sim in sims or []:
        lines.append(summarize_sim(sim))
    for s in skipped:
        lines.append(f"  skipped {s['name']}: {s['error']}")
    return "\n".join(lines)


def _fmt(v: Any) -> str:
    return f"{v:.4f}" if isinstance(v, (int, float)) and v is not None else "  -   "

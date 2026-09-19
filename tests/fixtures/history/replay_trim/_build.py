"""Regenerate the trimmed replay cache in this directory (deterministic, offline).

    PYTHONPATH=. python tests/fixtures/history/replay_trim/_build.py   # rewrites the cache files
    python -m arb_engine backtest --week 1 --season 2026 --offline --cache-dir tests/fixtures/history/replay_trim \
        --no-polymarket --slices --placebo --results-json tests/fixtures/results/week1_p03.json

Two synthetic NFL games in the ``HistoryClient`` cache layout (``espn/``, ``kalshi/``): a
week scoreboard with two finals, one ESPN-shaped summary per game (~35 rows each with
kickoffs, a TD with its PAT folded in and ESPN's real scoring ``end`` block ``{down -1,
yardLine 0, yardsToEndzone 0, team: scorer}``, field goals, punts, timeouts, the two-minute
warning, end-of-quarter rows and kneels), a pickcenter block for game 1 only (game 2
exercises the spread fallback chain), an ESPN win-probability entry per play, and
1-minute Kalshi candles for both tickers whose mid follows the last play *ending at or
before the candle* (so the before / after alignments differ by construction). The numbers
are not real prices; the fixture exists so the per-slice / per-class report is
reproducible byte for byte.
"""

from __future__ import annotations

import json
import math
import os
import random
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

GAMES = [
    # id, away, away_id, home, home_id, kickoff, seed, pickcenter(home spread, home ml, away ml) or None
    ("401872656", "NE", "17", "SEA", "26", "2026-09-10T00:20Z", 74, (-3.5, -180, 150)),
    ("401872657", "SF", "25", "LAR", "14", "2026-09-11T00:35Z", 40, None),
]
NAMES = {"NE": "New England Patriots", "SEA": "Seattle Seahawks", "SF": "San Francisco 49ers", "LAR": "Los Angeles Rams"}


def _clock(secs: int) -> str:
    return f"{secs // 60}:{secs % 60:02d}"


def _sig(z: float) -> float:
    return 1 / (1 + math.exp(-z))


def build_game(gid: str, away: str, away_id: str, home: str, home_id: str, kickoff_iso: str, seed: int, pickcenter):
    rng = random.Random(seed)
    ko = datetime.strptime(kickoff_iso, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
    wall = ko + timedelta(seconds=30)
    ids = {"home": home_id, "away": away_id}
    abbr = {"home": home, "away": away}
    score = {"home": 0, "away": 0}
    tos = {"home": 3, "away": 3}
    plays: list[dict] = []
    seq = [0]
    period, clock = 1, 900
    opening_receiver = "away" if rng.random() < 0.5 else "home"
    prior_home = 0.60 if pickcenter else 0.42

    def other(s: str) -> str:
        return "away" if s == "home" else "home"

    def yardline(poss: str, yl100: int) -> int:
        return yl100 if poss == "away" else 100 - yl100

    def poss_text(poss: str, yl100: int) -> str:
        own = yl100 > 50
        side = abbr[poss] if own else abbr[other(poss)]
        n = 100 - yl100 if own else yl100
        return f"{side} {n}" if yl100 != 50 else "50"

    def add(text: str, type_id: str, type_text: str, poss: str, yl100: int, down: int, dist: int, end_poss: str, end_yl: int, end_down: int, end_dist: int, scoring: bool = False, wall_step: int = 40) -> dict:
        nonlocal wall
        seq[0] += 1
        wall = wall + timedelta(seconds=wall_step + rng.randrange(0, 20))
        p = {
            "id": f"{gid}{seq[0] * 37:04d}", "sequenceNumber": str(seq[0] * 100), "text": text,
            "awayScore": score["away"], "homeScore": score["home"], "period": {"number": period}, "clock": {"displayValue": _clock(clock)},
            "scoringPlay": scoring, "wallclock": wall.strftime("%Y-%m-%dT%H:%M:%SZ"), "type": {"id": type_id, "text": type_text},
            "start": {"down": down, "distance": dist, "yardLine": yardline(poss, yl100), "yardsToEndzone": yl100, "team": {"id": ids[poss]}, "possessionText": poss_text(poss, yl100), "downDistanceText": f"{down} & {dist} at {poss_text(poss, yl100)}" if down else ""},
            "end": {"down": end_down, "distance": end_dist, "yardLine": yardline(end_poss, end_yl), "yardsToEndzone": end_yl, "team": {"id": ids[end_poss]}, "possessionText": poss_text(end_poss, end_yl)},
        }
        if end_yl == 0:
            # ESPN's real shape for a score: the scorer "on the goal line" with down -1 and no
            # possession text (tests/fixtures/espn/summary_401872932.json, the DET TD at 1:42 Q4).
            p["end"] = {"down": -1, "distance": end_dist, "yardLine": 0, "yardsToEndzone": 0, "team": {"id": ids[end_poss]}}
        plays.append(p)
        return p

    def kickoff(kicker: str) -> str:
        rec = other(kicker)
        add(f"K.{abbr[kicker]} kicks 65 yards from {abbr[kicker]} 35 to end zone, Touchback.", "53", "Kickoff", kicker, 65, 0, 0, rec, 70, 1, 10)
        return rec

    def tick(secs: int) -> bool:
        """Advance the game clock; returns True when the quarter ended."""
        nonlocal clock
        clock = max(0, clock - secs)
        return clock == 0

    poss = kickoff(other(opening_receiver))
    yl100, down, dist = 70, 1, 10
    two_min_done = set()
    game_over = False
    while not game_over:
        # Administrative rows first.
        if period in (2, 4) and clock <= 120 and period not in two_min_done and clock > 0:
            two_min_done.add(period)
            add("Two-Minute Warning", "75", "Two-minute warning", poss, yl100, down, dist, poss, yl100, down, dist, wall_step=20)
        trailing = other(max(score, key=lambda s: score[s])) if score["home"] != score["away"] else None
        if period == 4 and clock < 240 and trailing and tos[trailing] > 0 and rng.random() < 0.35:
            tos[trailing] -= 1
            add(f"Timeout #{3 - tos[trailing]} by {abbr[trailing]} at {_clock(clock).zfill(5)}.", "21", "Timeout", poss, yl100, down, dist, poss, yl100, down, dist, wall_step=15)
        leader = other(trailing) if trailing else None
        if period == 4 and clock <= 100 and leader == poss and rng.random() < 0.8:
            add(f"Q.{abbr[poss]} kneels to {poss_text(poss, yl100 + 1)} for -1 yards.", "5", "Rush", poss, yl100, down, dist, poss, yl100 + 1, min(down + 1, 4), dist + 1)
            yl100 += 1
            down = min(down + 1, 4)
            dist += 1
            if tick(40):
                game_over = True
            continue
        # A scrimmage play.
        is_pass = rng.random() < 0.55
        gain = int(rng.gauss(9, 11)) if is_pass else int(rng.gauss(5, 5))
        if rng.random() < 0.12:
            gain = rng.randrange(22, 48)  # explosive play
        if is_pass and rng.random() < 0.3:
            gain = 0
        gain = max(-8, min(gain, yl100))
        new_yl = yl100 - gain
        if new_yl <= 0:
            score[poss] += 7
            add(f"Q.{abbr[poss]} pass deep middle to W.{abbr[poss]} for {gain} yards, TOUCHDOWN. K.{abbr[poss]} extra point is GOOD." if is_pass else f"R.{abbr[poss]} up the middle for {gain} yards, TOUCHDOWN. K.{abbr[poss]} extra point is GOOD.", "67" if is_pass else "68", "Passing Touchdown" if is_pass else "Rushing Touchdown", poss, yl100, down, dist, poss, 0, -1, 10, scoring=True)
            if tick(rng.randrange(80, 130)):
                pass
            if period == 4 and clock == 0:
                game_over = True
                continue
            poss = kickoff(poss)
            yl100, down, dist = 70, 1, 10
            if tick(10):
                pass
        else:
            if gain == 0 and is_pass:
                text = f"Q.{abbr[poss]} pass incomplete short right to W.{abbr[poss]}."
                tid, ttext = "3", "Pass Incompletion"
            elif is_pass:
                text = f"Q.{abbr[poss]} pass short left to W.{abbr[poss]} to {poss_text(poss, new_yl)} for {gain} yards."
                tid, ttext = "24", "Pass Reception"
            else:
                text = f"R.{abbr[poss]} right tackle to {poss_text(poss, new_yl)} for {gain} yards."
                tid, ttext = "5", "Rush"
            if gain >= dist:
                n_down, n_dist = 1, 10
            else:
                n_down, n_dist = down + 1, dist - gain
            if n_down > 4:
                # Fourth down failed: turnover on downs handled as a punt / FG decision below instead.
                n_down = 4
            if down == 4:
                if yl100 <= 35:
                    made = rng.random() < 0.85
                    if made:
                        score[poss] += 3
                    add(f"K.{abbr[poss]} {yl100 + 17} yard field goal is {'GOOD' if made else 'NO GOOD'}, Center-C.X, Holder-H.Y.", "59" if made else "60", "Field Goal Good" if made else "Field Goal Missed", poss, yl100, down, dist, other(poss) if not made else poss, 100 - (yl100 + 7) if not made else 0, 1 if not made else -1, 10, scoring=made)
                    if tick(rng.randrange(60, 120)):
                        pass
                    if period == 4 and clock == 0:
                        game_over = True
                        continue
                    if made:
                        poss = kickoff(poss)
                        yl100, down, dist = 70, 1, 10
                    else:
                        poss, yl100, down, dist = other(poss), 100 - (yl100 + 7), 1, 10
                else:
                    punt = rng.randrange(38, 52)
                    land = max(1, yl100 - punt)
                    add(f"P.{abbr[poss]} punts {punt} yards to {poss_text(other(poss), 100 - land)}, Center-C.X.", "52", "Punt", poss, yl100, down, dist, other(poss), 100 - land, 1, 10)
                    poss, yl100, down, dist = other(poss), 100 - land, 1, 10
                    if tick(rng.randrange(60, 120)):
                        pass
            else:
                add(text, tid, ttext, poss, yl100, down, dist, poss, new_yl, n_down, n_dist)
                yl100, down, dist = new_yl, n_down, min(n_dist, new_yl)
                if tick(rng.randrange(75, 150)):
                    pass
        if clock == 0:
            if period == 4:
                game_over = True
            else:
                add(f"End of {['1st', '2nd', '3rd'][period - 1]} Quarter", "2", "End Period", poss, yl100, down, dist, poss, yl100, down, dist, wall_step=25)
                period += 1
                clock = 900
                if period == 3:
                    tos = {"home": 3, "away": 3}
                    poss = kickoff(other(opening_receiver))  # the team that kicked to open receives the 2H kick
                    yl100, down, dist = 70, 1, 10
    add("END GAME", "66", "End of Game", poss, yl100, down, dist, poss, yl100, down, dist, wall_step=20)
    # ESPN win probability: a pre-game entry then one per play (post-play state).
    wp = [{"homeWinPercentage": round(prior_home, 4), "tiePercentage": 0.0, "playId": f"{gid}1"}]
    for p in plays:
        per, clk = p["period"]["number"], int(p["clock"]["displayValue"].split(":")[0]) * 60 + int(p["clock"]["displayValue"].split(":")[1])
        gsr = (4 - per) * 900 + clk
        margin = p["homeScore"] - p["awayScore"]
        z = math.log(prior_home / (1 - prior_home)) * (gsr / 3600) + 0.16 * margin * math.sqrt(3600 / (gsr + 45)) + (0.25 if p["end"]["team"]["id"] == home_id else -0.25) * (gsr > 0)
        if gsr == 0 and margin != 0:
            z = 12 if margin > 0 else -12
        wp.append({"homeWinPercentage": round(_sig(z), 4), "tiePercentage": 0.0, "playId": p["id"]})
    summary = {
        "header": {"id": gid, "season": {"year": 2026, "type": 2}, "week": 1, "competitions": [{"id": gid, "date": kickoff_iso, "status": {"type": {"id": "3", "name": "STATUS_FINAL", "state": "post", "completed": True}}, "competitors": [
            {"id": home_id, "homeAway": "home", "winner": score["home"] > score["away"], "score": str(score["home"]), "team": {"id": home_id, "abbreviation": home, "displayName": NAMES[home]}},
            {"id": away_id, "homeAway": "away", "winner": score["away"] > score["home"], "score": str(score["away"]), "team": {"id": away_id, "abbreviation": away, "displayName": NAMES[away]}},
        ]}]},
        "winprobability": wp,
        "drives": {"previous": [{"id": f"{gid}d1", "plays": plays}]},
    }
    if pickcenter:
        sp, hml, aml = pickcenter
        summary["pickcenter"] = [{"details": f"{home} {sp}", "spread": sp, "provider": {"id": "100", "name": "Draft Kings"}, "homeTeamOdds": {"favorite": sp < 0, "moneyLine": hml}, "awayTeamOdds": {"favorite": sp > 0, "moneyLine": aml}, "pointSpread": {"home": {"close": {"line": str(sp)}}}, "moneyline": {"home": {"close": {"odds": str(hml)}}, "away": {"close": {"odds": f"+{aml}"}}}}]
    return summary, wp, prior_home


def candles(summary: dict, wp: list[dict], prior_home: float, ticker_is_home: bool, start_ts: int, end_ts: int, seed: int) -> dict:
    """1-minute candles whose mid follows ESPN's WP of the last play ending at or before the candle."""
    rng = random.Random(seed + (0 if ticker_is_home else 1))
    plays = summary["drives"]["previous"][0]["plays"]
    wp_by_id = {w["playId"]: w["homeWinPercentage"] for w in wp}
    events = sorted((datetime.strptime(p["wallclock"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp(), wp_by_id[p["id"]]) for p in plays)
    out = []
    t = (start_ts // 60 + 1) * 60
    while t <= end_ts:
        p = prior_home
        for ets, v in events:
            if ets <= t:
                p = v
            else:
                break
        p = p if ticker_is_home else 1 - p
        p = min(0.98, max(0.02, p + rng.gauss(0, 0.012)))
        width = rng.choice([0.02, 0.02, 0.03, 0.04, 0.06])
        bid = round(max(0.01, p - width / 2), 2)
        ask = round(min(0.99, bid + width), 2)
        close = round((bid + ask) / 2, 2)
        out.append({"end_period_ts": t, "yes_bid": {"close_dollars": f"{bid:.4f}"}, "yes_ask": {"close_dollars": f"{ask:.4f}"}, "price": {"close_dollars": f"{close:.4f}"}, "volume_fp": f"{rng.randrange(0, 400)}.00", "open_interest_fp": "10000.00"})
        t += 60
    return {"candlesticks": out, "ticker": "home" if ticker_is_home else "away"}


class _CandleHttp:
    """Serves generated candles for any candlesticks URL and the series object."""

    def __init__(self, by_ticker: dict[str, tuple[dict, list, float, bool, int]]):
        self.by_ticker = by_ticker

    def get(self, url: str, params=None, headers=None, raw=False):
        if url.endswith("/candlesticks"):
            ticker = url.split("/markets/")[1].split("/")[0]
            summary, wp, prior, is_home, seed = self.by_ticker[ticker]
            return candles(summary, wp, prior, is_home, int(params["start_ts"]), int(params["end_ts"]), seed)
        if "/series/" in url:
            return {"series": {"ticker": "KXNFLGAME", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}}
        raise AssertionError(url)


def main() -> None:
    from arb_engine.backtest import GameReplayer
    from arb_engine.venues.espn import ESPNClient
    from arb_engine.venues.history import HistoryClient

    events = []
    summaries = {}
    by_ticker = {}
    for gid, away, away_id, home, home_id, ko, seed, pc in GAMES:
        summary, wp, prior = build_game(gid, away, away_id, home, home_id, ko, seed, pc)
        summaries[gid] = summary
        events.append({"id": gid, "name": f"{NAMES[away]} at {NAMES[home]}", "date": ko, "status": {"type": {"id": "3", "name": "STATUS_FINAL", "state": "post", "completed": True}}, "competitions": [{"id": gid, "date": ko, "competitors": [{"homeAway": "home", "team": {"abbreviation": home}}, {"homeAway": "away", "team": {"abbreviation": away}}]}]})
        th, ta = GameReplayer.kalshi_tickers(None, home, away, datetime.strptime(ko, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc))
        by_ticker[th] = (summary, wp, prior, True, seed)
        by_ticker[ta] = (summary, wp, prior, False, seed)
    scoreboard = {"week": {"number": 1}, "season": {"year": 2026, "type": 2}, "events": events}

    class _Espn:
        sport = "nfl"

        def scoreboard_week(self, season, week, seasontype=2):
            return scoreboard

        def summary(self, event_id):
            return summaries[event_id]

    # Wipe and refill the cache through the real client so keys match what the replay asks for.
    for sub in ("espn", "kalshi"):
        d = os.path.join(HERE, sub)
        if os.path.isdir(d):
            for f in os.listdir(d):
                os.remove(os.path.join(d, f))
    hist = HistoryClient(http=_CandleHttp(by_ticker), cache_dir=HERE)
    rep = GameReplayer(espn=_Espn(), history=hist, sport="nfl")
    from arb_engine.backtest import replay_week

    results, skipped = replay_week(2026, 1, replayer=rep, polymarket=False)
    assert not skipped, skipped
    print(f"wrote cache for {len(results)} games: " + ", ".join(f"{r.final} ({r.n_plays} plays)" for r in results))


if __name__ == "__main__":
    main()

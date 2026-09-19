# Roadmap

Ordered by expected value for the NFL/tennis focus.

1. ~~Spreads and totals~~ — done 2026-09-16 for the NFL on all three venues (every line,
   canonical `spread:<FAV>-<line>` / `total:<line>` keys, overlay support for Robinhood's
   Spread/Totals pages). Next: first-half/quarter lines (`KXNFL1HSPREAD`, Polymarket
   `first_half_spreads`, Rothera `KXNFL1H…` routed to Kalshi), team totals, NCAAF lines.
2. **Depth everywhere.** Kalshi and Polymarket books are wired (`--books` = second pass for
   candidate events, rate-limited); Robinhood only exposes top-of-book size. Show the sized
   result in the overlay via the bridge.
3. ~~Maker strategy runner~~ — done 2026-09-18 (`python -m arb_engine maker`, see README):
   paper / demo / live brokers, batched Kalshi polling, hedge alerts, journal. Next: auto-hedge
   on Polymarket once the CLOB client is wired; partial-fill handling in paper mode; a
   queue-position estimate from the order book (rest only when expected fill time is short);
   run the demo broker against a demo API key to verify the V2 order/fill field names.
3b. ~~In-play model~~ — done 2026-09-18: ESPN live game state (`venues/espn.py`) + XGBoost
   WP model on nflverse play-by-play (`models/wp.py`, `docs/MODEL.md`), blended with the
   market in `quant/inplay_fair.py`; `arb-engine games` / `inplay --espn`; overlay LIVE strip.
   Next: retrain each week (`scripts/train_wp_model.py`), add injuries/weather features, and
   back-test the STEAL signal on journaled ticks.
4. **Streaming quotes.** Kalshi websocket (`wss://api.elections.kalshi.com/trade-api/ws/v2`)
   and Polymarket market channel instead of polling; the Robinhood quotes API polls fine at
   ~2 s.
5. **Polymarket execution.** `py-clob-client` with a dedicated wallet; keep the same gates.
6. **Sportsbook consensus.** The Odds API `h2h` lines → `devig_power` → weight into
   `consensus_fair_value` (weights already parameterised). Adds a "true" fair value that
   does not depend on the exchanges themselves.
7. **Tennis specifics.** Retirement/walkover rule table per venue; ITF/challenger
   coverage on Polymarket via tag ids; player-name canonicalisation with a small alias file.
8. ~~NCAAF~~ — done 2026-09-18: `data/ncaaf_teams.json` (761 programs from ESPN + venue codes,
   `scripts/build_ncaaf_teams.py`), `scan/live/games --sport ncaaf` across Kalshi, Polymarket
   (`cfb`) and Robinhood's CDNA-routed college games. College spreads/totals from Kalshi
   and Robinhood/CDNA are in (8,689 line events on 2026-09-18). Still open: NBA/NHL tables the
   same way (`KXNBAGAME`/`KXNHLGAME`, Robinhood `nba`/`nhl`), Polymarket college spreads/totals.
9. ~~History & backtest~~ — done 2026-09-18: `scan --record out/history.db` / `inplay --record`
   persist every event, quote and in-play tick to SQLite (`arb_engine/store.py`);
   `python -m arb_engine backtest --espn <id> …` replays a finished game play-by-play
   against Kalshi 1-min candles, Robinhood 5-min bars and Polymarket price history and
   scores every source (model / ESPN / each venue / blend) by log-loss and Brier, plus
   counts the minutes an arb existed; `backtest --week N` does a whole week and fits the
   blend weights (week 1: model beats every market, weights moved to 0.30/0.55/0.15). `arb-engine record --every 300`
   keeps scanning into SQLite and `arb-engine stats` reports the arb share by market type ×
   hours-to-kickoff and the length of each arb episode — run it through a game week to learn
   when the tail-line arbs appear and how long they last.
10. ~~Category-page badges~~ — done 2026-09-18: on `/prediction-markets/<category>/` every
    game card's price buttons get `fair · max` badges (first 16 games, `ARB +x%` when one
    exists); worker `analyzeMany` with a small concurrency and per-URL cache.

# Roadmap

Ordered by expected value for the NFL/tennis focus.

1. **Spreads and totals.** Kalshi `KXNFLSPREAD`/`KXNFLTOTAL` (`floor_strike`), Polymarket
   `spreads`/`totals` (`line`), Rothera `NFLSPREAD-…`/`NFLTOTAL-…`. Match on the exact line;
   half-point lines only (pushes differ by venue).
2. **Depth everywhere.** Kalshi and Polymarket books are wired (`--books`); Robinhood only
   exposes top-of-book size. Use `size_from_books` in the overlay via the bridge.
3. **Maker strategy runner.** Post resting orders at `max_buy_maker` on Kalshi (post-only),
   watch fills via `GET /portfolio/fills`, hedge the other leg on the cheapest venue, with
   per-event exposure limits. Dry-run first; demo environment second.
4. **Streaming quotes.** Kalshi websocket (`wss://api.elections.kalshi.com/trade-api/ws/v2`)
   and Polymarket market channel instead of polling; the Robinhood quotes API polls fine at
   ~2 s.
5. **Polymarket execution.** `py-clob-client` with a dedicated wallet; keep the same gates.
6. **Sportsbook consensus.** The Odds API `h2h` lines → `devig_power` → weight into
   `consensus_fair_value` (weights already parameterised). Adds a "true" fair value that
   does not depend on the exchanges themselves.
7. **Tennis specifics.** Retirement/walkover rule table per venue; ITF/challenger
   coverage on Polymarket via tag ids; player-name canonicalisation with a small alias file.
8. **NCAAF/NBA/NHL alias tables** like `data/nfl_teams.json`.
9. **History & backtest.** Persist scans (SQLite) to measure how often and how long
   fee-adjusted arbs exist, by sport, venue pair and time-to-kickoff.
10. **Category-page badges** in the extension (annotate every game card on
    `/prediction-markets/nfl/`), not only the event page.

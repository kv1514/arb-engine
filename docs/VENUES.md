# Venue facts (verified 2026-09-15)

Everything the engine assumes about each venue, with the source it was taken from. When a
venue changes a schedule, update the model, this file, `tests/test_fees.py` and rerun
`python scripts/gen_fee_vectors.py`.

## Kalshi (direct)

| Item | Value | Source |
|---|---|---|
| REST base | `https://api.elections.kalshi.com/trade-api/v2` (prod), `https://demo-api.kalshi.co/trade-api/v2` (demo) | docs.kalshi.com |
| Auth | RSA-PSS (SHA-256, MGF1, max salt) over `timestamp_ms + METHOD + path`; headers `KALSHI-ACCESS-KEY/-SIGNATURE/-TIMESTAMP`; path includes `/trade-api/v2`, excludes query | docs.kalshi.com, verified against mcp-server-kalshi |
| Taker fee | `round_up(M × 0.07 × C × P × (1−P))` | Fee schedule PDF, "Last updated and effective: July 7, 2026" |
| Maker fee | `round_up(M × 0.0175 × C × P × (1−P))` on series with `fee_type = quadratic_with_maker_fees` (KXNFLGAME, KXNFLSPREAD, KXNFLTOTAL, KXNCAAFGAME, KXATPMATCH, KXWTAMATCH, KXNBAGAME, KXNHLGAME, KXMLBGAME (M=0.5), KXEPLGAME, …). Props/quarters/futures are `quadratic` = taker-only | Fee schedule PDF + `GET /series` (`fee_type`, `fee_multiplier`) |
| Rounding | Schedule text says "rounded up such that fee + positionCost is rounded to a centicent"; its worked table still rounds to the cent (100 @ $0.05 → $0.34). Engine defaults to **cent** (conservative); `KALSHI_FEE_ROUNDING=centicent` switches. Check one of your own fills to settle it | Fee schedule PDF |
| Fee-free series | e.g. KXBTCY, KXCITRINI (`fee_multiplier = 0`) | kalshi.com/fee-schedule |
| Settlement / membership fee | none | Fee schedule PDF |
| Order API (v2) | `POST /portfolio/events/orders` with `side: bid|ask` (YES leg), `price` as 4-dp dollar string, `count` string, `time_in_force`, `post_only`, optional `exchange_index` (tennis/MLB/NBA markets live on exchange index 3) | docs.kalshi.com "Create Order (V2)" |
| Market fields | `yes_bid_dollars`, `yes_ask_dollars`, `yes_ask_size_fp` (contracts), `occurrence_datetime` (game start, NFL), `exchange_index`, `price_level_structure` | live API |
| Order book | `GET /markets/{ticker}/orderbook` → `orderbook_fp.yes_dollars` / `no_dollars` = resting **bids** `[price, size]`; YES ask = 1 − best NO bid | live API |
| NFL tie rule | "$0.50 for each team" | market `rules_secondary` |
| Browser access | The prod API returns **403 to any browser `Origin` other than kalshi.com** (extension origins included). The extension strips the header with a `declarativeNetRequest` rule and otherwise falls back to the local bridge | tested with curl |

## Robinhood event contracts (Robinhood Derivatives, LLC)

| Item | Value | Source |
|---|---|---|
| Exchanges | KalshiEX (symbols `KX…`), **Rothera Exchange and Clearing** (Robinhood/Susquehanna JV, symbols like `NFLGAME-26SEP20PHITEN-PHI`), ForecastEX, Nadex | robinhood.com event page footer + contract `exchange` field |
| Commission | `min(round_up_cent(k × P × (1−P) × C), $0.01 × C)`, k = 0.10 (0.05 with Gold). Examples (100 contracts): $0.01→$0.10/$0.05, $0.05→$0.48/$0.24, $0.25→$1.00/$0.94, $0.50→$1.00/$1.00 | Support article "Event contracts overview", pricing effective June 1, 2026 |
| Exchange fee | "up to $0.01 per contract", varies by exchange; ForecastEX embeds it in the spread (Yes+No = $1.01). Engine charges $0.01 for KalshiEX/Rothera/Nadex, $0 for ForecastEX | same article; chancemetrics.com |
| Charged on | open and close (held-to-settlement pays only the open side) | same article |
| Order types | limit only: IOC or GTD (expires 3 AM ET next day); dollar orders are IOC | Support article "Trading event contracts" |
| Public data | Category pages are SSR Next.js with `__NEXT_DATA__` (`props.pageProps.events[*].eventContracts`, `quotes`, `eventStates`): `https://robinhood.com/us/en/prediction-markets/nfl/`, `/tennis/`. Quotes: `GET https://api.robinhood.com/marketdata/event/contract/quotes/v1/?ids=…` (also `?symbols=`), ≤20 per call, unauthenticated. Events: `/prediction-markets/v1/events?ids=…`; state: `/prediction-markets/v1/event_state?event_ids=…` (`eventProgress` = date before kickoff, "Live"/"Interrupted"/… in play) | observed 2026-09-15 |
| Routing observed | NFL game winners/spreads/totals → Rothera (separate book from Kalshi); NFL props & quarters, all tennis (ATP/WTA/ITF/challengers) → KalshiEX (same book as Kalshi, higher fees) | observed 2026-09-15 |
| Tie rule (Rothera NFL) | not spelled out in the public blurb → flagged `tie-rule-unverified` | robinhood.com event page |
| Trading API | none public; the overlay is read-only | — |

## Polymarket (global CLOB)

| Item | Value | Source |
|---|---|---|
| Metadata | `https://gamma-api.polymarket.com/events?tag_slug=nfl&active=true&closed=false` (heavy: every market per event); `GET /markets?slug=nfl-{away}-{home}-{utc-date}` (5 KB, exact moneyline); `GET /public-search?q=…` | live API |
| Sports fields | `sportsMarketType` (`moneyline`, `spreads`, `totals`, `tennis_completed_match`…), `gameStartTime` (UTC), `line`, `outcomes`/`outcomePrices`/`clobTokenIds` (JSON strings), `bestBid`/`bestAsk` (outcome[0] token), `feeSchedule` | live API |
| Taker fee | `C × rate × (P × (1−P))^exponent`, USDC, 5 dp; sports `rate 0.05`, `exponent 1` → peak $1.25 per 100 shares at 50¢ | docs.polymarket.com/trading/fees; changelog 2026-07-10 (0.03 → 0.05) |
| Maker | no fee; 15% of sports taker fees rebated to makers (`rebateRate`) | same |
| Books | `GET https://clob.polymarket.com/book?token_id=…`, batch `POST /books` | live API |
| Ties | NFL moneyline "resolves 50-50"; spreads: tie goes to the underdog side; totals: exact line pushes are avoided with .5 lines | market descriptions |
| Trading | needs a wallet + `py-clob-client` (EIP-712 signed orders); not wired here | docs |

## Polymarket US (CFTC-regulated, separate product)

| Item | Value | Source |
|---|---|---|
| Fee | `θ × C × P × (1−P)`, banker's rounding to the cent; taker θ = 0.06 → **0.0695 from 2026-09-16**; maker rebate θ = −0.0125; volume rebates 10/25/50% above $250K/$1M/$10M prior-month taker volume | docs.polymarket.us/fees |

## Sportsbooks (optional, not yet wired)

Lines carry vig; use `quant.odds.devig_power` / `devig_shin` before comparing. The Odds API
(`ODDS_API_KEY`) is the planned source — see `docs/ROADMAP.md`.

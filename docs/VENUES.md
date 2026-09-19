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
| Spreads / totals | `KXNFLSPREAD-{date}{pair}-{TEAM}{⌈line⌉}` = "{team} wins by over {line} points?" (`floor_strike`, `strike_type: greater`, both teams listed); `KXNFLTOTAL-{date}{pair}-{⌈line⌉}` = "over {line} points scored?". NO = other side. Both series `quadratic_with_maker_fees`. `GET /markets?event_ticker=` returns all lines of a game in one call | live API 2026-09-16 |
| Rate limit | ~10 req/s on the public tier; the client rate-limits to 8/s (`KALSHI_RATE_LIMIT`) and backs off on 429 | observed |
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
| Routing observed | NFL game winners/spreads/totals → Rothera (separate book from Kalshi); NFL props, first-half/quarter lines and all tennis (ATP/WTA/ITF/challengers) → KalshiEX (same book as Kalshi, higher fees) | observed 2026-09-15 |
| Spreads / totals | Event types `EVENT_TYPE_SPREAD` (48 contracts: `NFLSPREAD-{date}{pair}-{TEAM}{⌊line⌋}`, "Buffalo wins by over 1.5 points", `floorStrikeValue`) and `EVENT_TYPE_TOTALS` (45 contracts: `NFLTOTAL-…-{⌊line⌋}`, "Over 49.5 points"); quotes API returns `yes_*` and `no_*` prices and sizes per contract | observed 2026-09-16 |
| Tie rule (Rothera NFL) | not spelled out in the public blurb → flagged `tie-rule-unverified` | robinhood.com event page |
| Trading API | none public; the overlay is read-only | — |

## Polymarket (global CLOB)

| Item | Value | Source |
|---|---|---|
| Metadata | `https://gamma-api.polymarket.com/events?tag_slug=nfl&active=true&closed=false` (heavy: every market per event); `GET /markets?slug=nfl-{away}-{home}-{utc-date}` (5 KB, exact moneyline); `GET /public-search?q=…` | live API |
| Sports fields | `sportsMarketType` (`moneyline`, `spreads`, `totals`, `first_half_spreads`, `team_totals`, `q1_spreads`…, `tennis_completed_match`), `gameStartTime` (UTC), `line` (negative = outcome[0] is the favourite: "Spread: Bills (-1.5)" outcomes `[Bills, Lions]`), `outcomes`/`outcomePrices`/`clobTokenIds` (JSON strings), `bestBid`/`bestAsk` (outcome[0] token), `feeSchedule`. Slugs: `{event}-spread-{home|away}-{L}pt5`, `{event}-total-{L}pt5`; `GET /events?slug=` returns the whole game (all lines, ~1 MB) | live API |
| Fee schedule (observed) | `feeSchedule` is per market: NFL moneylines carried `{rate: 0.05, exponent: 1, takerOnly: true}` in the 2026 preseason, but on 2026-09-18 the week-2 moneylines (`nfl-sea-ari-2026-09-20` etc.) returned `{rate: 0, takerOnly: true}` — zero taker fee. The adapter always uses the market's own schedule, so the all-in equals the ask when the rate is 0; re-check before assuming either number | live API 2026-09-18 |
| Robinhood CDNA (college) | `EXCHANGE_SOURCE_CDNA`, symbols `NX.F.OPT.CFB-…-M.O.1.1/1.2` — a fourth routing exchange with its own book, used for college-football game winners (quotes API returns yes_ask/yes_bid/sizes like the others). Fee model: commission + $0.01/contract exchange fee assumed (`fees/robinhood.py`), unverified against an order ticket | live API 2026-09-18 |
| Tennis settlement text | Kalshi: every `KX{ATP,WTA}MATCH` market carries `rules_primary` ("wins … after a ball has been played") and `rules_secondary` (no ball played → "fair price"; postponed → open up to two weeks). Polymarket: market `description` (retirement → advancer; walkover / cancelled / >7 days → 50-50). Quoted in SPORTS.md | live API 2026-09-18 |
| Kalshi `occurrence_datetime` | Not kickoff for college games (`KXNCAAFGAME-26SEP19PURUCLA` carries 2026-09-20T06:00Z for a 03:00Z kick); the adapter keys events by the ticker date | live API 2026-09-18 |
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

## ESPN (game state — not a venue, no prices)

Unofficial, undocumented JSON the espn.com front-end uses. No key, no `Origin` restriction
observed, but ESPN can change or throttle it without notice — treat every field as
best-effort and never as a settlement source. Adapter: `arb_engine/venues/espn.py`
(`ESPNClient`, `ESPNFeed`, `GameState`). Fixtures recorded 2026-09-18 in
`tests/fixtures/espn/` (week 2: DET@BUF final `401872932`, MIN@CHI and PHI@TEN scheduled).

| Item | Value | Source |
|---|---|---|
| Scoreboard | `GET https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard[?dates=YYYYMMDD]` — whole week in one ~280 KB call. Per event: `id`, `date` (UTC, `2026-09-18T00:15Z`), `status.type.{name,state,completed}` (`STATUS_SCHEDULED`/`STATUS_IN_PROGRESS`/`STATUS_HALFTIME`/`STATUS_FINAL`…; `state` = `pre`/`in`/`post`), `status.period`, `status.displayClock` (`MM:SS`), `competitions[0].competitors[].{homeAway,score,team.abbreviation,id}`, `competitions[0].odds[0]` (pre-game only: DraftKings `details` `'CHI -4.5'`, `spread` (home-relative), `overUnder`, `pointSpread.home.close.line`, `moneyline`) | observed 2026-09-18 |
| Live-only block | `competitions[0].situation`: `possession` (team id), `down`, `distance`, `yardLine` (absolute 0–100 from the **home** goal line), `possessionText` (`'BUF 34'`), `downDistanceText`, `shortDownDistanceText`, `homeTimeouts`, `awayTimeouts`, `isRedZone`, `lastPlay.{text,probability.homeWinPercentage}`. Absent before kickoff and after the final; `competitors[].possession` (bool) also flips live | ESPN scoreboard schema; field-position convention verified on 342 plays of DET@BUF |
| Summary | `GET …/nfl/summary?event=<id>` (~630 KB raw). Used: `winprobability[]` (`homeWinPercentage`, `tiePercentage`, `playId` — ESPN's own model, one row per play, 190 rows for a full game), `pickcenter[0]` (closing DraftKings `details`/`spread`/`overUnder`/`pointSpread`/`moneyline`, present after the final too), `header.competitions[0].{status,competitors[].score/possession,situation}`, `drives.{current,previous}[].plays[].{end.down,end.distance,end.yardsToEndzone,end.team.id,text}`. Ignored: `boxscore`, `news`, `article`, `videos`, `standings`, `leaders`, `injuries` | observed 2026-09-18 |
| Derived | `status` `pre`/`live`/`final`/`other` (postponed); `game_seconds_remaining` = `(4 − period) × 900 + clock` in regulation, `clock` (≤ 600) in OT (period 5); `yardline_100` = `100 − yardLine` for home possession, `yardLine` for away (fallback: parse `possessionText`); `vegas_spread_home` negative = home favoured (`pointSpread.home.close.line` → `details` re-signed by home code → numeric `spread`); `event_key` = `nfl:<A>|<H>:<ET date>` so it joins Kalshi/Robinhood/Polymarket events | adapter |
| Cadence | Scoreboard every **10–15 s** while any game is live (one call covers all games; ESPN updates it a few seconds after each play), every 60 s otherwise; summary only on demand (WP series / closing line), at most once per 30 s per game. The client rate-limits itself to 4 req/s | recommendation |
| Caveats | Unofficial: no SLA, no changelog, fields disappear (odds vanish from the scoreboard at kickoff, `situation` at the final). `winprobability` is ESPN's model, use it as a benchmark not a feature. `yardLine` for timeouts/kickoffs is unreliable (ESPN emits `yardsToEndzone 0` on official timeouts). Clock can lag the broadcast by 5–20 s; Kalshi/Rothera in-play quotes usually move first | observed |

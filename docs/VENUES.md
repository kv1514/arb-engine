# Venue facts (verified 2026-09-15, API and rule text re-read 2026-09-19)

Everything the engine assumes about each venue, with the source it was taken from. When a
venue changes a schedule, update the model, this file, `tests/test_fees.py` and rerun
`python scripts/gen_fee_vectors.py`. Settlement rules live in `arb_engine/data/settlement_rules.json`
(below, "Settlement registry"); who may *execute* where lives in `arb_engine/data/venue_rules.json`
("Eligibility").

## Kalshi (direct)

| Item | Value | Source |
|---|---|---|
| REST base | `https://external-api.kalshi.com/trade-api/v2` (prod), `https://external-api.demo.kalshi.co/trade-api/v2` (demo). The pre-2026 hosts `api.elections.kalshi.com` / `demo-api.kalshi.co` still answer (200 on `/exchange/status`, 2026-09-19); the client falls back to them **once per session, only on a connection error** (never on a 4xx/5xx). `KALSHI_BASE_URL` pins a host and disables the fallback. `history.py` and the extension's DNR rule still target `api.elections.kalshi.com` | docs.kalshi.com (api_keys, getting started), tested 2026-09-19 |
| Auth | RSA-PSS (SHA-256, MGF1, **salt = digest length, 32 bytes**) over `timestamp_ms + METHOD + path`; headers `KALSHI-ACCESS-KEY/-SIGNATURE/-TIMESTAMP`; path includes `/trade-api/v2`, excludes query. A `MAX_LENGTH` salt (222 bytes on a 2048-bit key) is rejected by the gateway as a bad signature | docs.kalshi.com api_keys page + starter client (`padding.PSS.DIGEST_LENGTH`), 2026-09-19 |
| Taker fee | `round_up(M × 0.07 × C × P × (1−P))` | Fee schedule PDF, "Last updated and effective: July 7, 2026" |
| Maker fee | `round_up(M × 0.0175 × C × P × (1−P))` on series with `fee_type = quadratic_with_maker_fees` (KXNFLGAME, KXNFLSPREAD, KXNFLTOTAL, KXNCAAFGAME, KXATPMATCH, KXWTAMATCH, KXNBAGAME, KXNHLGAME, KXMLBGAME (M=0.5), KXEPLGAME, …). Props/quarters/futures are `quadratic` = taker-only | Fee schedule PDF + `GET /series` (`fee_type`, `fee_multiplier`) |
| Rounding | Schedule text says "rounded up such that fee + positionCost is rounded to a centicent"; its worked table still rounds to the cent (100 @ $0.05 → $0.34). Engine defaults to **cent** (conservative); `KALSHI_FEE_ROUNDING=centicent` switches. Check one of your own fills to settle it | Fee schedule PDF |
| Fee-free series | e.g. KXBTCY, KXCITRINI (`fee_multiplier = 0`) | kalshi.com/fee-schedule |
| Settlement / membership fee | none | Fee schedule PDF |
| Order API (V2 writes) | `POST /portfolio/events/orders` with `side: bid\|ask` (YES leg), `price` as 4-dp dollar string, `count` string, `time_in_force`, `post_only`, optional `exchange_index` (tennis/MLB/NBA markets live on exchange index 3), `expiration_time` as **int64 Unix seconds** (an RFC 3339 string fails validation); `DELETE /portfolio/events/orders/{id}` (flat `{order_id, reduced_by, …}`); `DELETE /portfolio/events/orders/batched` with `{"orders": [{"order_id": …}, …]}` in chunks of 20, each entry reporting `reduced_by` (a 200 with `"0.00"` is an errored cancel); `DELETE /portfolio/events/orders` cancels every resting order of the (sub)account — the kill-switch backstop | docs.kalshi.com "Create Order (V2)", "Cancel Order", "Batch Cancel Orders", 2026-09-19 |
| Order API (reads) | Reads did not move: `GET /portfolio/orders[?status=resting]` (cursor-paged), `GET /portfolio/orders/{id}` (wrapped in `{"order": …}`), `GET /portfolio/fills`. Rows carry the legacy `side`/`action` **and** `outcome_side` / `book_side` + `yes_price_dollars`; `order_side_price()` reads the canonical fields first. There is no `GET` under `/portfolio/events/` | docs.kalshi.com api-reference orders/get-orders, get-order, portfolio/get-fills, 2026-09-19 |
| Order expiry (engine) | Every resting maker order carries `expiration_time = min(kickoff, now + KALSHI_GTD_HORIZON_S)` (default 1 h) so an orphaned order dies on the exchange even if the process does not; `SelfMatchGuard` refuses an order that would cross our own resting book; after a kill the runner sweeps orphaned resting orders (listing failures are logged, never swallowed) | `execution/kalshi.py`, `strategy/broker.py` |
| Demo check | `scripts/kalshi_demo_check.py` (needs a **demo** key, refuses prod) exercises the salt/host, V2 create, reads, single + batched cancel and the fills path; `--record` replaces the schema-derived fixtures in `tests/fixtures/kalshi_orders/` with real trimmed responses. **Not yet run** — the order fixtures are derived from the OpenAPI schema | `docs/ROADMAP.md` "Needs you" |
| Market fields | `yes_bid_dollars`, `yes_ask_dollars`, `yes_ask_size_fp` (contracts), `occurrence_datetime` (game start, NFL), `exchange_index`, `price_level_structure` | live API |
| Order book | `GET /markets/{ticker}/orderbook` → `orderbook_fp.yes_dollars` / `no_dollars` = resting **bids** `[price, size]`; YES ask = 1 − best NO bid | live API |
| NFL tie rule | "$0.50 for each team" (`tie: half` in the settlement registry; NHL the same; college / NBA have no ties) | market `rules_secondary`, fixture `tests/fixtures/rules/kalshi_nfl_moneyline.txt` |
| Spreads / totals | `KXNFLSPREAD-{date}{pair}-{TEAM}{⌈line⌉}` = "{team} wins by over {line} points?" (`floor_strike`, `strike_type: greater`, both teams listed); `KXNFLTOTAL-{date}{pair}-{⌈line⌉}` = "over {line} points scored?". NO = other side. Both series `quadratic_with_maker_fees`. `GET /markets?event_ticker=` returns all lines of a game in one call | live API 2026-09-16 |
| Rate limit | the client rate-limits public reads to 15/s (`KALSHI_RATE_LIMIT`) and backs off on 429; the documented read budget is well above that, the write budget scales with the account's tier | docs.kalshi.com rate limits, observed |
| Browser access | The prod API returns **403 to any browser `Origin` other than kalshi.com** (extension origins included). The extension strips the header with a `declarativeNetRequest` rule and otherwise falls back to the local bridge | tested with curl |

## Robinhood event contracts (Robinhood Derivatives, LLC)

| Item | Value | Source |
|---|---|---|
| Exchanges | KalshiEX (symbols `KX…`), **Rothera Exchange and Clearing** (Robinhood/Susquehanna JV, symbols like `NFLGAME-26SEP20PHITEN-PHI`), ForecastEX, Nadex | robinhood.com event page footer + contract `exchange` field |
| Commission | `min(round_up_cent(k × P × (1−P) × C), $0.01 × C)`, k = 0.10 (0.05 with Gold). Examples (100 contracts): $0.01→$0.10/$0.05, $0.05→$0.48/$0.24, $0.25→$1.00/$0.94, $0.50→$1.00/$1.00 | Support article "Event contracts overview", pricing effective June 1, 2026; Robinhood Derivatives (RHD) Fee Schedule 2026-05-28 |
| Exchange fee (default) | "up to $0.01 per contract", varies by exchange; ForecastEX embeds it in the spread (Yes+No = $1.01). Engine default `flat_001`: $0.01 for KalshiEX / Rothera / CDNA (Nadex), $0 for ForecastEX | same article; chancemetrics.com |
| Exchange fee, Rothera (opt-in) | Rothera Fee Schedule (certified 20260520) defines a per-**order** fee `max(round_half_up(k × P × (1−P) × C, $0.01), $0.01)`, k = 0.02 for retail (the schedule's own example uses 0.06 and prints $1.365 as $1.37, hence half-up). At $0.97–0.99 that is ~$0.0006 per contract on a 100-lot, 17–25× below the $0.01 ceiling, and size-dependent (a 1-lot pays the full cent). `ROBINHOOD_ROTHERA_FEE_MODEL=quadratic` (settings `rothera_fee_model`, or `fee_params` on a quote) switches; **`flat_001` stays the default** until an order ticket shows which fee Robinhood passes on. On the NFL fixture scan the flip moves all 14 Rothera rows, improves 5 margins and flips no sign (`docs/FEES_EXPLAINED.md`) | Rothera Fee Schedule 20260520, read 2026-09-19; `fees/robinhood.py` |
| Exchange fee, CDNA (opt-in) | CDNA = North American Derivatives Exchange, Inc. d/b/a "Crypto.com \| Derivatives North America" (ex-Nadex; college symbols `NX.F.OPT.CFB-…`). Its published fee is a range, so `CDNA_FEE_MODEL` offers `flat_001` (default) \| `flat_002` ($0.02, top of the range) \| `weighted_007` (0.07 × P × (1−P) × C rounded up, the Nadex 2026-08-01 taker-style schedule). All three **unverified** against an order ticket; the `nadex` exchange key uses the same model | Nadex fee schedule effective 2026-08-01; `fees/robinhood.py` |
| Charged on | open and close (held-to-settlement pays only the open side) | same article |
| Order types | limit only: IOC or GTD (expires 3 AM ET next day); dollar orders are IOC | Support article "Trading event contracts" |
| Public data | Category pages are SSR Next.js with `__NEXT_DATA__` (`props.pageProps.events[*].eventContracts`, `quotes`, `eventStates`): `https://robinhood.com/us/en/prediction-markets/nfl/`, `/tennis/`. Quotes: `GET https://api.robinhood.com/marketdata/event/contract/quotes/v1/?ids=…` (also `?symbols=`), ≤20 per call, unauthenticated. Events: `/prediction-markets/v1/events?ids=…`; state: `/prediction-markets/v1/event_state?event_ids=…` (`eventProgress` = date before kickoff, "Live"/"Interrupted"/… in play) | observed 2026-09-15 |
| Routing observed | NFL game winners/spreads/totals → Rothera (separate book from Kalshi); NFL props, first-half/quarter lines and all tennis (ATP/WTA/ITF/challengers) → KalshiEX (same book as Kalshi, higher fees) | observed 2026-09-15 |
| Spreads / totals | Event types `EVENT_TYPE_SPREAD` (48 contracts: `NFLSPREAD-{date}{pair}-{TEAM}{⌊line⌋}`, "Buffalo wins by over 1.5 points", `floorStrikeValue`) and `EVENT_TYPE_TOTALS` (45 contracts: `NFLTOTAL-…-{⌊line⌋}`, "Over 49.5 points"); quotes API returns `yes_*` and `no_*` prices and sizes per contract | observed 2026-09-16 |
| Tie rule (Rothera NFL) | Not spelled out in the public blurb. The registry row carries `tie: no_winner` **derived from the plan's reading of Rothera's certified contract terms rule 40.2(d) ("strictly greater": a tied game has no winner, neither YES pays) — status `unverified`**: rothera.com was unreachable on 2026-09-19, the clause is not quoted, and the row flips to `derived` only when the certified text is pasted into `tests/fixtures/rules/rothera_nfl_moneyline_note.txt`. Consequences the engine already draws: `TIE_PAYOUT` Rothera YES 0 / NO 1 (Kalshi-routed 0.5), the Rothera **NO** contract on the other team is emitted as `<id>#no` (`rothera_no_leg`, default on) because it dominates the YES on a tie, `tie_margin` is reported next to `margin`, and a pair whose legs settle a tie differently is flagged `tie-rule-mismatch`; the scan still flags `tie-rule-unverified` | robinhood.com event page; `data/settlement_rules.json`; `quant/arbitrage.py` |
| Trading API | none public; the overlay is read-only | — |

## Polymarket (global CLOB)

| Item | Value | Source |
|---|---|---|
| Metadata | `https://gamma-api.polymarket.com/events?tag_slug=nfl&active=true&closed=false` (heavy: every market per event); `GET /markets?slug=nfl-{away}-{home}-{utc-date}` (5 KB, exact moneyline); `GET /public-search?q=…` | live API |
| Sports fields | `sportsMarketType` (`moneyline`, `spreads`, `totals`, `first_half_spreads`, `team_totals`, `q1_spreads`…, `tennis_completed_match`), `gameStartTime` (UTC), `line` (negative = outcome[0] is the favourite: "Spread: Bills (-1.5)" outcomes `[Bills, Lions]`), `outcomes`/`outcomePrices`/`clobTokenIds` (JSON strings), `bestBid`/`bestAsk` (outcome[0] token), `feeSchedule`. Slugs: `{event}-spread-{home|away}-{L}pt5`, `{event}-total-{L}pt5`; `GET /events?slug=` returns the whole game (all lines, ~1 MB) | live API |
| Fee schedule (observed) | `feeSchedule` is per market: NFL moneylines carried `{rate: 0.05, exponent: 1, takerOnly: true}` in the 2026 preseason, but on 2026-09-18 the week-2 moneylines (`nfl-sea-ari-2026-09-20` etc.) returned `{rate: 0, takerOnly: true}` — zero taker fee. The adapter always uses the market's own schedule, so the all-in equals the ask when the rate is 0; re-check before assuming either number | live API 2026-09-18 |
| Robinhood CDNA (college) | `EXCHANGE_SOURCE_CDNA`, symbols `NX.F.OPT.CFB-…-M.O.1.1/1.2` — a fourth routing exchange with its own book, used for college-football game winners (quotes API returns yes_ask/yes_bid/sizes like the others). Fee model: commission + $0.01/contract exchange fee assumed (`fees/robinhood.py`), unverified against an order ticket | live API 2026-09-18 |
| Tennis settlement text | Kalshi: every `KX{ATP,WTA}{,CHALLENGER}MATCH` market carries `rules_primary` ("wins … after a ball has been played") and `rules_secondary` (no ball played → "fair price"; postponed → open up to two weeks). Polymarket: market `description` (retirement → advancer; walkover / cancelled → 50-50; no winner **14 days** after the scheduled start → 50-50 — the 2026-09-18 template said 7 days, the 2026-09-19 one says 14 with an explicit deadline date; `parse_polymarket()` reads each market's own number at ingest). Quoted in SPORTS.md | live API 2026-09-19, fixtures `tests/fixtures/rules/*tennis*` |
| Kalshi `occurrence_datetime` | Not kickoff for college games (`KXNCAAFGAME-26SEP19PURUCLA` carries 2026-09-20T06:00Z for a 03:00Z kick); the adapter keys events by the ticker date | live API 2026-09-18 |
| Taker fee | `C × rate × (P × (1−P))^exponent`, USDC, 5 dp; sports `rate 0.05`, `exponent 1` → peak $1.25 per 100 shares at 50¢ | docs.polymarket.com/trading/fees; changelog 2026-07-10 (0.03 → 0.05) |
| Maker | no fee; 15% of sports taker fees rebated to makers (`rebateRate`) | same |
| Books | `GET https://clob.polymarket.com/book?token_id=…`, batch `POST /books` | live API |
| Book meta | The book carries `tick_size` (0.01, or 0.001 near the extremes) and `min_order_size` (contracts), plus `neg_risk`; the adapter puts them in `quote.meta` (the CLOB's copy overrides Gamma's) so max-buy prices land on the market's real grid and a leg below the venue minimum is flagged `below-min-size` | live API 2026-09-19 |
| `restricted` flag | Gamma marks every captured sports event/market `restricted: true` (geoblock); it is **venue-wide**, not per market, so it backs the eligibility table's default (signal-only for US accounts) but never vetoes an explicit `EXECUTABLE_VENUES` opt-in. Carried as `quote.meta["restricted"]` | Gamma `/events`, fixture `tests/fixtures/polymarket_events_nfl.json` |
| Ties | NFL moneyline "resolves 50-50"; spreads: tie goes to the underdog side; totals: exact line pushes are avoided with .5 lines | market descriptions |
| Trading | needs a wallet + `py-clob-client` (EIP-712 signed orders); not wired here | docs |

## Polymarket US (CFTC-regulated, separate product)

| Item | Value | Source |
|---|---|---|
| Fee | `θ × C × P × (1−P)`, banker's rounding to the cent; taker θ = 0.06 → **0.0695 from 2026-09-16**; maker rebate θ = −0.0125; volume rebates 10/25/50% above $250K/$1M/$10M prior-month taker volume | docs.polymarket.us/fees |
| Worked example (pinned) | 1,000 contracts at $0.50: taker 0.0695 × 1,000 × 0.25 = $17.375 → **$17.38**; maker rebate −0.0125 × 1,000 × 0.25 = −$3.125 → **−$3.12** (banker's rounding both ways). `POLY_US_WORKED_EXAMPLE` in `fees/polymarket.py`, checked by `tests/test_fees.py` and the JS twin | docs.polymarket.us/fees, verified 2026-09-19 |
| Adapter | none yet (`venue_rules.json`: `polymarket_us` executable for US persons, `adapter: false`), so it never appears in a scan. Reopen when the public market-data gateway is confirmed by hand (`docs/ROADMAP.md`) | — |

## Settlement registry (`arb_engine/data/settlement_rules.json`, 2026-09-19)

23 rows keyed by (venue, sport, market type, exchange) with the fields `tie`, `postponed`,
`cancelled`, `walkover`, `retirement`, `ot_included`. Every row names its source document,
the date it was read and a **rule-text fixture** under `tests/fixtures/rules/` whose sha256 is
pinned in the row (the test suite fails when the captured text changes), plus a `status`:

| status | meaning | rows |
|---|---|---|
| `verbatim` | the field is quoted from the fixture text | every Kalshi row (NFL / NCAAF / NBA / NHL / tennis moneylines, NFL and NCAAF lines), every Polymarket NFL / NCAAF / NHL / tennis row |
| `derived` | inferred from the fixture text; the row's `derived_fields` list says which fields and why (e.g. tennis `retirement: advancer` from "wins … after a ball has been played") | tennis walkover shares |
| `unverified` | an assumption; the fixture is a note explaining what could not be fetched | Rothera NFL moneyline (`tie: no_winner`, rule 40.2(d) reading), Rothera NFL lines, CDNA college moneyline (`cancelled: vwap_1w`) and lines, Polymarket NBA (template assumed = NFL/NHL until NBA game markets list) |

What the rows say: Kalshi NFL / NHL moneylines pay `half` on a tie, postponed games stay open
48 h, cancellations settle at a "fair price"; Polymarket NFL / NCAAF / NBA moneylines pay
50-50 on a tie, stay open until the game is completed, and cancel to 50-50; both include
overtime. `matching/settlement_rules.py` (`lookup`, `compare_rules`, `pair_flags`) turns a
difference between the two legs of a pair into a `settlement-mismatch:<field>` flag; a pair
inside one book (Kalshi × its Robinhood mirror) never flags. `scripts/capture_fixtures.py
--rules` refreshes the captured texts (every mode hits the network and rewrites committed
fixtures; `--help` prints the modes and does nothing else);
`TENNIS_SETTLEMENT` in the adapters is now backed by the registry with the literal text as a
fallback.

## Eligibility (`arb_engine/data/venue_rules.json`, 2026-09-19)

Which venues a **US-resident account can execute on**, as a table with a `verified` date
and a `source` per row; `compliance.stale_verification()` flags rows older than 30 days so
the table is re-read before every live week.

| venue | `executable_for_us` | why |
|---|---|---|
| kalshi | yes | CFTC-designated contract market; sports contracts offered to US residents (state contests of 2025-26 to be listed in `state_restrictions` once verified) |
| robinhood | yes | Robinhood Derivatives LLC (CFTC-registered FCM) routing to KalshiEX / Rothera / CDNA; no order API, hedges are executed by hand |
| polymarket | **no** | the CFTC order of 2022-01-03 and Polymarket's Terms of Use bar US persons; Gamma marks the markets `restricted: true`. Prices stay in the fair value as a **signal** |
| polymarket_us | yes, `adapter: false` | separate CFTC-regulated product; no adapter in this repo yet |

`EXECUTABLE_VENUES=kalshi,robinhood,polymarket` (env, or settings `executable_venues`)
overrides the table for an account that really can trade elsewhere; `--allowed-venues` on
`scan` / `live` / `maker` takes `executable`, `all` or a comma list. Effects: the scanner
keeps a non-executable venue's quote in the consensus but never as an arb leg and marks the
row **signal-only**; the maker's default hedge venues are `robinhood` only and a watch whose
hedge sits elsewhere raises `HEDGE VENUE NOT EXECUTABLE`; the bridge's `/analyze` resolves
eligibility exactly like `scan()`, and the overlay's direct mode mirrors it with the popup's
"I can trade on Polymarket" checkbox. On the committed fixture scans the table removes the
only arb (its cheap leg was Polymarket) and re-routes 5 of 8 NFL and 7 of 10 college maker
hedges from Polymarket to Robinhood (`docs/FEES_EXPLAINED.md`, eligibility table).

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
| Hardening (`venues/espn.py`, `venues/http.py`) | **403 fallback**: `site.api.espn.com` → the web host → a plain User-Agent, cached 60 s per (host, UA). **StateGuard**: a score that decreases without an explanation is held for one distinct poll (repeated sightings of the same poll do not spend the budget); a score change arriving before its `lastPlay` nulls ESPN's WP and marks the state `suspect`; the timeouts fallback (recount from play texts) never overrides a present value. **Per-sport clocks**: `period_clock_to_gsr` handles NFL playoff OT (`format.overtime`) and college `period >= 5` as an overtime sentinel; `classify_play` is text-first; `pickcenter` gives the sportsbook moneyline and spread. Golden fixtures pin every pre-P02 field on the DET@BUF captures | `tests/fixtures/espn/guard_sequences.json`, 2026-09-19 |
| ESPN `winprobability` timing | The entry on a play describes the state **after** it (88.5 % of scoring-play jumps land before the play's own entry, 148 scoring plays, week 1) — see `docs/MODEL.md`. Compare it with post-play sources, never as a pre-snap input | `tests/fixtures/results/espn_wp_alignment_p04.json` |
| Caveats | Unofficial: no SLA, no changelog, fields disappear (odds vanish from the scoreboard at kickoff, `situation` at the final). `winprobability` is ESPN's model, use it as a benchmark not a feature. `yardLine` for timeouts/kickoffs is unreliable (ESPN emits `yardsToEndzone 0` on official timeouts). Clock can lag the broadcast by 5–20 s; Kalshi/Rothera in-play quotes usually move first | observed |

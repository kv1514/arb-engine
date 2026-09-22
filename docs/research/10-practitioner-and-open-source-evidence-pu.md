# 10 · Practitioner and open-source evidence (Kalshi / Polymarket / Betfair bots, 2024–2026)

**Summary.** No public Kalshi or Polymarket sports bot reports a live fill rate or P&L with a sample size: every practitioner number in this cluster is self-reported, simulated without a fill model, or came from Polymarket's 2024 liquidity-rewards regime that no longer exists and never applied to a retail Kalshi maker. What the practitioners converged on is structural — sharp-price-primary fair values, maker-only resting orders re-quoted on small moves, flat stakes, inventory rebuilt from exchange fills, low-volatility market selection, CLV tracked before P&L — and their failure modes (directional wipe-outs, inventory drift, fuzzy matching) are the transferable content. The one academic source with Kalshi-matched data (Bürgi–Deng–Whelan 2026: makers −9.6 % vs takers −31.5 %, makers buying ≥ 50¢ +2.6 %) supports the repo's existing maker-and-model design but its longshot finding does not translate into a longshot STEAL rule, because `docs/MODEL.md` shows the WP model's worst calibration in the 0.8–0.9 favourite bin (−0.043), not the 0.0–0.1 bin (+0.024).

## Verified sources

### 1. mlb-kalshi-bot (mmoore07129, 2026, GitHub README) — https://github.com/mmoore07129/mlb-kalshi-bot
*Method.* Fair `p_fair = 0.55·p_Pinnacle + 0.30·p_LowVig + 0.15·p_BetOnline` (de-vigged via The Odds API); own XGBoost (24,686 MLB games 2015–2025) is fallback only after its vetoes proved anti-calibrated. `EV = p_fair·(1 − ask) − (1 − p_fair)·ask − fee`, `fee = 0.07·M·P·(1 − P)`; place if `EV ≥ clamp(0.005 + 1.5·σ_p, 0.005, 0.08)`; model-only path needs `EV ≥ 0.08, p ≥ 0.55`; breaker if `|p_model − p_Pinnacle| > 0.50`. Flat $20 stakes (Kelly rounded below Kalshi's minimum); maker YES bids amended on ≥ 2¢ ask moves, cancelled when EV falls below the admission threshold; settlement P&L from Kalshi's reported `fee_cost`; CLV cron every 5 min.
*Numbers (self-reported).* Holdout accuracy 56.0 % ± 0.5 %, 65.9 % at ≥ 66 % confidence (n = 323). No live P&L, fills or latency.
*Limitations.* Data-collection phase; pre-game, single sport; paid odds feed the user lacks (`ODDS_API_KEY`). Grade C.

### 2. I cloned a Polymarket market-making bot and ran it (Terry Lee, 2025, Substack) — https://tezlee.substack.com/p/i-cloned-a-polymarket-market-making
*Method.* poly-maker fork: two-sided quotes around mid, reward score `S = ((v − s)/v)^2 · b` (v = max spread/100, s = distance from mid, b = multiplier), cancel on threshold moves, merge YES/NO pairs to USDC.
*Numbers (self-reported).* No net profit; a single 30–40 % adverse move "can wipe out accumulated gains within minutes"; without position snapshots the bot "kept adding to losing sides"; only low-volatility, tight-spread markets worked.
*Limitations.* One anonymous operator, no volumes; profit source (rewards) absent on Kalshi. Grade C.

### 3. kalshi-sports-bot (lucavernhes-personal, 2026, GitHub README) — https://github.com/lucavernhes-personal/kalshi-sports-bot
*Method.* Paper "five-minute delta": fill when `|ΔP_Kalshi| over ≈ 5 min ≥ 0.10`, ESPN readings within 30 s at both ends, `|p_ESPN − p_Kalshi| > 0.05`, in-progress games only, one fill per ESPN play and side, stakes $10/$15/$20, 15 s poll.
*Numbers.* None stated; CSVs exist. No fill model, no fees, ESPN post-play WP used as pre-play. Grade C.

### 4. Automated Market Making on Polymarket (Polymarket News interviewing @defiance_cr, 2025) — https://news.polymarket.com/p/automated-market-making-on-polymarket
*Method.* Rank markets by realised movement over 3 h / 24 h / 7 d / 30 d, target low volatility and high rewards; two-sided quotes ("nearly 3×" one side).
*Numbers (self-reported).* $200/day early, $700–800/day at peak on $10k, 2024 election season; stopped when rewards fell. Rewards income, not spread; regime over. Grade C.

### 5. Polymarket Predictions changelog (Polymarket, 2026) — https://docs.polymarket.com/changelog/predictions
Sports taker fee 0.03 → 0.05 and maker rebate 25 % → 15 % (Jul 10 2026); POST /order 200/s sustained; 1-second taker delay being tested on NBA/MLB and a 3-second placement delay on marketable sports orders. Venue statements, not measurements; Polymarket is signal-only for this user. Grade A for the facts.

### 6. Makers and Takers: The Economics of the Kalshi Prediction Market (Bürgi, Deng, Whelan, UCD, Jan 2026, working paper) — https://www.karlwhelan.com/Papers/Kalshi.pdf
*Method.* Kalshi transaction data 2021–Apr 2025, contracts ≥ $1,000 volume, final spread ≤ 20¢, open ≥ 24 h. `return = (1{win} − P − fee)/P`, taker `fee = 0.07·P·(1 − P)` rounded up, by 10¢ band and by initiating side; heterogeneous-belief model with endogenous maker/taker sorting and small-probability overweighting.
*Data.* 12,403 events, 46,282 YES contracts, 156,986 YES prices; results insensitive to excluding sports.
*Numbers.* Mean return ≈ −20 %; ≤ 10¢ contracts lose > 60 %; > 70¢ significantly positive; makers −9.64 % vs takers −31.46 %; makers buying ≥ 50¢ +2.6 %; bias "diminishing over time".
*Limitations.* Pre-2025 fee regime, mostly non-sports, excludes in-play windows. Grade B.

### 7. Kalshi co-founder says in-house trading arm "not profitable" (InGame, Dec 2025) — https://www.ingame.com/kalshi-in-house-trading-arm-not-profitable/
November 2025 volume $5.82 B, sports ≈ $5.17 B; Kalshi Trading ≈ $310 M of sports trades, "less than 6 % of the making volume"; Susquehanna believed the largest external maker, rebated. Self-interested statement, no audit. Implication: the counterparty behind a resting NFL bid is a rebated professional with faster data. Grade C.

### 8. Kalshi API tutorial: auth, websockets, rate limits, orders (botforkalshi.com, Sept 2026) — https://www.botforkalshi.com/blog/kalshi-api-tutorial
Rate tiers (verified against docs.kalshi.com/getting_started/rate_limits): Basic 200 read / 100 write per s, Advanced 300/300, Expert 600/600, Premier 1,000/1,000, up to Prestige 10,000/8,000; `GET /account/limits` returns the tier at runtime. Websocket `orderbook_snapshot` then `orderbook_delta` with a `seq`; a skipped seq means drop the local book and resnapshot. Fix `client_order_id` before the first submit. No latency measurements. Grade C (tutorial), rate facts A.

### 9. Arbitrage Analysis in Polymarket NBA Markets (Cheng, Yang, Zou, 2026, arXiv 2605.00864) — https://arxiv.org/abs/2605.00864
Reconstructed LOB from > 75 M snapshots over 173 NBA games; single-market arb when `ask_YES + ask_NO < 1`. Seven executable single-market episodes, median persistence 3.6 s; 290 combinatorial episodes, median 101 bp, 76.9 % capped at ≈ 14.8 shares; the middle "never empirically realized". Polymarket only, snapshots not messages. Grade B.

### 10. polymarket-arbitrage (ImMike, 2025, GitHub README) — https://github.com/ImMike/polymarket-arbitrage
Text-similarity matching at 0.6, `min_edge 0.01`, zero-fee config; "99.6 % win rate, $573 profit" in simulation with no fill model and the README's own warning that opportunities are "rare and fleeting". Negative example only. Grade C.

### 11. kalshi-oms (milesChild, 2024, GitHub README) — https://github.com/milesChild/kalshi-oms
Rust OMS: strategies over TCP, exchange server on Kalshi REST, fills from the websocket, RabbitMQ per message struct. No numbers, no reconciliation logic. Confirms the OMS-separate-from-strategy, fills-from-stream pattern. Grade C.

## What this means for arb_engine

**Paper fills are the biggest untested assumption.** `PaperBroker` (`arb_engine/strategy/broker.py`) is by its own docstring "optimistic about queue position … an upper bound on fill frequency". Sources 6–7 say who sits ahead of that bid (rebated professionals, > 94 % of making volume); source 2 shows what inventory drift does to a retail maker. The Kalshi trade tape is already cached (`venues/trades.py`, `tests/fixtures/trades/`), so a back-of-queue fill is implementable and replayable offline. Everything downstream of `MakerRunner.check_fills` inherits the current optimism.

**The favourite–longshot finding cuts the other way from the brief.** Source 6's longshot losses would matter if the model manufactured cheap-side STEALs, but `docs/MODEL.md`'s 2025 calibration has the 0.0–0.1 bin at +0.024 (predicted 0.034, actual 0.057: the model *under*-states longshots) and 0.8–0.9 at −0.043 (predicted 0.851, actual 0.808). With the market under-pricing favourites, the place where model error can fake an edge is the favourite side at asks of 0.80–0.90. An ask-price stratum in `strata_tables` / `simulate_pairings` (`arb_engine/backtest.py`) is worth adding; a pre-set "raise the edge below 0.15" rule is not. It matters because the NFL STEAL P&L is undemonstrated: the executable pairing gives −10.3 % / +2.7 % / +50.6 % ROI at 3 / 5 / 8 % edges on 16 / 14 / 10 games, every 90 % interval including zero, while the shuffled-outcomes placebo swings +19.6 % / −48.6 % / +20.5 %; college is +3.5 % / +7.1 % / +5.5 % against a placebo of −23.2 % / −21.7 % / −13.1 %, intervals still including zero.

**Market-primary blends are not supported by the repo's numbers.** Source 1's sharp-book-primary fair fits pre-game MLB with Pinnacle; in play the repo measures market-only log-loss 0.4417 vs model 0.4060 on NFL week 1 (model − Kalshi interval [−0.055, −0.018]) and 0.2462 vs 0.2345 on the 11,739 college week-2 plays with all three sources (Kalshi's before-candle alone is 0.2907 on the 9,269 plays it covers, against the model's 0.2156 on all 12,771 scrimmage plays), and the grid fit already includes the market corner and picks the model corner (blend − model +0.0114, [+0.0061, +0.0169]). The 0.12 `disagreement` gate is the circuit breaker. Nothing here moves `SPORT_WEIGHTS`.

**Rate limit and idempotency.** `DEFAULT_RATE_LIMIT = 15.0` (`venues/kalshi.py`) is ~13× under the verified Basic tier, but a 16-game slate polls in 1–2 s at that rate, so quote age is set by the 10 s `live` poll; reading `GET /account/limits` is hygiene, not a P&L lever. `OrderPlan.client_order_id` (`execution/kalshi.py`) already defaults to `uuid4` with the payload built once per `place()`, so idempotency is met.

**Arb list is monitoring, not P&L.** Source 9's 3.6 s persistence and ~15-share caps agree with the 10 s poll and the "fillable, not just positive" rule in `docs/ARCHITECTURE.md`; the repo's own persistence statistic waits on a `scan --record` run (`out/history.db` has no scans).

**Dropped** (checked, not transferable): Polymarket rewards P&L and the "99.6 % win rate" simulation; an `exp(−Δt/delay)` consensus weight (1-minute candles cannot see a 1–3 s delay; 36 `steal_observations` rows); `fee_cost` reconciliation and a markout-EWMA spread term (both need real fills).

## Recommendations

| # | change | module | metric it should move | offline test on committed data | grade | effort |
|---|---|---|---|---|---|---|
| 1 | Back-of-queue paper fills: a resting bid fills only as public trade prints at or through its price accumulate past the size that was ahead of it when placed (cancellations ahead still ignored) | `strategy/broker.py` `PaperBroker`, fed by `venues/trades.py` | paper fill rate (from upper bound to conditional estimate), hedge-timing P&L in `check_fills` | unit test on `tests/fixtures/trades/kalshi_trades_page*.json` + `kalshi_orderbook.json`; rerun `maker --broker paper` over `tests/fixtures/history` and diff fills | B | M |
| 2 | Ask-price stratum (< 0.15, 0.15–0.50, 0.50–0.80, ≥ 0.80) in the STEAL tables with a per-bucket game-cluster interval; no pre-set edge change | `backtest.py` `strata_tables`, `simulate_pairings` | STEAL P&L per contract by ask bucket; whether the 0.8–0.9 bucket carries the fake edge | `backtest --week 1 --season 2026 --offline --placebo` on `tests/fixtures/history/replay_trim`; require the interval per bucket | B | S |
| 3 | Realised-σ of the Kalshi mid over the trailing 30 min as a watch feature with `--max-sigma`, pre-game only, plus jump-triggered pull | `strategy/maker.py` `discover`, `reconcile`; `venues/history.py` candles | maker markout at +2 / +15 min, fill rate | replay `maker --broker paper` over `kalshi_candles_*.json` with the filter on/off, compare markout via `store.update_ladder` | C | S |
| 4 | Rebuild inventory from `GET /portfolio/fills` on every `reconcile` step instead of the runner's own order list; cancel a bid when its edge drops below the edge that admitted it | `strategy/maker.py` `reconcile`, `strategy/broker.py` `KalshiBroker.poll` | duplicate / stale resting orders, inventory drift after a missed fill | `SequencedFakeHttp` test: fill reported by the exchange but absent from the runner's list must be adopted, not re-bid | C | S |
| 5 | Read `GET /account/limits` at start-up and set the limiter to a fraction of the account tier, falling back to 15 req/s | `venues/kalshi.py` `DEFAULT_RATE_LIMIT`, `HttpClient` | 429 count, quote age on a full slate (expect little change: the 10 s poll dominates) | fake-http test that the limiter adopts the returned tier; replay a recorded slate at the higher rate | B (facts) | S |
| 6 | "Reversal" pairing: enter only when Kalshi moved ≥ 0.10 over the prior 5 min while the fair moved < 0.05, scored beside STEAL under both placebos | `backtest.py` `simulate_pairings`, `tickreplay.py` | P&L per contract, CLV at +15 min | `backtest --week N --placebo`; keep only if the game-cluster interval excludes zero | C | S |
| 7 | Regression test that a fuzzy title match can never create an event key | `matching/` | none (guard) | pytest on `tests/fixtures/*_nfl.json` with a near-duplicate title | C | S |

## Open questions

1. **Does a retail post-only bid behind SIG ever fill at positive markout?** No public source measures it; source 6's +2.6 % is pre-sports and pre-maker-fee. Settled by *Kalshi demo-key check* (real order shapes) and then *Record a live Sunday slate* run with `maker --mode demo`.
2. **Does the favourite–longshot bias persist in Kalshi in-play NFL in 2026?** Source 6 excludes in-play windows and reports it diminishing. Settled by recommendation 2 on the ~70 NFL games the ROADMAP's *In-play STEAL as a strategy* row already requires.
3. **How fast does the engine's own edge decay per second of delay, and how much confidence should the consensus give a 1–3 s-delayed Polymarket mid?** Unmeasurable on 1-minute candles. Settled by *Record a live Sunday slate* (`stats --convergence`, `event-study` on the `steal_observations` ladder).
4. **Does Kalshi's reported `fee_cost` match the engine's fee model at fractional cents?** Settled by *Kalshi cent-vs-centicent rounding* (one real fill).
5. **What is the executable size and persistence of the engine's own cross-venue arbs?** `out/history.db` has no scans; settled by any `scan --record` run before the Sunday slate.

## Bibliography

- mmoore07129 (2026). mlb-kalshi-bot. https://github.com/mmoore07129/mlb-kalshi-bot
- Lee, T. (2025). I cloned a Polymarket market-making bot and ran it. https://tezlee.substack.com/p/i-cloned-a-polymarket-market-making
- lucavernhes-personal (2026). kalshi-sports-bot. https://github.com/lucavernhes-personal/kalshi-sports-bot
- Polymarket News / @defiance_cr (2025). Automated Market Making on Polymarket. https://news.polymarket.com/p/automated-market-making-on-polymarket
- Polymarket (2026). Predictions changelog. https://docs.polymarket.com/changelog/predictions
- Bürgi, C., Deng, W., Whelan, K. (2026). Makers and Takers: The Economics of the Kalshi Prediction Market. https://www.karlwhelan.com/Papers/Kalshi.pdf
- InGame (2025). Kalshi Co-Founder Says In-House Trading Arm 'Not Profitable'. https://www.ingame.com/kalshi-in-house-trading-arm-not-profitable/
- botforkalshi.com (2026). Kalshi API Tutorial: Auth, WebSockets, Rate Limits & Orders. https://www.botforkalshi.com/blog/kalshi-api-tutorial
- Cheng, G., Yang, J., Zou, H. (2026). Arbitrage Analysis in Polymarket NBA Markets. arXiv 2605.00864. https://arxiv.org/abs/2605.00864
- ImMike (2025). polymarket-arbitrage. https://github.com/ImMike/polymarket-arbitrage
- milesChild (2024). kalshi-oms. https://github.com/milesChild/kalshi-oms

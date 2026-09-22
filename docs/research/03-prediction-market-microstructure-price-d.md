# 03 — Prediction-market microstructure: price discovery, lead-lag, in-play drift, liquidity, feed latency

**Summary.** The one in-play study on Kalshi (NBA, 409k contract-minutes) finds the mid moves 0.64-for-one with a public-information benchmark and closes about half the residual over five minutes, yet the drift trade is negative once it crosses the spread — before Kalshi's taker fee. Cross-venue evidence says order-book exchanges own price discovery and sportsbooks follow, while Polymarket books are shallow at the touch (L1/L10 ≈ 0.14) and executable arbitrage lives for seconds, in play, at ~15-share size. The repo's own picture — model beats the Kalshi mid on log-loss (0.406 vs 0.438–0.443, NFL week 1) while taker STEAL P&L stays inside its interval — is what an underreacting-but-wide book should produce, so the next measurement is the engine's own β/ρ on NFL rows, not another blend fit.

## Papers

### 1. When Do Markets Fully Process Public Information? Evidence from Real-Time Prediction Markets
Angelini & De Angelis, 2026, arXiv 2606.07811 (working paper). <https://arxiv.org/html/2606.07811>

*Method.* Benchmark `q_it`: 5-fold game-level cross-fitted logit of the payoff on pre-game close, margin, |margin|, clock, period, home, recent scoring, margin×time; the Kalshi price is excluded (Brier 0.164 = Kalshi mid). Impact `dp_it = a + b*dq_it + G*X_it + e` (1-min Kalshi mid; X = lags, margin, minutes left, spread, volume, OI). Drift `[p_{t+h} − p_t] − [q_{t+h} − q_t] = a + rho*Gap_t + e`, `Gap = dq − dp`, h ∈ {1,2,5,10,15} min. Trading test: long YES if Gap > 0, short if < 0, 5-min hold.
*Data.* Kalshi NBA, 1,438 games, 409,512 contract-minutes, 2025-04-15 to 2026-05-25.
*Numbers.* `b = 0.638` (SE 0.010, b = 1 rejected p < 0.001), 0.509 in clutch time. `rho_5 = 0.459` (SE 0.029), 0.507 on high-quality quotes. Underreaction smaller for salient plays, larger when illiquid; spread itself is not a significant regressor (0.0003). Trading: +0.39 % / +0.87 % (|Gap| ≥ 200 bp) at the midpoint; **−1.20 % / −0.92 % ask-to-bid**.
*Limitations.* NBA only; benchmark error attenuates b; 1-min resolution makes b an upper bound for fast plays; no fees mentioned, so Kalshi's 7 %·p(1−p) taker (3.5 ¢ round-trip at 0.50) makes the executable case worse; the benchmark includes the pre-game close, so it is closer to `blended_fair` than to the WP model; no time split over 13 months of rising volume.

### 2. Arbitrage Analysis in Polymarket NBA Markets
Cheng, Yang & Zou (UCLA), 2026, arXiv 2605.00864v1. <https://arxiv.org/html/2605.00864v1>

*Method.* L1 snapshots every 3.6–5.5 s; `Ask_A + Ask_B < 1` (single market) and `Ask(ML_A) + Ask(Spread_B) < 1` when the spread outcome is a subset of the moneyline; $10 L1 minimum, forward-fill, $100 cap per episode, zero fees then.
*Data.* 173 NBA games, 3,042 markets, 75.1 M snapshots, Feb–Mar 2026.
*Numbers.* Single-market: 7 episodes (0.0001 % of time), median 3.6 s. ML-vs-spread: 290 episodes, 0.1762 % of in-game time vs 0.0034 % pre-game, median 16 s, median 101 bp, 279/290 in play, 76.9 % capped at ~14.8 shares.
*Limitations.* Frequencies are lower bounds, durations upper bounds; one month, one venue, L1 only; not executable for a US user — transfers as structure, not P&L.

### 3. The Anatomy of a Decentralized Prediction Market
Dubach, 2026, arXiv 2604.24366v1 (SSRN 6658364; Zenodo 10.5281/zenodo.19811426). <https://arxiv.org/html/2604.24366v1>

*Method.* Tick-level WebSocket archive joined to on-chain fills for true aggressor side; 600-market pre-registered panel; Glosten–Harris `S_eff/2 = c + phi`; Kyle λ on a 60 s grid; depth ratio L1/L10.
*Data.* 30.29 bn feed rows, 52 days (Feb–Apr 2026), 385k markets; sports panel 142.
*Numbers.* Median half-spread 53.5 bp in [0.90,1.00] vs 1,818 bp in [0,0.10], ≈ 400 bp at 0.5. Median L1/L10 = 0.137 (IQR 0.033–0.428). ≈ 32 effective makers. Ingestion latency median 41.5 ms, p99 6.1 s. Feed-inferred trade direction right on only 59.2 % of volume.
*Limitations.* Polymarket only; sports pooled pre-game with in-play; negative effective half-spreads on fast books mean any mid-referenced cost metric is unreliable — a Kalshi candle mid included. Kalshi's tape carries `taker_side` explicitly (`venues/trades.py`), so the direction problem does not transfer.

### 4. Price Discovery Across Political Prediction Markets: 2024 U.S. Presidential Election
Aktuğ & Torul, 2026, working paper. <https://web.bogazici.edu.tr/torul/pridis.pdf>

*Method.* VECM on logit 5-min probabilities `dy_t = alpha*beta'*y_{t−1} + sum Gamma_i dy_{t−i} + e_t`, rank n−1; Hasbrouck `IS_j = ([psi F]_j)^2 / (psi Sigma psi')` bounded over orderings; Gonzalo–Granger CS; Putniņš `ILS = IS/CS`; forward-fill Monte Carlo.
*Data.* Trump-wins contract, nine venues, Jan–Nov 2024; 16,460 complete 5-min observations; Kalshi from Oct 4.
*Numbers.* Full sample IS: Polymarket 0.475 [0.375, 0.576], Betfair 0.371, best sportsbook 0.110. Kalshi window: Polymarket 0.438, Kalshi 0.409 [0.378, 0.441], Betfair 0.057, Pinnacle 0.031; ILS Kalshi 1.69, Polymarket 1.47, sportsbooks < 1. A venue updating 0.1 % of bins gets an artefactual 21 % IS under forward-fill.
*Limitations.* Political, 2024, 5-min grid, 32-day Kalshi window; executed and quoted prices mixed. The cited Ng et al. (2025) study including Robinhood could not be located and is not used.

### 5. Executable Arbitrage and Market Efficiency in Prediction Markets
Gebele, Mutzel & Matthes (TU Munich), 2026, arXiv 2608.00666v1. <https://arxiv.org/html/2608.00666v1>

*Method.* Payoff-space vs protocol-executable no-arbitrage: `sum_{k in S} N_k = (|S|−1) + sum_{j not in S} Y_j`, realised by settlement or Polymarket's NO→YES converter; depth-aware walks; all-taker fees; on-chain attribution.
*Data.* Polymarket CLOB 32,702 events, 259 M trades to 2025-12-31; L2 panel Apr–May 2026.
*Numbers.* 2,098 YES-side vs 36 NO-side episodes; median 16.15 s. $1.118 M realised, 97 % via the converter, 75 % to ten addresses; median profit per conversion ≈ $1 (H1 2024) → $0.20 (2025) → $0.08 (2026).
*Limitations.* No sports split, no Kalshi. A framing paper (an arb exists only if the venue lets you close it) plus the only decay curve in the set.

### 6. Latency Indicator (Beta) — Sportradar Live Data
Sportradar, 2026, vendor documentation. <https://docs.sportradar.com/live-data/latency-indicator-beta>

Categories: 1 low 0–<4 s, 2 moderate 4–<8 s, 3 high 8–<12 s, 4 very high 12–<16 s, 5 ≥ 16 s. No distribution, no ESPN number. Trade-press figures (broadcast 7–15 s, courtsiders 2–3 s ahead) are self-reported practitioner numbers. The engine's 10 s ESPN poll plus ESPN's own ingestion is category 3–4 at best — an assumption, not a measurement.

### 7. Do Betting Markets Sense a Goal Coming? Evidence from the German Bundesliga
Winkelmann & Deutscher, 2025, arXiv 2505.21275. <https://arxiv.org/abs/2505.21275>

1 Hz odds and stakes at one bookmaker over one season; neither anticipates goals. Abstract only; dealer book. The null result for pre-event drift: a feed-stale gate should target post-event latency only.

## What this means for arb_engine

**The repo's numbers are paper 1's picture on the NFL.** `docs/MODEL.md`: model log-loss 0.4060 on 2,263 in-play scrimmage plays vs Kalshi 0.4425 (before) / 0.4382 (after), intervals [−0.0547, −0.0179] and [−0.0491, −0.0140]; executable STEAL ROI −10.3 % / +2.7 % / +50.6 % at 3 / 5 / 8 % edges with every interval including zero; the one-candle-later placebo +2.6 % / +39.8 % / +55.6 %. A placebo entered a candle *later* being as profitable is what `rho_5 ≈ 0.46` looks like — half the gap still there five minutes on — but 16 games cannot separate that from noise. The widest gaps sit on dead-ball rows (mean |model − Kalshi before| 0.0607 timeouts, 0.0549 kickoff-pending, 0.0532 try), where a 1-min candle is most likely stale, which is what the `FeedFreshness` gates in `strategy/inplay.py` exist for; paper 7 says key them on post-event latency only.

**Round-trip drift is not the engine's trade.** Paper 1's +0.39 % becomes −1.20 % at ask-to-bid before fees; `fees/kalshi.py` adds 7 %·p(1−p) taker ($1.75 per 100 contracts at 0.50) and 1.75 %·p(1−p) maker ($0.44) on every game series the engine trades. `strategy/maker.py::_price()` already prices with `role="maker"`; the LOCK legs in `backtest.simulate_steal` are taker hedges, and MODEL.md's break-even LOCK losing on every NFL variant (−4.7 / −6.3 / −13.0 / −9.6 %) is the same spread arithmetic. Any edge is hold-to-settlement or maker-side — what the `steal_observations` ladder and `store.convergence()` already measure, minus a fee column.

**Width is not confidence on NFL.** `quant/inplay_fair.py::market_confidence_from_spread` already scales the market weight by width, but MODEL.md shows Kalshi at 0.4875 on the ≤ 4 ¢-book plays vs the model's 0.4478 on the same 2,048 plays — a larger relative deficit than overall (−0.0397 vs −0.0365). Paper 1 finds spread insignificant and volume/OI carrying the illiquidity effect; the candles carry `volume_fp` (`venues/history.py`), so volume is the measurable variable.

**Lead-lag needs L1 ticks.** Paper 4's forward-fill artefact rules out the 5-min Rothera bars MODEL.md describes as the only Robinhood history; `store.record_l1` at the poll interval is the right input. Depth is unmeasured: `record_l1` stores L1 only, so paper 3's L1/L10 has no Kalshi counterpart.

## Recommendations

| # | change | module | metric | offline test | grade | effort |
|---|---|---|---|---|---|---|
| R1 | Engine's own `b` and `rho_h`: `dp = kalshi_after − kalshi_before` on `dq = model_after − model` per play, Gap carried to +1/+2/+5/+15-min candles; per volume tercile and width tercile; per-week series | `quant/eventstudy.py` (new `impact_drift()` beside `underreaction_*`), `backtest.week_report` | `b`, `rho_5` with game-cluster intervals | W1 week cache (`out/cache/replay`, one prior network run) + `history/kalshi_candles_before_after.json`; synthetic rows with planted `b = 0.6` | A / B (NBA→NFL) | M |
| R2 | Fee-inclusive round-trip on the ladder: `(mid_{+h} − ask_0)` minus taker fee both ways, and the maker variant | `store.convergence()`, `backtest.simulate_steal` | after-fee P&L per contract at +5 min vs hold | `tests/fixtures/ticks/synthetic_40.json` via `tickreplay`; fee vectors | A | S |
| R3 | Feed latency: ESPN tick to first Kalshi print moving ≥ 0.02 on scoring plays; set `inplay_stale_after_s` (15 s) from the p90 | `quant/eventstudy.py::absorption` (+10 s), `strategy/inplay.py::FeedFreshness` | latency distribution; gated-STEAL count | arithmetic on `tests/fixtures/trades/replay_rows_trim.json` + `kalshi_trades_page*.json`; the number needs recorded ticks | C | S |
| R4 | Maker markout ladder per fill (+30 s … +15 min) and a trailing taker-side imbalance gate from `venues/trades.py` | `strategy/maker.py::check_fills()` / `reconcile()`, `store.py` (`maker_fills`) | markout per contract after the 1.75 % maker fee | `PaperBroker` on `tests/fixtures/kalshi_orderbook.json` with a scripted tape | C (press-reported sign) | M |
| R5 | Kalshi–Robinhood lead-lag from `record_l1` ticks at 10 s; never from 5-min bars | new `quant/leadlag.py`, `store.tick_rows` | lead in seconds; hedge-venue choice | synthetic ticks with a planted 20 s lead | B | M |
| R6 | Store five book levels in `record_l1` (prerequisite for depth-aware sizing; `quant/arbitrage.walk_book` exists) | `store.py::record_l1`, `_l1_columns` | fill rate and slippage per STEAL, once recorded | schema test on `tests/fixtures/kalshi_orderbook.json` | C | S |

Dropped: a volume/width market-weight law in `blended_fair` (inverted by the ≤ 4 ¢ row; the roadmap gate rejects it on this week — fold into R1), depth-walk STEAL sizing with a longshot half-spread add-on (untestable on L1 records; R6 first), and the press-reported 1.91 ¢/contract Kalshi maker markout as a design input (R4 measures the sign).

## Open questions

1. Is Kalshi NFL's `b` ≈ 0.64 and `rho_5` ≈ 0.46, or has 2026 volume pushed it to full adjustment inside a minute? R1 gives the point estimate; the interval needs ~70 games — **"Record a live Sunday slate"** plus the next `backtest --week` runs.
2. Does the drift survive the taker fee? R2 offline; executable version from **"Record a live Sunday slate"** via `backtest-ticks` / `clv`.
3. ESPN's real latency against the Kalshi tape? No source exists; only **"Record a live Sunday slate"**; until then `inplay_stale_after_s = 15` is a guess.
4. Who leads, Kalshi or Robinhood, at 10 s? Only political 5-min evidence; **"Record a live Sunday slate"** feeds R5.
5. Are NFL moneylines "single-name" for Bartlett–O'Hara, and does the YES-overbetting surplus exist in two-sided markets? A reading task (SSRN PDF blocked); R4 sets the sign empirically. No roadmap row.
6. Is late-game leader underpricing (γ ≈ 1.27–1.31 on NBA/MLB/NHL trades) an NFL effect? MODEL.md's q4_late slice (314 rows: Kalshi after 0.1299, model 0.1297, ESPN 0.1246) says the market is level there; settle with `backtest --pool` after four weeks.
7. Kalshi in-play depth profiles? R6, then **"Record a live Sunday slate"**.
8. How fast is the rent decaying? R1's per-week `rho` series is the only instrument.

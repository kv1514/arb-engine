# 04 — Market efficiency and biases in sports prediction markets and sportsbooks (2022–2026)

## Summary

The exchange studies agree that the favourite-longshot bias (FLB) that dominates political and
crypto contracts is weak to absent in **sports**: Polymarket sports longshots earn +2.4 % rather
than −6 %, Kalshi sports prices are near-calibrated inside 48 h (slope 0.90–1.10), and Kalshi's
platform-wide FLB is mostly a *taker* phenomenon (takers −31 %, makers −10 %). Sportsbook closing
odds do carry an FLB (γ ≈ 0.07), which is why power / Shin de-vigging beats multiplicative by
0.0002–0.0013 log-loss and why efficiency tests must regress on normalised probabilities with
p(1−p) weights. For arb_engine that means no FLB correction of exchange mids, an efficient-market
null for the STEAL P&L, and small offline upgrades to `quant/odds.py` and `quant/calibration.py`.

## Papers

### 1. The Favorite-Longshot Bias in Prediction Markets: Evidence from Polymarket
Cardozo & Rivero-Wildemauwe, 2026, arXiv:2609.12878 — https://arxiv.org/html/2609.12878

**Method.** Per purchase `r_f = (y_f − p_f)/p_f`, aggregated by equal child market, pooled
dollars or equal parent event; longshot < 0.10, favourite ≥ 0.90; wash-trade filter; splits by
category and maker vs taker. **Data.** 588 M trades, 614,883 markets, Nov 2022 – Mar 2026.
**Results.** Platform longshots −6.3 % [−8.4, −4.2] (equal child) but +4.1 % equal-event;
**sports longshots +2.43 % / +18.11 %** (pooled CI includes 0); maker longshots −3.5 % vs taker
−31.2 %; no post-purchase price decline. **Limitations.** No fees, sports not split by sport or
in-play; the platform sign flips with the weighting. Grade B.

### 2. Makers and Takers: The Economics of the Kalshi Prediction Market
Bürgi, Deng & Whelan, 2026, UCD WP2025/19 — https://www.karlwhelan.com/Papers/Kalshi.pdf

**Method.** Mincer–Zarnowitz `Y − P = α + ψP + ε`, clustered SEs; post-fee return
`R = (Y − P − C)/(P + C)`, `C = 0.07·P(1−P)` for takers; structural model with belief dispersion
σ = 0.107 and small-probability overweighting β = 0.09. **Data.** 46,282 contracts, 313,972
daily prices, 2021 – Apr 2025; sports only from Jan 2025, no sports-only table. **Results.**
α = −1.736 (0.153), ψ = 0.034 (0.005), p < 0.001 in every sub-sample; ≤ 10c contracts lose
> 60 % post-fee; **makers −9.64 % vs takers −31.46 %**; β = 0 cannot fit. **Limitations.**
Daily last trade, markets open ≥ 24 h (no in-play), pre-2025 fees, sports thin. Grade B.

### 3. Decomposing Crowd Wisdom: Domain-Specific Calibration Dynamics in Prediction Markets
Le, 2026, arXiv:2602.19520v2 — https://arxiv.org/html/2602.19520v2

**Method.** Logistic recalibration `logit P(y=1) = a + b·logit(p)` per domain × horizon × size
cell (b > 1 = compression toward 0.5); event-clustered bootstraps. **Data.** Kalshi 64.7 M
trades (sports 43.2 M, 55,637 markets) and Polymarket 288.7 M, to Dec 2025. **Results.** Kalshi
sports slope **1.10 at 0–1 h**, 0.96 at 1–3 h, 0.90–1.08 at 3–48 h, 1.74 at ≥ 1 month;
clustered SEs ~50× naive. **Limitations.** 0–1 h mixes pre-game and in-play; no price-level
split, so not an FLB estimate; last-trade prices. Grade B.

### 4. Comparing Two Methods for Testing the Efficiency of Sports Betting Markets
Hegarty & Whelan, 2024, Sports Economics Review 8, 100042 —
https://mpra.ub.uni-muenchen.de/121382/1/MPRA_paper_121382.pdf

**Method.** Under efficiency `O_ij = μ_i/P_ij`, `μ_i = 1/Σ_k(1/O_ik)`, so `P^N = (1/O_j)/Σ(1/O_k)`
is the book's probability; test `Y − P^N = α + γP^N + ε`, WLS weights `P(1−P)`; raw inverse odds
shown biased toward negative γ; 10,000-run simulations. **Data.** 84,230 football and 55,988
ATP/WTA matches, 2011–2022, average closing odds. **Results.** γ = 0.075 (0.007) football,
0.066 (0.006) tennis; inverse odds find 0.009 n.s.; median t under true FLB 11–13 vs 0.5–1.8.
FLB exists but no positive-return bets. **Limitations.** Average odds, closing only, sportsbook.
Grade A (method), B (magnitude).

### 5. Forecast Sports Outcomes under Efficient Market Hypothesis
Goto, Takeishi & Yairi, 2026, arXiv:2604.17194 — https://arxiv.org/html/2604.17194

**Method.** Multiplicative `y = x^−1/Σx^−1`; power `y_i ∝ x_i^−β`, β solved so Σy = 1; Shin
(closed form `z = (s−1)(c_i² − s)/(s(c_i² − 1))`); OO-EPC `y = x^−1 − zσ`,
`σ_i = sqrt(x_i^−1(1 − x_i^−1)/x_i^−1)`, `z = (Σx_i^−1 − 1)/Σσ_i`; FL-GLM: one β fitted by ML on
history. **Data.** 90,014 football matches 2012–2024, five books. **Results (self-reported).**
Pinnacle log-loss multiplicative 1.00449, Shin 1.00432, power 1.00431, OO-EPC 1.00428, FL-GLM
1.00306; all beat multiplicative by 0.0002–0.0013; no overround–accuracy correlation.
**Limitations.** Football, three-way markets exaggerate differences vs two-way NFL, no ROI. Grade B.

### 6. Adjusting Bookmaker's Odds to Allow for Overround
Clarke, Kovalchik & Ingram, 2017, Am. J. Sports Science 5(6) —
https://www.sciencepublishinggroup.com/article/10.11648/j.ajss.20170506.12

**Method.** Additive `p_i = q_i − (Σq − 1)/n` (can go negative); normalisation `q_i/Σq`; Shin;
power `p_i = q_i^k`, k solved so Σp = 1 (stays in (0,1), invertible). **Data.** Three bookmaker
datasets, three sports (abstract only). **Results.** Power "universally outperforms"
multiplicative and matches or beats Shin. **Limitations.** Abstract only. Grade B.

### 7. Are Betting Markets Inefficient? Evidence From Simulations and Real Data
Winkelmann, Ötting, Deutscher & Makarewicz, 2024, J. Sports Economics 25(1), 54–97 —
https://ideas.repec.org/a/sae/jospec/v25y2024i1p54-97.html

**Method.** Simulate fully efficient markets to count false "inefficient periods" at realistic
sample sizes; 14 football seasons tested season by season. **Results.** Single-season
inefficiencies are not persistent; literature-sized effects arise under full efficiency.
**Limitations.** Abstract-level read (full text paywalled at SAGE, re-checked 2026-09-20). Grade C by the rubric; the methodological point is uncontroversial and the null it prescribes is the repo's own arithmetic once implemented.

### 8. Do Betting Markets Sense a Goal Coming? Evidence from the German Bundesliga
Winkelmann & Deutscher, 2025, arXiv:2505.21275 — https://arxiv.org/html/2505.21275v1

**Method.** In-match implied probability regressed on pre-match probability, time terms, red
cards, xG and an anticipation term `1/minutes-to-goal`; beta state-space model for stakes.
**Data.** Bundesliga 2018/19, one bookmaker at 1 Hz, 256 matches. **Results.** No anticipation:
−0.005 [−0.012, +0.002] odds, +0.096 [−0.031, +0.222] stakes. **Limitations.** Bookmaker, one
season. Grade C for Kalshi.

## What this means for arb_engine

* **No FLB correction of exchange mids** in `quant/fairvalue.py` / `strategy/inplay.py`.
  `docs/MODEL.md` does not measure FLB; it attributes the market's deficit (0.4060 model vs
  0.4382–0.4425 Kalshi on 2,263 NFL scrimmage plays; 0.4875 vs 0.4478 on ≤ 4¢ books) to thin,
  slow books. Le's slope of 1.10 would move the market's log-loss by ~0.001–0.003, an order of
  magnitude below the measured gap ([−0.0491, −0.0140]) and below blend − model +0.0114
  [+0.0061, +0.0169]: fit `a, b` as a bounded diagnostic, not as the fix for the blend.
* **No fee term in the STEAL edge.** `backtest.py` (`all_in = ask + fee/contracts`) and
  `evaluate_inplay` already deduct the taker fee before `fair − all_in ≥ edge`; only the non-fee
  maker/taker wedge is new, and candles cannot measure it. Stratify replay P&L by price instead.
* **The STEAL evidence needs the Winkelmann null.** MODEL.md's executable ROI (−10.3 % / +2.7 %
  / +50.6 % at 3 / 5 / 8 % edges, every interval including zero) sits beside a shuffle placebo
  of +19.6 % / −48.6 % / +20.5 %; a Bernoulli(market fair) null in `simulate_pairings` is the
  paper's prescription.
* **De-vig is already right; extend it.** `quant/odds.py` implements Clarke's hierarchy, power
  default, `devig_range` as uncertainty (MODEL.md: 0.0181 mean / 0.0193 max over 16 closing
  lines). OO-EPC is three lines; the larger lever is an FL-GLM β on nflverse closing lines,
  since γ ≈ 0.07 means multiplicative leaves a price-dependent bias.
* **Regression form.** MZ / under-reaction regressions in `quant/eventstudy.py` and
  `quant/calibration.py` should use the normalised probability or exchange mid, WLS `p(1−p)`
  and game-clustered SEs, as `bootstrap_paired` already clusters.

## Recommendations

| change | module | metric it should move | offline test | grade | effort |
|---|---|---|---|---|---|
| MZ regression of outcome − Kalshi mid by price decile, normalised prob, WLS p(1−p), game-clustered | `quant/calibration.py`, `backtest.interval_report` | γ, ψ per decile with intervals (is there an in-play sports FLB?) | `backtest --week 1 --offline --cache-dir out/cache/replay`; pin structure on `tests/fixtures/history/replay_trim` | A (method) | S |
| Efficient-market null: `placebo="bernoulli"` drawing outcomes from the market fair, full STEAL pipeline | `backtest.simulate_pairings` | ROI sampling distribution under "the market is right" at each edge | same week-1 / NCAAF week-2 caches; compare with the shuffle placebo row | C (abstract-level source) / A once run | S |
| FL-GLM: one β by ML on nflverse closing moneylines 2016–2024, scored on 2025 | `quant/odds.py`, `scripts/` | log-loss of the sportsbook fair vs power / multiplicative | nflverse `games.csv` cache used by `build_margin_table.py` | B | M |
| Logistic recalibration `(a, b)` of the Kalshi market component, grouped CV | `quant/inplay_fair` blend, `models/cv.py` | market-only and blend log-loss (expect ≤ 0.003) | week-1 replay rows, `--offline` | B | S |
| OO-EPC as a fourth closed-form method in `DEVIG_METHODS` / `devig_range` | `quant/odds.py` | cross-method range on closing lines | `lines-eval --offline` reproduces `lines_eval_p13.json` plus the new column | B | S |
| STEAL replay P&L stratified by entry price bucket (< 0.20 / 0.20–0.80 / > 0.80) | `backtest.simulate_steal` | ROI and interval per bucket | week-1 cache; bucket counts (entries 0.17–0.83) | B | S |

## Open questions

1. Is there an in-play FLB in Kalshi **sports**? No paper answers it; the decile MZ regression
   does offline, and the "Record a live Sunday slate" step in `docs/ROADMAP.md` adds
   trade-tape (taker-side) resolution.
2. Does the maker/taker wedge hold on thin in-play NFL books? Same recorded slate
   (`venues/trades.py` carries the taker side); fill rates are not in candles.
3. Le's 0–1 h slope mixes horizons; recommendation 4 fits the per-quarter in-play slope.
4. CLV as a skill metric has no peer-reviewed exchange validation here (grade C); it waits on
   the recorded slate scored by `clv --db`.
5. Fractional-Kelly vs fixed 10 contracts is a cheap replay test, but only paired with the
   Bernoulli null, since Kelly amplifies fair-value error.
6. The de-vig effect on two-way NFL moneylines is likely smaller than on three-way football;
   the FL-GLM fit measures it.

# 07 — Forecast combination and calibration

*Cluster: extremizing linear/logit pools, Bayesian model averaging under regime shift,
isotonic / Platt / beta recalibration, CORP decompositions, sequential calibration
monitoring. Repo numbers are quoted from `docs/MODEL.md`.*

## Summary

A linear pool of one strong source and weaker correlated ones is usually worse than the
strong source alone, and a fitted stacker or beta/BLP layer on top recovers only
~0.001–0.002 in log score — far below the +0.011 the repo's grid already recovers by moving
to the model corner. The one tool with a clear payoff this season is diagnostic: a CORP
miscalibration/discrimination split per source, which `quant/calibration.corp_decomposition`
implements but `backtest.py` never reports, deciding whether Kalshi's in-play deficit
(0.438–0.443 vs the model's 0.406) is stale mids or missing information. Dynamic model
averaging and an e-process monitor were reviewed and dropped as unmeasurable at 16–86 games
a week.

## Papers

**Combining Probability Forecasts** — Ranjan, Gneiting (2010), JRSS-B 72(1), 71–91.
<https://academic.oup.com/jrsssb/article/72/1/71/7076442>
Theorem 1: if calibrated components p_i differ and w_i > 0 sum to 1, the linear pool
p = sum_i w_i p_i is uncalibrated and under-confident; its recalibration q = P(Y=1 | p) wins
under every proper score. Fix: p_BLP = H_{alpha,beta}(sum_i w_i p_i), H the Beta CDF
(alpha = beta = 1 is the linear pool, > 1 extremizes), fitted with the weights by maximum
likelihood. Data: probability of precipitation, 29 US cities 2003–2008, ~73,000 training
cases. Brier: best single 0.0815, linear pool 0.0800, BLP 0.0783; reliability 0.0021 → 0.0004,
so the whole gain is calibration. Limits: needs calibrated components and iid cases — the
repo's Kalshi mid and post-play ESPN are neither, and ~150 plays per game make the
effective sample games.

**Combining predictive distributions** — Gneiting, Ranjan (2013), EJS 7, 1747–1782.
<https://arxiv.org/pdf/1106.1638>
Theorem 3.1: a positive-weight linear pool is at least as dispersed as its least dispersed
component; Theorem 3.2: no such formula is coherent. Fixes: spread-adjusted pool and
G_{alpha,beta}(y) = B_{alpha,beta}(sum_i w_i F_i(y)) (eq. 8); k = 1 is a pure recalibration.
Data: simulation, Sea-Tac temperature, S&P 500 1962–1995 (4,133 / 4,298 train / test, GARCH-t
and MA(1)). Simulation PIT variance 0.066 (linear) vs 0.081 / 0.084 (SLP / BLP; neutral
0.083). S&P: weights 0.82 / 0.18, alpha = 1.100, beta = 1.081, linear pool's log score only
"very slightly lower" — "little reward" when components share information. Limits: gains
domain-dependent; the closest analogue to two correlated sources, and the BLP moved nothing.

**Bayesian Ensembles of Binary-Event Forecasts: When Is It Appropriate to Extremize or
Anti-Extremize?** — Lichtendahl, Grushka-Cockayne, Jose, Winkler (2018), arXiv:1705.02391.
<https://arxiv.org/pdf/1705.02391>
Private information only (Prop. 1): p_hat = F_{N_k}(-(k-1) F_0^{-1}(p_0) + sum_i
F_{n_i}^{-1}(p_i)), always extremizing; shared information (Props. 2–3) attenuates and can
anti-extremize. Generalized probit ensemble (eq. 10): p_hat = F(beta_0 F^{-1}(p_0) + sum_i
beta_i F^{-1}(p_i)) — a GLM of y on logit(p_i) with intercept; coefficient sum < 1 =
anti-extremizing, measured against the prior. Data: stacking lasso-logistic, random forest
and xgboost on Fannie Mae 2007 loans (1,056,724 rows) and Kaggle used cars (72,983). Log
score (loans / cars): xgboost 0.2337 / 0.2946; equal-weight average 0.2347 / 0.2966 (worse);
optimal linear pool 0.2330 / 0.2932; generalized probit 0.2327 / 0.2925. Limits: three base
models; exponential-family information. Closest analogue to arb_engine.

**Modeling Probability Forecasts via Information Diversity** — Satopää, Pemantle, Ungar
(2016), JASA 111(516). <https://arxiv.org/html/1406.2148>
Gaussian partial-information model: forecaster i sees X_{B_i} (Var delta_i, overlap rho_ij)
and reports p_i = Phi(X_{B_i} / sqrt(1 - delta_i)); with X_i = Phi^{-1}(p_i) sqrt(1 - delta_i)
the symmetric-overlap aggregator is p''_cs = Phi([sum_i X_i / ((N-1) lambda + 1)] /
sqrt(1 - N delta / ((N-1) lambda + 1))): high overlap lambda collapses onto one forecaster.
Data: Good Judgment Project year 2, 44 forecasters, 123 events. Brier: simple average 0.132,
log-odds 0.128, probit 0.128, information aggregator 0.123. Limits: needs many calibrated
streams; with three sources only the overlap diagnostic is usable.

**Online Prediction Under Model Uncertainty via Dynamic Model Averaging** — Raftery, Kárný,
Ettler (2010), Technometrics 52(1), 52–66. <https://pmc.ncbi.nlm.nih.gov/articles/PMC2895940/>
Model probabilities pi_{t|t-1,k} = pi_{t-1|t-1,k}^alpha / sum_l pi_{t-1|t-1,l}^alpha, updated
by the one-step predictive likelihood; alpha < 1 lets weights recover after a shift. Data:
cold-rolling mill, 19,058 samples, 17 models, alpha = lambda = 0.99. MSE 68.9 vs 77.5 during
start-up, 20.6 vs 20.7 when stable — gains only under regime change. Limits: continuous
regression, hand-set forgetting; a game of ~150 dependent plays would need a tempered
likelihood.

**Beta calibration** — Kull, Silva Filho, Flach (2017), AISTATS, PMLR 54.
<https://proceedings.mlr.press/v54/kull17a.html>
logit(mu) = a ln s - b ln(1 - s) + c, a logistic regression on (ln s, -ln(1 - s)); identity
is a = b = 1, c = 0, which Platt cannot represent, so Platt can un-calibrate a calibrated
model; a = b = 1/k undoes k-fold over-extremization. Data: 41 UCI datasets, Naive Bayes and
two Adaboost scorers, 10x5-fold CV. Ranked first on log-loss for all three (Friedman
p = 6.9e-17, 1.0e-12, 4.7e-15), never significantly beaten by isotonic or logistic. Limits:
binary; matters in the small-sample regime, which is where the repo sits.

## What this means for arb_engine

*The blend penalty is a weight problem, not an extremizing problem.* On NFL week 1 the
0.30 / 0.55 / 0.15 blend scores 0.4173 vs the model's 0.4060 (blend − model +0.0114
[+0.0061, +0.0169]) and `backtest.fit_blend_weights` reaches the model corner under both
pools. Lichtendahl shows the same sign at a tenth of the size, and the fitted stacker's
gain over xgboost was ~0.001. Since `games_needed` scales as 1/delta², a 0.001 effect needs
~10 × (0.0114 / 0.001)² ≈ 1,300 games against the 10 quoted for the observed +0.0114: a
GLM or BLP layer is a diagnostic here, not a live pool.

*Ranjan–Gneiting's premise fails in play.* Kalshi scores 0.4425 / 0.4382 (before / after)
against 0.4060, and 0.4875 vs 0.4478 on ≤ 4¢ books; ESPN is post-play (88.5 % of its jumps
land before the play's entry). A dominated component dilutes discrimination, which no
recalibration restores. CORP settles it: if Kalshi's MCB is large and its DSC near the
model's, a k = 1 beta map on the mid inside `quant/inplay_fair.blended_fair` could earn the
market its weight back; if the gap is DSC the deferred "move the weights toward the model"
row is right to wait. The honest prior is DSC: the gap grows through the game (Q3 0.3776 vs
0.4357) instead of sitting at a fixed offset, and it is widest on timeout / kickoff-pending /
try rows (mean |model − Kalshi| 0.05–0.06) where the mid has not re-priced — a lag that
costs discrimination, not a monotone bias a map could undo.

*Beta calibration of the model should come out as identity.* The 2025 ECE is 0.020 and the
gaps are not tail-only (0.2–0.3 +0.040, 0.8–0.9 −0.043); `docs/MODEL.md` calls it season noise
because nflfastR shows the same shape (+0.028 / −0.040), and 2025 was the selection season.
The gate is load-bearing: ship the map only if (a, b) leave 1 by more than 1.65
game-bootstrap SEs. No 2026 reliability table exists yet.

*Effective sample size governs every fit.* 2,263 NFL or 12,771 college rows are 16 or 86
outcomes; `fit_blend_weights` minimises unweighted row log-loss, so a blowout dominates.
Weight rows 1/n_game and cross-validate with the grouped folds in `models/cv.py`.

*Tie ordering.* Any map must act on `home_p` before the `p_tie` split in `blended_fair`,
because `strategy/inplay.evaluate_inplay.leg_fair` builds the Rothera YES leg from
`blend.win` downstream.

*Dropped.* DMA gains only under regime change and both weeks already sit at the model
corner; reopen if CORP shows the market's DSC rising. An e-process monitor on one row per
game per clock stratum: this review's own simulation (not a published number) gave median
e ≈ 1.0–1.2 after 16 games and 300–1,400+ games to reach e = 20, so it cannot replace
`games_needed`.

## Recommendations

| # | change | module | metric it should move | offline test | grade | effort |
|---|---|---|---|---|---|---|
| 1 | CORP MCB / DSC / UNC per source in the week report, rendered into `docs/MODEL.md` | `backtest.week_report` → `quant/calibration.corp_decomposition`, `scripts/render_results.py` | none; decides MCB (recalibrate mid) vs DSC (keep market weight ~0) | `ReplayTrimFixtureTests` gains a `corp` key on the 2-game trim cache; unit: MCB = 0 on an isotonic vector, brier = MCB − DSC + UNC | A | S |
| 2 | 1/n_game row weights in `fit_blend_weights` and all calibration fits; grouped CV | `backtest.fit_blend_weights`, `models/cv.py` | OOF variance of fitted weights; best-grid − current interval | weighted = unweighted when games have equal rows; on the W1 week cache (`--offline --cache-dir out/cache/replay`) the best point stays at the model corner, matching `replay_nfl_2026_w1.json` | B | S |
| 3 | `beta_calibration(p, y, weights)` with game-bootstrap SEs; apply to `home_p` only if \|a−1\| or \|b−1\| > 1.65 SE | `quant/calibration.py`, `quant/inplay_fair.blended_fair` | model log-loss / MCB on 2026 replays; expected no change | recovers (1, 1, 0) on calibrated synthetic data, (1/2, 1/2) on 2× extremized; gate silent on the trim fixture | B | M |
| 4 | GLM stacker diagnostic on logits (game-weighted, grouped CV): coefficient sum and k = 1 Kalshi (a, b) per quarter | `backtest.fit_blend_weights` (`stacker` key) | OOF log-loss vs model corner (≤ 0.002 expected); not live | two identical synthetic sources → sum ≈ 1; trim fixture smoke | B | M |
| 5 | Reliability bands by source for the 2026 replays | `backtest.week_report` → `quant/calibration.reliability_band` | none; gates rec 3 | bins n < 10 count-only; band covers a calibrated synthetic stream | A | S |
| 6 | Overlap diagnostic: correlation of probit(model), probit(kalshi) residuals per quarter | `quant/calibration.py`, `backtest --slices` | none; explains the model-corner collapse | synthetic overlapping streams | C | S |
| — | Dynamic model averaging; e-process monitor | none | — | — | dropped (C) | — |

## Open questions

1. **Effective sample size** — 1/n_game weights or one row per clock stratum? Rec 2 on the
   committed weeks; feeds the deferred blend-weight gate (`backtest --pool`).
2. **MCB or DSC?** Rec 1 on candles; the **"Record a live Sunday slate"** step ("Needs
   you") shows whether the mid was stale or the book empty on real L1 ticks.
3. **Stacker coefficient sum by quarter** — shared-information attenuation (Lichtendahl)
   or diverse-information extremizing (Satopää)? Rec 4; same recorded-Sunday step.
4. **Does the 2025 tail pattern persist in 2026?** Rec 5 bands; the clean answer is the
   "Retraining the WP model on 2025–2026" row once the 2026 season file is final.
5. **Tie ordering** — the Rothera YES tie payout is itself unverified: the **"Paste the
   Rothera tie clause"** step.
6. **STEAL P&L** is a bounded payoff, not a proper score; a bounded-mean confidence
   sequence is not reviewed here, and the ROADMAP's ~70-game gate stands.
7. Satopää et al. (2014, IJF) was not fetchable (ScienceDirect 403); read the journal copy
   before quoting its numbers.

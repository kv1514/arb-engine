# 09 — Machine learning for pre-game and in-game outcome prediction (2024–2026)

*Cluster: gradient boosting, sequence / transformer models on play-by-play, LLM forecasters
and agents — does any of it beat the closing line, and what is worth replicating. Repo
numbers are quoted from `docs/MODEL.md` (2025 hold-out, NFL 2026 week 1, college 2026
week 2) and `docs/ROADMAP.md`.*

## Summary

Nothing published in 2024–2026 shows a pre-game ML model beating a sharp closing line out of
sample over more than one season, and the LLM results (five frontier agents, all negative ROI
on one EPL season) argue against importing that class at all. What matters for this repo is
statistical rather than architectural: Brill–Yurko–Wyner show an NFL win-probability model
fit to hundreds of thousands of plays carries the information of roughly half its games,
which puts the shipped stack (2,467 training games, unconstrained residual trees, a
documented 17-vs-16-point dip) exactly where XGBoost needs shrinkage and 5–6-point intervals.
The two usable ideas are Beuoy's play-by-play Kelly contest, which asks the STEAL question
("which source would have taken the others' money?") more directly than pooled log-loss, and
Moshrefi's last-ten-minutes calibration break on Kalshi, to be measured on the cached
NFL / NCAAF candles before anything is gated on it.

## Papers

**Exploring the Difficulty of Estimating Win Probability: A Simulation Study** — Brill,
Yurko, Wyner (2025), arXiv:2406.16171v5. <https://arxiv.org/html/2406.16171v5>
*Method.* A "random walk football" simulator (ball moves ±1 yardline per play, L = 4
yardlines, T = 56 first-down plays per game) where the true WP is computable by dynamic
programming; XGBoost WP models fit to simulated datasets of the real dataset's nominal size
(G = 4,101 games, 229,635 plays) with varying dependence K (plays sharing one outcome);
RMSE(WP_hat − WP_true), bias by state, coverage of a cluster bootstrap and of a *fractional*
bootstrap resampling a share phi of games. *Data.* Matched to nflverse 2005–2020 first-down
plays. *Numbers.* Effective sample size ≈ 2,291 games (~56 % of 4,101): sixteen seasons of
plays carry ~eight seasons of games. RMSE rises linearly in K. 90 % coverage needs intervals
of mean width 6.3 WP points; the naive cluster bootstrap covers 71 %, the fractional
bootstrap at phi = 0.35 ~90 % (85 % near WP 0.3 / 0.7). Bias is worst early at large
differentials. *Limits.* Authors: real football is messier, so real ESS is likely worse; phi
cannot be tuned on real data; hyperparameter-dependent. Mine: no market comparison, so it
cannot say whether the market is more or less uncertain than the model.

**Prices, Probabilities, and Parlays: Systematic Bias in Sports Prediction Markets** —
Moshrefi (2026), arXiv:2607.14430v1. <https://arxiv.org/html/2607.14430>
*Method.* ~23 M Kalshi moneyline trades bucketed by time-to-expiry; per bucket a power-law
recalibration p_true = p^gamma / (p^gamma + (1 − p)^gamma) and Platt scaling with slope a; a
quadratic-in-log meta-regression of (gamma, a) on time-to-expiry; a Prelec weighting
w(p) = exp(−(−ln p)^alpha) for the near-expiry distortion; 12,639 cross-game parlays vs the
product of leg prices. *Data.* Kalshi NBA / NHL / MLB, March–May 2026. *Numbers.*
Mid-contract prices calibrated (gamma_hat ≈ 1, a_hat ≈ 1); in the final ten minutes
gamma_hat ∈ [1.27, 1.31], a_hat 1.62–4.56 — prices compressed toward 0.5, the trailing side
over-priced. Two-leg parlays ≈ fair (0.99), ten-leg ~1.22. *Limits.* Authors: cannot
separate insurance demand from liquidity withdrawal or herding; independence across games
assumed. Mine: trades, not quotes, so the taker edge after the 7 %·p(1 − p) fee is smaller
than the calibration gap; no NFL / NCAAF.

**Kelly Betting as Bayesian Model Evaluation** — Beuoy (2026), arXiv:2602.09982v1.
<https://arxiv.org/html/2602.09982>
*Method.* Each model i is a Kelly bettor with bankroll b_i; consensus
p_market = sum_i p_i b_i / (1 − sum_i p_i w_i), w_i the models' existing win shares; positions
update play by play as w'_i = (p_i / m_i) · sum_j m_j w_j; the bankroll ratio equals the
posterior odds, posterior = (p_model / p_market) × prior, readable before the game resolves.
*Data.* First-to-100 simulation (correct model vs wrong-point-probability / faulty-recency /
drift models, 50 games × 1,000 reps); NFL 2023–24 (4 games), MLB 2022, NBA 2023 playoffs
(84 games, ~31,000 plays). *Numbers.* Correct-model identification, Kelly vs log-loss vs
Brier: faulty-recency 96.0 % vs 73.1 % vs 80.2 %; non-predictive variable 74.4 % vs 57.6 % vs
58.3 %; wrong point probability 55.1 % vs 49.9 % vs 49.9 %. After 50 games the contest names
the right model 55 % (43 % ties) vs 2 % for log-loss. NBA playoffs: FiveThirtyEight +13.8 %
credibility, driven by single improbable plays. *Limits.* Single-author preprint, simulation
power claims only; rewards a volatile model whose sharp moves resolve its way, so luck
dominates for a while; real applications are 4–84 games.

**Machine learning for sports betting: should model selection be based on accuracy or
calibration?** — Walsh, Joshi (2024), Machine Learning with Applications; arXiv:2303.06021v4.
<https://arxiv.org/html/2303.06021v4>
*Method.* NBA game models (logistic, RF, SVM, MLP) on box-score aggregates; one feature
branch maximises accuracy, one minimises classwise expected calibration error,
classwise-ECE = sum_k sum_b (n_b / N) · |mean p_k in bin b − observed frequency of k in b|,
20 bins, ≥ 80 % non-empty to block predict-the-base-rate. Bet when p_model > 1 / decimal
odds; fixed $100 or eighth-Kelly f = (1/8)(b p − q) / b. *Data.* Train 2014/15–2015/16,
validate 2016/17, test 2017/18, bet on 2018/19 vs Westgate closes (~1,000 bets, 88–90 % of
games). *Numbers.* Calibration-selected SVM +32.45 % ROI fixed / +36.93 % eighth-Kelly;
accuracy-selected SVM +5.56 % / −75.9 %; model averages +34.69 % vs −35.17 %; hit rate
~38.8 % vs 38.5 %. *Limits.* Authors: one season, arbitrary 80 % rule, no in-game data. Mine:
+35 % on 88 % of games against a Vegas close is outside what efficiency allows and is one
favourable underdog season; no interval; vig not removed, so "value" lands on longshots.
Direction sound, magnitude unreliable.

**KellyBench: A Benchmark for Long-Horizon Sequential Decision Making** — Grady, Parker,
Zarov, Course, Taylor, Taylor (2026), arXiv:2604.27865v1. <https://arxiv.org/html/2604.27865v1>
*Method.* Agentic environment: 120 EPL 2023/24 matchdays at closing bookmaker odds (5.3 %
overround), log-wealth reward, ≥ 1 bet per matchday, agents may build their own models, 5
seeds; baselines favourites-only 5 % stake, Dixon–Coles, a human quant, an AI researcher.
*Data.* 380 matches; GPT-5.4, Claude Opus 4.6, GLM-5, Gemini 3.1 Pro, Kimi K2.5. *Numbers.*
Average ROI −7.9 % (GPT-5.4; seeds +34.1 % to −32.9 %), −11.2 %, −51.6 %, −66.0 %, −89.6 %;
3 of 25 seeds positive; three models bankrupt on at least one seed. Human quant +5.1 %, AI
researcher −4.3 %, Dixon–Coles −15.4 %. *Limits.* Authors: efficient league, one season,
bookmaker odds, possible outcome leakage (which strengthens the negative result). Mine:
losses are dominated by staking discipline — LLM agents cannot size, on top of not forecasting.

**A Systematic Review of Machine Learning in Sports Betting** — Galekwa, Tshimula, Tajeuna,
Kyandoghere (2024), arXiv:2410.21484v1. <https://arxiv.org/html/2410.21484v1>
*Method / data.* PRISMA-style review of 219 papers, 2010–2024. *Numbers.* Gradient boosting /
ensembles are the most consistently reported winners, deep learning mixed, logistic
regression competitive; reported ROI where any: soccer 1.6–5.4 %, tennis 3.3–4.4 %; almost
no in-play work outside cricket. *Limits.* Evidence quality is not weighed: the 91–94 %
"accuracies" are leakage or in-sample and the ROI numbers are single-season without
intervals. A map, not evidence.

**Evaluating real-time probabilistic forecasts with application to NBA outcome prediction** —
Yeh, Rice, Dubin (2020), arXiv:2010.00781 / The American Statistician.
<https://arxiv.org/abs/2010.00781>
*Method.* Calibration surfaces over (time, predicted probability) and tests of dynamically
updated forecasts against naive baselines and a logistic regression on team strength plus
score differential. *Data.* ESPN NBA in-game WP feeds. *Numbers.* ESPN is well calibrated
(Brier ~0.075 per the review) and beats naive baselines but shows no significant skill over
the logistic regression. *Limits.* Pre-window, ESPN only; included because it is the bar the
one 2026 sequence-model paper (Cicek, abstract only, no numbers) clears — beating ESPN is
beating a logistic regression, not a market.

## What this means for arb_engine

*Model class.* The shipped stack (`arb_engine/models/wp.py`, trained by
`scripts/train_wp_model.py`: logistic IRLS baseline plus 200 XGBoost residual rounds, depth 4,
`min_child_weight=20`, no monotone constraints) is the class the review literature keeps
picking; nothing here argues for a sequence model or an LLM. Brill's ESS reframes
`docs/MODEL.md`: 56 % of 2,467 training games is ~1,380 independent games, and since Brill's
56 % is for first-down plays (K = 56) while the repo trains on all downs (362,579 plays,
~147 per game) the true ESS is probably lower. That is the regime where Brill's XGBoost dips,
and the repo documents the dip: "the shipped export scores a 17-point Q2 lead *below* a
16-point one ... about 10 points of win probability", patched at inference by the running-max
guard in `home_win_probability`. `--monotone` costs "~0.003 log-loss on 2025", against a
2025 gap to nflfastR of 0.0017 the file itself calls "not an established edge" on a season
that "doubled as the selection set". The week-1 relative gap to Kalshi (0.406 vs 0.438–0.443,
interval [−0.055, −0.018], 10–12 games needed) is real; Brill says the model's *absolute*
error is nonetheless several points, which is why STEAL at a 3 % edge loses on both honest
pairings (−11.7 % / −10.3 %) while the 8 % rows (+16.4 % / +50.6 %, intervals [−1.20, +2.63] /
[−0.43, +3.80]) are a 10–14-game sign.

*Evaluation.* Beuoy's contest is the missing report. `backtest.week_report` pools log-loss
per play, which charges a slow market for every stale minute; a play-by-play Kelly contest
between `model`, `espn`, `kalshi_before` and `kalshi_after` reads off, per game, whether the
model's bankroll grows before settlement (re-pricing lag, which pays only if you can hold) or
at settlement (a mispriced market, which pays STEAL). The `q4_late` slice (Kalshi 0.1345 /
0.1299 vs model 0.1297 vs ESPN 0.1246, 314 rows) shows no NFL late deficit, so Moshrefi's
compression is a hypothesis for `strata_tables`, not a gate; the college `q4_late` gap
(0.2379 / 0.2212 vs 0.1783) mixes candles capped at 0.99.

*Calibration.* Walsh–Joshi's direction (select on calibration, not accuracy) is already the
repo's practice — the 2025 reliability table, ECE 0.020, `quant/calibration.py`'s CORP and
reliability-band tools — but `backtest.py` reports none of it for the 2026 replays (section
07, recs 1 and 5). The 0.8–0.9 bin gap (−0.043, nflfastR −0.040) is season noise; a source
switch on trailing ECE over two replayed weeks would flip on that noise.

*Do not build.* KellyBench (all five agents negative, human quant +5.1 %) and the claim check
on LLM forecasters (best cell Brier 0.497 vs 0.498 de-vigged close, no paired comparison
surviving Holm) close the LLM route; Cicek's sequence model beats only ESPN, which
Yeh–Rice–Dubin show is a logistic regression in disguise, and at ESS ~1,400 games extra
capacity is variance.

## Recommendations

| # | change | module | metric it should move | offline test (week caches of one prior network run unless marked unit / committed) | grade | effort |
|---|---|---|---|---|---|---|
| 1 | Retrain with `--monotone` and heavier shrinkage (`min_child_weight` 50–100, fewer rounds), select on 2024, score once on 2025; drop the inference running-max guard once the export has no dips | `scripts/train_wp_model.py`, `models/wp.home_win_probability` | 2025 log-loss (accept ≤ +0.003 vs 0.4751); 17-vs-16 dip → 0; week-1 replay log-loss vs 0.4060 | `tests/test_wp_model.py`: integer leads 0–30 at fixed clock / spread monotone on the raw walker; `backtest --week 1 --offline` on the week cache (`out/cache/replay`, one prior network run; `replay_nfl_2026_w1.json` pins the numbers only) keeps model − Kalshi clear of zero | A (dip) / B (retrain effect) | M |
| 2 | Interval-width table: fractional game bootstrap (phi ≈ 0.35, 50–100 offline refits) exported as width by (quarter × lead bucket) to `data/nfl_wp_width.json`; STEAL requires edge ≥ width (no fee term: the taker fee is already inside `all_in` in `backtest.py` / `evaluate_inplay`), LOCK shows hold EV with the band | `scripts/train_wp_model.py`, `strategy/inplay.evaluate_inplay`, `backtest.simulate_steal` | STEAL-qualifying rows at 3 / 5 / 8 % (159 / 35 / 7 on week 1); honest-pairing P&L interval width | Unit: widths monotone in clock; `simulate_steal` on the W1 / W2 week caches (`--offline --cache-dir out/cache/replay`) with the gate trades less and no worse, checked against the `replay_*.json` summaries | B | M |
| 3 | Play-by-play Kelly contest (Beuoy) between model / ESPN / kalshi_before / kalshi_after per game: bankroll ratios at end of Q3 and at settlement, game-cluster bootstrapped | `backtest.week_report`, `quant/calibration.py` helper | separates "market re-prices late" from "market wrong at settlement"; nothing live | Unit: correct model wins ≥ 90 % of 200 synthetic first-to-N games; `replay_trim` cache smoke | B | S |
| 4 | Measure Moshrefi's late compression on the cached candles: Platt slope a and gamma for kalshi_before / after at `gsr ≤ 600` vs the rest, with bands; no gating | `backtest.strata_tables`, `quant/calibration.reliability_band` | none; decides whether a q4_late multiplier is ever built | week-1 / week-2 fixtures report a_hat with a 90 % band; unit recovers a = 1 on calibrated synthetic data | B | S |
| 5 | Reliability table + classwise ECE + CORP split per source for the 2026 replays, rendered into `docs/MODEL.md` (joint with section 07 recs 1 / 5); no source switching on it | `backtest.week_report`, `quant/calibration.corp_decomposition`, `scripts/render_results.py` | none; tests whether the 2025 tail gap (0.851 → 0.808) persists | trim-fixture smoke; `tests/test_docs_results.py` renders the block | B | S |
| 6 | Live-servable feature ablation: drive plays / yards and turnover differential (no nflverse EPA) via `models/cv.py` grouped folds, selected on 2024 | `scripts/train_wp_model.py`, `models/cv.py`, `venues/espn.py` | 2024-selected log-loss gain must exceed its game-cluster interval before shipping | `pbp_2025_trim.csv.gz` builds the columns; folds never split a game | C | M |
| 7 | Pre-game anchor ablation: spread from pickcenter vs inverted from the pre-game Kalshi mid (`resolve_spread` fallback) vs the de-vigged sportsbook close | `backtest.resolve_spread`, `backtest.lineless_report` | q1 log-loss (0.5537 model vs 0.5713 Kalshi) | `backtest --offline` on the week-1 cache with each source forced; `SpreadFallbackTests` gains a case | C (Clegg not verified here; unit staking lost) | S |
| — | LLM forecaster / agent; transformer or sequence model on play-by-play | (none) | — | — | B (negative; EPL at bookmaker odds, transferred) | — |

## Open questions

1. Does NFL / NCAAF Kalshi in play show Moshrefi's last-ten-minute compression? Rec 4
   measures it on the cached candles now; whether the *executable* edge survives the taker
   fee needs **"Record a live Sunday slate"** in `docs/ROADMAP.md` (`backtest-ticks`, `clv`
   on real L1 ticks instead of candle closes).
2. What is the effective sample size of the 2016–2024 training set? Brill's ~56 % is for
   first-down plays in a simplified game; on all downs it is probably lower, which sets how
   much shrinkage rec 1 needs. Settled by **"Retraining the WP model on 2025–2026"**, once
   the 2026 file is final and 2024 can be the selection season.
3. Is the week-1 gap (0.406 vs 0.438–0.443) a slow market or a good model? Rec 3 separates
   re-pricing lag from settlement error; only the second pays a hold-to-settlement STEAL.
   Settled by the recorded Sunday plus the ~70 NFL games **"In-play STEAL as a strategy"**
   already asks for.
4. Clegg et al.'s +4.5 % in-play ROI (Kelly only; unit staking −3.4 %; prices two minutes
   after the prediction; behind Betfair on every proper score) has the shape of the repo's
   leaky pairing, which went from +19 % / +44 % to −10 % / +3 % / +51 % with zero-crossing
   intervals once made executable. Unreplicated until rerun on bid / ask prices; no roadmap
   item, the harness is the tool. Hubáček's decorrelation penalty −c·(p_hat − 1/o)^2 with
   the Kalshi mid as o is likewise untried in play and needs far more than 16 + 86 games.
5. Whether play history adds anything beyond state at ESS ~1,400 is what rec 6 answers
   cheaply; no 2024–2026 paper evaluates a sequence model against a live market.
6. Bürgi–Deng–Whelan's "contracts above 50 c earn a small positive return" predates Kalshi
   sports at scale; if it holds on 2026 NFL moneylines it favours the maker runner's resting
   bids on favourites over taking longshots. Settled by the **"Kalshi demo-key check"** and
   then the first recorded fills, which give the sign of maker-vs-taker returns.
7. Every positive ROI here is single-season with no game-level interval; the repo's
   games-needed column is stricter than any published paper in this cluster and should stay so.

## Bibliography

- Brill, Yurko, Wyner 2025. Exploring the Difficulty of Estimating Win Probability: A Simulation Study. arXiv:2406.16171v5. <https://arxiv.org/html/2406.16171v5>
- Moshrefi 2026. Prices, Probabilities, and Parlays: Systematic Bias in Sports Prediction Markets. arXiv:2607.14430v1. <https://arxiv.org/html/2607.14430>
- Beuoy 2026. Kelly Betting as Bayesian Model Evaluation: A Framework for Time-Updating Probabilistic Forecasts. arXiv:2602.09982v1. <https://arxiv.org/html/2602.09982>
- Walsh, Joshi 2024. Machine learning for sports betting: should model selection be based on accuracy or calibration? Machine Learning with Applications; arXiv:2303.06021v4. <https://arxiv.org/html/2303.06021v4>
- Grady, Parker, Zarov, Course, Taylor, Taylor 2026. KellyBench: A Benchmark for Long-Horizon Sequential Decision Making. arXiv:2604.27865v1. <https://arxiv.org/html/2604.27865v1>
- Galekwa, Tshimula, Tajeuna, Kyandoghere 2024. A Systematic Review of Machine Learning in Sports Betting: Techniques, Challenges, and Future Directions. arXiv:2410.21484v1. <https://arxiv.org/html/2410.21484v1>
- Yeh, Rice, Dubin 2020. Evaluating real-time probabilistic forecasts with application to National Basketball Association outcome prediction. arXiv:2010.00781; The American Statistician. <https://arxiv.org/abs/2010.00781>

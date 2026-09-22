# 01 — In-play NFL win-probability models: model vs market for a US in-play trader

**Summary.** The literature agrees with the repo's replay that a state model on nflfastR's
feature set is at least as good as an in-play market mid (NFL week 1: model 0.406 log-loss vs
Kalshi 0.438–0.443), and the one paper measuring Kalshi live prices finds the mid absorbs only
~64 % of a benchmark WP change on impact and drifts the rest in over 5–15 min. It also says a
WP point estimate carries several points of sampling noise (effective sample size ≈ 56 % of
games), that a pre-game anchor's weight should decay with elapsed time and lead, and that
kneel/kickoff/try/OT states lie outside the canonical training rows, as `data/nfl_wp_rules.json`
assumes. Nothing here shows a hold-to-settlement taker beats the Kalshi fee; that stays open.

## Verified papers

**Baldwin (2021), NFL win probability from scratch using xgboost in R**, Open Source Football,
https://opensourcefootball.com/posts/2021-04-13-creating-a-model-from-scratch-using-xgboost-in-r/.
The nflfastR recipe: rows with down, clock, `yardline_100`, `score_differential`, `qtr <= 4`,
ties dropped; `diff_time_ratio`, `spread_time`, `home`; 5-fold CV grouped by `game_id` on
2001–2018. Tuning logloss 0.44787 unconstrained vs 0.44826 with a monotone constraint on
`spread_time` (+0.0004); 2019–2020 test 0.427. Limits: no overtime or 4th-down-intent
handling (author's own flag); the repo measured the same switch at ~0.003 (`--monotone`,
MODEL.md), 7x Baldwin's. Grade A.

**Lock & Nettleton (2014), Using random forests to estimate win probability before each play of
an NFL game**, JQAS 10(2), https://econpapers.repec.org/RePEc:bpj:jqsprt:v:10:y:2014:i:2:p:9:n:10.
Random forest, 2001–2012; down, differential, time, adjusted score `diff / f(time)`, spread,
timeouts, **total points scored**, yard line, distance; WP = vote fraction. Abstract only
(publisher 403/405); Maddox et al. re-implement it at Brier 0.1705. Limits: step-wise, poorly
calibrated at the extremes. Grade C.

**Yurko, Ventura & Horowitz (2018), nflWAR**, arXiv:1802.00998 / JQAS 15(3),
https://arxiv.org/abs/1802.00998. EP by multinomial logit on the next score; WP by a GAM on
`omega = score_differential + EP(state)`, time, half, timeouts; drive-level resampling. No WP
accuracy in the abstract; superseded on calibration by nflfastR. Grade C.

**Maddox, Sides & Harvill (2022), Bayesian estimation of in-game home team win probability for
FBS college football**, arXiv:2207.13747, https://arxiv.org/abs/2207.13747. Beta–binomial
cells over (time t, lead l, expected possessions tau, expected differential omega):
`p_hat = (n + alpha_{t,l}) / (N + alpha_{t,l} + beta_{t,l})`, 14-expert prior;
`tau = ((3600 − t)/3600)·(xi_1 + xi_2)/2`; anchor `p* = (1 − D2)·p_pregame + D2·p_hat`,
`D2 = 0.09589 + 0.00018·t + 0.02523·l`. ESPN pbp 2004–2021, 2017–2021 test: Brier 0.1250 vs
0.1453 unanchored vs 0.1705 Lock–Nettleton. Limits: college, Brier only, D2 fitted on the test
seasons. Grade B for the *shape*.

**Brill, Yurko & Wyner (2023), Analytics, have some humility**, arXiv:2311.03490 / The American
Statistician 2025, https://arxiv.org/abs/2311.03490. XGBoost WP on first-down plays with
`scoreTimeRatio = diff / (0.01 + gsr)`, monotone constraints; randomized cluster bootstrap
(games, then drives), B = 101; 4,101 games 2006–2021. 48 % of 2018–2022 fourth-down calls
"confident" (>= 83 % of refits), 27 % "uncertain"; marginal-play 90 % intervals span about
−4 to +5 pp. Limits: sampling noise only; first-down rows. Grade B.

**Brill, Yurko & Wyner (2024), Exploring the Difficulty of Estimating Win Probability**,
arXiv:2406.16171 / JQAS, https://arxiv.org/abs/2406.16171. "Random walk football" with true WP
by dynamic programming; XGBoost; fractional bootstrap resampling `G·phi` games. 4,101-game
ESS = 2,291 (56 %); nominal-90 % coverage / width: row 0.60 / 0.027, cluster 0.71 / 0.036,
randomized 0.76 / 0.042, phi = 0.5 0.85 / 0.055, phi = 0.35 0.90 / 0.063. Limits: toy game,
no spread; phi needs a known truth. Grade B.

**Angelini, De Angelis & Singleton (2022), Informational efficiency and behaviour within
in-play prediction markets**, IJF 38(1),
https://www.carlsingletoneconomics.com/uploads/4/2/3/0/42306545/information_efficiency_angelini_de_angelis_singleton.pdf.
WLS with weights `p(1 − p)`: `y − p_{t+h} = gamma_0 + gamma_1·t + gamma_2·t^2 + beta·p_tau + u`,
efficiency curve with 90 % bands, split by favourite/longshot scorer. Betfair, 1,004 EPL
matches, 10-s ticks. Pre-match beta = −0.2599 / −0.2530; early favourite goals over-priced,
late longshot goals under-priced, significant to 5 min; gross ROIs 40–56 % before 2–5 %
commission. Limits: soccer, in-sample cells. Grade B.

**Angelini & De Angelis (2026), When Do Markets Fully Process Public Information?**,
arXiv:2606.07811, https://arxiv.org/abs/2606.07811. Cross-fitted logit benchmark q; updating
`Delta p = alpha + beta·Delta q + Gamma·X + eta` (beta = 1 if efficient); drift
`p_{t+h} − p_t = alpha_i + delta_t + rho·Gap_t + eps`, Gap = Delta q − Delta p. Kalshi NBA,
1,438 games, 409,512 contract-minutes. Brier benchmark 0.164 = live mid 0.164; beta = 0.630
(SE 0.005); rho raw / net 0.195 / 0.459 at 5 min, 0.236 / 0.484 at 15; 5-min midpoint return
+0.39 to +0.87 % but executable −1.20 to −0.36 %. Limits: NBA; round trip pays the spread
twice, no taker fee. Grade A diagnostic, B rule.

## What this means for arb_engine

* **Replicates the model-over-market finding and explains LOCK.** beta = 0.64 is MODEL.md's
  shape: the after candle beats the before candle (0.438 vs 0.443), model − Kalshi is
  [−0.055, −0.018]. A hedge inside the drift window sells the edge back and pays a second
  spread, as every NFL break-even LOCK row shows (−4.7 / −6.3 / −13.0 / −9.6 %). Not universal:
  college W2's 10 % LOCK is +6.3 % [+0.39, +0.72] on 25 games. `backtest.py` already holds
  `model_after` and `kalshi_after` per row, so the updating regression is a stdlib solve.
* **STEAL edges sit inside model noise, but 5–8 % is not shown to be outside it.** The
  executable pairing is −10.3 / +2.7 / +50.6 % at 3 / 5 / 8 % on 16 / 14 / 10 games, all
  intervals through zero, while the one-candle-later placebo posts +2.6 / +39.8 / +55.6 %.
  Retire 3 % from the headline; do not crown 5–8 %.
* **Time-varying weights are the right question, but the mechanism is mostly in the model**:
  `spread_time = spread·exp(−4·elapsed_share)` is D2's decaying anchor. The market weight sits
  at the model corner on NFL (blend − model +0.011 [+0.006, +0.017]) and at 0.10–0.20 on
  college; `w_market(t, lead)` must clear the MODEL.md weight-change policy.
* **Monotone constraints**: the repo's 0.003 exceeds the whole model-vs-`vegas_wp` gap
  (0.0017); keep the running-max guard until re-measured under `models/cv.py loso_folds`.
* **Kneel/kickoff/try/OT** are outside every canonical model, justifying the rules layer; W1
  shows 35 kneel rows, one STEAL-qualifying at 3 %, so the ROADMAP gate stands.

## Recommendations

| # | change | module | metric | offline test | grade | effort |
|---|---|---|---|---|---|---|
| 1 | Updating regression + 1/5/15-min drift table (Delta q = Delta `model_after`, Delta p = Delta `kalshi_after`) | `quant/eventstudy.py`, `backtest.py interval_report` | beta-hat, rho-hat on NFL; sets gap g | `backtest --week 1 --bar-mode both --offline`; NCAAF W2 | A | S |
| 2 | Gap-aware STEAL (edge AND last-60-s Delta model − Delta mid >= g, start 2 pp); LOCK blackout 15 min unless guaranteed > hold EV + rho_net·gap | `strategy/inplay.py evaluate_inplay`, `backtest.py simulate_pairings` | hold P&L per contract at 5 / 8 % vs both placebos; LOCK ROI | `backtest --week 1 --placebo`; rows surviving of 159 / 35 / 7 | B | M |
| 3 | `fractional_bootstrap(phi=0.5)` column beside phi = 1; drop 3 % from the headline | `quant/calibration.py`, `backtest.py interval_report` | interval widths; model − Kalshi still excludes zero | `backtest --week 1 --offline`; `tests/test_calibration.py` | B | S |
| 4 | `w_market(t, lead) = clamp(a0 − a1·elapsed_share − a2·\|lead\|/7, 0.05, 0.6)` fitted by game-cluster log-loss | `quant/inplay_fair.py blended_fair`, `backtest.py fit_blend_weights`, `walk_forward` | scrimmage log-loss vs model-only (0.4060 NFL, 0.2156 NCAAF) | `backtest --pool 'out/*.json'` on the persisted week `--json` files (gitignored `out/`; one prior network run) plus a new `--weights-fn` flag in `cli_plugins/backtest_flags.py` (none exists today); walk-forward W1–W3 | B | M |
| 5 | Re-measure `--monotone` vs the running-max guard under LOSO | `scripts/train_wp_model.py`, `models/cv.py loso_folds` | LOSO log-loss delta; guard-activation count | train 2016–2023 / test 2024, score once on 2025 | A | M |
| 6 | Efficiency-curve WLS at +1/+5/+15 min after scores, favourite- vs underdog-scored | `quant/eventstudy.py` | CLV at +5/+15; shuffle placebo must not reproduce the region | recorded ticks; W1–W3 replays | B | S |
| 7 | Per-state WP SD table as a `class_gaps` variant only | `scripts/train_wp_model.py --bootstrap`, `backtest.py class_gaps` | STEAL rows per class; placebo overlap | needs B = 50–100 retrains | B | L |
| 8 | `total_points`, `ep_adjusted_diff` features, only inside the #5 retrain | `models/wp.py posteam_features` | logistic-alone log-loss (0.4763); q3 / q4_early | LOSO retrain; W1 strata | C | M |

## Open questions

1. Is the NFL book slower than NBA's beta = 0.64? The ≤ 4¢ slice (0.488 vs 0.448) says thin
   books are worst. Recommendation 1, then **Record a live Sunday slate** for the sub-minute path.
2. Does under-reaction survive a 7 %·p(1 − p) taker fee held to settlement? Only a round trip
   is tested. **Record a live Sunday slate** (`clv`, `backtest-ticks`) plus the ~70-game gate
   on "In-play STEAL as a strategy".
3. How wide is the stack's own WP interval by state? Same run as **Retraining the WP model on
   2025–2026**.
4. Do D2-style weights generalise walk-forward? Three pooled NFL weeks (`games_needed` 10 for
   blend − model, 71 for ESPN − model); calendar time, not a "Needs you" step.
5. Should ESPN, scored post-play (88.5 % of jumps land before the entry), enter a pre-snap fair
   at all? Same recorded Sunday.
6. Drive-level state-space WP (ESS = plays, not games) has no repo code and would give tie / OT
   / kneel states without a rules table; scope after the items above.

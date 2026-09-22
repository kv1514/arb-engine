# 02 · College football in-play models: cfbfastR, NFL-to-college transfer, overtime, data

**Summary.** The transferred NFL model already beats every college source the repo scores
(week 2: 0.2156 vs Kalshi 0.2892–0.2907 and ESPN 0.2345 on 12,771 scrimmage plays), and
nothing in the literature argues for rescaling the spread — cfbfastR uses the identical
`spread_time` and the repo's rescale/clamp experiments are within noise and slightly worse.
The one measured break is untimed college overtime, where `backtest.py` leaves `model_p=None`
and the harness's clock mapping is the only fix with a measured gain (3.32 → 1.05 log-loss
on 79 rows of three games). The Brill–Yurko–Wyner papers show a play-level WP model learns
from far fewer independent outcomes than its row count suggests, which bounds what a native
college retrain can add and favours monotone constraints at training time over the current
inference-time running-max guard.

## Verified papers

### Exploring the Difficulty of Estimating Win Probability: A Simulation Study
Brill, Yurko, Wyner (2025), JQAS (accepted); arXiv:2406.16171 — <https://arxiv.org/html/2406.16171>

*Method.* "Random walk football": ball at midfield on `L = 4` yardlines, ±1 per play with
equal probability, touchdown at an end zone then reset, `T = 56` plays per game. True WP by
dynamic programming, `WP(t, x, s) = P(S_{g,T+1} > 0 | X_gt = x, S_gt = s)`; `K` sets
within-game dependence (`K = 1` independent plays, `K = T` every play shares the outcome,
i.e. real football). XGBoost fitted on simulated data; naive, cluster (resample games),
randomized cluster (games then plays) and fractional cluster (`G·phi` games) bootstraps.

*Data.* Simulated, 4,101 games × 56 plays = 229,656 plays, mirroring NFL 2006–2021.

*Results.* Effective sample size at `K = T` is 2,291 games, "56 % of the nominal sample
size". Bias is worst early in the game. Covering the true WP 90 % of the time needs a mean
band of 6.3 WP points; nominal-90 % coverage is 0.60 naive, 0.71 cluster, 0.76 randomized
cluster, 0.90 fractional cluster at `phi = 0.35` (width 0.063).

*Limitations.* The toy game is far simpler than football, so real effective sizes are
"likely even smaller"; `phi` cannot be tuned without the true WP.

### Analytics, have some humility: a statistical view of fourth-down decision making
Brill, Yurko, Wyner (2025), The American Statistician; arXiv:2311.03490 — <https://arxiv.org/html/2311.03490>

*Method.* XGBoost WP on first downs: score differential, seconds remaining, spread, yards
to end zone, receive-2nd-half-kickoff, both timeouts, total score, `scoreTimeRatio`.
Monotone constraints: increasing in score differential, `scoreTimeRatio`, offensive
timeouts; decreasing in spread, yards to end zone, defensive timeouts. Randomized cluster
bootstrap (games, then drives within game), `B = 101`; 90 % interval on a WP gain is
`[g_hat_(6), g_hat_(96)]`; "confident" if ≥ 83 % of refits agree, "uncertain" below 67 %.

*Data.* NFL 2006–2021: 229,635 first-down plays, 4,101 non-tied games.

*Results.* Only 48 % of 2018–2022 fourth-down decisions are confident; 27 % uncertain.

*Limitations.* The bootstrap under-covers (companion paper); `B = 101` chosen for category
stability, not coverage.

### nflfastR EP, WP, CP, xYAC and xPass models
Baldwin (2021), Open Source Football — <https://opensourcefootball.com/posts/2020-09-28-nflfastr-ep-wp-and-cp-models/>

*Method.* XGBoost on seconds remaining, yard line, score differential, down, distance,
timeouts, receive-2nd-half-kickoff, home, `diff_time_ratio = diff · exp(4·(3600 − gsr)/3600)`
and `spread_time = spread · exp(−4·(3600 − gsr)/3600)`; spread model 534 rounds, eta 0.05,
depth 5. Rows: non-tie games, `qtr <= 4` (overtime excluded).

*Data.* NFL 2000–2019, leave-one-season-out CV. *Results.* Calibration error 0.0055 base /
0.0066 spread vs nflscrapR 0.0397; no log-loss.

*Limitations.* Overtime excluded; no uncertainty; the `exp(±4·elapsed)` decay was never
fitted for college pace. The repo's logistic stage already matches it on 2025 (0.4763 vs
`vegas_wp` 0.4768); cfbfastR's college WP is a port with no structural change.

### cfbfastR-data / cfbfastR-cfb-raw — data for a native college model
sportsdataverse, Gilani et al. (2026), GitHub — <https://github.com/sportsdataverse/cfbfastR-data>

*What exists.* Play-by-play as RDS / CSV / parquet (`pbp/rds/play_by_play_{year}.rds`;
live tree under the `cfbfastR_cfb_pbp` release), `betting/`, `schedules/`; cfbfastR-cfb-raw
holds per-game ESPN JSON keyed by the same game ids `venues/espn.py` parses. Columns include
`wp`, `vegas_wp`, `pos_team_spread`, `TimeSecsRem`, `period`, drive ids. CFBD (key required)
adds multi-book lines and a proprietary 2025+ in-game WP.

*Sample.* 2002–present, ~870 FBS games per season (Sides, self-reported: 176 games with
|spread| > 20 were 17 % of 2021); 2014–2024 gives ~9,000 games vs the NFL model's 2,467.
OT possessions exist (`period >= 5`) but cfbfastR's `wp` is undefined on them.

*Limitations.* The closing-line column is undocumented; ESPN college pbp has known
possession/clock errors; OT plays carry no clock; the 2023 running-clock rule changes what
every seconds-remaining feature means across eras.

## What this means for arb_engine

*Spread transfer is closed.* `docs/MODEL.md` (`college_experiment_p04`, 185 games): clamp
|spread| ≤ 19.5 +0.0014 [−0.0067, +0.0077], rescale × 27/32 +0.0033 [−0.0051, +0.0110],
both inside noise and slightly worse; no paper shows a rescale helping.

*Overtime is the one real break.* Every college WP source trains a separate OT model or
declines; `backtest.py`'s `in_play_gate` keeps college OT rows (no game clock, `gsr=None`) in play and `GameReplayer.model_at` returns `model_p=None` for them ("no game clock (college OT): no model"); the 120-s mapping lives only in `scripts/college_experiment.py` (`DEFAULT_OT_SECONDS`, `variant_gsr`). The
week-2 OT slice (58 rows) reads ESPN 0.8924, Kalshi 0.6838/0.6687, blend 0.6327/0.6301 — the
blend already beats Kalshi there, so zeroing ESPN in OT would collapse it to the worse Kalshi
mid. OT needs a model column (a college OT rule in `models/wp.py` with possession-outcome
rates from cfbfastR `period >= 5` rows); three games / 79 rows cannot score it on 2026 alone.

*Monotonicity.* The shipped export scores a 17-point lead below a 16-point one, patched by
`home_win_probability(monotone=True)`; `scripts/train_wp_model.py --monotone` constrains three
features at ~0.003 log-loss on 2025. Brill's list adds timeouts and field position: a
training-time fix that leaves the stdlib walker untouched.

*Uncertainty and STEAL.* `quant/calibration.py::bootstrap_paired` is Brill's cluster variant,
and their 4,101 → 2,291 effective games is the theory behind the repo's `games_needed`
column (642–2,342 for college). It does *not* explain the STEAL intervals ([−0.64, +1.03]
$/game at 3 % on 49 college games): those are settled binary P&L, dominated by outcome
variance, and raising the edge 3 → 8 % did not close them. A WP band is worth testing only
as a stratifier; the fractional bootstrap (`phi` untunable) is not.

*Native college model.* Data is not the constraint (~9,000 games, same ESPN ids, so
`quant/feedparity.py` can check the parse before training and `models/cv.py::loso_folds`
gives the folds). The bar is the week-2 model − Kalshi gap, −0.0096 [−0.0248, +0.0110],
games needed 642: one replay week cannot score a retrain out of sample.

## Recommendations

| # | change | module | metric | offline test | grade | effort |
|---|---|---|---|---|---|---|
| 1 | Promote the 120-s OT clock mapping into the replay so college OT rows carry `model_p`; then a college OT rule fitted on cfbfastR `period >= 5` | `backtest.py` (`GameReplayer.model_at`, where the `None` is produced; `in_play_gate` has no OT logic), `models/wp.py` rules | `ot` slice model log-loss (n=0 now; ESPN 0.89, Kalshi 0.68) | `college_experiment.py --offline` reproduces 1.0491 on `college_experiment_p04.json` | B (3 games) | S / M |
| 2 | \|spread\| stratum (≤ 7, 7–14, 14–19.5, > 19.5) in `strata_tables` / `class_gaps` to test the heavy-favourite STEAL / 0.99-cap hypothesis | `backtest.py` | per-stratum log-loss, STEAL rows at 3/5/8 % | `backtest --week 2 --sport ncaaf --offline` / `--week 1` on the week caches (`out/cache/replay`, one prior network run); the committed `tests/fixtures/results/replay_*.json` are metrics-only, `load_results` on them returns no games | C | S |
| 3 | Brill's full monotone list at training; drop the running-max guard | `train_wp_model.py --monotone`, `models/wp.py` | 2025 log-loss ≤ 0.4751 + 0.003, zero guard corrections, both replay weeks | retrain (numpy/XGBoost), `tests/test_wp_model.py`, `backtest --offline` + `bootstrap_paired` | B | M |
| 4 | Build the college set: cfbfastR pbp 2014–2024, closing-line column resolved, 2023+ era flag, parity vs `venues/espn.py` | `quant/feedparity.py`, `models/cv.py`, new build script | parity ≥ 99.8 % (nflverse bar); later week-2 log-loss vs 0.2156 | parity on cached summaries vs hand-downloaded JSON | B data / C gain | L |
| 5 | STEAL tercile experiment: split qualifying rows by a bootstrap WP-band width, P&L per tercile | `simulate_steal`, `train_wp_model.py` | executable-pairing P&L per game by tercile | both replay fixtures; refits offline | C | M |

## Open questions and what settles them

`docs/ROADMAP.md` has no college "Needs you" row; most of these need one.

1. **Closing-line column in cfbfastR `betting/` / `pos_team_spread`** — new Needs-you row:
   download one season's parquet by hand (the sandbox cannot reach release assets) and paste
   the schema.
2. **Post-2021 OT outcome rates** (`q_TD`, `q_FG`, `q_0` from the 25; two-point rates in the
   OT2 try and OT3+ shootout) — unpublished, fitted from the same download; ~35 OT games a
   season means wide intervals. Wilson's pre-2021 constants are the Kansas-plan era and
   should not be coded without re-estimation.
3. **Does the college gap survive clipping both sources to [0.005, 0.995]?** Settled by
   recommendation 2 on the committed fixtures.
4. **Era flag or 2023+ retrain?** 2023–2025 is ~2,600 games, ~1,500 independent outcomes by
   Brill's result; `loso_folds` on the set from (1).
5. **cfbfastR-cfb-raw vs `venues/espn.py` parity** (Maddox et al. report unreliable
   possession ids) — recommendation 4.
6. **College STEAL** (+3.5 / +7.1 / +5.5 % ROI, intervals including zero, shuffle placebo
   −23 / −22 / −13 %) is unchanged by this cluster; settled by the existing "Record a live
   Sunday slate" row and the deferred ~70-game replay gate.
7. Not fetched: Lock & Nettleton 2014 (403); CFBD's 2025 WP numbers (proprietary).

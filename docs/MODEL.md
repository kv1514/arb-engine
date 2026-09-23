# In-game NFL win-probability model

`arb_engine/models/wp.py` scores an NFL game state — score, clock, possession, down and
distance, field position, timeouts and the pre-game spread — and returns the probability
that the home team wins. It is the engine's own estimate of fair value for an in-play
moneyline, independent of any venue's quotes. Inference is standard-library only
(the exported polynomial and trees are evaluated in pure Python, ~0.2 ms per call);
training needs numpy and XGBoost and is done offline by `scripts/train_wp_model.py`.

## Training data

* **Source**: nflverse play-by-play releases,
  `https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_<year>.csv.gz`
  (downloaded with `curl -L --compressed`, parsed with the stdlib `gzip` + `csv` modules and
  cached per season as `.npz`).
* **Seasons**: 2016–2024 for training (2,467 games, 362,579 plays), **2025 held out**
  (284 games, 40,371 plays). Regular season and playoffs.
* **Rows kept** (mirrors nflfastR's `vegas_wp` training set): a possession team is set,
  `down` is 1–4, clock / field position / timeouts / spread / result are all present,
  the game did not end in a tie, and `qtr <= 4`. Kickoffs, PATs, two-point tries,
  timeouts and other no-down rows are excluded. Overtime plays are excluded from training
  (see limitations).
* **Label**: 1 if the possession team won the game (`result` is home margin; sign
  compared with whether the posteam is the home team).

## Features (possession-team perspective, exactly nflfastR's list)

| feature | definition |
| --- | --- |
| `score_differential` | posteam score − defteam score |
| `game_seconds_remaining` | seconds left in regulation (3600 at kickoff) |
| `half_seconds_remaining` | seconds left in the half |
| `receive_2h_ko` | 1 in the first half when the posteam kicked off to open the game (it receives the second-half kickoff); 0 otherwise. Derived from the `defteam` of the game's first play, since the CSV has no such column |
| `spread_time` | posteam spread × exp(−4 · elapsed_share), elapsed_share = (3600 − gsr)/3600. Spread is positive when the posteam is favoured (nflverse `spread_line` is home-favoured-positive; it is negated for the away team) |
| `diff_time_ratio` | score_differential ÷ exp(−4 · elapsed_share) |
| `down`, `ydstogo`, `yardline_100` | down, distance, yards to the opponent's goal line |
| `posteam_timeouts_remaining`, `defteam_timeouts_remaining` | timeouts left |
| `is_home` | 1 when the posteam is the home team |

nflfastR's own `wp` / `vegas_wp` columns are **never** used as features; `vegas_wp` is only
the benchmark below.

## Model

Two stages, stacked:

1. **Logistic baseline** (numpy IRLS): the 12 features standardized, plus their squares
   and all pairwise products (90 terms + intercept, L2 = 1e-3). On its own it scores
   log-loss 0.4763 on 2025 — already level with nflfastR — and is smooth in every input.
2. **XGBoost 3.4.1 `binary:logistic` residual trees** trained with the baseline's margin as
   `base_margin`: 200 rounds, `max_depth=4`, `eta=0.05`, `min_child_weight=20`,
   `subsample=0.8`, `colsample_bytree=0.8`, `base_score=0.5`, no monotone constraints.
   The trees only have to learn what the polynomial cannot (the pre-game spread curve at
   big spreads, end-of-half and 4th-down kinks), so they stay small.

Because the trees are unconstrained they can dip at a thinly-trained split (the shipped
export scores a 17-point Q2 lead *below* a 16-point one for the same clock, spread and
field position — about 10 points of win probability). `home_win_probability` therefore
enforces monotonicity in the score at inference: for each perspective it also scores every
smaller margin from a tie up to the actual one and takes the running max (leader) / min
(trailer), so a bigger lead never lowers the leader's probability. Retraining with
`--monotone` (constraints on `score_differential`, `diff_time_ratio`, `spread_time`) removes
the dips at the source at a cost of ~0.003 log-loss on 2025; the guard keeps the shipped
export sane until that trade-off is revisited.

Stacking was chosen over either piece alone on the 2025 season: plain XGBoost
(300 rounds, depth 5) scored 0.4790, the logistic alone 0.4763, the stack 0.4751. A simple
probability average of the two separate models did not beat the logistic alone. **Note that
2025 doubled as the selection set**: the shipped configuration (`s_d4`) is the best of ~15
stacked / plain / logistic variants compared on the same 2025 log-loss, so the 2025 numbers
below are selection-biased and 2025 is not an unseen season in the strict sense. A clean
estimate needs `--train-seasons 2016-2023 --test-season 2024` for model selection with the
frozen config then scored once on 2025.

The export (`arb_engine/data/nfl_wp_model.json`, 0.14 MB) holds the feature list,
`base_score`, the logistic coefficients (`base_logistic`) and the trees from
`Booster.get_dump(dump_format="json")` compacted into per-tree arrays. The stdlib walker
adds `logit(base_score)` + logistic margin + leaf values, follows each split's `missing`
branch for absent / NaN inputs, and rounds inputs to float32 so comparisons match XGBoost
bit for bit (max |walker − Booster.predict| on 3,000 held-out plays: 3e-7).

If XGBoost cannot be imported the script re-execs with `DYLD_LIBRARY_PATH` pointing at a
wheel-bundled `libomp.dylib` (scikit-learn ships one — this Mac has no Homebrew), and if
that fails it ships stage 1 alone (`model_type: "logistic"`, also supported by the walker)
and says so in the output and meta file.

## Held-out results (2025 season, 40,371 plays)

| | log-loss | Brier | ECE (10 bins) |
| --- | --- | --- | --- |
| **this model (stacked)** | **0.4751** | **0.1584** | **0.020** |
| logistic baseline alone | 0.4763 | 0.1587 | 0.019 |
| plain XGBoost (300 × depth 5) | 0.4790 | 0.1595 | 0.021 |
| nflfastR `vegas_wp` (same rows) | 0.4768 | 0.1588 | 0.019 |

Correlation with `vegas_wp` 0.9954; mean absolute difference 0.022. nflfastR's model uses
the same feature set on a much longer history of seasons. The 0.0017 log-loss gap is **not
an established edge**: the 2025 season is ~284 games, so the standard error on a log-loss
difference this size is of the same order, and the configuration was picked as the best of
~15 candidates on this very season (see above). Read the table as "as good as nflfastR on
2025", nothing more, until the model is re-selected on 2024 and scored once on 2025.

Calibration, this model (held-out 2025):

| predicted bin | n | mean predicted | actual win rate | gap |
| --- | --- | --- | --- | --- |
| 0.0–0.1 | 5,834 | 0.034 | 0.057 | +0.024 |
| 0.1–0.2 | 3,525 | 0.150 | 0.180 | +0.030 |
| 0.2–0.3 | 3,609 | 0.249 | 0.289 | +0.040 |
| 0.3–0.4 | 3,282 | 0.350 | 0.350 | +0.001 |
| 0.4–0.5 | 3,438 | 0.449 | 0.439 | −0.011 |
| 0.5–0.6 | 3,507 | 0.551 | 0.549 | −0.003 |
| 0.6–0.7 | 3,689 | 0.649 | 0.667 | +0.017 |
| 0.7–0.8 | 3,623 | 0.750 | 0.726 | −0.024 |
| 0.8–0.9 | 3,591 | 0.851 | 0.808 | −0.043 |
| 0.9–1.0 | 6,273 | 0.965 | 0.953 | −0.012 |

nflfastR `vegas_wp` on the same rows shows the same shape (+0.028 in the 0.0–0.1 bin,
−0.040 in 0.8–0.9): 2025 had more comebacks than the models expected in both directions,
which is season noise rather than a defect of either model.

Pre-game (0–0, 3600 s, nobody in possession — see below), P(home) by the home team's
sportsbook spread (negative = home favoured):

| spread | −10 | −7 | −3 | −1 | 0 | +1 | +3 | +7 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| P(home) | 0.792 | 0.714 | 0.572 | 0.500 | 0.485 | 0.471 | 0.400 | 0.257 |

The usual spread→moneyline conversions give about 0.59 (−3), 0.73 (−7) and 0.80 (−10);
the model is within a couple of points of each, a little low for home favourites and a
little high for home underdogs (P(−3) + P(+3) = 0.97, pick'em reads 0.485 for the home
side). That asymmetry is in the training data: in 2016–2024 home teams favoured by 1–2.5 won only 51% and home dogs by
1–2.5 won 48%, i.e. the post-2020 collapse of home-field advantage relative to the closing
line. Treat pre-game output as a sanity check, not a substitute for the market price.

## How the engine builds the state

`home_win_probability(home_score, away_score, game_seconds_remaining, possession, down,
distance, yardline_100, home_timeouts, away_timeouts, vegas_spread_home, receive_2h_ko_home)`:

* `possession="home"` / `"away"`: that team is the posteam; its lead, timeouts and
  spread sign are flipped accordingly; the answer is `wp` or `1 − wp`.
* `possession=None` (pre-game, between plays, halftime, unknown): a **neutral state**
  — 1st & 10 from the team's own 25 (`yardline_100=75`) — is scored once with the home team
  as posteam and once with the away team, and the two P(home) values are averaged. This
  removes the possession premium without inventing a possessor.
* `receive_2h_ko_home=None` in the first half: both second-half-kickoff assignments are
  scored and averaged (pre-game the coin toss is unknown). In the second half the flag
  is 0 for everyone, as in nflfastR.
* `vegas_spread_home` uses the ESPN / DraftKings sign (negative = home favoured), the
  number `pickcenter[].spread` reports; it is negated internally to nflverse's convention.
  Clock is clamped to `[0, 3600]`; `half_seconds_remaining` is derived from the game clock
  when not supplied.
* Optional keyword arguments `play_class`, `overtime`, `final`, `season` and `p_onside`
  route dead-ball states through the rules table below (try, kickoff pending, overtime,
  final, era yardline). Leaving them out reproduces the pre-rules output bit for bit, which
  `tests/test_wp_model.py` pins.

## Known limitations

* **No injuries, weather, roster or coaching information.** Everything about team
  strength enters through the pre-game spread; a mid-game injury changes nothing until it
  shows up in the score.
* **Overtime**: the model is trained on regulation plays only. An OT state (nflverse restarts
  `game_seconds_remaining` at 600 in OT) is scored as if it were a tied fourth quarter
  with that much time left; the sudden-death / one-possession rules are not modelled. With
  `overtime=True` the output is clamped away from 0 and 1 and a 20–20 tie at 0:00 with no
  spread scores 0.480 (the model's home-side asymmetry at pick'em, not 0.5). Treat OT
  outputs as approximate; the replay reports OT rows as their own slice.
* **Spread source at inference** is ESPN's `pickcenter` DraftKings line (opening/current,
  not closing) and the training spread is the nflverse closing line; a line that moves a
  lot before kickoff will bias early-game estimates slightly.
* **The residual trees are piecewise constant**: the logistic stage keeps the output
  smooth, but tiny changes in the clock or field position can still produce small steps,
  and states outside the training distribution (e.g. 4th & 40) are extrapolated from the
  polynomial plus their nearest tree leaves.
* **Home advantage** is whatever 2016–2024 contained (see the pre-game note above).
* Ties are excluded from training, so the output is P(win) with P(tie) folded into the
  complement; a Kalshi/Rothera moneyline market settles differently on a tie.

## Retraining

```bash
pip3 install --use-deprecated=legacy-certs numpy xgboost scikit-learn   # scikit-learn only for its libomp.dylib
python3 scripts/train_wp_model.py                                       # defaults reproduce the shipped model
python3 -m unittest tests.test_wp_model
```

`--train-seasons 2016-2025 --test-season 2026` once a new season of play-by-play exists;
the meta file (`arb_engine/data/nfl_wp_model.meta.json`) records the held-out metrics,
calibration table, parity check and pre-game table for whatever was last exported.

## Dead-ball and boundary rules (`data/nfl_wp_rules.json`)

The exported model is frozen; a small rules table layered on top of it handles the states
the training rows never contained. Nothing in it is fitted: every constant is a rulebook
spot, a league-wide rate or a provisional number flagged as such, and **the default call
is bit-for-bit what it was before the table existed** — the corrections only engage when
the caller passes `play_class`, `overtime`, `final`, `season` or `p_onside` (the replay
harness does; the live watcher passes what ESPN gives it).

* **Era neutral yardline**: the neutral / kickoff-receiver state starts at the 25 through
  2023, the 30 in 2024 (dynamic kickoff) and about the 31 from 2025 (touchback at the 35,
  returns land short of it). An unknown season keeps 75 so old callers get the old number.
* **Kickoff-pending state** (`kickoff_state_wp`): a mixture of the receiver starting at the
  era yardline and the kicker recovering an onside kick at its own 45; `p_onside` is 0
  unless the kicker trails by 1–16 with under five minutes left (then 6 %, provisional).
* **Try state** (`try_state_wp`): `p_conv × WP(score + points, kickoff pending) + (1 −
  p_conv) × WP(score, kickoff pending)`, PAT 94 % / two-point 48 %, the two-point decision
  from the conventional margin chart inside the last 15 minutes.
* **Overtime**: scored as a tied-or-not fourth-quarter state with that much clock; the
  decided shortcut at 0:00 never fires in OT and outputs are clamped away from 0 and 1. A
  20–20 tie at 0:00 is priced as overtime (P(home) 0.480 with no spread — the model's
  home-side asymmetry at pick'em, not 0.5).
* **Final**: `final=True` returns 1 / 0, or 0.5 for a tied final.
* **Spread inversion** (`spread_from_pregame_probability`): bisection on the running-min
  envelope of the integer-grid pre-game table (the raw half-point grid wiggles by ~1.5
  points around pick'em, a tree artefact); NaN is rejected.
* **Kneel-out floor**: off by default; it stays off until a per-class table shows fewer
  STEAL-qualifying kneel rows at no log-loss cost (the week-1 table below shows 35 kneel
  rows and one qualifying at 3 %, so there is nothing to fix yet).
* **Timeouts are not plays**: the model scores the post-timeout state (same score, clock,
  down and distance, the calling team one timeout down).

Offline checks on the shipped export (`tests/fixtures/results/week1_p05.json`):

<!-- results:week1_p05_rules -->
| rule | check | value |
|---|---|---|
| spread inversion | max \|error\| over the pre-game table (points) | 0.033 |
| spread inversion | integer-grid table monotone / grid points | yes / 61 |
| neutral yardline | season 2023: yardline_100 / pre-game Δ at −3 / late one-score kickoff-pending Δ | 75 / +0.000000 / +0.000000 |
| neutral yardline | season 2024: yardline_100 / pre-game Δ at −3 / late one-score kickoff-pending Δ | 70 / +0.000030 / -0.015624 |
| neutral yardline | season 2025: yardline_100 / pre-game Δ at −3 / late one-score kickoff-pending Δ | 69 / -0.000201 / -0.018787 |
| kickoff-pending state | synthetic rows / mean receiver shift / mean \|shift\| | 120 / +0.01023 / 0.01023 |
| try state | rows / two-point rows / bracket violations / mean \|shift\| vs 1st & goal from the 2 | 116 / 24 / 0 / 0.142135 |
| overtime | rows / outputs exactly 0 or 1 / P(home) for a tie at 0:00 | 60 / 0 / 0.479751 |
| kneel floor | rows / lifted with the flag off / lifted with the flag on / flag default | 24 / 0 / 10 / off |
<!-- /results:week1_p05_rules -->

## The replay harness (`backtest --week N`)

`python -m arb_engine backtest --week 1 --season 2026 --bar-mode both --slices --placebo`
replays every final of a week play by play. For each play it takes the ESPN wall-clock
stamp and the **state before the play** (score, clock, possession, down and distance,
field position, per-team timeouts recounted from the play texts, the pickcenter closing
spread), classifies the play (`scrimmage`, `kickoff`, `try`, `timeout`, `kneel`,
`end_period`, `ot`), inserts the synthetic dead-ball rows a game really passes through (a
scoring play's after-state is the *try* state, then *kickoff pending*, not ESPN's end-of-drive
block), scores the model on the pre-play and the post-play state, reads ESPN's own number, and
looks up every price source at that moment:

* **Kalshi** 1-minute candles, bid/ask close, under **two alignments**: `kalshi_before` = the
  last candle ending at or before the play (what a taker could have hit), `kalshi_after` =
  the first candle ending after it (the market has seen the play). Neither is "the" market:
  the truth sits between them, so every model-vs-market number below is quoted as a range.
* **Polymarket** 1-minute price history (last trade; a quiet minute repeats a stale price).
* **Robinhood/Rothera** 5-minute trade bars only when contract ids are known (open events),
  so the week runs carry no Rothera column.

The pooled tables are computed three ways — all rows, in play only (score can still change),
and **in-play scrimmage plays only, the headline** — because on kickoffs, tries and timeouts
the model, ESPN and the market are not looking at the same state. The blend weights are fitted
on a grid over the (market, model, ESPN) simplex, linear and logit pools, and every comparison
carries a 90 % paired **game-cluster bootstrap interval** (games resampled, B = 1000) plus the
number of games that interval would need to exclude zero at the observed effect. The
STEAL/LOCK simulations run under two honest pairings (pre-play fair vs the candle before,
post-play fair vs the candle after — the executable one), the leaky pairing for comparison,
and two placebos (one candle later still; the games' outcomes shuffled). Responses go through
a read-through cache (`--cache-dir`, `--offline`), so a week reruns without the network. The
text reports and metrics fixtures of the runs quoted here are in `docs/results/` and
`tests/fixtures/results/` ([how they are kept in sync](results/README.md)).

## NFL 2026 week 1 (16 games, 2,904 rows, 2,263 in-play scrimmage plays)

<!-- results:nfl_w1_pooled -->
| source (P(home) per play) | games | scrimmage plays | log-loss | Brier | log-loss, all in-play rows | log-loss, all rows |
|---|---|---|---|---|---|---|
| model (state before the play) | 16 | 2,263 | 0.4060 | 0.1336 | 0.3993 | 0.3974 |
| model (state after the play) | 16 | 2,263 | 0.4022 | 0.1323 | 0.3956 | 0.3937 |
| ESPN win probability | 16 | 2,263 | 0.4205 | 0.1391 | 0.4132 | 0.4109 |
| Kalshi mid, last candle before the play | 16 | 2,261 | 0.4425 | 0.1472 | 0.4332 | 0.4311 |
| Kalshi mid, first candle after the play | 16 | 2,260 | 0.4382 | 0.1456 | 0.4290 | 0.4267 |
| Polymarket last trade | 16 | 2,263 | 0.4415 | 0.1471 | 0.4318 | 0.4297 |
| market consensus | 16 | 2,263 | 0.4417 | 0.1471 | 0.4322 | 0.4301 |
| blend 0.30 market / 0.55 model / 0.15 ESPN | 16 | 2,263 | 0.4173 | 0.1378 | 0.4097 | 0.4077 |
| blend, post-play state and candle | 16 | 2,263 | 0.4146 | 0.1368 | 0.4074 | 0.4054 |
| Kalshi, book ≤ 4¢ wide | 16 | 2,048 | 0.4875 | 0.1625 | 0.4837 | 0.4834 |
| model on those same plays | 16 | 2,048 | 0.4478 | 0.1475 | 0.4450 | 0.4448 |
<!-- /results:nfl_w1_pooled -->

**Model vs market, stated as the range it is:** on in-play scrimmage plays the model scores
**0.406 against Kalshi's 0.438–0.443** (after / before the play). The paired 90 %
game-cluster interval on model − Kalshi is [−0.055, −0.018] against the candle before the
play and [−0.049, −0.014] against the candle after — both exclude zero, and the same holds
for the post-play model against the post-play candle. ESPN (0.421) sits between the two; the
ESPN − model interval includes zero. On the plays where Kalshi's book was ≤ 4¢ wide the
market is *worse* (0.488 vs the model's 0.448 on those same plays): the in-play NFL books
are thin and slow rather than merely wide.

<!-- results:nfl_w1_intervals -->
| paired log-loss difference (in-play scrimmage) | mean | 90% game-cluster interval | excludes zero | games | games needed at this effect |
|---|---|---|---|---|---|
| model − Kalshi (before the play) | -0.0362 | [-0.0547, -0.0179] | yes | 16 | 10 |
| model − Kalshi (after the play) | -0.0314 | [-0.0491, -0.0140] | yes | 16 | 12 |
| model after − Kalshi after | -0.0353 | [-0.0532, -0.0176] | yes | 16 | 10 |
| blend (current weights) − model | +0.0114 | [+0.0061, +0.0169] | yes | 16 | 10 |
| ESPN − model | +0.0147 | [-0.0053, +0.0320] | no | 16 | 71 |
| best grid weights − current weights | -0.0114 | [-0.0169, -0.0061] | yes | 16 | 10 |
<!-- /results:nfl_w1_intervals -->

The bracket is not an artefact of the old harness (which read only the after candle): the
kalshi_before column is what a taker could have hit and it is the worse of the two.

Per quarter and per play class (log-loss; cells under 50 rows show the count only):

<!-- results:nfl_w1_slices -->
| slice | rows | model | model after | ESPN | Kalshi before | Kalshi after | blend | blend after |
|---|---|---|---|---|---|---|---|---|
| q1 | 630 | 0.5537 | 0.5499 | 0.5799 | 0.5713 | 0.5676 | 0.5612 | 0.5592 |
| q2 | 834 | 0.4914 | 0.4903 | 0.5024 | 0.5355 | 0.5335 | 0.5048 | 0.5041 |
| q3 | 673 | 0.3776 | 0.3742 | 0.4003 | 0.4357 | 0.4315 | 0.3960 | 0.3939 |
| q4_early | 413 | 0.2292 | 0.2197 | 0.2271 | 0.2495 | 0.2397 | 0.2331 | 0.2269 |
| q4_late | 314 | 0.1297 | 0.1257 | 0.1246 | 0.1345 | 0.1299 | 0.1278 | 0.1251 |
| ot | 24 | – | – | – | – | – | – | – |
<!-- /results:nfl_w1_slices -->

<!-- results:nfl_w1_classes -->
| play class | rows | model | ESPN | Kalshi before | Kalshi after | blend | mean \|model − Kalshi before\| | STEAL-qualifying rows at 3% / 5% / 8% |
|---|---|---|---|---|---|---|---|---|
| end_period | 50 | 0.4021 | 0.4468 | 0.4720 | 0.4742 | 0.4248 | 0.0576 | 12 / 6 / 4 |
| kickoff | 171 | 0.3934 | 0.4089 | 0.4220 | 0.4192 | 0.4032 | 0.0281 | 5 / 1 / 1 |
| kickoff_pending_synth | 93 | 0.3396 | – | 0.4013 | 0.3755 | 0.3589 | 0.0549 | 29 / 13 / 5 |
| kneel | 35 | – | – | – | – | – | 0.0165 | 1 / 0 / 0 |
| scrimmage | 2,263 | 0.4060 | 0.4205 | 0.4425 | 0.4382 | 0.4173 | 0.0301 | 159 / 35 / 7 |
| timeout | 369 | 0.3922 | 0.3984 | 0.4081 | 0.4028 | 0.3960 | 0.0607 | 97 / 63 / 32 |
| try_synth | 93 | 0.3447 | – | 0.4013 | 0.3755 | 0.3622 | 0.0532 | 28 / 11 / 6 |
<!-- /results:nfl_w1_classes -->

The model's advantage grows through the game (Q1 0.554 vs 0.571, Q3 0.378 vs 0.436) and is
smallest late in Q4, where ESPN is marginally the best source. Timeouts and synthetic
kickoff-pending / try rows show the widest model-vs-market gaps (0.05–0.06 mean absolute)
and the most STEAL-qualifying rows — those are dead-ball states where the market has not
re-priced yet, and the watcher gates on them (`docs/ARCHITECTURE.md`, feed gates).

### Blend weights: what the fit says and why the weights are not moving

<!-- results:nfl_w1_fit -->
| pool | plays (all three sources) | best market / model / ESPN | log-loss | current weights | log-loss | market only | model only | ESPN only |
|---|---|---|---|---|---|---|---|---|
| linear | 2,263 | 0.00 / 1.00 / 0.00 | 0.4060 | 0.30 / 0.55 / 0.15 | 0.4173 | 0.4417 | 0.4060 | 0.4205 |
| logit | 2,263 | 0.00 / 1.00 / 0.00 | 0.4060 | 0.30 / 0.55 / 0.15 | 0.4173 | 0.4417 | 0.4060 | 0.4205 |
<!-- /results:nfl_w1_fit -->

On NFL week 1 the current blend (market 0.30 / model 0.55 / ESPN 0.15) is **worse than the
model alone**: 0.417 vs 0.406, blend − model +0.011 with a 90 % interval of [+0.006, +0.017]
that excludes zero, and the best grid point is the model corner under both pools. College week
2 (86 games, next section) shows **no difference** (blend − model +0.0025, [−0.008, +0.010]),
and the two college weeks replayed under the previous harness had the blend ahead of the model
in both (week 1 0.152 vs 0.161, week 2 0.220 vs 0.229 on all plays). Sixteen NFL games
contradicting 185 college games is not a reason to re-weight.

**Weight-change policy.** The weights move only when (a) the season-to-date pooled replay
(`backtest --pool out/week*.json`) puts the game-cluster interval on best-grid − current
clear of zero, or (b) two consecutive weeks agree on the direction. Until then
`SPORT_WEIGHTS` stays at 0.30 / 0.55 / 0.15 for both sports, the market keeps its
pre-game-information role (injuries, weather reach the model only through the closing
spread), and the logit pool remains selectable (`pool="logit"` in `blended_fair`) but not
the live default, because on both weeks it lands within 0.001 of the linear pool.

### Does the edge survive fees? STEAL and LOCK simulations

One STEAL entry per game, 10 contracts at Kalshi's candle ask, taker fees, fair = blend:

<!-- results:nfl_w1_steal -->
| pairing (fair = blend, hold to settlement) | edge | games | locks | wins | losses | staked $ | P&L $ | ROI | 90% interval, P&L per game |
|---|---|---|---|---|---|---|---|---|---|
| pre-play fair vs candle before (honest, conservative) | 0.03 | 16 | 0 | 6 | 10 | 67.93 | -7.93 | -11.7% | [-2.09, +1.36] |
| pre-play fair vs candle before (honest, conservative) | 0.05 | 15 | 0 | 9 | 6 | 72.81 | +17.19 | +23.6% | [-0.51, +2.78] |
| pre-play fair vs candle before (honest, conservative) | 0.08 | 14 | 0 | 8 | 6 | 68.76 | +11.24 | +16.4% | [-1.20, +2.63] |
| post-play fair vs candle after (honest, executable) | 0.03 | 16 | 0 | 6 | 10 | 66.89 | -6.89 | -10.3% | [-2.01, +1.27] |
| post-play fair vs candle after (honest, executable) | 0.05 | 14 | 0 | 6 | 8 | 58.40 | +1.60 | +2.7% | [-1.86, +2.09] |
| post-play fair vs candle after (honest, executable) | 0.08 | 10 | 0 | 5 | 5 | 33.20 | +16.80 | +50.6% | [-0.43, +3.80] |
| pre-play fair vs candle after (leaky, for comparison) | 0.03 | 16 | 0 | 9 | 7 | 75.43 | +14.57 | +19.3% | [-0.79, +2.50] |
| pre-play fair vs candle after (leaky, for comparison) | 0.05 | 15 | 0 | 7 | 8 | 68.51 | +1.49 | +2.2% | [-1.73, +2.05] |
| pre-play fair vs candle after (leaky, for comparison) | 0.08 | 13 | 0 | 9 | 4 | 62.65 | +27.35 | +43.7% | [+0.15, +3.78] |
| placebo: fair vs one candle later still | 0.03 | 16 | 0 | 6 | 10 | 58.47 | +1.53 | +2.6% | [-1.61, +1.93] |
| placebo: fair vs one candle later still | 0.05 | 15 | 0 | 9 | 6 | 64.40 | +25.60 | +39.8% | [-0.01, +3.57] |
| placebo: fair vs one candle later still | 0.08 | 13 | 0 | 9 | 4 | 57.83 | +32.17 | +55.6% | [+0.37, +4.22] |
| placebo: games' outcomes shuffled (post-play fair vs candle after) | 0.03 | 16 | 0 | 8 | 8 | 66.89 | +13.11 | +19.6% | [-1.24, +2.88] |
| placebo: games' outcomes shuffled (post-play fair vs candle after) | 0.05 | 14 | 0 | 3 | 11 | 58.40 | -28.40 | -48.6% | [-3.68, -0.11] |
| placebo: games' outcomes shuffled (post-play fair vs candle after) | 0.08 | 10 | 0 | 4 | 6 | 33.20 | +6.80 | +20.5% | [-2.13, +3.86] |
<!-- /results:nfl_w1_steal -->

<!-- results:nfl_w1_lock -->
| rule (post-play fair vs candle after) | edge | games | locks | wins | losses | staked $ | P&L $ | ROI | 90% interval, P&L per game |
|---|---|---|---|---|---|---|---|---|---|
| lock when guaranteed ≥ 0% of hold EV (break-even) | 0.02 | 16 | 13 | 12 | 3 | 136.47 | -6.47 | -4.7% | [-1.10, +0.17] |
| lock when guaranteed ≥ 0% of hold EV (break-even) | 0.04 | 15 | 12 | 12 | 3 | 128.11 | -8.11 | -6.3% | [-1.35, +0.14] |
| lock when guaranteed ≥ 0% of hold EV (break-even) | 0.06 | 12 | 8 | 8 | 4 | 91.91 | -11.91 | -13.0% | [-2.08, -0.11] |
| lock when guaranteed ≥ 0% of hold EV (break-even) | 0.10 | 6 | 3 | 3 | 3 | 33.18 | -3.18 | -9.6% | [-2.60, +0.99] |
| lock when guaranteed ≥ 50% of hold EV | 0.02 | 16 | 13 | 13 | 3 | 134.17 | -4.17 | -3.1% | [-0.97, +0.35] |
| lock when guaranteed ≥ 50% of hold EV | 0.04 | 15 | 12 | 12 | 3 | 125.13 | -5.13 | -4.1% | [-1.20, +0.38] |
| lock when guaranteed ≥ 50% of hold EV | 0.06 | 12 | 8 | 8 | 4 | 88.72 | -8.72 | -9.8% | [-1.87, +0.24] |
| lock when guaranteed ≥ 50% of hold EV | 0.10 | 6 | 3 | 3 | 3 | 33.18 | -3.18 | -9.6% | [-2.60, +0.99] |
| lock when guaranteed ≥ 100% of hold EV | 0.02 | 16 | 11 | 11 | 5 | 112.90 | -2.90 | -2.6% | [-1.15, +0.70] |
| lock when guaranteed ≥ 100% of hold EV | 0.04 | 15 | 10 | 10 | 5 | 108.93 | -8.93 | -8.2% | [-1.52, +0.35] |
| lock when guaranteed ≥ 100% of hold EV | 0.06 | 12 | 8 | 8 | 4 | 87.60 | -7.60 | -8.7% | [-1.81, +0.37] |
| lock when guaranteed ≥ 100% of hold EV | 0.10 | 6 | 3 | 3 | 3 | 26.85 | +3.15 | +11.7% | [-2.20, +2.88] |
| fair = model, hold to settlement | 0.02 | 16 | 0 | 7 | 9 | 66.11 | +3.89 | +5.9% | [-1.69, +2.04] |
| fair = model, hold to settlement | 0.04 | 16 | 0 | 7 | 9 | 64.88 | +5.12 | +7.9% | [-1.39, +1.95] |
| fair = model, hold to settlement | 0.06 | 15 | 0 | 7 | 8 | 59.87 | +10.13 | +16.9% | [-1.17, +2.70] |
| fair = model, hold to settlement | 0.10 | 14 | 0 | 7 | 7 | 54.96 | +15.04 | +27.4% | [-0.94, +2.95] |
<!-- /results:nfl_w1_lock -->

**The STEAL edge is not demonstrated on the NFL yet.** Under the executable pairing the
hold-to-settlement P&L per game has a 90 % interval that includes zero at every edge
(−10 % / +3 % / +51 % ROI at 3 / 5 / 8 %, on 10–16 games and $33–67 staked), the
one-candle-later placebo posts ROIs as large or larger (+3 % / +40 % / +56 %) and the
shuffled-outcomes placebo swings from +20 % to −49 % to +21 %: with sixteen games the
sign is noise. By the harness's games-needed column the weakest log-loss comparison here
already needs ~70 games (about four NFL weeks); the P&L intervals are far wider than the
log-loss ones, so at least that many are needed before an ROI number means anything.
**Every break-even LOCK variant loses on this week** (−4.7 % / −6.3 % / −13.0 % / −9.6 % at
2 / 4 / 6 / 10 % edges; the 50 % and 100 %-of-hold-EV variants lose too except the 100 %
rule at 10 % on 6 games), as it did under the old harness:
a lock fires mostly after the position has gone against the entry and hands the edge back.
The watcher therefore treats LOCK NOW as a risk decision and prints the hold EV beside the
guarantee. The pre-P03 numbers previously quoted here (+16 % / +3.5 % / +9 % with the
blend, +15 % to +24 % with the model) came from the leaky pre-play-fair-vs-after-candle
pairing and are superseded by the table above.

## College football, 2026 week 2 (86 games, 15,060 rows, 12,771 in-play scrimmage plays)

`python -m arb_engine backtest --sport ncaaf --week 2 --season 2026 --bar-mode both --slices --placebo`
replays every FBS final with the **NFL** model scoring college states (`KXNCAAFGAME`
candles, Polymarket `cfb` history, ESPN college play-by-play) — no college training and no
college-specific features; the spread-rescale experiment below says none is needed.

<!-- results:ncaaf_w2_pooled -->
| source (P(home) per play) | games | scrimmage plays | log-loss | Brier | log-loss, all in-play rows | log-loss, all rows |
|---|---|---|---|---|---|---|
| model (state before the play) | 86 | 12,771 | 0.2156 | 0.0684 | 0.2166 | 0.2153 |
| model (state after the play) | 86 | 12,771 | 0.2159 | 0.0680 | 0.2154 | 0.2142 |
| ESPN win probability | 86 | 12,768 | 0.2345 | 0.0730 | 0.2351 | 0.2338 |
| Kalshi mid, last candle before the play | 80 | 9,269 | 0.2907 | 0.0930 | 0.2907 | 0.2896 |
| Kalshi mid, first candle after the play | 80 | 9,243 | 0.2892 | 0.0925 | 0.2893 | 0.2882 |
| Polymarket last trade | 63 | 9,321 | 0.2450 | 0.0776 | 0.2450 | 0.2437 |
| market consensus | 85 | 11,740 | 0.2461 | 0.0773 | 0.2461 | 0.2448 |
| blend 0.30 market / 0.55 model / 0.15 ESPN | 86 | 12,771 | 0.2179 | 0.0689 | 0.2186 | 0.2173 |
| blend, post-play state and candle | 86 | 12,771 | 0.2170 | 0.0686 | 0.2176 | 0.2164 |
| Kalshi, book ≤ 4¢ wide | 66 | 7,164 | 0.3718 | 0.1198 | 0.3744 | 0.3744 |
| model on those same plays | 66 | 7,164 | 0.3648 | 0.1167 | 0.3691 | 0.3690 |
<!-- /results:ncaaf_w2_pooled -->

<!-- results:ncaaf_w2_intervals -->
| paired log-loss difference (in-play scrimmage) | mean | 90% game-cluster interval | excludes zero | games | games needed at this effect |
|---|---|---|---|---|---|
| model − Kalshi (before the play) | -0.0096 | [-0.0248, +0.0110] | no | 80 | 642 |
| model − Kalshi (after the play) | -0.0078 | [-0.0236, +0.0134] | no | 80 | 1,027 |
| model after − Kalshi after | -0.0073 | [-0.0225, +0.0137] | no | 80 | 1,162 |
| blend (current weights) − model | +0.0025 | [-0.0077, +0.0102] | no | 86 | 2,342 |
| ESPN − model | +0.0187 | [-0.0020, +0.0377] | no | 86 | 204 |
| best grid weights − current weights | -0.0030 | [-0.0087, +0.0041] | no | 85 | 835 |
<!-- /results:ncaaf_w2_intervals -->

The pooled gap to Kalshi is large (0.216 vs 0.289–0.291) but the game-cluster interval
includes zero: college weeks mix 40-point favourites (Kalshi's candle capped at 0.99, the model
at 0.9999) with close games where the two swap places, so eighty games do not pin the
per-game mean. On the ≤ 4¢-book plays the model still leads (0.365 vs 0.372), unlike the earlier
harness's after-only reading, which had the market slightly ahead there.

<!-- results:ncaaf_w2_fit -->
| pool | plays (all three sources) | best market / model / ESPN | log-loss | current weights | log-loss | market only | model only | ESPN only |
|---|---|---|---|---|---|---|---|---|
| linear | 11,739 | 0.10 / 0.90 / 0.00 | 0.2341 | 0.30 / 0.55 / 0.15 | 0.2369 | 0.2462 | 0.2345 | 0.2545 |
| logit | 11,739 | 0.20 / 0.80 / 0.00 | 0.2341 | 0.30 / 0.55 / 0.15 | 0.2369 | 0.2462 | 0.2345 | 0.2545 |
<!-- /results:ncaaf_w2_fit -->

<!-- results:ncaaf_w2_slices -->
| slice | rows | model | model after | ESPN | Kalshi before | Kalshi after | blend | blend after |
|---|---|---|---|---|---|---|---|---|
| q1 | 3,560 | 0.2456 | 0.2446 | 0.2720 | 0.2753 | 0.2732 | 0.2509 | 0.2502 |
| q2 | 4,335 | 0.2221 | 0.2255 | 0.2399 | 0.2731 | 0.2737 | 0.2231 | 0.2233 |
| q3 | 3,488 | 0.2057 | 0.2070 | 0.2238 | 0.3192 | 0.3216 | 0.2102 | 0.2110 |
| q4_early | 2,117 | 0.1998 | 0.1966 | 0.2051 | 0.3454 | 0.3459 | 0.2001 | 0.1991 |
| q4_late | 1,414 | 0.1783 | 0.1598 | 0.1764 | 0.2379 | 0.2212 | 0.1547 | 0.1449 |
| ot | 58 | – | – | 0.8924 | 0.6838 | 0.6687 | 0.6327 | 0.6301 |
<!-- /results:ncaaf_w2_slices -->

<!-- results:ncaaf_w2_classes -->
| play class | rows | model | ESPN | Kalshi before | Kalshi after | blend | mean \|model − Kalshi before\| | STEAL-qualifying rows at 3% / 5% / 8% |
|---|---|---|---|---|---|---|---|---|
| end_period | 264 | 0.2346 | 0.2914 | 0.3163 | 0.3152 | 0.2364 | 0.0462 | 27 / 15 / 6 |
| kickoff | 940 | 0.2066 | 0.2188 | 0.2767 | 0.2791 | 0.2079 | 0.0351 | 75 / 41 / 18 |
| kickoff_pending_synth | 587 | 0.1813 | – | 0.2601 | 0.2501 | 0.1831 | 0.0472 | 93 / 48 / 25 |
| kneel | 87 | 0.1310 | 0.1100 | 0.1898 | 0.1974 | 0.1212 | 0.0218 | 5 / 1 / 0 |
| ot | 48 | – | – | – | – | – | – | 0 / 0 / 0 |
| scrimmage | 12,771 | 0.2156 | 0.2345 | 0.2907 | 0.2892 | 0.2179 | 0.0367 | 1038 / 530 / 234 |
| timeout | 853 | 0.2449 | 0.2360 | 0.2741 | 0.2692 | 0.2195 | 0.0574 | 122 / 90 / 58 |
| try | 9 | – | – | – | – | – | 0.0585 | 3 / 1 / 0 |
| try_synth | 587 | 0.1854 | – | 0.2601 | 0.2501 | 0.1859 | 0.0458 | 88 / 49 / 25 |
<!-- /results:ncaaf_w2_classes -->

<!-- results:ncaaf_w2_steal -->
| pairing (fair = blend, hold to settlement) | edge | games | locks | wins | losses | staked $ | P&L $ | ROI | 90% interval, P&L per game |
|---|---|---|---|---|---|---|---|---|---|
| pre-play fair vs candle before (honest, conservative) | 0.03 | 51 | 0 | 30 | 21 | 308.51 | -8.51 | -2.8% | [-1.04, +0.67] |
| pre-play fair vs candle before (honest, conservative) | 0.05 | 39 | 0 | 26 | 13 | 232.28 | +27.72 | +11.9% | [-0.32, +1.74] |
| pre-play fair vs candle before (honest, conservative) | 0.08 | 32 | 0 | 17 | 15 | 142.90 | +27.10 | +19.0% | [-0.29, +2.01] |
| post-play fair vs candle after (honest, executable) | 0.03 | 49 | 0 | 31 | 18 | 299.58 | +10.42 | +3.5% | [-0.64, +1.03] |
| post-play fair vs candle after (honest, executable) | 0.05 | 40 | 0 | 26 | 14 | 242.71 | +17.29 | +7.1% | [-0.63, +1.28] |
| post-play fair vs candle after (honest, executable) | 0.08 | 30 | 0 | 17 | 13 | 161.07 | +8.93 | +5.5% | [-0.86, +1.67] |
| pre-play fair vs candle after (leaky, for comparison) | 0.03 | 52 | 0 | 36 | 16 | 325.29 | +34.71 | +10.7% | [-0.02, +1.43] |
| pre-play fair vs candle after (leaky, for comparison) | 0.05 | 44 | 0 | 27 | 17 | 262.24 | +7.76 | +3.0% | [-0.82, +1.14] |
| pre-play fair vs candle after (leaky, for comparison) | 0.08 | 34 | 0 | 20 | 14 | 185.22 | +14.78 | +8.0% | [-0.74, +1.64] |
| placebo: fair vs one candle later still | 0.03 | 55 | 0 | 35 | 20 | 332.15 | +17.85 | +5.4% | [-0.47, +1.14] |
| placebo: fair vs one candle later still | 0.05 | 44 | 0 | 28 | 16 | 251.72 | +28.28 | +11.2% | [-0.39, +1.54] |
| placebo: fair vs one candle later still | 0.08 | 36 | 0 | 24 | 12 | 191.37 | +48.63 | +25.4% | [+0.20, +2.38] |
| placebo: games' outcomes shuffled (post-play fair vs candle after) | 0.03 | 49 | 0 | 23 | 26 | 299.58 | -69.58 | -23.2% | [-2.45, -0.34] |
| placebo: games' outcomes shuffled (post-play fair vs candle after) | 0.05 | 40 | 0 | 19 | 21 | 242.71 | -52.71 | -21.7% | [-2.69, -0.08] |
| placebo: games' outcomes shuffled (post-play fair vs candle after) | 0.08 | 30 | 0 | 14 | 16 | 161.07 | -21.07 | -13.1% | [-2.15, +0.76] |
<!-- /results:ncaaf_w2_steal -->

<!-- results:ncaaf_w2_lock -->
| rule (post-play fair vs candle after) | edge | games | locks | wins | losses | staked $ | P&L $ | ROI | 90% interval, P&L per game |
|---|---|---|---|---|---|---|---|---|---|
| lock when guaranteed ≥ 0% of hold EV (break-even) | 0.02 | 59 | 51 | 51 | 8 | 513.82 | -3.82 | -0.7% | [-0.40, +0.22] |
| lock when guaranteed ≥ 0% of hold EV (break-even) | 0.04 | 46 | 38 | 38 | 8 | 397.08 | -17.08 | -4.3% | [-0.93, +0.07] |
| lock when guaranteed ≥ 0% of hold EV (break-even) | 0.06 | 37 | 29 | 29 | 8 | 301.72 | -11.72 | -3.9% | [-0.85, +0.13] |
| lock when guaranteed ≥ 0% of hold EV (break-even) | 0.10 | 25 | 23 | 23 | 2 | 216.35 | +13.65 | +6.3% | [+0.39, +0.72] |
| lock when guaranteed ≥ 50% of hold EV | 0.02 | 59 | 51 | 51 | 8 | 503.09 | +6.91 | +1.4% | [-0.23, +0.41] |
| lock when guaranteed ≥ 50% of hold EV | 0.04 | 46 | 38 | 38 | 8 | 381.86 | -1.86 | -0.5% | [-0.60, +0.45] |
| lock when guaranteed ≥ 50% of hold EV | 0.06 | 37 | 29 | 29 | 8 | 292.64 | -2.64 | -0.9% | [-0.65, +0.44] |
| lock when guaranteed ≥ 50% of hold EV | 0.10 | 25 | 23 | 23 | 2 | 209.56 | +20.44 | +9.8% | [+0.55, +1.20] |
| lock when guaranteed ≥ 100% of hold EV | 0.02 | 59 | 42 | 47 | 12 | 470.06 | -0.06 | -0.0% | [-0.56, +0.54] |
| lock when guaranteed ≥ 100% of hold EV | 0.04 | 46 | 33 | 36 | 10 | 349.37 | +10.63 | +3.0% | [-0.45, +0.93] |
| lock when guaranteed ≥ 100% of hold EV | 0.06 | 37 | 26 | 28 | 9 | 272.84 | +7.16 | +2.6% | [-0.42, +0.82] |
| lock when guaranteed ≥ 100% of hold EV | 0.10 | 25 | 21 | 22 | 3 | 195.99 | +24.01 | +12.2% | [+0.32, +1.59] |
| fair = model, hold to settlement | 0.02 | 58 | 0 | 45 | 13 | 423.04 | +26.96 | +6.4% | [-0.21, +1.11] |
| fair = model, hold to settlement | 0.04 | 54 | 0 | 39 | 15 | 390.99 | -0.99 | -0.2% | [-0.85, +0.74] |
| fair = model, hold to settlement | 0.06 | 45 | 0 | 35 | 10 | 323.54 | +26.46 | +8.2% | [-0.34, +1.64] |
| fair = model, hold to settlement | 0.10 | 35 | 0 | 23 | 12 | 230.75 | -0.75 | -0.3% | [-1.21, +1.12] |
<!-- /results:ncaaf_w2_lock -->

On college the real executable pairing is **+3.5 % / +7.1 % / +5.5 %** ROI at 3 / 5 / 8 %
edges (49 / 40 / 30 games) while the shuffled-outcomes placebo loses **−23 % / −22 % /
−13 %** — the sign separates from the placebo here, unlike the NFL — but every P&L interval
still includes zero. The break-even lock loses at 2–6 % edges (−0.7 % to −4.3 %) and is
positive only at the 10 % edge on 25 games; the 100 %-of-hold-EV lock is flat to slightly
positive. Same caveats as the NFL: candle-close asks, no depth, no CDNA history, one week.

## Replay input verification (P04)

Three checks on what the harness feeds the model, all offline-reproducible from
`tests/fixtures/results/`.

### ESPN's win probability is scored after the play

`scripts/check_espn_wp_alignment.py` looks at scoring plays and asks whether ESPN's
`winprobability[playId].homeWinPercentage` jumps *into* the play's own entry (post-play) or
*out of* it (pre-play):

<!-- results:espn_wp_alignment_p04 -->
| pooled (NFL 2026 week 1) | value |
|---|---|
| games / plays / scoring plays scored | 16 / 2904 / 148 |
| share of ESPN WP jumps that land before the play's own entry | 88.5% |
| mean \|jump\| between the previous entry and the play's entry | 0.0588 |
| mean \|jump\| between the play's entry and the next | 0.0136 |
| model (pre-play state) closer to ESPN's post-play number | 29.7% |
| mean \|model − ESPN\| scored pre-play / post-play | 0.0649 / 0.0947 |
| rows that needed the fallback alignment | 0.0% |
| verdict | ESPN's entry is the state **post** the play |
<!-- /results:espn_wp_alignment_p04 -->

<!-- results:espn_wp_alignment_p04_games -->
| game | scoring plays | jump lands before the entry | mean \|jump\| before | mean \|jump\| after | alignment |
|---|---|---|---|---|---|
| NE@SEA | 5 | 100.0% | 0.0889 | 0.0017 | post |
| SF@LAR | 6 | 100.0% | 0.0532 | 0.0024 | post |
| TB@CIN | 12 | 100.0% | 0.0464 | 0.0009 | post |
| NO@DET | 10 | 90.0% | 0.0470 | 0.0509 | post |
| NYJ@TEN | 7 | 71.4% | 0.0187 | 0.0006 | post |
| BAL@IND | 11 | 81.8% | 0.0262 | 0.0047 | post |
| ATL@PIT | 9 | 100.0% | 0.0721 | 0.0088 | post |
| CHI@CAR | 17 | 82.3% | 0.1111 | 0.0409 | post |
| CLE@JAX | 8 | 75.0% | 0.0120 | 0.0003 | post |
| BUF@HOU | 12 | 100.0% | 0.0986 | 0.0075 | post |
| MIA@LV | 8 | 87.5% | 0.0394 | 0.0063 | post |
| GB@MIN | 11 | 90.9% | 0.0687 | 0.0079 | post |
| WSH@PHI | 8 | 75.0% | 0.0681 | 0.0325 | post |
| ARI@LAC | 8 | 87.5% | 0.0430 | 0.0084 | post |
| DAL@NYG | 7 | 100.0% | 0.0486 | 0.0077 | post |
| DEN@KC | 9 | 77.8% | 0.0422 | 0.0036 | post |
<!-- /results:espn_wp_alignment_p04_games -->

88.5 % of the jumps land before the entry, so ESPN's number on a play describes the state
**after** it. The replay therefore compares ESPN with the post-play model (`model_after`)
and the post-play candle, and the live watcher treats ESPN as a lagging confirmation, not a
pre-snap source.

### Feed parity: ESPN-derived state vs nflverse play-by-play

`scripts/backtest_live_feed.py` aligns the replay's per-play state with nflverse's
`play_by_play_2026.csv.gz` for the same 16 games:

<!-- results:feed_parity_w1_p04 -->
| field (ESPN-derived replay state vs nflverse pbp) | aligned plays compared | agree | rate |
|---|---|---|---|
| possession | 2,510 | 2,509 | 99.96% |
| down | 2,292 | 2,291 | 99.96% |
| distance | 2,292 | 2,288 | 99.83% |
| yardline_100 | 2,463 | 2,459 | 99.84% |
| home_timeouts | 2,633 | 2,632 | 99.96% |
| away_timeouts | 2,633 | 2,633 | 100.00% |
| clock (±10 s) | 2,633 | 2,494 | 94.72% |
<!-- /results:feed_parity_w1_p04 -->

<!-- results:feed_parity_w1_p04_alignment -->
| alignment | value |
|---|---|
| games / aligned plays | 16 / 2633 |
| ESPN plays unmatched / nflverse plays unmatched | 8 / 107 |
| administrative rows skipped (ESPN / nflverse) | 263 / 16 |
| plays aligned one row off (scoring plays carry the post-play clock) | 115 |
| play-id match rate | 96.09% |
| wall-clock anomalies | 12 |
| timeouts source | text |
<!-- /results:feed_parity_w1_p04_alignment -->

<!-- results:feed_parity_w1_p04_games -->
| game | plays | possession | down | yardline | home timeouts | clock | wall-clock anomalies |
|---|---|---|---|---|---|---|---|
| NE@SEA | 152 | 100.0% | 100.0% | 100.0% | 100.0% | 96.9% | 0 |
| SF@LAR | 146 | 100.0% | 99.3% | 99.3% | 100.0% | 96.0% | 11 |
| TB@CIN | 150 | 100.0% | 100.0% | 99.3% | 100.0% | 93.1% | 0 |
| NO@DET | 198 | 99.5% | 100.0% | 100.0% | 99.5% | 95.2% | 0 |
| NYJ@TEN | 150 | 100.0% | 100.0% | 100.0% | 100.0% | 95.6% | 0 |
| BAL@IND | 152 | 100.0% | 100.0% | 100.0% | 100.0% | 93.0% | 0 |
| ATL@PIT | 158 | 100.0% | 100.0% | 100.0% | 100.0% | 96.5% | 0 |
| CHI@CAR | 171 | 100.0% | 100.0% | 100.0% | 100.0% | 92.2% | 0 |
| CLE@JAX | 138 | 100.0% | 100.0% | 100.0% | 100.0% | 93.0% | 0 |
| BUF@HOU | 164 | 100.0% | 100.0% | 100.0% | 100.0% | 93.5% | 0 |
| MIA@LV | 153 | 100.0% | 100.0% | 100.0% | 100.0% | 95.1% | 0 |
| GB@MIN | 166 | 100.0% | 100.0% | 99.4% | 100.0% | 94.2% | 0 |
| WAS@PHI | 158 | 100.0% | 100.0% | 100.0% | 100.0% | 95.1% | 0 |
| ARI@LAC | 160 | 100.0% | 100.0% | 99.4% | 100.0% | 95.2% | 0 |
| DAL@NYG | 147 | 100.0% | 100.0% | 100.0% | 100.0% | 95.5% | 1 |
| DEN@KC | 147 | 100.0% | 100.0% | 100.0% | 100.0% | 95.4% | 0 |
<!-- /results:feed_parity_w1_p04_games -->

Possession, down, distance, field position and both timeout counts agree on ≥ 99.8 % of
aligned plays (eleven of the sixteen games are 100 % on every field, clock aside); the clock
agrees within 10 s on 94.7 % because ESPN stamps scoring plays with the post-play clock (115
plays aligned one row off). The `receive_2h_ko` flag is taken from ESPN's unflipped opening kickoff (the kicker receives
the second-half kick) and the replay's own timeout counts, recounted from the play texts,
agree with nflverse on 2,632 / 2,633 home and 2,633 / 2,633 away rows (99.96 % / 100 %).

### College spread rescale / clamp: within noise

`scripts/college_experiment.py --week 1 2` re-scores 185 college games with the cheapest
adaptations of the NFL model (rescale the spread by 27/32, clamp |spread| at 19.5, map the
OT clock):

<!-- results:college_experiment_p04 -->
| phase | games | rows | baseline (NFL model as is) | clamp \|spread\| ≤ 19.5 | rescale × 0.84375 | rescale + OT clock (120 s) |
|---|---|---|---|---|---|---|
| regulation | 185 | 32,052 | 0.1852 | 0.1867 | 0.1885 | 0.1885 |
| overtime | 3 | 79 | 3.3189 | 3.3139 | 3.3045 | 1.0491 |
| all | 185 | 32,131 | 0.1929 | 0.1943 | 0.1961 | 0.1906 |
<!-- /results:college_experiment_p04 -->

<!-- results:college_experiment_p04_intervals -->
| phase | variant − baseline (log-loss) | mean | 90% game-cluster interval | excludes zero | games |
|---|---|---|---|---|---|
| regulation | clamp | +0.0014 | [-0.0067, +0.0077] | no | 185 |
| regulation | rescale | +0.0033 | [-0.0051, +0.0110] | no | 185 |
| regulation | rescale_ot | +0.0033 | [-0.0051, +0.0110] | no | 185 |
| all | clamp | +0.0014 | [-0.0067, +0.0077] | no | 185 |
| all | rescale | +0.0032 | [-0.0052, +0.0109] | no | 185 |
| all | rescale_ot | -0.0023 | [-0.0148, +0.0086] | no | 185 |
<!-- /results:college_experiment_p04_intervals -->

Neither the rescale nor the clamp moves the regulation log-loss beyond noise (every
interval includes zero, and the point estimates are slightly worse). The one real fix is the
overtime clock mapping (3.32 → 1.05 log-loss on the 79 OT rows of three games), which the
harness applies; the model itself is unchanged.

## The first live Sunday: momentum, lead-lag and the LAG rule (NFL 2026-09-20)

The earlier LAG profitability claims are **unverified**. They predate observation-time
recording, lossless YES/NO rows, strict forward-label windows, independent-book identity,
two-sided fees, latency, partial fills and failed-leg unwind. They are retained below only
as a description of the legacy discovery data and must not be used as evidence for alerts.

The causal synthetic acceptance case deliberately rejects the momentum prototype:

<!-- results:micro_synthetic -->
| candidate | MAE | persistence MAE | skill | decision |
|---|---|---|---|---|
| H1_momentum_prototype | 0.030 | 0.017 | -0.0128 | reject |
<!-- /results:micro_synthetic -->

Fixture: `tests/fixtures/results/micro_synthetic.json`. The frozen whole-game evaluation
uses H3 at 30 seconds as primary; all other horizons/candidates receive Holm correction.

### The discovery set re-run with executable accounting

`scripts/microstructure_eval.py --fold discovery` replays the 15 games the LAG rule was
designed on (2026-09-20 plus the Monday night game; `tests/fixtures/microstructure/
manifest.json`) through `quant/microdata` and `quant/paperexec`: every trade is an
immediate-or-cancel buy limited to the ask at decision time, meeting the book after the
latency, filled only for the displayed size, sold to the bid at the horizon, **both fees
paid**, unsold-and-unsettled trades excluded and missed fills counted. The recorder polled
every 5 s that day, so the latency is 5 s: the book one second after a decision was never
observed. Intervals are 90 % game-block bootstrap; cells read "mean per contract [interval]
(completed / attempted)".

<!-- results:micro_discovery_trades -->
| horizon | B1 buy at random | H1 momentum | H2 dip | H3 lead-lag |
|---|---|---|---|---|
| 5 s | -4.0c [-4.3, -3.6] (18237/31127) | -4.4c [-4.7, -4.0] (117/194) | -5.1c [-5.4, -4.7] (124/232) | -5.5c [-6.7, -3.9] (48/111) |
| 15 s | -4.0c [-4.3, -3.6] (18162/31127) | -4.3c [-4.7, -3.8] (116/194) | -5.1c [-5.5, -4.7] (122/232) | -5.6c [-7.8, -3.3] (47/111) |
| 30 s | -4.0c [-4.4, -3.6] (18098/31127) | -3.7c [-4.4, -3.1] (115/194) | -5.8c [-7.0, -4.4] (121/232) | -6.1c [-9.0, -2.9] (47/111) |
| 60 s | -4.0c [-4.4, -3.6] (17910/31127) | -3.5c [-4.7, -2.0] (115/194) | -5.9c [-7.6, -3.6] (120/232) | -4.5c [-8.9, +0.9] (44/111) |
<!-- /results:micro_discovery_trades -->

A buy at a random moment costs about 4c per contract round trip (spread plus both fees);
momentum buys, dip buys and the LAG rule all do no better, and at 5-30 s their intervals lie
below zero. None of H1-H3 survives the Holm correction; the primary H3 at 30 s has a
one-sided p of 0.997 that it is positive. **The +$0.073 per contract claimed for LAG below
does not survive executable accounting at the latency these data allow.** They cannot say
how LAG does at the fast lane's ~1 s: that needs games recorded with observation times
(from 2026-09-24 on) and is what the validation fold will measure.

Forecasting the next move does no better than assuming the price stays put (leave-one-game-
out; skill = unchanged-price MAE minus model MAE, negative = worse than unchanged):

<!-- results:micro_discovery_forecast -->
| horizon | model | MAE | unchanged-price MAE | skill 90 % CI |
|---|---|---|---|---|
| 5 s | B3 ridge on dmid_30 | 0.158c | 0.149c | [-0.010, -0.008]c |
| 5 s | B4 ridge on leader gap | 0.198c | 0.167c | [-0.038, -0.026]c |
| 15 s | B3 ridge on dmid_30 | 0.422c | 0.403c | [-0.024, -0.015]c |
| 15 s | B4 ridge on leader gap | 0.543c | 0.440c | [-0.130, -0.083]c |
| 30 s | B3 ridge on dmid_30 | 0.893c | 0.877c | [-0.019, -0.013]c |
| 30 s | B4 ridge on leader gap | 1.100c | 0.881c | [-0.268, -0.182]c |
| 60 s | B3 ridge on dmid_30 | 1.561c | 1.548c | [-0.017, -0.010]c |
| 60 s | B4 ridge on leader gap | 1.854c | 1.556c | [-0.352, -0.252]c |
<!-- /results:micro_discovery_forecast -->

Two-venue arbitrage (H4) on independent executable books, the Robinhood leg at a person's
15 s and the Kalshi leg at 5 s, failed legs unwound at the bid:

<!-- results:micro_discovery_arb -->
| attempts | completed | fill rate | mean per contract | 90 % CI | games positive |
|---|---|---|---|---|---|
| 164 | 97 | 59% | +0.26c | [-0.84, +1.46]c | 40% |
<!-- /results:micro_discovery_arb -->

**LAG, then lock.** `strategy/laglock.py` watches every filled LAG position for ten minutes
and buys the other outcome as soon as the pair costs <= $1 with fees (tie-safe pairs only:
a Kalshi YES + a Rothera YES pays $0.50 on a tie). Replayed on the same games, the lock's
own IOC facing the same latency:

<!-- results:micro_discovery_lock -->
| signals | entries filled | locked | median time to lock | lock or hold (10 min) | same entries held, never locked | locked ones only |
|---|---|---|---|---|---|---|
| 111 | 48 | 6 (12%) | 59 s | -0.0c [-4.7, +5.9] | -0.3c [-4.9, +5.1] | +13.6c [+7.1, +16.8] |
<!-- /results:micro_discovery_lock -->

Locking works mechanically - about one entry in eight became a guaranteed profit, a minute
after entry - but it is not an edge by itself: a lock is mostly available *after* the entry
has moved in its favour, so it turns winners into certainties and leaves the losers to lose.
Against the fair baseline (the same entries held the same ten minutes, never locked) it adds
a fraction of a cent. What would make LAG pay is a better entry, and that is what the
signal grades are logged for: **hard lag** (the follower's all-in is below what the leader's
book would *pay*, its bid - not its mid), **agreement** (other independent books moved the
same way), and **lock now** (the other outcome is already cheap enough somewhere: a hard lag
usually is, on the leader's own venue - which makes it an ARB, pushed as one). The
validation games (fast lane, 1 s, observation times recorded) measure each grade.

Fixture: `tests/fixtures/results/micro_discovery.json`. Descriptive only: these games
designed the rule, so they can reject it but never validate it.

`live --record` ran through the first NFL Sunday with this code (14 games, 15,943 in-play
ticks at 5 s; the laptop slept through part of the early window, so the coverage is partial).
`scripts/leadlag_study.py --db out/history.db --date 2026-09-20` reproduces every number
below from the recorded per-venue L1 (`tests/fixtures/results/leadlag_nfl_2026_w2.json`).

> **Legacy measurement:** that committed fixture predates strict horizon matching, exit-fee
> deductions, full-depth paper-fill checks and settlement-risk execution gates. Its tables
> describe the original experiment and must not be read as net executable performance.
> Re-run the current script on the source database before using any figure below.

**Do big moves revert ("odds inflated by momentum") or continue?** Every Kalshi mid move of
≥ 5¢ between polls ≤ 30 s apart was followed for five minutes. Without a score change (a
drive, a turnover, a big play — 148 of the 157 moves):

| horizon | n | continued | reverted | flat | mean later move ÷ initial move |
|---|---|---|---|---|---|
| +30 s | 142 | 14 | 12 | 116 | +0.03 |
| +2 min | 136 | 24 | 21 | 91 | +0.05 |
| +5 min | 126 | 36 | 29 | 61 | **+0.06** |

The price kept going, or stayed: never on average back toward where it came from. That is
the under-reaction the NBA Kalshi study measures (impact 0.64), not over-reaction. **Buying
the dip against a momentum move had no edge today.** The WP model moved only 9 % as much as the market
on those plays — its ESPN state had not updated yet — which is why the model-vs-market STEAL
rule lost as well (273 ungated STEALs, 91 settled at −$0.39 per contract on average).

**Who leads?** For every ≥ 5¢ move on one venue, had the other already moved half as much
the same way over the previous 60 s, and how long did it take to catch up?

| leader → follower | lead moves | follower had moved first | caught up ≤ 5 min | median lag |
|---|---|---|---|---|
| Robinhood (Rothera) → Kalshi | 178 | 51 | 144 | **23 s** |
| Kalshi → Robinhood | 157 | 132 | 80 | 42 s |
| Robinhood → Polymarket | 178 | 13 | 80 | 165 s |
| Kalshi → Polymarket | 153 | 11 | 85 | 139 s |

Rothera reprices first; Kalshi follows about 23 s later; Polymarket trails by minutes and
often never catches up within five. So the dip that is actually cheap is the *lagging
venue's stale price*, not the move itself.

**The LAG rule** (`arb_engine/strategy/leadlag.py`, on by default in `live`): when a leader
venue's mid moves ≥ `leadlag_move` (5¢) within `leadlag_window_s` (30 s) and an executable
follower's mid has moved less than half of that (or the other way), buy the side the leader
moved toward on the follower, provided its all-in ask sits ≥ `leadlag_min_edge` (2¢) below
the leader's mid and both quotes are fresh. Replayed over the same ticks (fills at the
recorded ask assumed; exit by selling to the follower's *bid*, entry fee paid but exit fee
not paid in this legacy fixture):

| exit | signals | wins | losses | mean P&L per contract |
|---|---|---|---|---|
| +30 s | 172 | 143 | 29 | +$0.054 |
| +60 s | 172 | 149 | 22 | **+$0.073** |
| +5 min | 172 | 117 | 38 | +$0.067 |
| held to settlement (3 finals recorded, 32 signals) | 32 | 23 | 9 | +$0.207 |

161 of the 172 signals were Rothera leading Kalshi. The optimistic assumption is the fill:
the recorded Kalshi ask is at least one 5 s poll old by the time it is seen, and top-of-book
depth is what is recorded. Two things now measure that live: the **fast lane** (`live
--fast 1`, on by default in the launcher) refreshes Kalshi + Robinhood top of book every
second for the live games on its own thread, and the **paper book** (`strategy/paperlag.py`)
opens a paper order at the follower's ask on every LAG, fills it only if the next seconds
still show that ask with size (10 s window), marks it to the bid at +30/+60/+300 s and
settles it from the final score — `scripts/leadlag_study.py --date <day>` prints the fill
rate, fill latency and fill-adjusted P&L from the `lag_paper` table. Until that number exists
the rule is not sized beyond `bankroll × kelly_fraction / ask` and the follower's displayed
depth. (The Monday-night game was already decided when the fast lane went live: 0 signals.)

## Spreads and totals: line fair values vs Kalshi mids (P13)

`quant/lines.py` prices a spread or total from a margin distribution: `NormalMargin` (σ by
line from `data/margin_sigma.json`, in-play scaling by √(fraction of clock left) with sd
floors of 3 / 6 points, explicit tie mass capped at the lattice cell) and `EmpiricalMargin`
(key-number buckets from `data/nfl_margin_dist.json`, 2016–2025; the season in progress is
excluded so the evaluation cannot leak). `scripts/eval_lines.py` (`arb-engine lines-eval`)
scores both against the Kalshi spread market at the closing line and the total market at the
closing total, on *the Kalshi market's own half-point line*, under the same two candle
alignments as the moneyline replay:

<!-- results:lines_eval_p13 -->
| market · phase | paired rows | unpaired rows dropped | normal (log-loss / Brier) | empirical | Kalshi mid before the play | Kalshi mid after the play |
|---|---|---|---|---|---|---|
| spread · pre | 14 | 2 | 0.6837 / 0.2453 | 0.6974 / 0.2521 | 0.6885 / 0.2477 | 0.6885 / 0.2477 |
| spread · inplay | 2,500 | 388 | 0.6162 / 0.2150 | – | 0.6281 / 0.2182 | 0.6219 / 0.2158 |
| total · pre | 15 | 1 | 0.6931 / 0.2500 | – | 0.6984 / 0.2526 | 0.6984 / 0.2526 |
| total · inplay | 2,399 | 489 | 0.6399 / 0.2234 | – | 0.6406 / 0.2221 | 0.6362 / 0.2209 |
<!-- /results:lines_eval_p13 -->

<!-- results:lines_eval_p13_inputs -->
| inputs | value |
|---|---|
| games scored / skipped | 16 / 0 |
| margin σ / total σ / tie mass (pre-game) | 12.6968 / 13.2174 / 0.003622 |
| margin table seasons / σ seasons / rows used | 2016–2025 / 1999–2025 / 2761 |
| seasons excluded as incomplete | 2026 |
| de-vig methods on the closing moneylines: games / heavy favourites / range across methods (mean, max) | 16 / 3 / 0.0181, 0.0193 |
| mean \|de-vigged close − spread-implied\| (points) | 1.05 |
<!-- /results:lines_eval_p13_inputs -->

In play the normal model scores **0.616 on spreads against Kalshi's 0.622–0.628** and
**0.640 on totals against 0.636–0.641**. Read that as *in-sample*, not as a result: the
in-play sd floors (3 points on the spread, 6 on the total, `SPREAD_INPLAY_SD_FLOOR` /
`TOTAL_INPLAY_SD_FLOOR` in `quant/lines.py`) were chosen on these same 16 games. With no
floor (pure √(fraction-of-clock) scaling) the same rows score **0.632 on spreads and 0.718
on totals — behind Kalshi under both alignments**; floor 3 / 6 moves that to 0.616 / 0.640,
and larger floors (6 / 10) do better still on this sample. The possession-sized 3 / 6 was
kept rather than the sample's best because there is no other week to check it on, and the
pace blend was turned off (`PACE_WEIGHT_MAX = 0`) for the same reason (it hurt at every
floor here). So the honest standing is: *untuned* the model is worse than the market in
play; *tuned on this week* it is level with the market on totals and a hair ahead on
spreads, and whether that survives out of sample is the "re-fit the in-play sd floors" row
in `docs/ROADMAP.md` ("Needs you") — score weeks 2+ with `scripts/eval_lines.py --week N`
before reading anything into the 0.616. Pre-game there are only 14–15 paired rows. The
empirical margin table is pre-game only (in play it falls back to the normal). This is
enough to attach line fairs to the in-play watcher and the scanner as an *opt-in* reference
(`LINE_FAIR=1`, one knob shared by both; STEAL on a line needs twice the edge), not enough
to call it an edge, and not a reason to turn `LINE_FAIR` on for real money.

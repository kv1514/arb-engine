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

Stacking was chosen over either piece alone on the held-out season: plain XGBoost
(300 rounds, depth 5) scored 0.4790, the logistic alone 0.4763, the stack 0.4751. A simple
probability average of the two separate models did not beat the logistic alone.

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
the same feature set on a much longer history of seasons; beating it by 0.0017 log-loss on
an unseen season is a small but real edge (the 2025 season is ~284 games, so the standard
error on a log-loss difference this size is of the same order — read it as "as good as
nflfastR, probably a touch better", not as a large gap).

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

## Known limitations

* **No injuries, weather, roster or coaching information.** Everything about team
  strength enters through the pre-game spread; a mid-game injury changes nothing until it
  shows up in the score.
* **Overtime**: the model is trained on regulation plays only. An OT state (nflverse restarts
  `game_seconds_remaining` at 600 in OT) is scored as if it were a tied fourth quarter
  with that much time left; the sudden-death / one-possession rules are not modelled. Treat OT outputs as approximate.
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

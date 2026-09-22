# Research synthesis (2026-09-20)

Ten clusters in `docs/research/` read against `docs/MODEL.md` and `docs/ROADMAP.md`
(2026-09-19). Grades: A = verified on the same venue or the repo's own arithmetic; B = verified
but transferred (other sport, venue, era); C = abstract-only, self-reported or theory. Repo
numbers are quoted from MODEL.md.

## Executive summary

* **The week-1 result has a published mechanism, and it is not a round-trip trade.** Kalshi
  NBA mids move 0.64-for-one with a public-information benchmark and close ~46 % of the
  residual over 5 min: hence the after candle beating the before candle (0.438 vs 0.443) and
  every NFL break-even LOCK losing (−4.7 to −13.0 %). The 5-min trade is −1.20 % ask-to-bid
  before the taker fee, so any edge is hold-to-settlement or maker-side, and the hold P&L
  intervals still include zero. A diagnostic / B transfer.
  [01](research/01-in-play-nfl-win-probability-models-nflfa.md),
  [03](research/03-prediction-market-microstructure-price-d.md)
* **STEAL ROI needs an efficient-market null**: literature-sized single-season
  "inefficiencies" arise under full efficiency, so add a Bernoulli(market fair) placebo. C
  (abstract-level read; the null itself is the repo's own arithmetic once run).
  [04](research/04-market-efficiency-and-biases-in-sports-p.md)
* **Retire the 3 % edge; do not crown 5–8 %.** Within-game dependence makes 4,101 games
  ~2,291 effective and a 90 % WP band ~6.3 pp; 5 % and 8 % are no better separated from the
  placebos. B. [01](research/01-in-play-nfl-win-probability-models-nflfa.md)
* **The blend penalty is a weight problem; CORP decides whether the market ever regains
  in-play weight.** A stacker or BLP layer recovers ~0.001 against the +0.011 the grid
  recovers at the model corner; fits must weight games, not plays. `corp_decomposition` is
  never reported: if Kalshi's deficit (0.4425 / 0.4382 vs 0.4060) is discrimination, the
  deferred weight change is right to wait. A / B.
  [07](research/07-forecast-combination-and-calibration-ext.md)
* **Monotone constraints belong at training time.** The 17-vs-16-point dip is the expected
  XGBoost failure at ~1,400 effective games; Baldwin's 0.0004 cost does not transfer (repo
  ~0.003), so the running-max guard stays until re-measured under LOSO. A / B.
  [09](research/09-machine-learning-for-pre-game-and-in-gam.md)
* **No FLB correction of exchange mids.** Sports longshots return +2.4 % on Polymarket, Kalshi
  sports slopes are 0.90–1.10, and Kalshi's platform FLB is a taker phenomenon measured
  outside in-play. Stratify P&L by entry price instead. B.
  [04](research/04-market-efficiency-and-biases-in-sports-p.md)
* **Sizing: the fraction is a risk spec, the fair is the lever.** `--kelly 0.25` (0.0155 at a
  3 ¢ edge) equals drawdown-constrained Kelly at α 0.7 / β 0.1 (0.0167); probability error
  costs growth at first order, fraction error at second. Corrections: at the college 10 % edge the break-even LOCK is
  +6.3 % on 25 games (the 50 %- and 100 %-of-hold-EV variants +9.8 % and +12.2 %), its only
  positive replay; the settlement wedge is ~0.002 % per game. B / A.
  [05](research/05-optimal-staking-under-fees-and-model-err.md)
* **Executable arbitrage is in-play, seconds-long, ~15 shares**, mostly ML × spread;
  `PaperBroker` fills are an upper bound behind rebated professionals. B / C.
  [06](research/06-arbitrage-and-hedging-across-prediction-.md),
  [10](research/10-practitioner-and-open-source-evidence-pu.md)
* **College transfer is closed except overtime**: the 120-s OT clock mapping (3.32 → 1.05 on
  79 rows) is the only measured college gain. B.
  [02](research/02-college-football-in-play-models-cfbfastr.md)
* **Tennis does not inherit the NFL edge.** In-play odds track the Markov model; a 3 ¢ edge
  needs serve precision ~0.006. Invert the market to (p_a, p_b); retirement hazards are a
  separate event from walkovers. A / B.
  [08](research/08-tennis-in-play-modelling-point-level-mar.md)
* **Do not build** LLM forecasters or sequence models (all five KellyBench agents negative).
  B negative (EPL at bookmaker odds, transferred). [09](research/09-machine-learning-for-pre-game-and-in-gam.md)

## Implementation candidates, merged and ranked

Source sections in brackets; near-duplicates merged. Effort S / M / L. Ranks 1–20 need no
new data, but not from a fresh clone: "W1 cache" / "W2 cache" in the offline-test column are
the week caches of one prior network run (`backtest --week 1 --offline --cache-dir
out/cache/replay`, `--sport ncaaf --week 2`; `out/` is gitignored, as are the persisted
`--json` week files). What is committed is the 2-game `tests/fixtures/history/replay_trim`,
the synthetic ticks (`tests/fixtures/ticks/`), candle / trade fixtures and the metrics-only
`tests/fixtures/results/replay_*.json`, which pin numbers but hold no rows
(`backtest.load_results` on them returns zero games). Ranks 21+ also wait on a Needs-you
row or new data.

| # | change | module | metric | offline test | grade | effort |
|---|---|---|---|---|---|---|
| 1 | Own impact `b` / drift `rho_h` (Δp Kalshi after − before on Δq model; Gap to +1/+5/+15 min), by volume / width tercile, per week; post-score efficiency curve [01, 03] | `quant/eventstudy.py`, `backtest.week_report` | `b`, `rho_5` with game-cluster intervals; sets gap `g` | W1 cache + `kalshi_candles_before_after.json`; planted `b = 0.6` on synthetic rows | A / B | M |
| 2 | `placebo="bernoulli"`: redraw outcomes from the market fair, rerun STEAL B times [04] | `backtest.simulate_pairings` | quantile of −10.3 / +2.7 / +50.6 % in the null | W1 / W2 caches | C (abstract) / A once run | S |
| 3 | CORP MCB / DSC / UNC, reliability bands, classwise ECE per source, rendered into MODEL.md [07, 09] | `backtest.week_report`, `quant/calibration.py` | recalibrate mid vs keep market weight ~0 | trim cache `corp` key | A | S |
| 4 | Fee-inclusive round trip on the `steal_observations` ladder, taker and maker [03] | `store.convergence()`, `simulate_steal` | after-fee P&L at +5 min vs hold | `synthetic_40.json`; fee vectors | A | S |
| 5 | Monotone retrain (Brill's full list, `min_child_weight` 50–100), LOSO, select 2024, score once 2025; drop the guard only if dips vanish [01, 02, 09] | `train_wp_model.py`, `models/cv.py`, `models/wp.py` | 2025 ≤ 0.4751 + 0.003; dip → 0 | leads 0–30 monotone; W1 cache `--offline` | A / B | M |
| 6 | `fractional_bootstrap(phi=0.5)` column; retire 3 % from the headline [01] | `quant/calibration.py` | model − Kalshi still excludes zero | W1 cache `--offline` | B | S |
| 7 | 1/n_game row weights in `fit_blend_weights` and calibration fits, grouped CV [07] | `backtest.py`, `models/cv.py` | OOF weight variance | W1 cache: best point stays at the model corner | B | S |
| 8 | Entry-price and \|spread\| strata in STEAL tables with per-bucket intervals; no edge change [02, 04, 10] | `strata_tables`, `simulate_pairings` | ROI per bucket (0.8–0.9 fake edge?) | W1 / W2 caches | B | S |
| 9 | Mincer–Zarnowitz by price decile, WLS p(1−p), game-clustered [04] | `quant/calibration.py` | γ, ψ per decile | W1 / W2 caches | A method | S |
| 10 | Beuoy play-by-play Kelly contest per game; credibility weights only on pooled weeks [05, 09] | `backtest.week_report` | re-pricing lag vs settlement error | synthetic games | B / C | S |
| 11 | `A_KL` and reliability band on STEAL-qualifying rows vs the shuffle placebo [05] | `backtest.class_gaps` | `A_KL` interval, ECE on selected rows | W1 cache `--offline --placebo` | A diag / C | S |
| 12 | Back-of-queue paper fills from the public trade tape [10] | `strategy/broker.py`, `venues/trades.py` | fill rate; hedge-timing P&L | trades fixtures; diff paper fills | B | M |
| 13 | Kelly-sized partial hedge and Kelly-gated lock beside the break-even rules [05, 06] | `simulate_steal`, `quant/sizing.py` | LOCK vs hold P&L | W1 / W2 caches; regenerate the `*_lock` results tables | C | M |
| 14 | Gap-aware STEAL (edge AND last-60-s Δmodel − Δmid ≥ `g`); 15-min LOCK blackout [01] | `strategy/inplay.py`, `simulate_pairings` | hold P&L at 5 / 8 % vs placebos | W1 cache; rows surviving of 159 / 35 / 7 | B | M |
| 15 | `rck_fraction(alpha, beta)` beside the 0.25× stake; document 0.25 ≈ α 0.7 / β 0.1 [05] | `quant/sizing.py`, `cli.cmd_kelly` | P(min wealth < α) ≤ β | W1 / W2 caches, B = 1000 game-cluster resamples | B | S |
| 16 | Sizing tests on the venue tick grid; fees never net across lock legs [05] | `tests/test_inplay.py`, `test_fees.py` | 9 / 15 / 24 contracts at $300 / 500 / 800 | unit | B | S |
| 17 | Shrunk Kelly at `q_low = σ(logit q − Φ⁻¹(1−α)·s)`, `source="shrunk"` [05] | `quant/sizing.py`, `simulate_steal` | P&L interval vs `blend` | W1 / W2 caches | B | M |
| 18 | Beta calibration of `home_p` with a 1.65-SE identity gate [07] | `quant/calibration.py`, `blended_fair` | MCB; expected identity | (1, 1, 0) on synthetic | B | M |
| 19 | GLM stacker (coefficient sum per quarter), k = 1 Kalshi (a, b), overlap diagnostic [04, 07] | `fit_blend_weights` | OOF log-loss (≤ 0.002 expected) | identical sources → sum ≈ 1 | B | M |
| 20 | Moshrefi late compression: Platt slope, γ at `gsr ≤ 600`; no gate [09] | `strata_tables` | whether a q4_late multiplier exists | a = 1 on synthetic | B | S |
| 21 | `w_market(t, lead)` = clamp(a0 − a1·elapsed − a2·\|lead\|/7) under the weight policy [01] | `blended_fair`, `fit_blend_weights` | scrimmage log-loss vs model-only | `--pool 'out/*.json'` on the persisted week files plus a new `--weights-fn` flag (none exists); walk-forward | B | M |
| 22 | College OT: 120-s mapping in `GameReplayer`, then a possession-outcome OT rule [02] | `backtest.py`, `models/wp.py` | `ot` slice model log-loss (n = 0 now) | `college_experiment.py` reproduces 1.0491 | B | S / M |
| 23 | Rank / size arbs on `size_from_books` when `--books` is on [06] | `scanner.py`, `quant/arbitrage.py` | fillable size; L1 arbs surviving | 54 arb vectors | B | S |
| 24 | `GET /account/limits` at start-up; limiter at a tier fraction [10] | `venues/kalshi.py` | 429 count; quote age | fake-http tier test | B facts | S |
| 25 | Tennis stdlib Markov (game / tiebreak / set / match) + `invert_match_probability`, signal-only [08] | new `models/tennis_markov.py` | `InplayView.disagreement` only | goldens `g(0.64) = 0.8126`; at `p_a + p_b = 1.28`, `p_a − p_b` = 0 / 0.01 / 0.02 / 0.05 → Bo3 0.500 / 0.550 / 0.599 / 0.734 | A / B | S |
| 26 | Gate in-play tennis STEAL / LOCK (`tennis-lag-unmeasured`); `tennis_retirement` rows beside `p_walkover` [08] | `strategy/inplay.py`, `settlement_rules.json` | zero in-play tennis takers | tennis synthetic ticks; gates bit-identical | A / B | S |
| 27 | OO-EPC de-vig; FL-GLM β on nflverse closes scored on 2025 [04] | `quant/odds.py` | cross-method range; 2025 log-loss | `eval_lines.py --offline` | B | S / M |
| 28 | Kalshi–Robinhood lead-lag from `record_l1` at 10 s, never 5-min bars [03] | new `quant/leadlag.py` | lead in seconds | planted 20 s lead | B | M |
| 29 | ML × spread-cover legs with middle EV behind `LINE_FAIR=1` [06] | `scanner.py`, `quant/lines.py` | pairs after the 7 % fee (~0) | NFL lines fixtures | B | M |
| 30 | Per-state WP band from 50–100 refits: STEAL stratifier first, edge floor only if terciles separate [01, 02, 09] | `train_wp_model.py --bootstrap`, `class_gaps` | P&L per tercile vs placebo | offline refits on `.npz` | B / C | L |
| 31 | Native college set: cfbfastR 2014–2024, closing line, era flag, feed parity [02] | `quant/feedparity.py`, new build script | parity ≥ 99.8 %; later W2 vs 0.2156 | needs a hand download | B / C | L |
| 32 | Five book levels in `record_l1`; ESPN→Kalshi latency p90 sets `inplay_stale_after_s` [03] | `store.py`, `FeedFreshness` | slippage; gated count | schema test | C | S |
| 33 | Maker: markout ladder, taker-imbalance pause, inventory from fills, realised-σ watch [03, 10] | `strategy/maker.py`, `store.py` | markout after the 1.75 % fee | `PaperBroker` scripted tape | C | M |
| 34 | STEAL entry variants: two-poll persistence gate; reversal pairing [06, 10] | `strategy/inplay.py`, `tickreplay.py` | count, P&L, CLV gates on / off | `backtest-ticks --gates both` | C | S |
| 35 | Retrain-side ablations inside #5: `total_points`, `ep_adjusted_diff`, drive / turnover, anchor source [01, 09] | `models/wp.py`, `resolve_spread` | gain must exceed its interval | LOSO; `SpreadFallbackTests` | C | M |
| 36 | Tennis serve-shrink (κ 640 / 160 / 40); one recorded tennis day [08] | `tennis_markov.py`, `store.py` | absorption after a break | none until recorded | B / C | M |
| 37 | Fuzzy-title match can never create an event key [10] | `matching/` | guard | near-duplicate fixture | C | S |
| — | Confirmed, no change: settlement mismatches stay hard flags [06] | `settlement_rules.py` | — | `verify()` | B | — |

Dropped: joint slate solve (~1 % at this size), dynamic model averaging and e-process
monitor (unmeasurable at 16–86 games), volume / width market weight (inverted by the ≤ 4 ¢
row), LLM or sequence models, Polymarket rewards P&L, longshot-only edge rules, any
fair-source switch on trailing ECE.

## Open questions and what settles them

1. **Is Kalshi NFL's `b` ≈ 0.64, and does the drift survive a taker fee held to
   settlement?** #1, #4 offline; then **Record a live Sunday slate** and the ~70-game
   *In-play STEAL* gate.
2. **Stale mids (MCB) or missing information (DSC)?** #3 on candles; the recorded slate's
   L1 ticks show stale mid vs empty book.
3. **Slow market or mispriced market?** #10; only settlement error pays a hold STEAL.
   Recorded slate plus ~70 games.
4. **Effective sample size; does the 2025 tail pattern persist?** #5, #3 offline; cleanly,
   *Retraining the WP model on 2025–2026*.
5. **ESPN latency, Kalshi–Robinhood lead-lag, quote lifetimes, depth.** Only **Record a live
   Sunday slate** (#28, #32, #34).
6. **Does a retail post-only bid fill at positive markout?** **Kalshi demo-key check**, then
   the recorded slate with `maker --mode demo` (#12, #33).
7. **Tie payout ordering and tie-margin sign.** **Paste the Rothera tie clause**.
8. **Per-leg fees and rounding on 3–15 contracts.** **Rothera order-ticket fee preview**,
   **Kalshi cent-vs-centicent rounding** (#16).
9. **Blend-weight direction (NFL model corner vs college 0.10 / 0.90 / 0.00).** The deferred
   *Move the blend weights* gate (#7, #21).
10. **College closing-line column, post-2021 OT rates, cfbfastR parity.** New Needs-you step:
    download one season's parquet by hand (#22, #31).
11. **Tennis: live point / server in ESPN, Kalshi lag, `p_a + p_b` per tour.** A tennis
    recorded day; **Re-run the walkover shares**; **Polymarket US gateway curl**.
12. **In-play FLB and late compression on Kalshi sports.** #9, #20 offline; taker-side
    resolution needs the recorded slate.

## Postscript: what the first live Sunday settled (2026-09-22)

The recorded slate the open questions kept pointing at ran two days after this synthesis
(NFL 2026-09-20; the numbers and their reproduction are in `docs/MODEL.md`, "The first live
Sunday", from `tests/fixtures/results/leadlag_nfl_2026_w2.json`). Against the clusters:

* **Question 5 (lead-lag, quote lifetimes) is answered, and it is the trade.** Of Rothera's
  178 ≥ 5¢ moves, Kalshi had moved first on only 51 and caught up within five minutes on
  144, median 23 s; Polymarket trails both by minutes. Every two-leg ARB the day showed was
  such a flash, in-play and seconds long, none pre-game: cluster 06's prediction, with the
  venue order measured. Candidate #28 ran on the day's L1 at 5 s (`scripts/leadlag_study.py`)
  rather than on 10 s bars, and became the LAG rule.
* **The partial-adjustment mechanism of cluster 03 does not produce a reversal to fade.**
  Kalshi ≥ 5¢ moves without a score change continued rather than reverted at +30 s, +2 min
  and +5 min (buying the dip after a move lost). What the same mechanism *does* produce is the
  laggard's stale ask: buying the follower venue as soon as the leader has repriced replayed
  149 W / 22 L (+$0.073 per contract selling to the bid 60 s later; 23 W / 9 L, +$0.207 held
  to settlement on the three games that finished inside the recording). Fill-adjusted
  numbers wait on the paper book (`lag_paper`).
* **Question 3 (slow vs mispriced) now has data on the taker side**: the 3 % STEAL lost live
  (91 settled, −$0.39 per contract), consistent with the retire-the-3 %-edge finding above.
  The hold-STEAL gate in the roadmap stays closed.
* Still open from the list: 6 (post-only fills), 7–8 (Rothera clauses and fees), 10–11
  (college native set, tennis).

## Section index

| file | one line |
|---|---|
| [01 NFL in-play WP](research/01-in-play-nfl-win-probability-models-nflfa.md) | Kalshi under-reaction explains LOCK; WP noise bounds STEAL edges; anchor decay is `spread_time` |
| [02 College](research/02-college-football-in-play-models-cfbfastr.md) | Spread rescale closed; OT is the one break; Brill's monotone list; cfbfastR data |
| [03 Microstructure](research/03-prediction-market-microstructure-price-d.md) | Drift is a negative round trip; width is not confidence; lead-lag needs L1 ticks |
| [04 Efficiency and biases](research/04-market-efficiency-and-biases-in-sports-p.md) | No FLB in sports exchange mids; efficient-market null; de-vig upgrades |
| [05 Staking](research/05-optimal-staking-under-fees-and-model-err.md) | 0.25× Kelly as a drawdown spec; calibrate the fair, not the fraction |
| [06 Arbitrage and hedging](research/06-arbitrage-and-hedging-across-prediction-.md) | Arbs are seconds-long and small; settlement wedge irrelevant; Kelly partial hedge |
| [07 Combination and calibration](research/07-forecast-combination-and-calibration-ext.md) | Blend is a weight problem; CORP split; beta calibration with identity gate |
| [08 Tennis](research/08-tennis-in-play-modelling-point-level-mar.md) | Markov backbone; invert the market; retirement hazards; gate in-play tennis |
| [09 ML for prediction](research/09-machine-learning-for-pre-game-and-in-gam.md) | Nothing beats the close; monotone + shrinkage; Kelly contest; no LLMs |
| [10 Practitioner evidence](research/10-practitioner-and-open-source-evidence-pu.md) | No bot reports fills; back-of-queue fills; makers vs takers; rate limits |

## Bibliography (verified URLs, deduplicated)

Academic and working papers:

- Aktuğ, Torul (2026). Price Discovery Across Political Prediction Markets. <https://web.bogazici.edu.tr/torul/pridis.pdf>
- Angelini, De Angelis (2026). When Do Markets Fully Process Public Information? arXiv:2606.07811. <https://arxiv.org/abs/2606.07811>
- Angelini, De Angelis, Singleton (2022). Informational efficiency and behaviour within in-play prediction markets. IJF 38(1). <https://www.carlsingletoneconomics.com/uploads/4/2/3/0/42306545/information_efficiency_angelini_de_angelis_singleton.pdf>
- Baldwin (2020). nflfastR EP, WP, CP, xYAC and xPass models. <https://opensourcefootball.com/posts/2020-09-28-nflfastr-ep-wp-and-cp-models/>
- Baldwin (2021). NFL win probability from scratch using xgboost in R. <https://opensourcefootball.com/posts/2021-04-13-creating-a-model-from-scratch-using-xgboost-in-r/>
- Beuoy (2026). Kelly Betting as Bayesian Model Evaluation. arXiv:2602.09982. <https://arxiv.org/abs/2602.09982>
- Brill, Yurko, Wyner (2023 / 2025). Analytics, have some humility. arXiv:2311.03490. <https://arxiv.org/abs/2311.03490>
- Brill, Yurko, Wyner (2024 / 2025). Exploring the Difficulty of Estimating Win Probability. arXiv:2406.16171. <https://arxiv.org/abs/2406.16171>
- Bürgi, Deng, Whelan (2026). Makers and Takers: The Economics of the Kalshi Prediction Market. <https://www.karlwhelan.com/Papers/Kalshi.pdf>
- Cardozo, Rivero-Wildemauwe (2026). The Favorite-Longshot Bias in Prediction Markets: Evidence from Polymarket. arXiv:2609.12878. <https://arxiv.org/html/2609.12878>
- Cheng, Yang, Zou (2026). Arbitrage Analysis in Polymarket NBA Markets. arXiv:2605.00864. <https://arxiv.org/abs/2605.00864>
- Clarke, Kovalchik, Ingram (2017). Adjusting Bookmaker's Odds to Allow for Overround. <https://www.sciencepublishinggroup.com/article/10.11648/j.ajss.20170506.12>
- Divos, del Bano Rollin, Bihari, Aste (2018). Risk-Neutral Pricing and Hedging of In-Play Football Bets. arXiv:1811.03931. <https://arxiv.org/pdf/1811.03931>
- Dubach (2026). The Anatomy of a Decentralized Prediction Market. arXiv:2604.24366. <https://arxiv.org/html/2604.24366v1>
- Easton, Uylangco (2010). Forecasting outcomes in tennis matches using within-match betting markets. IJF 26(3). <https://ideas.repec.org/a/eee/intfor/v26yi3p564-575.html>
- Galekwa, Tshimula, Tajeuna, Kyandoghere (2024). A Systematic Review of Machine Learning in Sports Betting. arXiv:2410.21484. <https://arxiv.org/html/2410.21484v1>
- Gebele, Matthes (2026). When Certainty Is Not Worth It: Capital Lock-Up and Settlement Discounting. arXiv:2605.31431. <https://arxiv.org/html/2605.31431>
- Gebele, Mutzel, Matthes (2026). Executable Arbitrage and Market Efficiency in Prediction Markets. arXiv:2608.00666. <https://arxiv.org/html/2608.00666v1>
- Gneiting, Ranjan (2013). Combining predictive distributions. EJS 7. <https://arxiv.org/pdf/1106.1638>
- Goto, Takeishi, Yairi (2026). Forecast Sports Outcomes under Efficient Market Hypothesis. arXiv:2604.17194. <https://arxiv.org/html/2604.17194>
- Grady et al. (2026). KellyBench: A Benchmark for Long-Horizon Sequential Decision Making. arXiv:2604.27865. <https://arxiv.org/html/2604.27865v1>
- Hegarty, Whelan (2024). Comparing Two Methods for Testing the Efficiency of Sports Betting Markets. <https://mpra.ub.uni-muenchen.de/121382/1/MPRA_paper_121382.pdf>
- Kovalchik, Reid (2019). A calibration method with dynamic updates for within-match forecasting of wins in tennis. IJF 35(2). <https://ideas.repec.org/a/eee/intfor/v35y2019i2p756-766.html>
- Kull, Silva Filho, Flach (2017). Beta calibration. AISTATS. <https://proceedings.mlr.press/v54/kull17a.html>
- Le (2026). Decomposing Crowd Wisdom: Domain-Specific Calibration Dynamics in Prediction Markets. arXiv:2602.19520. <https://arxiv.org/html/2602.19520v2>
- Lichtendahl, Grushka-Cockayne, Jose, Winkler (2018). Bayesian Ensembles of Binary-Event Forecasts. arXiv:1705.02391. <https://arxiv.org/pdf/1705.02391>
- Lock, Nettleton (2014). Using random forests to estimate win probability before each play of an NFL game. JQAS 10(2). <https://econpapers.repec.org/RePEc:bpj:jqsprt:v:10:y:2014:i:2:p:9:n:10>
- Long (2026a). Risk-Constrained Kelly for Mutually Exclusive Outcomes. arXiv:2604.11577. <https://arxiv.org/abs/2604.11577>
- Long (2026b). Utility-Invariant Support Selection and Eventwise Decoupling. arXiv:2603.24064. <https://arxiv.org/abs/2603.24064>
- Maddox, Sides, Harvill (2022). Bayesian estimation of in-game home team win probability for FBS college football. arXiv:2207.13747. <https://arxiv.org/abs/2207.13747>
- Meister (2024). Application of the Kelly Criterion to Prediction Markets. arXiv:2412.14144. <https://arxiv.org/abs/2412.14144>
- Metel (2017). Kelly betting on horse races with uncertainty in probability estimates. arXiv:1701.02814. <https://arxiv.org/abs/1701.02814>
- Moshrefi (2026). Prices, Probabilities, and Parlays: Systematic Bias in Sports Prediction Markets. arXiv:2607.14430. <https://arxiv.org/html/2607.14430>
- Oliver et al. (2024). Retirements of professional tennis players in ATP and WTA tour events. EJSS 24(10). <https://pmc.ncbi.nlm.nih.gov/articles/PMC11451576/>
- Palau et al. (2024). Retirements in second- and third-tier tournaments on the ATP and WTA tours. PLOS ONE 19(6). <https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0304638>
- Raftery, Kárný, Ettler (2010). Online Prediction Under Model Uncertainty via Dynamic Model Averaging. Technometrics 52(1). <https://pmc.ncbi.nlm.nih.gov/articles/PMC2895940/>
- Ranjan, Gneiting (2010). Combining Probability Forecasts. JRSS-B 72(1). <https://academic.oup.com/jrsssb/article/72/1/71/7076442>
- Satopää, Pemantle, Ungar (2016). Modeling Probability Forecasts via Information Diversity. JASA 111(516). <https://arxiv.org/html/1406.2148>
- Uhrín, Šourek, Hubáček, Železný (2021). Optimal sports betting strategies in practice. arXiv:2107.08827. <https://arxiv.org/abs/2107.08827>
- Walsh, Joshi (2024). Machine learning for sports betting: accuracy or calibration? arXiv:2303.06021. <https://arxiv.org/html/2303.06021v4>
- Wen, Zhou, Huang (2026). Can LLMs Help Decentralized Dispute Arbitration? arXiv:2604.15674. <https://arxiv.org/abs/2604.15674>
- Winkelmann, Deutscher (2025). Do Betting Markets Sense a Goal Coming? arXiv:2505.21275. <https://arxiv.org/abs/2505.21275>
- Winkelmann, Ötting, Deutscher, Makarewicz (2024). Are Betting Markets Inefficient? J. Sports Economics 25(1). <https://ideas.repec.org/a/sae/jospec/v25y2024i1p54-97.html>
- Xie, Muppidi (2026). Forecasting the Winner of a Live Tennis Match. arXiv:2609.07617. <https://arxiv.org/html/2609.07617>
- Yeh, Rice, Dubin (2020). Evaluating real-time probabilistic forecasts with application to NBA outcome prediction. arXiv:2010.00781. <https://arxiv.org/abs/2010.00781>
- Yurko, Ventura, Horowitz (2018). nflWAR. arXiv:1802.00998. <https://arxiv.org/abs/1802.00998>

Data, vendor and practitioner sources (grade C unless noted):

- sportsdataverse (2026). cfbfastR-data. <https://github.com/sportsdataverse/cfbfastR-data>
- Sportradar (2026). Latency Indicator (Beta). <https://docs.sportradar.com/live-data/latency-indicator-beta>
- Polymarket (2026). Predictions changelog (fee facts, grade A). <https://docs.polymarket.com/changelog/predictions>
- botforkalshi.com (2026). Kalshi API tutorial (rate tiers verified against docs.kalshi.com). <https://www.botforkalshi.com/blog/kalshi-api-tutorial>
- InGame (2025). Kalshi co-founder says in-house trading arm "not profitable". <https://www.ingame.com/kalshi-in-house-trading-arm-not-profitable/>
- Polymarket News (2025). Automated Market Making on Polymarket. <https://news.polymarket.com/p/automated-market-making-on-polymarket>
- Lee (2025). I cloned a Polymarket market-making bot and ran it. <https://tezlee.substack.com/p/i-cloned-a-polymarket-market-making>
- mmoore07129 (2026). mlb-kalshi-bot. <https://github.com/mmoore07129/mlb-kalshi-bot>
- lucavernhes-personal (2026). kalshi-sports-bot. <https://github.com/lucavernhes-personal/kalshi-sports-bot>
- ImMike (2025). polymarket-arbitrage. <https://github.com/ImMike/polymarket-arbitrage>
- milesChild (2024). kalshi-oms. <https://github.com/milesChild/kalshi-oms>

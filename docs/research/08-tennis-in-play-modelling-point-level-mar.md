# 08 — Tennis in-play modelling: point-level Markov, serve estimation, retirements, market efficiency

**Summary.** The literature gives the engine a closed-form point-level backbone (a Markov chain over game, tiebreak, set and match driven by two serve-point probabilities p_a, p_b) whose only measured in-play improvement is shrinking those probabilities toward the serve rates observed so far in the match (Xie–Muppidi 2026: log-loss 0.514 / 0.427 / 0.284 at 25 / 50 / 75 % of the match vs 0.521 / 0.455 / 0.310 for a fixed-ability Elo Markov; Kovalchik–Reid 2019: 28 % lower serve-prediction error, +4 pp match accuracy). No fetched paper compares a model with an exchange price, and the one market study (Easton–Uylangco 2010) finds in-play odds "extremely highly correlated" with the Markov model and anticipating breaks up to four points early, so nothing published says a tennis fair should beat Kalshi's in-play mid the way the repo's NFL model does (0.406 vs 0.438–0.443, docs/MODEL.md). What is well measured and usable now is the retirement hazard by tour, tier, surface and round (ATP 2.11 %, WTA 1.73 %, Challenger 2.94 %, ITF men 3.44 %), a *different* event from the pre-first-ball walkover share the repo derives from Kalshi (3.25 % tour / 1.98 % challenger).

## Verified papers

### Kovalchik & Reid (2019) — A calibration method with dynamic updates for within-match forecasting of wins in tennis
*International Journal of Forecasting* 35(2):756–766. https://ideas.repec.org/a/eee/intfor/v35y2019i2p756-766.html

* **Method.** Calibrate (p_a, p_b) pre-match from a rating-based match probability, then update in play by empirical Bayes: the serve-win probability is the prior shrunk toward the observed serve rate with a weight that grows with serve points seen; the update strength is fitted on historical matches. Abstract only, so the exact weights are not quoted (Xie–Muppidi below is the explicit form).
* **Data.** Large professional sample for fitting; applied to the 2017 season, two Australian Open men's matches illustrated.
* **Headline.** 28 % lower in-match serve-prediction error and +4 pp match accuracy over a constant-ability model.
* **Limits.** No log-loss by stage, no calibration, no market comparison; the gain is against fixed (p_a, p_b), not a price.

### Xie & Muppidi (2026) — Forecasting the Winner of a Live Tennis Match
arXiv:2609.07617 (v1, 7 Sep 2026). https://arxiv.org/html/2609.07617

* **Method.** Backbone `V(s) = q(s) V(T1(s)) + (1 - q(s)) V(T2(s))`, `q(s) = p_1` when player 1 serves, else `1 - p_2`; deuce `D(q) = q^2 / (q^2 + (1-q)^2)`. Elo-asymmetric inputs `e = clip(alpha (Elo_1 - Elo_2), -0.15, 0.15)`, `p_a = clip(beta + e, 0.45, 0.88)`, `p_b = clip(beta - e, 0.45, 0.88)`, `(beta, alpha) = (0.64, 1.3e-4)` at 25 % progress, `(0.59, 1.3e-4)` later. Serve-shrink `theta_hat = n/(n+kappa) theta_live + kappa/(n+kappa) theta_prior`, `kappa = 640 / 160 / 40` at 25 / 50 / 75 %. Plus a histogram gradient-boosting model on score features and "Trace" (HGBM over `[p_M, p_S, logit p_M, logit p_S, p_S - p_M, z(s)]`). Elo `K = 250 / (m + 5)^0.4`.
* **Data.** Sackmann Grand Slam point-by-point: 8,222 matches (4,181 ATP, 4,041 WTA), 1,505,355 point states; chronological split, train 2011–2021 (6,774), validation 2022 (473), test 2023–2024 (975 matches, 179,832 points).
* **Headline.** Log-loss at 25 / 50 / 75 %: symmetric Markov 0.629 / 0.536 / 0.350; Elo Markov 0.521 / 0.455 / 0.310; serve-shrink 0.514 / 0.427 / 0.284; HGBM 0.538 / 0.379 / 0.230; Trace 0.475 / 0.353 / 0.200. Accuracy 73.2 / 77.6 / 77.7 / 74.0 / 77.8 %. WTA swings more (holds 66 % vs 79 % ATP).
* **Limits.** Slams only, no surface Elo, no fatigue / injury, serve-side features only (authors). Not peer reviewed; kappa bucketed by progress, not by points seen; no interval on 975 matches; no market or latency context. Shrink beats Elo Markov by only 0.007–0.028; the learned model is far ahead of every Markov variant.

### Easton & Uylangco (2010) — Forecasting outcomes in tennis matches using within-match betting markets
*International Journal of Forecasting* 26(3):564–575. https://ideas.repec.org/a/eee/intfor/v26yi3p564-575.html

* **Method.** Point-by-point comparison of the Klaassen–Magnus match probability with in-play odds, men and women, with an event analysis around breaks and holds.
* **Data.** Professional matches with point-level in-play odds (abstract only; counts not visible).
* **Headline.** Model and market extremely highly correlated; the market anticipates a break or hold up to four points early; the one lag: players lose more points than expected after conceding a break and the odds do not absorb it instantly.
* **Limits.** 2010 bookmaker market; the post-break lag's size in cents is not quoted.

### Oliver, Baiget, Cortés, Martínez, Crespo & Casals (2024) — Retirements of professional tennis players in ATP and WTA tour events
*European Journal of Sport Science* 24(10). https://pmc.ncbi.nlm.nih.gov/articles/PMC11451576/

* **Method.** Retrospective cohort; retirement share and `IR = retirements / games played` with 95 % CIs by category, surface, round, year.
* **Data.** ATP 167,211 matches (1973–2019, 3,539 retirements); WTA 46,268 (1975–2019, 801).
* **Headline.** ATP 2.11 %, WTA 1.73 %. ATP Slams 2.69 %, Masters 2.35 %, 250/500 1.94 %; WTA Premier 3.51 %, Slams 1.01 %. ATP IR per 1,000 games: hard 0.96, clay 0.92, grass 0.67. Round: qualifying 2.58 %, final 1.79 %. Marked rise 2006–2014.
* **Limits.** No minutes-played exposure, thin WTA data (authors); ends 2019; retirements only, walkovers not split; best-of-5 inflates the Slam share.

### Palau, Baiget, Cortés, Martínez, Crespo & Casals (2024) — Retirements in second- and third-tier tournaments on the ATP and WTA tours
*PLOS ONE* 19(6): e0304638. https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0304638

* **Method / data.** Same design for the repo's `tier:challenger` / `tier:itf` circuits: Challenger + ITF men 584,806 matches (1978–2019); WTA 125 + ITF women 267,380 (1994–2018).
* **Headline.** Challenger 2.94 %, ITF men 3.44 %, WTA 125 2.94 %, ITF women 2.73 %. IR per 1,000 games men 1.56 (1.54–1.59), women 1.36 (1.33–1.39); hard 1.59 vs grass 0.79. No round effect for men.
* **Limits.** Missing minutes, sparse covariates (authors); retirements only — the repo's 1.98 % challenger walkover share is a different event.

## What this means for arb_engine

* **Pre-match stays market-led, and that is already the code.** `quant/inplay_fair.blended_fair()` uses the market alone when `live=False` (`PREGAME_ORDER`), so no `SPORT_WEIGHTS["tennis"]` entry is needed. The claim check's Kovalchik 2016 Table 4 (bookmaker log-loss 0.55 vs best rating model 0.59–0.60, 2,395 ATP 2014 matches) was not fetched here; it is cited only as the reason not to build a ratings feed.
* **The NFL result does not transfer.** MODEL.md: WP model 0.406 vs Kalshi 0.438–0.443 (paired 90 % game-cluster interval [−0.055, −0.018]); college 0.216 vs 0.289–0.291 (interval includes zero); on ≤ 4¢ NFL books the market is *worse* (0.488 vs 0.448) — "thin and slow rather than merely wide". Tennis odds already track the Markov model and lead scoring events, and the best Markov gain on record is 0.007–0.028 with no market comparison. A tennis Markov fair is a sanity check and hazard carrier, not a STEAL source.
* **STEAL and LOCK have not earned tennis.** MODEL.md: every NFL hold-to-settlement STEAL interval includes zero (executable pairing −10.3 % / +2.7 % / +50.6 % at 3 / 5 / 8 %; the shift placebo +2.6 % / +39.8 % / +55.6 %) and every break-even LOCK loses (−4.7 % / −6.3 % / −13.0 % / −9.6 %). Extending an undemonstrated rule to the fastest-moving book is the wrong order.
* **Magnification.** The claim check reproduced with an independent stdlib Markov: at `p_a + p_b = 1.28`, `p_a - p_b = 0 / 0.01 / 0.02 / 0.05` gives Bo3 0.500 / 0.550 / 0.599 / 0.734 (Bo5 0.500 / 0.562 / 0.622 / 0.782), about 5 pp per 0.01 (recomputed 2026-09-20 with a second independent stdlib chain: symmetric players give exactly 0.500). The watcher's 3-cent `steal_edge` would need `p_a - p_b` to ~0.006, tighter than any estimator here. So invert: solve `p_a - p_b` from the market price given a per-tour `p_a + p_b`, then propagate through the score with serve-shrink.
* **Retirement hazard is an additional prior, not a replacement.** `matching/settlement_rules.tennis_pair_flags()` spends `p_walkover` (3.25 % / 1.98 %, docs/SPORTS.md) on the `walkover-exposed` gate, a pre-first-ball event. Retirements settle to the advancer on both venues, so the literature rates belong in a separate block feeding the STEAL hold EV in `strategy/inplay.evaluate_inplay()`, leaving the gate bit-identical.
* **No tennis state feed exists.** `venues/espn.py` has no tennis path and `tests/fixtures/ticks/` holds only synthetic football ticks; every in-play item below is blocked on point-level state and a recorded day.

## Recommendations

| # | change | module | metric | offline test (committed data) | grade | effort |
|---|---|---|---|---|---|---|
| 1 | Stdlib Markov: game, tiebreak (1-2-2 rotation), set (tiebreak at 6-6), match Bo3/Bo5, `advantage_final_set` flag | new `models/tennis_markov.py` | none; correctness | goldens `g(0.64) = 0.8126`; at `p_a + p_b = 1.28`, `p_a − p_b` = 0 / 0.01 / 0.02 / 0.05 → Bo3 0.500 / 0.550 / 0.599 / 0.734, Bo5 0.500 / 0.562 / 0.622 / 0.782 (A serves first; tiebreak at 6-6); Sampras–Becker 0.598 advantage / 0.596 tiebreak final set | A | S |
| 2 | `invert_match_probability(p_market, p_sum, best_of)` → `(p_a, p_b)` | `models/tennis_markov.py`; signal-only source in `quant/inplay_fair.py` | `InplayView.disagreement` only | round trip `market → (p_a, p_b) → market` within 1e-6; monotone | B | S |
| 3 | Serve-shrink `theta_hat = n/(n+kappa) theta_live + kappa/(n+kappa) theta_prior`, kappa 640 / 160 / 40 as a setting | `models/tennis_markov.py`, `strategy/inplay.py` state builder | tennis log-loss by stage, once a feed exists | unit test of the weights only; no tennis point rows are committed | B | M |
| 4 | `tennis_retirement` rows (ATP 2.11 %, WTA 1.73 %, Challenger 2.94 %, ITF men 3.44 %, WTA 125 2.94 %, ITF women 2.73 %; hard / grass IR 0.96 / 0.67 tour, 1.59 / 0.79 Challenger), `status: literature` | `data/settlement_rules.json`, `matching/settlement_rules.py` | STEAL hold EV in `evaluate_inplay()` | `tests/test_settlement_rules.py`: rows load; `walkover_probability()` / `tennis_pair_flags()` unchanged | A rates / C use | S |
| 5 | Gate in-play tennis STEAL and LOCK NOW to GATED (`tennis-lag-unmeasured`) until a lag distribution exists | `strategy/inplay.py` (`FeedFreshness.event_reasons`) | zero in-play tennis takers | `tickreplay` on `tests/fixtures/ticks/synthetic_*.json` with `sport="tennis"`: all STEAL rows GATED, football bit-identical | B | S |
| 6 | Record one tennis day and run `event-study` around breaks / holds on the +10 s … +15 min ladder | `store.py`, `quant/eventstudy.py` (game-boundary events) | absorption at +10 s / +60 s; two-sided-book share of in-play minutes | none until recorded; ladder covered by synthetic tick tests | C | M |

Dropped on evidence: the maker path (no recordable P&L, KM 2001 effect sizes unread, and MODEL.md's LOCK replays show post-move hedging hands the edge back); Barnett–Clarke serve/return combining (abstract only, no stats feed, dominated by Rec 2); and the "3.56 % in a 5-second courtsider window" figures, which appear on no fetchable page.

## Open questions and what settles them

1. Does ESPN's tennis competition object expose game, point and server live? Without it Recs 3 and 5 run at set/game granularity at best. No ROADMAP item covers this; it is a by-hand fetch during a live match.
2. How fast is Kalshi's in-play tennis price against a public feed, and how many in-play minutes on `KXATPMATCH` / `KXWTAMATCH` have a two-sided book at all? Settled by a tennis analogue of **"Record a live Sunday slate"** (`live --record`, then `backtest-ticks`, `stats --convergence`, `event-study`).
3. Which `p_a + p_b` per tour and surface in 2026 (KM 1.255 men / 1.097 women; Xie–Muppidi beta 0.64 → 0.59; hold rates imply ~0.64–0.66 ATP, 0.56–0.58 WTA)? Needs a Sackmann re-estimate before Rec 2 is trusted; no ROADMAP item.
4. Kappa as a function of points seen rather than progress, on non-Slam tiers with no public point data — open.
5. Size in cents of the post-break lag on a 2026 exchange — the same recorded day and the event-study ladder.
6. Walkover vs retirement split in the Kalshi settled feed: retirements settle 0/1 and are invisible to `tennis_settlement_share.py`. **"Re-run the walkover shares"** (`--pages 20 --write`) widens the walkover sample; the retirement side needs a separate tally.
7. Barnett–Clarke's combining equations and KM 2001's effect sizes were not read; confirm before any doc cites them.
8. Polymarket's `tennis_completed_match` market could price the retirement hazard directly (signal-only); it has not been fetched or matched, and depends on the **"Polymarket US gateway curl"** item.

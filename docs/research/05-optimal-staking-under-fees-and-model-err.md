# 05 — Optimal staking under fees and model error

*Fractional Kelly under estimation error, drawdown-constrained growth, Kelly for fee-laden
two-outcome contracts, shrunk Kelly, slates. Written 2026-09-20 against `docs/MODEL.md`
and `docs/ROADMAP.md` (2026-09-19); every repo number is quoted from those files.*

## Summary

Tuned fractional Kelly, or its drawdown-constrained twin (for a two-outcome contract the
same bet with the fraction set by a risk spec instead of taste), is the only sizing rule
in this cluster that survives a model whose probabilities are slightly wrong. The
fraction matters far less than the probability it multiplies: a probability error costs
growth at first order, a fraction error only at second order (Meister 2024). For
arb_engine, whose `quant/sizing.kelly_fraction` is already the bounded-contract Kelly and
whose replays cannot yet reject zero STEAL P&L, that means keep `--kelly 0.25`, calibrate
the fair on the rows that trade, and shrink toward the market before sizing rather than
build a slate optimiser.

## Papers

### Uhrín, Šourek, Hubáček, Železný (2021), *Optimal sports betting strategies in practice: an experimental review*
arXiv:2107.08827; IMA J. Management Mathematics. <https://arxiv.org/abs/2107.08827>

**Method.** One protocol for every rule as a portfolio `f` over outcomes plus cash:
Kelly `max E[log(O·f)]`; max-Sharpe; fractional `f_ω = ω·f_risky + (1−ω)·cash`;
KellyDrawdown adds the Busseti–Ryu–Boyd constraint `log Σ_i p_i (o_i f_i)^(−λ) ≤ 0`,
`λ = log β / log α`; KellyRobust maximises the worst case over `|p_i − p̂_i| ≤ η p̂_i`.
Ten games sized jointly per round; hyperparameters grid-tuned on a train split, scored on
a test split over 1,000 reshuffled runs. Diagnostic `A_KL` = the model's KL advantage
over the book; growth is proportional to it.
**Data.** Horses (2,700 races, `A_KL ≈ +0.0022`), NBA (16,000 games, `−0.0146`), soccer
(32,000, `−0.013`).
**Headline.** At a KL disadvantage full Kelly and max-Sharpe ruin in 100 % of runs
(median wealth 9e-6, 2e-9); tuned fractional Kelly ends 2.4× (NBA) / 10.05× (soccer) with
0 ruin; KellyDrawdown "very similar" (2.21 / 10.25); KellyRobust safest but slowest
(1.39 / 6.2). Flat staking ruins even with an edge.
**Limitations.** Known-`p` assumptions (theirs); `ω` tuned in sample, fixed-odds book
with no leg fees, ten fixed rounds rather than an in-play stream (ours).

### Metel (2017), *Kelly betting on horse races with uncertainty in probability estimates*
arXiv:1701.02814. <https://arxiv.org/abs/1701.02814>

**Method.** `max Σ_h π_h log(x_h O_h + w − Σ x_i)` with `β ~ N(β̂, Σ)` from a
sandwich-covariance logistic fit; variants: Kelly at `E[π]` (Emc), Jensen bound (Elb),
chance constraints `P(E log W ≥ t) ≥ 1−α` (CC / ECC). Two-outcome closed form: shift the
logit by `Φ^{-1}(1−α)·σ` and bet Kelly at that pessimistic quantile of `p`.
**Data.** Simulated, 2,500 trials per experiment, scored by log growth under the true `p`.
**Headline.** Table 2: oracle 27.46; naive Kelly 18.13; half-Kelly 13.27; Emc 18.47;
ECC `α = 0.4` 18.48 (best); ECC `α = 0.1` 14.60 — mild constraints help, `α ≤ 0.25`
"overly dampens".
**Limitations.** Simulated, `α` needs calibration (theirs); the metric ignores drawdown,
which is why half-Kelly looks worst; no fees; Gaussian-logit rather than boosted-model
error (ours).

### Beuoy (2026), *Kelly Betting as Bayesian Model Evaluation*
arXiv:2602.09982. <https://arxiv.org/abs/2602.09982>

**Method.** Kelly with an existing position: maximise
`G = p log(b + b f o + w) + (1−p) log(b − f b)`, giving `f = p − ((1−p)/o)(1 + w/b)`
(eq. 1). With positions, `w'_i = (p_i/m_i) Σ_j m_j w_j` (eq. 3) is Bayes: credibility
∝ likelihood `p/m` × prior (marked-to-market bankroll); carrying bankroll across games
gives an order-aware sequential model average.
**Data.** Simulated volleyball-like contests (10,000 games per scenario, 50-game
sequences × 1,000 runs); NFL illustration on ESPN WP.
**Headline.** Iterated, the Kelly criterion picks the right model in 98 / 110 scenarios
after 5 games and loses in 2 / 110 after 50; but after a **single** game it can be less
accurate than log-loss (76.3 % vs 80.5 %).
**Limitations.** Stylised contest, arbitrary priors (theirs); no fees or spread, and it
rewards low volatility, penalising a model that correctly beats a candle-aligned market
(ours).

### Long (2026a), *Risk-Constrained Kelly for Mutually Exclusive Outcomes*
arXiv:2604.11577. <https://arxiv.org/abs/2604.11577>

**Method.** `W_i = c + x_i/q_i`, `L_i = p_i/q_i` sorted, `τ_k = (1−P_k)/(1−Q_k)`;
`max Σ p_i U_γ(W_i)` s.t. `c + Σ x_i = 1`, `Σ p_i W_i^{−λ} ≤ 1`. Kelly:
`x_i = q_i (L_i − τ*)`. With an overround (`Σ q_i > 1`, any spread or fee) cash is
positive; the active set is the Kelly prefix for every CRRA `γ` and the drawdown
constraint preserves it, shrinking stakes only.
**Data / headline.** One example (`p = (0.5, 0.3, 0.2)`, `q = (0.45, 0.35, 0.30)`,
`λ = 2`): Kelly bets outcome 1 at 0.0909, RCK the same outcome at 0.0606.
**Limitations.** Fair / subfair regimes not covered (theirs); no data, known `p` (ours).

### Long (2026b), *Utility-Invariant Support Selection and Eventwise Decoupling for Simultaneous Independent Multi-Outcome Bets*
arXiv:2603.24064. <https://arxiv.org/abs/2603.24064>

**Method.** `m` independent events, additive terminal wealth `W = c + Σ_l g_{l,X_l}`,
edge ratios `r_li = p_li/π_li`. Corollary 4: each event's active set is the single-event
greedy prefix `r_{l,k+1} ≤ (1−P_{l,k})/(1−Q_{l,k})` for any concave utility; only sizes
couple. Remark 8: caps or fractional constraints break this; Remark 9: so does dependence.
**Data / headline.** Theory only.
**Limitations.** Interior and independent only (theirs); same-game STEALs (both sides,
ML + spread) are dependent and must be netted first (ours).

### Meister (2024), *Application of the Kelly Criterion to Prediction Markets*
arXiv:2412.14144. <https://arxiv.org/abs/2412.14144>

**Method.** Contract at price `p`, belief `q`: `U = (1−q) log(1−f) + q log(1 + f(1−p)/p)`,
optimum `f = (q−p)/(1−p)` — `sizing.kelly_fraction` with `k = p`. Finite horizon:
`D(k/N‖p+ε) − D(k/N‖p) = (p−k/N)/(p(1−p))·ε + O(ε²)` (first order in the probability
error) versus `U(p, 2p−1+ε) − U(p, 2p−1) = −ε²/(4p(1−p)) + O(ε³)` (second order in the
fraction).
**Data / headline.** Analytical. Quarter Kelly keeps 0.437 and half Kelly 0.749 of full
log growth at the 3¢ Kalshi edge below (exact; `2c − c²` gives 0.4375 / 0.75).
**Limitations.** Fees and mechanics ignored (theirs); the horizon section is a symmetric
double-or-nothing game (ours).

## What this means for arb_engine

**The default fraction is a drawdown spec.** With Kalshi's taker fee `0.07·p(1−p)` a
50¢ ask is `k = 0.5175`; at a 3¢ edge `f_K = 0.0622` and `--kelly 0.25` gives 0.0155.
The two-outcome Busseti constraint used by Uhrín and Long (2026a) is a fractional Kelly
bet with `f` fixed by `(α, β)`: recomputed in the claim pass, `α = 0.7, β = 0.1` gives
`f = 0.0167` (`λ = 6.456`, 0.27× Kelly); `α = 0.8` / `0.9` give 0.0110 / 0.0055. The
0.25 default is therefore "at most a 10 % chance of losing 30 %" under IID repeats, a
claim to document and test on bootstrapped seasons, not to assume. Long (2026a) also
licenses keeping the STEAL decision in `strategy/inplay.evaluate_inplay` separate from
`sizing.kelly_stake`.

**Calibration on the selected rows beats multiplier tuning.** On a $300–800 bankroll
0.25× Kelly is 9 / 15 / 24 contracts at ~$0.52; 0.27× changes nothing on the tick grid,
while a 1-point fair error moves growth linearly (Meister). `docs/MODEL.md` reports ECE
0.020 over all 2025 plays (bin gap −0.043 in 0.8–0.9) and 0.4060 vs 0.4382 / 0.4425
log-loss on 2,263 NFL week-1 in-play scrimmage rows, but no calibration or `A_KL` on the
rows that trade: 159 / 35 / 7 scrimmage and 97 / 63 / 32 timeout rows qualify at 3 / 5 /
8 %. Rows selected for disagreement are biased upward under overconfidence, so `A_KL`
must be read against the shuffle placebo.

**The replays can order rules, not rank them.** `backtest.simulate_steal` stakes 10
contracts once per game. NFL week 1, executable pairing: ROI −10.3 / +2.7 / +50.6 % on
16 / 14 / 10 games, every interval including zero, shuffle placebo +19.6 / −48.6 /
+20.5 %. College week 2: +3.5 / +7.1 / +5.5 % on 49 / 40 / 30 games, placebo −23.2 /
−21.7 / −13.1 %; sign separates, intervals include zero. Uhrín's tables rest on
16,000–32,000 games.

**LOCK.** "Loses in every replay" is wrong: break-even is −4.7 / −6.3 / −13.0 / −9.6 %
on NFL and −0.7 / −4.3 / −3.9 % at 2–6 % on college, but at the 10 % edge college shows
+6.3 % `[+0.39, +0.72]`, +9.8 % `[+0.55, +1.20]`, +12.2 % `[+0.32, +1.59]` on 25 games.
Beuoy's eq. 1 is `sizing.hedge_kelly`; a Kelly-gated lock is a cleaner rule, but the
comparison baseline (`fair = model, hold`: +5.9 to +27.4 % NFL, −0.2 / −0.3 % college at
4 / 10 %) has every interval including zero.

**Fees on both legs.** No paper charges per-leg fees. The Noon thesis claim in the brief
("64 of 330 matches flipped after commission") is not in the thesis — 64 is a page
number; it reports one worked match (+52.27 → −51.86 after 5 % commission) and "over 20"
too small to survive. The repo already puts the fee inside each leg's `all_in`.

**Slates.** The joint 2^n solve is dropped: ten simultaneous 3¢ STEALs give 0.0601 vs
0.0622 per bet (0.0166 vs 0.0167 under RCK), ~1 % relative, below tick rounding;
`strategy/live.apply_slate_cap` rarely binds at 0.25× (10 × 1.5 % = 15 %).

## Recommendations

| # | change | module | metric | offline test | grade | effort |
|---|---|---|---|---|---|---|
| 1 | `rck_fraction(fair, all_in, alpha, beta)`: bisection on `q(1+fb)^{−λ} + (1−q)(1−f)^{−λ} = 1`; print beside the 0.25× stake; document 0.25 ≈ `α=0.7, β=0.1` | `quant/sizing.py`, `strategy/inplay.evaluate_inplay`, `cli.cmd_kelly` | P(min wealth < α) ≤ β on bootstrapped seasons | resample game clusters of the STEAL trades from the W1 / W2 week caches (`backtest --week N --offline --cache-dir out/cache/replay`, one prior network run; the committed `replay_*.json` summaries hold no trades) (B = 1000); RCK < 0.25× < full drawdown on every draw | B | S |
| 2 | `A_KL` and `reliability_band` on STEAL-qualifying rows per edge, vs the shuffle placebo | `backtest.class_gaps`, `backtest.interval_report`, `quant/calibration.reliability_band` | `A_KL` interval and ECE on selected rows | `backtest --week 1 --offline --placebo` on the cached NFL w1 / NCAAF w2; new `results:*_akl` block via `scripts/render_results.py` | A (diagnostic) / C (as edge) | S |
| 3 | Lower-quantile fair before sizing: `q_low = σ(logit q − Φ^{-1}(1−α)·s)`, `s` from the band, `α ≈ 0.4`; `source="shrunk"` in `simulate_steal` | `quant/sizing.py`, `backtest.simulate_steal` | STEAL P&L-per-game interval vs `source="blend"`; qualifying-row count | both replay fixtures, executable pairing; pass if the interval is no wider and the placebo gap no smaller | B | M |
| 4 | Kelly-gated LOCK: fire only when `hedge_kelly(...)["contracts"] > 0`, replacing fraction-of-hold-EV | `backtest.simulate_steal` (`lock_fraction`), `strategy/inplay` LOCK NOW | lock vs hold P&L per game | regenerate `nfl_w1_lock` / `ncaaf_w2_lock`; college 10 % rows stay positive, 2–6 % rows stop firing | C | S |
| 5 | Sizing tests on the venue grid (Kalshi cent rounding, Robinhood commission + exchange fee, `min_size_for_legs`); assert fees never net across lock legs | `tests/test_inplay.py`, `tests/test_fees.py`, `quant/arbitrage.max_price_for_leg` | contracts at $300 / $500 / $800 = 9 / 15 / 24 | unit fixtures only | B | S |
| 6 | Beuoy credibility weights (bankroll carried across games) as a walk-forward alternative to 0.30 / 0.55 / 0.15 | `quant/inplay_fair.blended_fair`, `backtest --pool` | pooled in-play scrimmage log-loss vs fixed blend (NFL 0.4173 vs model 0.4060; college 0.2179 vs 0.2156) | `backtest --pool out/week*.json`, only once two weeks per sport exist | C | M |
| — | Joint 2^n slate solve with RCK | `strategy/live.apply_slate_cap` | — | dropped: ~1 % change at the user's size, no co-timed multi-game ticks | C | — |

## Open questions

* Is the Busseti bound close to the Monte-Carlo drawdown when the "gamble" is ~17
  heterogeneous weekly slates, not IID repeats? Rec. 1's bootstrap answers it offline;
  a `Prob(halve)` number should not be quoted to the user before that.
* Does one prior strength `s` (rec. 3) make shrunk Kelly reproduce the blend weights the
  fit prefers on college (0.10 / 0.90 / 0.00) but not on NFL (model corner)? If so the
  unresolved weights and the sizing collapse into one parameter. Settled by the ROADMAP
  row **"In-play STEAL as a strategy"** (~70 NFL games, four weeks of replay).
* Within-game re-entry (several STEALs as the fair moves) is Beuoy's existing-win-shares
  case; `simulate_steal` allows one entry per game and needs a multi-entry mode before any
  within-game rule can be scored. Settled by **"Record a live Sunday slate"** scored
  through `backtest-ticks` / `clv`.
* Should the maker runner's resting Kalshi bids be Kelly-sized rather than depth-sized?
  Untested; the same recorded Sunday plus the **"Kalshi demo-key check"** row.
* Same-game dependence (both sides, ML + spread) needs a netting step keyed on the event
  and `tie_margin` before Long (2026b) applies; only matters once a recorded slate shows
  exposure near `slate_cap`.
* At hundreds of dollars, Kalshi cent-vs-centicent rounding and Robinhood's commission +
  exchange fee dominate the growth arithmetic on 3–15 contracts. Settled by the
  **"Kalshi cent-vs-centicent rounding"** and **"Rothera order-ticket fee preview"** rows.
* Baker & McHale (2013), Grant, Johnstone & Kwon (2008) and Whitrow (2007) were not
  fetched (INFORMS 403); no shrinkage formula from them is quoted here. Chu, Wu & Swartz
  (Kelly at the posterior mean, "a rationale for half-Kelly") and the Noon thesis were
  checked only through the claim pass and are pointers, not sources.

## Bibliography

* Uhrín, Šourek, Hubáček, Železný 2021. *Optimal sports betting strategies in practice: an experimental review.* <https://arxiv.org/abs/2107.08827>
* Metel 2017. *Kelly betting on horse races with uncertainty in probability estimates.* <https://arxiv.org/abs/1701.02814>
* Beuoy 2026. *Kelly Betting as Bayesian Model Evaluation: A Framework for Time-Updating Probabilistic Forecasts.* <https://arxiv.org/abs/2602.09982>
* Long 2026a. *Risk-Constrained Kelly for Mutually Exclusive Outcomes: CRRA Support Invariance and Logarithmic One-Dimensional Calibration.* <https://arxiv.org/abs/2604.11577>
* Long 2026b. *Utility-Invariant Support Selection and Eventwise Decoupling for Simultaneous Independent Multi-Outcome Bets.* <https://arxiv.org/abs/2603.24064>
* Meister 2024. *Application of the Kelly Criterion to Prediction Markets.* <https://arxiv.org/abs/2412.14144>

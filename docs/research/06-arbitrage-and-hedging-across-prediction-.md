# 06 · Arbitrage and hedging across prediction markets

**Summary.** The 2026 microstructure papers find executable prediction-market arbitrage rare, short-lived (median 3.6 s single-market, 16 s combinatorial) and size-capped, and the settlement discount on near-certain contracts only 3–7 % per year — orders of magnitude below the LOCK losses in `docs/MODEL.md`. The hedging literature (Divos et al.) gives the structure — hedge ratio = forward difference of value to the next scoring event — but never tests hedge-vs-hold, the question the replay answers negatively for every break-even LOCK variant (−4.7 % to −13.0 % on NFL week 1). Nothing here rescues LOCK; the actionable content is a STEAL persistence gate, depth-aware sizing and keeping settlement mismatches as hard flags.

## Verified papers

### 1. Arbitrage Analysis in Polymarket NBA Markets
Guang Cheng, Jiaxin Yang, Haoxuan Zou (2026), arXiv 2605.00864 — https://arxiv.org/html/2605.00864v1

*Method.* L1 polling of the Polymarket CLOB every 3.6–5.5 s. Single-market arb: `Ask_A + Ask_B < 1` or `Bid_A + Bid_B > 1`. Combinatorial: `Ask(ML_A) + Ask(Sp_B) < 1`, the underdog's spread cover as a synthetic short of the favourite; a margin strictly between 0 and the spread pays both ($2.00 "middle"). Rules: ≥ $10 per leg, $100 per episode, zero fees, updates within 500 ms clustered.

*Data.* 173 NBA games, 4 Feb–4 Mar 2026, 75.1 M snapshots over 3,042 markets.

*Headline.* Single-market: 7 in-game episodes, median 3.614 s, $210.19 capped profit. Combinatorial: 290 episodes, median 16 s, median yield 101.01 bps, concentrated in the final minutes; 76.9 % capped at ~14.8 shares; zero middles realised.

*Limitations.* Polling gives a lower bound; one month, one sport, one venue, L1 only; fee-free NBA does not transfer to Kalshi's `0.07·p(1−p)` taker fee; NFL is play-discrete.

### 2. When Certainty Is Not Worth It: Capital Lock-Up and Settlement Discounting in Prediction Markets
Jonas Gebele, Florian Matthes (2026), arXiv 2605.31431 — https://arxiv.org/html/2605.31431

*Method.* `P_{i,t} = E_t[X_i] · D(τ)`, `D(τ) = exp(−r_PM(τ)·τ)`. From a frontier of near-certain contracts (mid ≥ 0.90 for seven daily snapshots, no reversal, ≥ 14 d, volume ≥ 100) recover `r_q(τ) = −(1/τ)·log P_q(τ)`, annualise `ASW_q = exp(365·r_q) − 1`, de-discount `P̃ = min{1, P / D̂_q(τ)}`. NegRisk baskets: `P̄_N ≈ 1 − (1 − D(τ))/(n−1)`.

*Data.* Polymarket hourly quotes to 31 Dec 2025: 141,848 events, 323,342 markets; near-certainty sample 4,483 events; Kalshi as comparison frontier.

*Headline.* ASW mean 3.06 %/yr, 0.1-pct 4.36 %, 0.5-pct 6.89 %; horizon coefficient on the price wedge (β = 0.00016780) shrinks 48–88 % after adjustment; calibration-gap MSE beyond 180 d improves 20.02 %. Kalshi's frontier is closer to par and flatter (yield-bearing collateral).

*Limitations.* Residual outcome uncertainty biases rates upward; stale mids in thin long-dated markets. At the engine's horizons a 3–7 %/yr wedge is 0.002–0.05 %: irrelevant to LOCK, relevant only to multi-week pre-game locks and maker collateral.

### 3. Risk-Neutral Pricing and Hedging of In-Play Football Bets
Peter Divos, Sebastian del Bano Rollin, Zsolt Bihari, Tomaso Aste (2018), arXiv 1811.03931 — https://arxiv.org/pdf/1811.03931

*Method.* Goals are independent Poisson processes with intensities λ1, λ2; a bet pays `X_T(N^1_T, N^2_T)`, `X_t = E^Q[X_T | F_t]`; two independent traded bets complete the market. Replication weights are forward differences `φ^i_t = X(t, ..., N^i_t + 1, ...) − X(t, N^1_t, N^2_t)` (Prop. 3.22); `∂X/∂λ_i = (T − t)·δ_i X`. Hedging `X` with `Z1, Z2` solves `[[δ1 Z1, δ1 Z2],[δ2 Z1, δ2 Z2]]·[ψ1, ψ2]ᵀ = [δ1 X, δ2 X]ᵀ`. Next Goal bets are the natural hedges because their delta never degenerates.

*Data.* Euro 2012, 10 matches, 1-minute bid/ask, 31 bet types.

*Headline.* Calibration error 1.57 ± 0.27 spreads; implied intensity drifts, `d ln(λ1+λ2) = μ dt + σ dW`, μ = 0.55 ± 0.16, σ = 0.51 ± 0.19 per 90 min; jump correlation between contract and replicating portfolio 80 % ± 19 % (47–99 % by game).

*Limitations.* Constant intensity violated; no fees or spreads in hedge P&L; 10 games; football. The transferable content is structural (hedge with the largest non-degenerate delta, expect ~20 % residual per jump); it never asks whether hedging beats holding.

### 4. Can LLMs Help Decentralized Dispute Arbitration? A Case Study of UMA-Resolved Markets on Polymarket
Junhao Wen, Juncen Zhou, Junjie Huang (2026), arXiv 2604.15674 (abstract only) — https://arxiv.org/abs/2604.15674

*Method.* Ex-ante classifier of which Polymarket events reach a UMA dispute; web-enabled LLM re-resolution compared with UMA's final vote.

*Data.* Disputed events totalling ~$972 M of volume.

*Headline.* Disputes are not reliably predictable in advance; once disputed, LLMs agree with UMA's final resolution 89.58 % of the time.

*Limitations.* Abstract only; sports share and time-to-resolution not extracted. Polymarket is signal-only for the engine's US user; the transferable point is that rule mismatches should stay hard flags, not priced probabilities.

### 5. Do Betting Markets Sense a Goal Coming? Evidence from the German Bundesliga
David Winkelmann, Christian Deutscher (2025), arXiv 2505.21275 — https://arxiv.org/html/2505.21275v1

*Method.* 1 Hz bookmaker odds and stakes aggregated to minutes; linear and zero-one-inflated-beta models of implied probability and relative stakes on `mintogoal^{-1}`, controlling for pre-match probability, time, xG and red cards.

*Data.* 2018/19 Bundesliga, 256 matches, 9,245 scoreless minute-observations, one bookmaker.

*Headline.* No anticipation: β = −0.005 (95 % CI [−0.012, 0.002]) for the bookmaker, β = 0.096 ([−0.031, 0.222]) for bettors.

*Limitations.* Bookmaker, minute resolution, football. Supports the premise that `feed-stale` is about post-event absorption, not pre-event leakage.

## What this means for arb_engine

**LOCK.** `docs/MODEL.md` reports that every break-even lock loses on NFL week 1 (−4.7 % / −6.3 % / −13.0 % / −9.6 % at 2 / 4 / 6 / 10 % edges) and on college week 2 at 2–6 % edges (−0.7 % to −4.3 %), because "a lock fires mostly after the position has gone against the entry and hands the edge back." Gebele–Matthes rules out the settlement wedge as the cause (0.05 × 3/8760 ≈ 0.002 % over a game); Divos et al. stops at replication quality (80 % ± 19 % jump correlation), never hedge-vs-hold P&L. The remaining lever is partial hedging: `quant/sizing.hedge_kelly` returns `n* = [(1−p)(1−h)X − p·h·Y] / (h(1−h))`, but `backtest.simulate_steal` only knows a full-size lock gated by `lock_fraction` of hold EV. A Kelly-sized partial-hedge variant in `simulate_pairings` is the one LOCK experiment the replay can run today; its evidence is the repo, not the literature.

**STEAL persistence.** Cheng et al.'s lifetimes (3.6 s single-market, 16 s combinatorial, 76.9 % of episodes at ~14.8 shares) are Polymarket, fee-free, L1. The engine's STEAL rows are model-vs-market disagreements concentrated in dead-ball classes (timeout 97 / 63 / 32, kickoff-pending 29 / 13 / 5 qualifying rows at 3 / 5 / 8 % on NFL week 1), already targeted by the P06 gates. A two-consecutive-poll requirement in `strategy/inplay.evaluate_inplay` (10 s cadence) is a hypothesis about Kalshi quote lifetimes the repo has never measured — the 1-minute replay candles see no sub-60 s episode.

**Depth.** The Dubach claim check (L1 holds a median 0.136 of top-10 depth on Polymarket) says ranking on `evaluate(legs, 100)` misstates fillable size; `walk_book` and `size_from_books` in `quant/arbitrage.py` exist but do not drive the ranking. Every P&L table in `docs/MODEL.md` is already caveated "candle-close asks, no depth".

**Combinatorial legs.** 290 of Cheng et al.'s 297 executable episodes were moneyline × spread. The engine prices spreads (`quant/lines.py`: in-play 0.616 vs Kalshi 0.622–0.628, tuned in-sample) but `scanner.analyze_event` pairs only same-market outcomes.

**Settlement.** Wen et al.'s unpredictability finding backs the current design: `matching/settlement_rules.pair_flags` emits hard flags; `tie_margin` in `quant/arbitrage.py` still rests on an unverified Rothera tie rule.

## Recommendations

| # | change | module | metric it should move | offline test | grade | effort |
|---|---|---|---|---|---|---|
| 1 | Kelly-sized partial-hedge variant (`hedge_kelly`) beside the full lock in `simulate_steal` / `simulate_pairings` | `backtest.py`, `quant/sizing.py` | LOCK P&L per game, 90 % game-cluster interval vs hold | `backtest --week 1 --offline` and `--sport ncaaf --week 2` on the week caches (`out/cache/replay`, one prior network run), checked against the metrics-only `tests/fixtures/results/replay_*.json` | C | M |
| 2 | Two-consecutive-poll persistence gate for STEAL (`persist` reason in `FeedFreshness`) | `strategy/inplay.py`, `tickreplay.py` | STEAL count, per-contract P&L and CLV with gates on/off | `backtest-ticks --gates both` on `tests/fixtures/ticks/synthetic_40.json`; real test needs a recorded slate | C | S |
| 3 | Rank and size arbs on `size_from_books` (walked depth) instead of `evaluate(legs, 100)` when `--books` is on | `scanner.py`, `quant/arbitrage.py` | fillable size, next-lot margin, share of L1 arbs that survive the walk | `tests/fixtures/kalshi_orderbook.json`, `polymarket_book.json`, 54 `arb_vectors.json` (JS parity) | B | S |
| 4 | Candidate moneyline × spread-cover legs with middle EV from `line_fair_for_event` (behind `LINE_FAIR=1`) | `scanner.py`, `quant/lines.py` | executable combinatorial pairs per scan; margin after Kalshi taker fee | fixture scan on `kalshi_markets_nfl_lines.json` + `robinhood_page_props_nfl.json`; expect near zero after the 7 % fee | B | M |
| 5 | Keep settlement-rule mismatches as hard flags; do not add a priced dispute/tie probability | `matching/settlement_rules.py` | none (confirms design) | `settlement_rules.verify()` on `tests/fixtures/rules/` | B | S |

## Open questions

1. Kalshi in-play quote lifetimes, depth and cross-venue lead–lag (Kalshi vs Rothera vs Polymarket) are unmeasured; every lifetime number here is Polymarket, fee-free, 0.001-tick. Settled by **Record a live Sunday slate** (`live --record`, then `backtest-ticks`, `clv`, `event-study`).
2. Whether a partial Kelly hedge beats holding at asymmetric fees on the two legs — no literature found; `hedge_kelly` is the repo's own closed form. Settled by rec 1 on the week caches of a prior network run, then the recorded slate.
3. The tie-margin sign for Kalshi ($0.50) vs Rothera ($0 / $1) pairs; Cheng et al. saw zero middles in 173 NBA games. Settled by **Paste the Rothera tie clause** (rule 40.2(d)).
4. The 2–4 % cross-venue band (Gebele–Matthes 2601.01706, non-sports) and the maker premium on favourites (Bürgi–Deng–Whelan, pre-April-2025) were claim-checked but not carried here; neither is measured on in-play sports, so the basis-z default and any maker pickoff haircut wait for recorded ticks. Settled by the recorded slate plus **Kalshi demo-key check**.
5. Whether the Rothera per-order fee changes the second leg's cost enough to flip any lock. Settled by **Rothera order-ticket fee preview**.

## Bibliography

- Cheng, Yang, Zou (2026). Arbitrage Analysis in Polymarket NBA Markets. https://arxiv.org/html/2605.00864v1
- Gebele, Matthes (2026). When Certainty Is Not Worth It: Capital Lock-Up and Settlement Discounting in Prediction Markets. https://arxiv.org/html/2605.31431
- Divos, del Bano Rollin, Bihari, Aste (2018). Risk-Neutral Pricing and Hedging of In-Play Football Bets. https://arxiv.org/pdf/1811.03931
- Wen, Zhou, Huang (2026). Can LLMs Help Decentralized Dispute Arbitration? A Case Study of UMA-Resolved Markets on Polymarket. https://arxiv.org/abs/2604.15674
- Winkelmann, Deutscher (2025). Do Betting Markets Sense a Goal Coming? Evidence from the German Bundesliga. https://arxiv.org/html/2505.21275v1

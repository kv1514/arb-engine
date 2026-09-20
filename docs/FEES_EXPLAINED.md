# Fees, plainly — and why Robinhood and Kalshi show the same odds

**Robinhood is a broker, not an exchange.** Every event contract you see there is listed on
one of four exchanges: **KalshiEX** (contract symbols starting `KX…`), **Rothera** (the
Robinhood/Susquehanna exchange; NFL game winners, spreads and totals — symbols like
`NFLGAME-…`), ForecastEX, or Nadex. When you buy a `KX…` contract on Robinhood you are
trading *in Kalshi's own order book* through Robinhood's pipe — same bids, same asks, same
counterparties. That is why Robinhood tennis and NFL props show exactly Kalshi's odds: they
are not copying each other, it is literally one book. Rothera contracts are a separate book
whose market maker (Susquehanna) prices off the same public information, so its odds track
Kalshi/Polymarket closely but not exactly — those small gaps are where cross-venue arbs live.

## What a trade actually costs (100 contracts, $1 payout each)

| you pay | at 50¢ | at 20¢ / 80¢ | at 5¢ / 95¢ |
|---|---|---|---|
| Kalshi direct, taker (hit the ask) — `ceil(0.07 × C × p × (1−p))` | $1.75 | $1.12 | $0.34 |
| Kalshi direct, maker (rest an order, game markets) — `0.0175 × …` | $0.44 | $0.28 | $0.09 |
| Robinhood, no Gold — commission `min(ceil(0.10 × p(1−p) × C), 1¢×C)` **+ 1¢/contract exchange fee** | $1.00 + $1.00 | $1.00 + $1.00 | $0.48 + $1.00 |
| Robinhood, Gold — commission constant 0.05 | $1.00 + $1.00 | $0.80 + $1.00 | $0.24 + $1.00 |
| Polymarket sports, taker — `C × 0.05 × p(1−p)` | $1.25 | $0.80 | $0.24 |
| Polymarket, maker | $0 | $0 | $0 |

The Robinhood rows charge the **$0.01/contract ceiling** Robinhood's article names ("up to
$0.01 per contract, varies by exchange"). The exchanges' own schedules say less than that, and
the engine carries them as opt-in models until an order ticket shows which number Robinhood
passes on (`docs/ROADMAP.md`, "Needs you"):

| 100 contracts, no Gold, total Robinhood fee | at 50¢ | at 20¢ / 80¢ | at 5¢ / 95¢ | switch |
|---|---|---|---|---|
| Rothera, `flat_001` (**default**): commission + $1.00 | $2.00 | $2.00 | $1.48 | — |
| Rothera, `quadratic`: commission + `max(round_half_up(0.02 × p(1−p) × C), $0.01)` **per order** | $1.50 | $1.32 | $0.58 | `ROBINHOOD_ROTHERA_FEE_MODEL=quadratic` |
| CDNA (college), `flat_001` (**default**) | $2.00 | $2.00 | $1.48 | — |
| CDNA, `flat_002` (top of the published range) | $3.00 | $3.00 | $2.48 | `CDNA_FEE_MODEL=flat_002` |
| CDNA, `weighted_007` (0.07 × p(1−p) × C, rounded up) | $2.75 | $2.12 | $0.82 | `CDNA_FEE_MODEL=weighted_007` |

The Rothera per-order fee is size-dependent: a 1-lot pays the full cent, a 1,000-lot at 50¢
pays $5.00, and at the tails it is ~$0.0006 per contract — which is exactly where the
scanner's arbs live. On the committed NFL fixture scan the flip moves every Rothera row,
improves 5 of 14 margins (the best one by 0.07 percentage points) and flips no sign, so no arb appears or
disappears from it (the `fee_flip_p10` table below).

Three consequences:

1. **Robinhood is never the cheapest place to trade a `KX…` contract.** Same book as Kalshi,
   +$1 per 100 contracts on top. Use Robinhood for Rothera contracts (NFL games, where its
   maker may quote better) and for convenience; use Kalshi directly for anything `KX…`.
2. **Fees are charged on the way in and on the way out**, but a contract held to
   settlement pays no exit fee anywhere. "Buy the dip, then buy the other side and hold to
   settlement" therefore pays two entry fees and nothing else.
3. **Fees are tiny at the tails and largest at 50¢.** A 1¢ price gap at 50/50 is worth less
   than the round trip; the same 1¢ gap at 5¢ is a real edge. That is why every fee-adjusted
   arb the scanner has found so far sits on far-tail spreads and totals.

## Your Chiefs/Broncos scenario, with real fees

Hold 100 Broncos bought at 50¢ on Robinhood (Rothera): cost $50 + $1.00 commission + $1.00
exchange fee = **$52.00**. The game swings, Chiefs trade 60¢ and Broncos 40¢.

* Buying Chiefs at 40¢: $40 + $0.96 + $1.00 = $41.96 → total **$93.96** for a position that
  pays **$100** whichever team wins → **+$6.04 locked**, nothing left to predict.
* The engine's number for that: `LOCK NOW: buy 100 x Kansas City at ≤ 0.46` — 46¢ is the
  highest Chiefs price that still guarantees a profit after both sets of fees. Below it,
  buy; above it, wait (or average down on Broncos, which lowers the bar).
* Buying *more Broncos* at 40¢ instead is not arbitrage — it is a bigger directional bet at a
  better average (45¢). It only becomes risk-free once the Chiefs side is bought under the
  lock price.

`python -m arb_engine inplay <robinhood game url> --position robinhood:DEN:0.50:100` watches
exactly this and alerts on LOCK NOW / STEAL. See `docs/VENUES.md` for sources and dates of
every fee number above.

## Which venue can be a leg at all

Global Polymarket is not executable for a US-resident account (`arb_engine/data/venue_rules.json`;
`docs/VENUES.md` "Eligibility"). Its price still counts — it is often the best-informed number
on an NFL line — so it stays in the fair value as a **signal**, but the engine never makes it a
leg of an arb, a hedge for the maker, or the "best" venue of a side, unless `EXECUTABLE_VENUES`
(or the popup's "I can trade on Polymarket") says your account can. On the committed fixture
scans that rule removed the only arb (its cheap leg was Polymarket) and re-routed most of the
maker's hedges to Robinhood:

<!-- results:eligibility_p11 -->
| fixture scan | snapshots | arbs before | with a non-executable leg | arbs after | maker watches before | hedge not executable | share | hedge venues before | hedge venues after |
|---|---|---|---|---|---|---|---|---|---|
| ncaaf | 53 | 1 | 1 | 0 | 10 | 7 | 70.0% | polymarket 7, robinhood 3 | robinhood 10 |
| nfl | 1 | 0 | 0 | 0 | 8 | 5 | 62.5% | polymarket 5, robinhood 3 | robinhood 8 |
<!-- /results:eligibility_p11 -->

<!-- results:eligibility_p11_summary -->
| summary (executable venues: kalshi, robinhood) | value |
|---|---|
| arbs before → after | 1 → 0 |
| arbs before with a non-executable leg | 1 |
| share of arbs that relied on a non-executable leg | 100.0% |
| arbs surviving | 0.0% |
<!-- /results:eligibility_p11_summary -->

## Ties, and why the NO contract can be the better leg

A Kalshi NFL moneyline pays $0.50 a side on a tie. Rothera's public blurb says nothing; the
engine's reading of its certified terms ("strictly greater" — a tied game has no winner) is
**unverified** and is carried as such: a Rothera YES pays $0 on a tie and a Rothera **NO** on
the other team pays $1. So "Kalshi YES on A + Rothera YES on B" locks $1 on a win either way but
only $0.50 on a tie (`tie_margin < 0`), while "Kalshi YES on A + Rothera NO on A" pays $1 on
a tie too. The scanner reports `tie_margin` next to `margin`, prefers the tie-paying leg when
all-ins are equal, emits the Rothera NO contract as its own leg (`<id>#no`), and flags
`tie-rule-mismatch` when the chosen legs settle a tie differently. On the committed fixtures:

<!-- results:arb_fixture_p09 -->
| metric | NCAAF fixture | NFL fixture | definition |
|---|---|---|---|
| arbs | 0 | 0 | pairs with margin > 0 at 100 contracts |
| arbs negative tie margin | 0 | 0 | arbs that lose money if the game ties |
| below min size flags | 0 | 0 | reports flagged below-min-size (a leg's venue minimum exceeds what is on offer) |
| cross book pairs | 20 | 10 | two-leg combinations across different order books for a two-outcome moneyline event (scanner.cross_book_pairs) |
| kalshi x rothera negative tie margin | 0 | 2 | of those, pairs whose tie_margin < 0 (Kalshi YES + Rothera YES: $0.50 on a tie); previously invisible |
| kalshi x rothera pairs | 0 | 4 | pairs with one Kalshi leg and one Rothera (Robinhood) leg, YES or NO |
| reports with no leg chosen | 1 | 1 | scan reports whose chosen legs include a Robinhood NO contract |
| rothera no leg dominates | 0 | 4 | outcomes where the Rothera NO contract on the other team is at or below the Rothera YES ask (strictly better: same win payout, $1 vs $0 on a tie) |
| tie rule mismatch flags | 0 | 0 | reports flagged tie-rule-mismatch (chosen legs settle a tie differently) |
<!-- /results:arb_fixture_p09 -->

Ties are rare (the margin model puts 0.36 % on a tie pre-game), so this changes the sign of nothing on the fixtures — but
a "lock" that loses on a tie is not a lock, and the table above is what the engine uses to say so.

## What the opt-in fee models change on the fixture scans

<!-- results:fee_flip_p10 -->
| fixture scan | exchange | model (defaults: Rothera flat_001, CDNA flat_001) | events | Robinhood rows | rows on that exchange | rows moved | margins improved | sign flips | arbs before → after | best margin before → after |
|---|---|---|---|---|---|---|---|---|---|---|
| ncaaf | cdna | flat_002 | 56 | 24 | 20 | 18 | 0 | 0 | 0 → 0 | -0.58% → -1.58% |
| ncaaf | cdna | weighted_007 | 56 | 24 | 20 | 18 | 1 | 0 | 0 → 0 | -0.58% → -0.89% |
| ncaaf | rothera | quadratic | 56 | 24 | 0 | 0 | 0 | 0 | 0 → 0 | -0.58% → -0.58% |
| nfl | cdna | flat_002 | 5 | 14 | 0 | 0 | 0 | 0 | 0 → 0 | -3.97% → -3.97% |
| nfl | cdna | weighted_007 | 5 | 14 | 0 | 0 | 0 | 0 | 0 → 0 | -3.97% → -3.97% |
| nfl | rothera | quadratic | 5 | 14 | 14 | 14 | 5 | 0 | 0 → 0 | -3.97% → -3.90% |
<!-- /results:fee_flip_p10 -->

## How many contracts?

Two different answers for two different trades:

* **A locked arb** has no variance, so Kelly does not apply — size is whatever both books hold
  at the prices that still clear the margin. The engine walks the order books
  (`quant.arbitrage.size_from_books`) and reports the profit-maximising count; the overlay shows it
  as `Buy N contracts` with each leg's count. If the count is 0 the "arb" is a 1-contract ghost.
* **A directional STEAL** (one side below fair, no lock yet) is a bet. With a bankroll `B` and
  fee-inclusive cost `k` per contract at fair probability `q`, the Kelly fraction is
  `f* = (q − k) / (1 − k)`; the engine uses ¼ of it by default (`--kelly`, popup), so the stake is
  `B · f* / 4` and the contract count is that divided by `k`, then capped by the contracts offered
  at that ask, and in `live` scaled down with every other STEAL on the tick so their sum stays
  under `--slate-cap`. Fees are already inside `k`, so the edge the sizing sees is the edge after
  fees. A STEAL on a CDNA-routed contract needs an extra 2 points of edge for its order delay,
  and a STEAL that the feed gates hold back (`GATED STEAL: wait: feed-stale …`) gets no size at all.
* **A hedge on an open lot** (`quant/sizing.hedge_kelly`): when you hold `held` contracts of
  one side and the other side's all-in `h` would lock (`h < 1 − avg cost`), the Kelly-optimal
  hedge count is the closed form `n* = [(1 − p)(1 − h) X − p h Y] / (h (1 − h))` clamped to
  `[0, held]` (X / Y = wealth if you win / lose unhedged); `inplay` prints it next to the LOCK
  price when a bankroll is set. A hedge priced exactly at fair fills the whole lot (Kelly
  removes variance it can buy for free), a dearer one fills less, and a hedge that cannot lock
  is reported as 0 — that would be a new position, and the STEAL sizing decides about those.

The honest footnote: on the replays so far the STEAL rule's hold-to-settlement P&L has a 90 %
interval that includes zero on the NFL (16 games) and the shuffled-outcome placebo produces
comparable ROIs there; college week 2 separates from its placebo but its intervals include zero
too; every break-even LOCK variant loses on the NFL week (−4.7 % to −13 %), and on college
the break-even lock loses at 2–6 % edges and is positive only at the 10 % edge (+6.3 % on 25
games) (`docs/MODEL.md`). Size accordingly.


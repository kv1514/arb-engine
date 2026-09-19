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
  at that ask. Fees are already inside `k`, so the edge the sizing sees is the edge after fees.


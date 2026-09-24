# Microstructure recording and paper-execution interfaces

This is the integration contract between the recorder/paper executor and the causal
dataset/evaluator. It describes data available to an offline experiment; it does not enable
alerts or order placement.

## L1 tick contract

`inplay_ticks.ts` is the time the complete tick was ready to persist and is never earlier
than any exact observation in the tick. `inplay_ticks.source` is `full` or `fast`.
`l1_json` retains the compatibility map `{venue: {outcome: row}}` and adds a lossless
`rows` list. Consumers must use `rows` when present.

Each lossless row contains:

| field | meaning |
|---|---|
| `venue`, `venue_market_id`, `outcome`, `side` | displayed contract identity; `side` is `yes` or `no` |
| `book_id` | underlying order book; Robinhood KX routes share `kalshi` |
| `ticker`, `contract_id`, `no_of`, `mirror_of` | adapter identity fields when supplied |
| `bid`, `ask`, `bid_size`, `ask_size` | normalized L1 in dollars and contracts |
| `quote_time` | venue timestamp, when supplied |
| `req_ts`, `obs_ts` | request start and local response completion |
| `approx_time` | `0` only when request timing was measured; `1` for legacy/full fallback timing |
| `refreshed` | `1` when this request returned the contract; carried rows are `0` |
| `source` | `full` or `fast` |
| `tie_payout`, `fee_params`, `exchange` | settlement and exact fee inputs |

Carried rows retain their original `req_ts`/`obs_ts`; a later tick does not make them fresh.
Consumers must exclude `refreshed=0` from decisions and labels. Legacy rows without measured
times fall back to their tick time with `approx_time=1` and must be reported separately.

## Kalshi print contract

`trade_prints` stores only `/markets/trades` results:

`trade_id` (primary key), `ticker`, exchange `ts`, YES `price`, `count`, `taker_side`, local
`req_ts`, and local receipt `obs_ts`.

The causal cutoff is `obs_ts <= decision_ts`; exchange `ts` alone is insufficient because an
old print can arrive in a later response. The poller uses an inclusive `min_ts`, pages to the
end before advancing its durable watermark, and relies on `trade_id` to remove overlap. On
restart it resumes from `Store.latest_trade_ts(ticker)` inclusively. `trade_poll_status()`
exposes last completion, observed gap, overdue state, and unfinished backlog per ticker.

Claude integration action: update `quant.microdata.load_db` to select print `obs_ts` when the
column exists, mark old databases approximate, and make `_Prints.at` gate on receipt time.

## Paper-execution contract

`ioc_round_trip` buys at an explicit decision-time limit. Entry uses the first refreshed row
in `[decision + latency, decision + latency + entry_tol]`; fill is
`min(requested, floor(ask_size * haircut))`, and `PaperTrade.cancelled` is the IOC remainder.

The exit target is frozen at `decision + latency + horizon`, independent of which row filled
the entry. The first refreshed row in the registered tolerance supplies displayed bid depth
once. Up to `max_rolls` unique later observations inside `roll_window_s` may close remaining
inventory; the rest settles when a known value is supplied, without an exit fee. Otherwise
it is unresolved and `pnl` is `None`. Every actual entry, exit, and unwind calls
`FeeModel.fee(price, actual_count, "taker")`.

`ioc_short_via_complement` requires the complementary contract's own rows, limit, depth, and
fee model. `two_leg_arb` rejects shared `book_id`, explicit settlement mismatch, and unknown
provided tie payouts. Callers claiming a guaranteed arbitrage must pass verified
`tie_payouts` and `settlement_compatible`; omission is suitable only for a labelled win-case
speculation metric. Excess from a failed leg is unwound after the other IOC result is known,
subject to latency, unique displayed depth, both fees, and a bounded unwind window.

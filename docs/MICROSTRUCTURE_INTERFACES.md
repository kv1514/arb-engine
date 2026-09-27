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
Rows sharing an exact `obs_ts` form one response-time information batch: all are installed
before any feature at that timestamp is computed.

### One instant, one batch, one decision

`quant.microdata.observation_instants(rows)` gives the batches in time order. Every
evaluator path installs a whole batch, and voids its conflicted contracts, before it
evaluates anything at that `t`: features, freshness, eligibility, pair or hedge choice, or
cooldowns. No path reads an observation after `t` to decide at `t`. The paths are:

* `microdata.build`;
* H4 `arb_scan`;
* `select_trades`;
* the H3-lock hedge watch.

**Choices within an instant are made on economics, never on names or arrival order.** A path
falls back to the contracts' identity only on an exact economic tie.

| path | order of preference |
|---|---|
| H4 pair | cheapest all-in cost, then the fresher older leg, then the deeper thinner leg |
| decisions for one exposure (`select_trades`, with a cooldown) | cheapest ask of the bought contract, then a direct buy before one through a complement |
| hedge at an instant | cheapest all-in hedge that locks, then the larger tie payout |

A hedge quote that could not fill before the remainder's exit ends the watch at that instant,
before any hedge there is sent.

**Several rows for one contract identity `(event_key, book_id, outcome, side)` at one
instant** are resolved by `microdata.resolve_instant`:

1. The direct venue's rows outrank a resale route's. A Robinhood KX row never replaces the
   Kalshi row it resells, whatever either shows.
2. Rows of the best route that agree on `bid`, `ask`, `bid_size`, `ask_size`, `tie_payout`,
   `fee_params`, `venue`, `exchange`, `no_of` and `mirror_of` are one observation. A measured
   time outranks an approximate one, then the smallest canonical JSON.
3. Rows of the best route that disagree on any of those fields are a **conflict**, and
   nothing about that contract at that instant is trusted:
   * it is no decision, no H4 leg, no fill and no label at `t`;
   * its earlier quote is **void** from `t` until its next unambiguous observation, so it is
     no cross-book comparison, complement or leg in that span either (reported as
     `ambiguous`).

This is conservative. An ambiguous instant can only remove opportunities and fills, never
create one, and the outcome is identical in every arrival order.

## Settlement-value contract

`quant.microdata.settlement_values()` returns exact values keyed by
`(event_key, book_id, normalized_outcome, side)`, the same tuple as `contract_key(row)`.
The normalized outcome is already what the purchase pays on a win: for example, Robinhood
`NO Detroit` has `outcome=Buffalo`, `side=no`, and `no_of=Detroit`; a Buffalo win pays it $1
and must not be inverted again. `no_of` is used as a consistency check.

For compatibility, an old `(event_key, outcome, side)` alias is emitted only when every
book has the same value. Consumers should call `settlement_for(values, contract_key(row))`
or use the exact four-field key. This prevents Kalshi's $0.50 tie payout and Rothera's
book-specific tie payout from overwriting each other. Claude's H3-lock replay must migrate
its direct three-field lookup to `settlement_for`.

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

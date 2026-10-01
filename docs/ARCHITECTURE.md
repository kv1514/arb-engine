# Architecture

```
            ┌────────────┐   ┌──────────────┐   ┌──────────────────┐
  venues    │ kalshi.py  │   │ polymarket.py│   │ robinhood.py     │   (public data; Kalshi also signed)
            └─────┬──────┘   └──────┬───────┘   └────────┬─────────┘
                  └──── VenueSnapshot(events, OutcomeQuote[]) ────┘
                                     │
  matching   merge_snapshots():  event key = sport + canonical participants + ET date
                                     │  MergedEvent{quotes_by_venue}
  scanner    analyze_event():  fee models (fees/registry: opt-in Rothera/CDNA models) → all-in cost per venue
                               → compliance.executable_venues(): non-executable venues stay in the
                                 fair value, never in a leg (signal-only rows)
                               → best leg per outcome (Rothera NO leg as <id>#no) → evaluate() margin
                                 + tie_margin → max_price_for_leg() taker/maker on the venue's tick
                               → consensus_fair_value() → settlement_rules.pair_flags()
                               → flags (live, stale, same-book, tie-rule-mismatch, below-min-size,
                                 settlement-mismatch:*, signal-only)
                                     │
             CLI (cli.py + cli_plugins/*)       bridge.py (HTTP, 127.0.0.1:8765)
                                                       │
                                     extension/background.js  ──►  content.js overlay
                                     (direct mode = same logic in arb-core.js)
```

## Key decisions

* **Per-outcome normalisation.** Every venue quote becomes "price to buy this outcome",
  so a NO quote on side B is just a YES quote on side A. Arbitrage is then
  `sum(all-in cost of the cheapest leg per outcome) < 1`. Spread/total lines are the same
  shape: one binary market per line becomes a two-outcome event (`BUF-1.5` / `DET+1.5`,
  `over` / `under`) whose NO quote is stored as `<market id>#no`.
* **Fillable, not just positive.** A positive margin at the reference size only counts as an
  arb when every leg has depth for at least one contract; `--books` then fetches real
  order books for candidate events (two passes, so Kalshi's rate limit is respected) and
  `size_from_books` returns the profit-maximising size.
* **Fees are exact.** `Decimal` in Python, `BigInt` in JS; both are checked against the
  venues' published tables and against each other (3,650 fee vectors + 54 arb vectors). The
  per-exchange models that differ from the historical $0.01/contract (Rothera per-order
  quadratic, CDNA range) are **opt-in** (`fees/registry.py`: settings > env > default) until
  an order ticket says which one Robinhood passes on.
* **Tie-aware margins.** A Kalshi YES pays $0.50 on an NFL tie, a Rothera YES (as read from
  its rulebook, unverified) $0, a Rothera NO $1. `Leg.tie_payout` feeds `ArbResult.tie_margin`
  next to the ordinary `margin`; the tie-break between equal all-ins goes to the tie-paying
  leg and the Rothera NO contract on the other team is a candidate leg in its own right.
* **Eligibility is a checked fact.** `compliance.py` reads `data/venue_rules.json` (verified
  date + source per venue): Polymarket is not executable for a US-resident account, so its
  quotes are a signal in the consensus and never an arb leg or a maker hedge unless
  `EXECUTABLE_VENUES` says otherwise. The scanner, the bridge's `/analyze`, the maker and the
  overlay all resolve it the same way.
* **Settlement is a registry, not tribal knowledge.** `data/settlement_rules.json` holds
  tie / postponed / cancelled / walkover / retirement / OT per (venue, sport, market type)
  with the venue's rule text pinned by sha256; `matching/settlement_rules.py` compares the two
  legs of a pair and flags the difference (tennis gets its own gates).
* **Max-buy price** is computed on the venue's tick grid with the real fee function, for a
  taker order and for a resting (maker) order — the latter is what you post on Robinhood or
  Kalshi to *wait* for an arb instead of paying the spread.
* **Same-book de-duplication.** Robinhood's Kalshi-routed quotes carry `book_id="kalshi"`
  and are never paired with Kalshi direct; they are shown for the fee comparison.
* **Live-match discipline.** `in_play` (Robinhood `eventProgress`) or start ≤ now marks an
  event live; live events are excluded from the arb list by default because cross-venue
  gaps in play are timing artefacts.
* **Two overlay modes.** Direct (service worker fetches venues itself; Kalshi needs the
  Origin-stripping rule) and bridge (Python engine does the modelling; richer and the place
  to add sportsbook consensus / order books).
* **Execution gates.** Kalshi orders are dry-run unless confirmed, demo unless
  `KALSHI_ENV=prod`, and prod additionally requires `ARB_LIVE_TRADING=1`. Every resting
  order carries an exchange-side expiry (`min(kickoff, now + 1 h)`), `SelfMatchGuard`
  refuses an order that would cross our own book, new orders are bounded by the Kalshi
  balance and by `--hedge-cash` (the hand-executed hedge legs the operator can find), the
  exchange status is polled and a pause cancels everything, and a kill leaves nothing behind
  (batched cancel with per-order retry, orphan sweep on restart).
* **CLI plugins.** `cli.py` owns the built-in subcommands and their golden `--help`
  fixtures; every feature since P01 adds its flags or subcommands from
  `arb_engine/cli_plugins/<feature>_flags.py` (`backtest`, `live`, `maker`, `record` /
  `event-study` / `backtest-ticks` / `clv`, `lines-eval`) through `register(subparsers,
  existing_parsers)`. A plugin that fails to import is reported and skipped, so one broken
  feature cannot take `scan` down. Settings are declared where they are used
  (`config.declare_setting(key, env=…, default=…)`), read as settings dict > environment >
  default.
* **Statistics live in one place.** `quant/calibration.py` (paired game-cluster bootstrap,
  isotonic / CORP decomposition, reliability bands, games-needed) and `models/cv.py`
  (grouped folds that never split a game) are what every replay, experiment and evaluation
  script reports through, so an "interval" means the same thing everywhere.

## Data flow for one Robinhood event (overlay / `rh-event`)

1. Fetch the event page, read `__NEXT_DATA__` → contracts (`symbol`, `exchange`, ids).
2. Live quotes from `api.robinhood.com/marketdata/event/contract/quotes/v1/?ids=`.
3. Kalshi ticker = `KX` + symbol (or the symbol itself if already `KX…`) → `/markets/{ticker}`
   + `/series/{series}` for `fee_type` / `fee_multiplier`.
4. Polymarket: NFL → `/markets?slug=nfl-{away}-{home}-{date}` (both team orders, date and
   date+1 UTC); tennis → `/public-search?q=<surnames>` filtered to the matching moneyline.
5. `analyze_event()` → per-venue all-in, fair, edge, max-buy (taker/maker), arb legs.

## Maker runner (`arb_engine/strategy`)

```
scan (every --rescan s) ──► discover(): Watch per (event, Kalshi side) with the cheapest
                             hedgeable ask on another venue for the other side
loop (every --interval s):
   refresh():   GET /markets?tickers=…  (batched)  +  Robinhood quotes API  +  Polymarket /markets?slug=
   _price():    price = min(max_buy_maker(hedge), Kalshi ask − tick); margin_if_filled;
                skip if < --min-margin or (queue_ahead) below Kalshi's best bid
   check_fills(): broker.poll → HEDGE NOW alert with max hedge price for the filled size
   reconcile(): cancel decayed / re-priced orders, place new ones within limits
shutdown: cancel every resting order
```

Brokers: `PaperBroker` (fills when the venue's best ask reaches our price — optimistic about
queue position), `KalshiBroker` (post-only V2 orders with an exchange-side expiry; demo unless
`KALSHI_ENV=prod` + `ARB_LIVE_TRADING=1` + `--confirm`). Hedge venues default to
`robinhood` (`--hedge-venues`; a watch whose hedge sits on a non-executable venue alerts
`HEDGE VENUE NOT EXECUTABLE` and is not rested), `--hedge-cash` caps the hand-executed hedge
legs resting at once ($250), each new order is bounded by the Kalshi balance, and an exchange
pause cancels everything. All venue data goes through one rate-limited Kalshi client
(15 req/s) shared by the scan adapter and the feed.

## In-play pricing (`strategy/inplay.py`)

```
venues (Kalshi / Polymarket / Rothera quotes) ─► consensus_fair_value ─► market P(home)   (sportsbook ML de-vigged in, pre-game only)
ESPN scoreboard (+summary every 30 s)       ─► StateGuard ─► GameState ─► models/wp.home_win_probability ─► model P(home)
                                                                       └─► espn_home_wp (a post-play number)
blended_fair(market 0.30 × confidence(book width), model 0.55, espn 0.15; renormalised; market-only pre-game;
             pool = linear | logit; p_tie split out per leg)            optional: quant/lines fair for a spread/total (LINE_FAIR=1)
  └─► FeedFreshness gates ─► STEAL (blend AND model ≥ all-in + edge) · LOCK price for the short side (+ hold EV) · disagreement > 0.05 flagged
```

**Gates (P06).** In the `inplay` watcher and the `live` slate every STEAL and LOCK NOW passes
through a per-event `FeedFreshness`, which tracks when the ESPN state and score last changed,
each venue's mid moves and quote age, frozen polls and pending scores (the gates need
poll-to-poll memory, so only those two long-running commands construct one). A signal that fails is printed and journaled as **`GATED STEAL: wait:
<reasons>`** / **`GATED LOCK NOW: wait: …`** with one or more of:

| reason | fires when |
|---|---|
| `feed-stale` | no ESPN state change for `--stale-after` s (default 15) while a venue mid moved ≥ 0.02 over the trailing max(poll interval, 10 s) — the market knows something the feed does not show yet |
| `clock-frozen` | identical state for ≥ `inplay_frozen_s` (default max(3 × poll interval, 30 s)) with the clock running *and* a venue mid moved ≥ 0.02 since the state last changed; ESPN's normal 10–20 s lag and a stoppage with a quiet market do not trip it |
| `quote-old:<venue>` | that venue's own quote timestamp is older than max(poll interval, 10 s) (CDNA quotes carry +3 s for their order delay) |
| `score-pending` | a score changed but the last play id has not advanced (the play is not fully published) |
| `suspect` / `review-pending` | the ESPN feed's own StateGuard flags (score went backwards, score before its play) |
| `disagreement` (STEAL only) | the model is more than `inplay_agreement_gap` (default 0.12) from the market *and* ESPN sides with the market — a provisional risk control until the first recorded Sunday measures it |

Every rule is in seconds, never polls: the overlay polls every 1 s, `inplay` every 5 s and
`live` every 10 s. STEAL/LOCK actions and sizing use the cheapest *executable* ask; a cheaper
non-executable ask (Polymarket) is shown as *signal only* and never recommended.

A STEAL on a CDNA-routed Robinhood contract also needs `--cdna-haircut` (default +0.02) of
extra edge for its 3 s order delay and is never lockable in play; a STEAL on a spread/total
line (line fair attached) needs twice the edge. `live` applies a slate-wide stake cap
(`--slate-cap`) across every STEAL on a tick. The bridge serves `GET /inplay?url=…&position=…`
and the overlay renders it as the LIVE strip — gated the same way: `bridge.py` keeps one
`FeedFreshness` per event across requests and passes `now=`, so the same poll that `live`
would print as `GATED STEAL: wait: feed-stale` reaches the page with `steal_gated` /
`lock_gated` set and is rendered as `GATED · wait` (`docs/EXTENSION.md`). A gated LOCK still
sets `SideView.lock_available` (with `lock_gated=True` and the reasons in `gated_reasons`) so
callers can show the price; the `GATED LOCK NOW` action line is the authoritative form and the
overlay never prints `NOW` for it.

**What the replays say the gates are for.** Timeouts and the synthetic kickoff-pending /
try rows carry the widest model-vs-market gaps and most of the STEAL-qualifying rows
(`docs/MODEL.md`, per-class table): those are dead-ball moments where the market has not
re-priced. `tickreplay.py` (`backtest-ticks`) replays a recorded game through the same
watcher with the gates on and off; on the synthetic 40-tick fixture the gates cut 11 STEALs
to 3 (8 gated) and turn −0.16 into +0.18 per contract. That is a fixture, not a result — the
real test needs a recorded Sunday (`docs/ROADMAP.md`, "Needs you").

## Read-only US three-venue comparison (`us-arbs`)

`cli_plugins/us_arbs.py` concurrently discovers NFL full-game moneylines on Kalshi,
Robinhood and Polymarket US. It requests Kalshi books only for shared pregame game keys;
Polymarket US offers/bids supply its L1, never metadata prices. Local GET wrappers stamp
actual request/response times for Kalshi books and Robinhood quote replies, and omitted
or failed reads cannot retain catalogue liquidity. This does not change legacy recorder
timing or add an execution path.

`quant/us_arbitrage.py` merges exact Eastern-date keys, rejects same-book pairs and
stale/future/carried/missing-depth observations, then sizes whole contracts at displayed
top-of-book prices. Decimal costs include both fees and a conservative split-fill fee
bound within each leg's cash cap. No volume rebates, maker fills or deeper-price walking
are assumed. Verified payoff requires independently known, compatible registry terms;
unknown/discretionary/different settlement or losing ties are **conditional**, not a
guarantee. Polymarket US has no verified NFL settlement registry entry yet. No trading
client, ledger mutation, account read or push is invoked.

The venue table now records `adapter: true` and `requires_opt_in: true` for Polymarket US.
Default live/maker eligibility remains Kalshi/Robinhood; the dedicated read-only command
explicitly includes the US price feed and still respects an operator's venue restriction.

## Separate US manual execution path

`cli_plugins/us_ioc_ops.py` registers `us-ioc`, independent of `us-arbs` and the
overlay. `execution/polymarket_us_ioc.py` builds an immutable decision-limit plan.
Dry-run stops there, without credentials or network. Confirmation requires both live
flags, the exact US host, fresh validated NFL pre-game identity/book/grid and buying power.
`venues/polymarket_us_trading.py` signs Ed25519 requests with the existing optional
cryptography package, refuses redirects, and never retries POSTs. Secrets stay outside Git.

`USOrderLedger` creates `pm_us_*` tables in the existing production Kalshi SQLite file.
Both venues reserve under `BEGIN IMMEDIATE`; `execution/shared_limits.py` sums unfinished
reservations at full worst cost and finished fills at paid cost, enforcing hard $25/$50
cash ceilings even for Kalshi lock-leg exemptions. Key fingerprints pin US recovery to
the sending key (automatic key-rotation/account equivalence is not inferred).
Production `KalshiExecutor.execute` verifies a matching pending reservation bound to the
same key/account, checks cash ceilings, and claims one send atomically. Direct calls
without that proof, or repeat calls of an already claimed intent, cannot send; existing
ledgered strategies/manual CLI use that path, while demo behavior stays unchanged.
No daily reset or inferred settlement/exit release restores room. All processes must
use this version and the same ledger directory; external/manual trading is outside scope.

US IOC remainder is cancelled through the gates and reread; only terminal scoped
cumulative quantities/average long price/commission release the unfilled part. Unknown
sends without exchange IDs are permanently held for investigation, never guessed from
activities or resent. Cumulative-count/cost regressions are sticky contradictions.
US sends are claimed once. Freshness is rechecked after both reservation and send-claim
transactions, since either can wait on another process. A local expiry releases cash
only before the send claim; once claimed, accounting remains conservative even if no
request was sent.
The signed US transport now receives the decision deadline and checks it again
after signing/serialization, immediately before its single network attempt. A
clock regression or removed live gate at that boundary refuses the send; a claimed
intent remains conservatively held. GET/POST paths are separately allowlisted.
There is no automatic hedge, two-leg atomicity, unwind, settlement proof or live-fill
evidence; `us-arbs` remains read-only and unverified-settlement candidates remain conditional.

## Automatic paired paper execution

`cli_plugins/auto_arb.py` adds a separate off-by-default public-book runner.
`execution/pairpaper.py` has no account transport. `live` returns a capability-blocked
report before credentials, network or production-order ledger access. It may read an
existing standing-permission store without creating files; manual live flags or an
armed permission do not unlock it.

The planner rechecks raw identity, settlement registry, two-sided receipt provenance,
venue eligibility, price/quantity grids and Decimal fees. Both venue game identities
must agree. Latest decision quotes cannot be replaced by cheaper older rows; conflicting
equal-time rows invalidate the observation. Future rows are invisible. Unknown US
settlement and Kalshi fair-price exceptions block current production pairs.

The frozen paper rule buys US first, buys Kalshi only for the first fill, and sells
US excess if the hedge misses/partially fills. Each dispatch has 3s latency and a 2s
arrival window, consuming the first usable refreshed observation; displayed liquidity
is halved. IOC remainder is cancelled, not rolled. At most two unwind windows are
allowed with a $1 fee-inclusive loss cap; unsold excess is unresolved and halts new
paper pairs. Neither observation delay nor a later better quote changes an IOC limit.
Actual simulated counts determine each existing FeeModel's entry/exit commission.
Fee parameters are frozen; changed or incomplete evidence cannot support a fill.

`auto_pair_paper.sqlite3` uses FULL synchronous SQLite transactions: state, fills,
cash and underlying-book liquidity consumption commit together. US NO purchases and
YES sales share the underlying long bid resource. Duplicate observations cannot add
size. Both fees and two unwind-fee bounds are reserved before admission. One pair is
active at a time and each game is tried once. Completed purchases and exit fees remain
charged without inferred settlements or sale-proceeds credits. Restart does not reset
the $50 paper cap. This isolated simulator does not strengthen guarantees of older
workers or establish profitable, leakage-free empirical performance.

## Standing permission boundary

`cli_plugins/trade_approval.py` authenticates the production Kalshi account/key through
`GET /communications/id` and the US sending key through `GET /v1/account/balances`.
No order/cancel transport is called. Default arming/revocation is dry-run; confirmed
arming freezes a six-hour (at most 24-hour) policy. Account identifiers are fingerprints,
not secrets or printed identifiers. US account equivalence across key rotations is not
inferred. Existing demo settings and workers remain unchanged.

`execution/standing_approval.py` stores the policy and exact-plan permits in private
`standing_approval.sqlite3`. FULL synchronous DELETE-mode transactions serialize arming,
approval, revocation and single-use consumption. Clock checks happen after lock waits
and after final verification. A permit deadline is the earliest of policy expiry,
kickoff and either original quote receipt plus six seconds, never a later decision time.
It holds entry and bounded contingency fee cash; all earlier permits still count toward
the lifetime $25-per-venue/$50 policy ceiling, including expired unused permission.
No settlement, sale proceeds or re-arming resets those holds.

The spec fingerprint includes quote producers/normalizers, planning/fee/identity code,
eligibility/settlement tables and referenced local rule-text fixtures. Registry caches
are refreshed under the transaction, fixture hashes checked, and an in-process disk
change requires restart before re-arming. Evidence integrity does not establish that
exceptional payoff interpretations are correct. Unknown US settlement still blocks pairs.

This policy performs no production reservation, reconciliation or submission. The future
coordinator must atomically fence policy generation, revocation, deadline and account
binding with its durable production send claim and cash reservation. `consume` does not
provide that dispatch fence. `ApprovalStore.dispatch_guard` now serializes a consumed
permit's generation, account, digest, expiry and single dispatch claim against revocation.
The coordinator must acquire policy before production-ledger locks, reserve both legs
and claim the first send inside the guard, then release it before network I/O. A later
revoke stops new claims, not an already committed claim. The two databases are NOT one
transaction: a unique production permit ID and non-retryable send claim remain mandatory
even if the guard rolls back after an external claim or crashes before committing.
The transport must still enforce the guard's deadline after signing. Until that
coordinator and verified recovery exist, `auto-arb --mode live` is always blocked.

`execution/pair_reservations.py` now stages the exact consumed permit's parent and
both immutable child terms inside the existing production ledger transaction.
`ApprovalStore.reservation_guard` holds policy before production but does not claim
dispatch; later submission must still use `dispatch_guard`, so revocation after
staging stops it. US entry/two exit fee bounds and Kalshi's conservative split-fill
bound count in shared exposure with all manual/strategy intents. The US published
date schedule and production Kalshi multiplier floor cannot be weakened by quote
fee parameters. One staged/unresolved parent blocks new admissions and manual
exposure; no child is a matching pending Kalshi or manual US intent yet. Child
transfer/executor ownership proofs and production recovery are still unfinished.
Expired local staging can release cash only when both children remain unclaimed;
parent/permit/game IDs remain. A policy transaction failure after production commit
does not undo those production holds. Tests exercise concurrency, restart, partial
transaction failures, expiration and account/fee/cap conflicts with temporary stores.

`execution/us_pair_orders.py` provides pure, non-sending recovery building blocks:
`USPairOrder` maps buy/sell YES/NO onto the always-long API price and automatic order
indicator; sale reservations count exit fees but never anticipated proceeds. Exact
`order_evidence` checks ID, market, action/side, IOC terms, counts, decision limits and
explicit money. Create snapshots never establish final inventory. `read_inventory`
requires complete, market-scoped positions pagination with explicit EOF, matching
account/host, decimal quantities and fresh request/receipt timing. Rounded deprecated
fields, partial listings and missing availability are not inventory proof. The parent
ledger must separately prove pair-owned excess; an account position is not a reduce-only
authorization. Tests are schema-shaped synthetic evidence, not observed live fills.

## History, replay and recording (`venues/history.py`, `backtest.py`, `store.py`)

```
ESPN summary (drives/plays with wallclock) ─► espn_timeline() ─► PlayRow[] (state before AND after each play, ts,
                                                                  play class, scoring flag, per-team timeouts,
                                                                  synthetic try / kickoff-pending rows)
Kalshi  /series/{s}/markets/{t}/candlesticks (1 min, yes_bid/yes_ask close) ─┐   bar_at(bars, ts, mode):
Robinhood /marketdata/event/contract/historicals/v1/ (5 min trade bars)     ─┼─►   kalshi_before = last candle ending <= ts
Polymarket /prices-history (1 min)                                          ─┘   kalshi_after  = first candle ending  > ts
HistoryClient: read-through JSON cache (--cache-dir), --offline never fetches
GameReplayer.replay(): per play → model P (pre / post state) / ESPN P / venue P under both alignments / consensus / blend
backtest --week: pooled_metrics (all / in play / in-play scrimmage) → strata_tables (slice, play class, lead)
                 → class_gaps → fit_blend_weights (linear, logit) → interval_report (game-cluster bootstrap, games_needed)
                 → simulate_pairings (pre_before, post_after, pre_after, shift, shuffle) + lock variants
                 → week_report → --results-json (metrics only, canonical JSON → docs tables)
                 --pool: pooled weeks + walk-forward fit
Store (SQLite): scans / quotes (scan --record), inplay_ticks (inplay --record),
                espn_ticks / <venue>_ticks (L1) / steal_observations with a +10 s … +15 min ladder (live --record)
quant/eventstudy.py (event-study): replay rows × public trade tapes (venues/trades.py) → absorption at
                +30 s / +2 min / +5 min / +15 min by |ΔWP| bucket, Mincer-Zarnowitz and under-reaction regressions,
                StateGuard anomaly episodes
tickreplay.py (backtest-ticks): a recorded game replayed through InplayWatcher, gates on / off, CLV and settled P&L
```

Kalshi candles carry the book (bid and ask) so intra-Kalshi arb minutes are exact; Robinhood
and Polymarket histories are trade/mid prices, so cross-venue counts are indicative only.
`scripts/render_results.py` renders the docs' tables from the `--results-json` fixtures and
`--check` fails CI when a doc drifts from its fixture (`docs/results/README.md`).

## Line fair values (`quant/lines.py`, `quant/odds.py`, `quant/fairvalue.py`, `quant/margintable.py`)

```
nflverse games.csv (2016-2025, season in progress excluded) ─► scripts/build_margin_table.py ─► data/nfl_margin_dist.json (key-number buckets)
                                                                                                data/margin_sigma.json (σ by line)
closing moneylines ─► odds.devig_shin / _additive / _power ─► fairvalue (de-vigged sportsbook close, one book pre-game via ESPN pickcenter)
NormalMargin(σ, tie mass) / EmpiricalMargin(buckets) ─► line_fair_for_event(moneyline | spread | total, push rule per venue) ─► middle EV
game_phase(pre / live / overtime / clock_unknown / final): a tie at 0:00 is priced as overtime, unknown clocks and finals return no fair
scripts/eval_lines.py (lines-eval): bracketed evaluation on the Kalshi market's own half-point line ─► tests/fixtures/results/lines_eval_p13.json
```

The scanner and the in-play watcher attach a line fair to spread / total events behind one
opt-in knob (`LINE_FAIR=1`); with it on, a STEAL on a line needs twice the edge.

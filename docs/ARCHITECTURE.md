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

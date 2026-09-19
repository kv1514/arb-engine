# Architecture

```
            ┌────────────┐   ┌──────────────┐   ┌──────────────────┐
  venues    │ kalshi.py  │   │ polymarket.py│   │ robinhood.py     │   (public data; Kalshi also signed)
            └─────┬──────┘   └──────┬───────┘   └────────┬─────────┘
                  └──── VenueSnapshot(events, OutcomeQuote[]) ────┘
                                     │
  matching   merge_snapshots():  event key = sport + canonical participants + ET date
                                     │  MergedEvent{quotes_by_venue}
  scanner    analyze_event():  fee models → all-in cost per venue → best leg per outcome
                               → evaluate() margin → max_price_for_leg() taker/maker
                               → consensus_fair_value() → flags (live, stale, same-book)
                                     │
             CLI (scan / quote / rh-event)      bridge.py (HTTP, 127.0.0.1:8765)
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
  venues' published tables and against each other (2,160 vectors).
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
  `KALSHI_ENV=prod`, and prod additionally requires `ARB_LIVE_TRADING=1`.

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
queue position), `KalshiBroker` (post-only V2 orders; demo unless `KALSHI_ENV=prod` +
`ARB_LIVE_TRADING=1` + `--confirm`). All venue data goes through one rate-limited Kalshi
client (8 req/s) shared by the scan adapter and the feed.

## In-play pricing (`strategy/inplay.py`)

```
venues (Kalshi / Polymarket / Rothera quotes) ─► consensus_fair_value ─► market P(home)
ESPN scoreboard (+summary every 30 s)       ─► GameState ─► models/wp.home_win_probability ─► model P(home)
                                                          └─► espn_home_wp
blended_fair(market 0.30 × confidence(book width), model 0.55, espn 0.15; renormalised; market-only pre-game)
  └─► STEAL (blend AND model ≥ all-in + edge) · LOCK price for the short side · disagreement > 0.05 flagged
```

The bridge serves `GET /inplay?url=…&position=…` and the overlay renders it as the LIVE strip.

## History, replay and recording (`venues/history.py`, `backtest.py`, `store.py`)

```
ESPN summary (drives/plays with wallclock) ─► espn_timeline() ─► PlayRow[] (state *before* each play, ts)
Kalshi  /series/{s}/markets/{t}/candlesticks (1 min, yes_bid/yes_ask close) ─┐
Robinhood /marketdata/event/contract/historicals/v1/ (5 min trade bars)     ─┼─► bar_at(bars, ts)
Polymarket /prices-history (1 min)                                          ─┘
GameReplayer.replay(): per play → model P / ESPN P / venue P / consensus / blend
                        → per-source log-loss + Brier (all plays and in-play only)
                        → arb minutes: Kalshi book (bid+ask) and Kalshi × Robinhood (indicative)
Store (SQLite): scans / quotes (scan --record), inplay_ticks (inplay --record)
```

Kalshi candles carry the book (bid and ask) so intra-Kalshi arb minutes are exact; Robinhood
and Polymarket histories are trade/mid prices, so cross-venue counts are indicative only.

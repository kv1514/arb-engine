# Working on arb-engine (for Codex, Grok, Claude and humans)

This repo is a fee-aware arbitrage / fair-value engine for **sports prediction markets**
(Kalshi, Polymarket, Robinhood's event contracts on Rothera + KalshiEX) plus a Chrome
overlay for robinhood.com. Read `README.md` first, then `docs/VENUES.md` for the verified
fee schedules and API facts every change must respect.

## Ground rules

1. **Fees are contracts with the venues, not estimates.** Do not change a fee formula
   without citing the venue document and date in `docs/VENUES.md`, updating
   `tests/test_fees.py` with the venue's own worked numbers, and regenerating
   `tests/fixtures/fee_vectors.json` (`python scripts/gen_fee_vectors.py`) so the
   extension's `arb-core.js` stays in parity (`scripts/test_js.sh`).
2. **Standard library only** in `arb_engine/` (the `cryptography` package is the one
   optional dependency, for Kalshi request signing). No pandas/requests/pydantic.
3. **Never place orders by default.** Anything that can submit an order must be dry-run
   unless `confirm=True` *and* the environment is demo, or `KALSHI_ENV=prod` with
   `ARB_LIVE_TRADING=1`. Keep the three gates in `arb_engine/execution/kalshi.py` and
   `strategy/broker.py` (`KalshiBroker` refuses to construct without them). The maker
   runner must cancel everything on shutdown and must never rest on a market whose hedge
   has disappeared.
4. **No secrets in the repo.** Keys live in `.env` (git-ignored) or the shell.
5. **Same-book awareness.** Robinhood re-sells Kalshi's order book for `KX*` contracts.
   Those quotes carry `book_id="kalshi"` and must never be arbed against Kalshi direct;
   they exist for the fee comparison. Rothera (`NFLGAME-…` etc.) is a separate book.
6. **In-play markets are not arbs.** Cross-venue gaps during a live game are staleness.
   Keep the `live` / `in_play` handling in `arb_engine/scanner.py`.

## Layout

```
arb_engine/
  fees/        venue fee models (Kalshi, Robinhood, Polymarket, Polymarket US) — Decimal math
  quant/       odds & de-vig, arbitrage evaluation, max-buy price, depth sizing, Kelly, fair value
  venues/      adapters: kalshi.py (public + signed), polymarket.py (Gamma/CLOB), robinhood.py (public web/API)
  matching/    canonical team/player keys, Eastern-date event keys, cross-venue merge
  scanner.py   sport-wide scan; eventlookup.py single Robinhood event; bridge.py local HTTP server
  execution/   Kalshi order plans + gated executor
  strategy/    maker runner (maker.py), brokers (paper / Kalshi), alerts + JSONL journal
extension/     Chrome MV3 overlay (arb-core.js is the JS twin of fees/ + quant/)
tests/         unittest suite (offline fixtures) + JS tests run by scripts/test_js.sh
docs/          VENUES.md (fee facts + sources), SPORTS.md, ARCHITECTURE.md, ROADMAP.md
```

## Commands

```bash
python -m unittest discover -s tests -t .      # Python tests (80)
bash scripts/test_js.sh                        # JS parity + background integration (node or jsc)
python -m arb_engine scan --sport nfl          # live scan (add --books for depth sizing)
python -m arb_engine rh-event <robinhood event url>
python -m arb_engine bridge                    # local server the extension uses when running
python -m arb_engine maker --mode paper        # rest Kalshi orders at arb-creating prices (paper by default)
python scripts/capture_fixtures.py             # refresh offline fixtures from the live APIs
```

## Conventions

- Prices are dollars per $1-payout contract (`0.52`), never cents, in Python and JS.
- `OutcomeQuote.ask` is always "what you pay to buy this outcome" — adapters fold NO-side
  quotes into the other outcome.
- Event keys: `nfl:<CODE>|<CODE>:<YYYY-MM-DD ET>`, `tennis:<surname>|<surname>:<date>`,
  lines append `:spread:<FAV>-<line>` / `:total:<line>` (half-point lines; match on the
  numeric line, never on venue ticker suffixes — Kalshi uses ⌈line⌉, Rothera ⌊line⌋).
- Money math in `Decimal` (Python) / `BigInt` (JS). Do not introduce float rounding into fees.
- Keep functions small and tested; fixtures are trimmed live responses in `tests/fixtures`.

## Good next tasks

See `docs/ROADMAP.md`. Highest value: spreads/totals line matching, Polymarket CLOB order
placement (py-clob-client), a websocket feed for Kalshi/Polymarket, and sportsbook
de-vigged consensus via The Odds API.

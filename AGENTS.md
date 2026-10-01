# Working on arb-engine (for Codex, Grok, Claude and humans)

This repo is a fee-aware arbitrage / fair-value engine for **sports prediction markets**
(Kalshi, Polymarket, Robinhood's event contracts on Rothera + KalshiEX + CDNA) plus a Chrome
overlay for robinhood.com. Read `README.md` first, then `docs/VENUES.md` for the verified
fee schedules and API facts every change must respect, and `docs/ARCHITECTURE.md` for the
data flow.

## Ground rules

1. **Fees are contracts with the venues, not estimates.** Do not change a fee formula
   without citing the venue document and date in `docs/VENUES.md`, updating
   `tests/test_fees.py` with the venue's own worked numbers, and regenerating
   `tests/fixtures/fee_vectors.json` (`python scripts/gen_fee_vectors.py`) so the
   extension's `arb-core.js` stays in parity (`scripts/test_js.sh`).
2. **Standard library only** in `arb_engine/` (the `cryptography` package is the one
   optional dependency, for Kalshi RSA and Polymarket US Ed25519 signing). No pandas/requests/pydantic.
3. **Never place orders by default.** Anything that can submit an order must be dry-run
   unless `confirm=True` *and* the environment is demo, or `KALSHI_ENV=prod` with
   `ARB_LIVE_TRADING=1`. Keep the gates in `arb_engine/execution/kalshi.py` and
   `strategy/broker.py` (`KalshiBroker` refuses to construct without them), including the
   host check: a mutation only goes to a known Kalshi host *of the client's environment*
   (`execution/ledger.env_host_problem`), so `KALSHI_BASE_URL` cannot move the demo gates
   onto production. The maker runner must cancel everything on shutdown and must never rest
   on a market whose hedge has disappeared.
3a. **Every order the engine sends goes through the order ledger** (`execution/ledger.py`):
   LAG entries and lock legs, the Robinhood-done button, the maker's resting orders
   (`KalshiBroker`) and confirmed manual orders. The intent and its worst-case cost (fees
   included, at the market's own fee multiplier read from the exchange - never an assumed
   1) are written and reserved before the request, the ledger's `client_order_id` goes on
   the order, an answer that never came back is `ambiguous` and blocks new exposure until
   reconciliation finds the order on the exchange, and actual fills and fees replace the
   reservation - only on final evidence (`order_evidence` / `fills_evidence`): a final order
   row (a terminal status, an explicit zero remaining quantity) whose count no earlier answer
   exceeded, *and* a complete, correctly scoped fills listing agreeing with it (every page, no
   error; only this order's rows, on its market and `book_side`; every fill with an id;
   duplicates identical) - an order that filled nothing too - with the fill cost and fees
   stated by the row or the listing. An absent fee is unknown, never zero: until it is
   stated, an IOC's final fills count at the limit plus the fee bound. Fill evidence only
   grows (`fill_seen`, restart-safe): a count below one an earlier answer showed, or row and
   listing apart, is *contradicted* - the whole worst case stays reserved, no lock leg is
   sized on it, the executor alerts - and a genuine exchange correction is booked only by an
   operator (`kalshi correct` / `OrderLedger.accept_correction`: re-read, final, complete,
   agreeing); `kalshi release` takes only a pending or unknown intent (never an accepted one,
   never one that showed fills) and only from a client provably of the sending account. A missing, failed,
   truncated or wrongly scoped read releases nothing and is read again. Writes are
   compare-and-set on the state (a stale decision never overwrites what another process
   recorded), every time handed to the ledger must be finite (a NaN passes every age check),
   and a 4xx is a definitive refusal only when it answered the request's one and only send
   (`refusal_hint`; `HttpClient` re-sends POSTs). Budgets are sums over the ledger, so they survive restarts and span
   processes. A ledger belongs to one Kalshi account (fingerprints of the key id and of
   `GET /communications/id`, never the identifiers): another account's client is refused,
   and an order is released as never accepted only by a client provably of the account
   that sent it. A lock leg gets the budget exemption only as a proven hedge of its entry,
   checked in two layers: the caller (`LagExecutor.buy_lock`) - a fresh Kalshi quote of the
   entry's market priced at or under its ask, the lock contract's settlement identity from the
   registry, the exchange still showing the entry's contracts; the ledger
   (`OrderLedger._lock_check`, atomic with the reservation) - the entry's fill *verified* by
   reconciliation (a create answer alone is not inventory; a contradicted one never is), same
   market and Kalshi event, the other outcome (no same-side additions), complementary payoffs
   (tie payouts known and summing to $1 where the market can tie), no *related* order of
   unknown outcome (unrelated ones do not stop a hedge), and the remaining inventory after
   exits and earlier lock legs (unverified ones count in full, and a refusal they cause is a
   wait, not the end of the watch), at most a set number of attempts. A resting maker order is read by the process that placed it; a restarted maker
   cancels what a dead one left resting. A sweep counts as a shutdown only when a complete
   listing shows the book empty. Tests never write to `out/` (`tests/__init__.py` points
   `ARB_ORDER_LEDGER_DIR` at a temporary directory).
3b. **Hard production cash ceilings are shared.** `execution/shared_limits.py` enforces
   $25 per reservation including fees and $50 total across production Kalshi and
   Polymarket US, inside one SQLite transaction/file (`kalshi_prod_ledger.sqlite3`).
   No hedge, strategy or daily-budget exemption bypasses these ceilings. US orders
   use `execution/polymarket_us_ioc.py` and `pm_us_*` tables. Only explicit confirmation
   plus `ARB_LIVE_TRADING=1` and `POLYMARKET_US_LIVE_TRADING=1` enables US mutations,
   on the exact `api.polymarket.us` host, without redirects or retries. Unknown sends,
   incomplete reads and contradictory cumulative fills retain their reservation;
   unresolved US orders also block new Kalshi exposure. Partial IOC remainder is cancelled
   only through those gates; only a subsequent final scoped row releases the unfilled
   part. US sends are claimed once, and quote expiry is checked after reservation and
   after that claim. Only a provably unclaimed, never-sent local expiry frees cash;
   US transport also checks that deadline after signing, immediately before its
   single send, and rechecks live gates; a pre-sign check alone is insufficient.
   claimed or unknown intents stay held. Filled inventory remains charged: no inferred settlement, exit or outside-trade
   cash release. Never reset/delete ledgers to restore room.
   Shared sums validate each cash component independently; negative/nonfinite
   amounts never offset other exposure. Legacy `done` rows without explicit cost
   or fees retain their original hold, and contradicted rows cannot release it.
   Restart all production processes on this code and the same `ARB_ORDER_LEDGER_DIR`; old code/separate files
   cannot participate in the shared cap.
   `PairReservationLedger` stages both child terms and entry/contingency cash in
   this same file atomically. Staged children are NOT sendable; no unresolved-order
   exemption is added. Existing executors count parent holds and block new exposure
   while a parent is staged/unresolved. Only expired staging with both children
   still provably unclaimed may release local cash; IDs/one-game fences remain.
   Production `KalshiExecutor.execute` additionally verifies the sending key/account,
   exact pending plan and cash ceilings in that shared ledger, then atomically claims
   one executor send. A raw executor call cannot bypass the ledger or resend a claimed
   intent; demo behavior and the venue fee formulas are unchanged.
   The dedicated `KalshiExecutor.execute(..., one_send=True, not_after=deadline)`
   path additionally requires literal `confirm=True`, a finite unexpired IOC/FOK
   deadline and an account-bound pending reservation even on demo. It bypasses
   the legacy retry/fallback client completely: one stdlib POST, no redirects,
   curl or host fallback, deadline/gates/identity rechecked after signing. It
   durably records acceptance or ambiguity itself; either a crash or a failed
   preparation leaves the send claim/hold and cannot be retried. Its constructor
   accepts `ledger_path`, `clock` and a test-only transport injection. Callers
   remain responsible for quote/settlement proof and final reconciliation; this
   path does not make staged pair children sendable. Other legacy mutation paths
   are unchanged and are not safe substitutes for an autonomous pair transport.
   The US transport also rejects host/key/seed/fingerprint changes during signing.
   `execution/pair_recovery.py` now adds a non-sending durable command outbox in
   the same production file. Entry claims use the policy dispatch guard; hedge
   quantities require final verified US fills; Kalshi verification also requires
   a complete scoped fills list. Counts/money only grow and contradictions stick.
   Unknown claims/IDs block recovery; no resend or absence-based cash release.
   At most two fee/loss-bounded US exit claims use refreshed bid depth at haircut
   0.5 and post-fill account inventory evidence. Duplicate receipt liquidity is
   charged once. Parent cash remains fully held after sales; no proceeds credit.
   Outbox commands explicitly say `send_authorized=False`: production child
   transport/ownership integration is still required. No automatic sender is
   connected, and descriptions/FAQs never become settlement approval.
3c. **Standing permission is not order execution.** `execution/standing_approval.py`
   grants a bounded, expiring policy once, using authenticated Kalshi account/key and US
   sending-key fingerprints. The default `trade-approval arm` is inert; `--confirm` only
   arms permission, never a trading flag or order transport. Exact fresh pairs may be
   approved and consumed once without another prompt; errors never grant permission.
   The spec hash pins planner, quote producers, identity/fee code, venue/settlement
   tables and referenced local rule evidence. Changed files require process restart
   and deliberate re-arming. Receipt deadlines, account changes, revocation and clock
   regressions fail closed. Permission cash is a separate conservative lifetime budget:
   at most $25 on each venue, $50 combined including entry and contingency fees;
   expired/unconsumed permits and re-arming never restore room. It is NOT production
   inventory evidence or a replacement for the shared order ledger. A future paired
   dispatcher must atomically fence generation/revocation/expiry and reserve real cash
   before sending. Already consumed permission is not a revocation-safe dispatch fence;
   `dispatch_guard` serializes the durable NEW-entry claim against revocation;
   lock policy before production, never hold a policy transaction during network I/O.
   Since the stores are separate, a unique parent permit ID and non-retryable
   production send remain mandatory after guard rollback/crash. A later revoke
   does not cancel an already committed claim. Pure `us_pair_orders` helpers send
   nothing; complete account inventory alone does not prove pair-owned excess.
   `reservation_guard` is separate: accounting admission leaves dispatch unclaimed
   so a later revoke still blocks actual submission. Never treat staging as consent
   to dispatch an expired/revoked permit.
   revocation cannot cancel an order already sent. Do not delete either ledger to reset caps.
4. **No secrets in the repo.** Keys live in `.env` (git-ignored) or the shell.
5. **Same-book awareness.** Robinhood re-sells Kalshi's order book for `KX*` contracts.
   Those quotes carry `book_id="kalshi"` and must never be arbed against Kalshi direct;
   they exist for the fee comparison. Rothera (`NFLGAME-…` etc.) is a separate book.
6. **In-play markets are not arbs.** Cross-venue gaps during a live game are staleness.
   Keep the `live` / `in_play` handling in `arb_engine/scanner.py`.
7. **Venue eligibility is a table, not a constant.** `compliance.executable_venues()` reads
   `arb_engine/data/venue_rules.json` (a verified date and a source per row). Polymarket is
   signal-only by default for a US account: its quotes feed the fair value, never an arb
   leg, a maker hedge or an overlay STEAL. `EXECUTABLE_VENUES` is the explicit operator
   override. Every analyzer / scanner call takes the resolved set
   (`scanner.resolve_executable_venues(settings)`); the bridge, `scan`, `live` and `maker`
   must agree.
8. **Shared files are owned by the loaders.** `arb_engine/cli.py` and `arb_engine/config.py`
   are only touched by the plugin loader and the settings registry themselves. A feature
   adds flags / subcommands through a module in `arb_engine/cli_plugins/` and declares its
   settings keys where they are read (`config.declare_setting`). Two work items that both
   edit `cli.py` or `config.py` are a merge conflict by design; two that add a plugin and
   declare their own keys are not.
9. **Replay first; results fixtures are metrics-only.** A change to the WP model, the blend,
   the gates or the fee models is accepted on the replay harness (`backtest`, `backtest-ticks`,
   `lines-eval`), not on a live scan. The numbers land in `tests/fixtures/results/*.json`,
   which are *summaries* (log-loss, Brier, arb minutes, STEAL counts, fee flips) and never
   raw feeds; every table in `docs/` and `README.md` is re-rendered from those files rather
   than typed by hand, so a claim in the docs can always be traced to a fixture and the
   fixture to a replay command. See **Acceptance discipline** below.

## Layout

```
arb_engine/
  config.py    settings registry: declare_setting / setting / KNOWN_SETTINGS / load_settings (see Settings)
  cli.py       argparse root + built-in subcommands; loads cli_plugins/ (never edited by a feature)
  cli_plugins/ one module per feature: register(subparsers, existing_parsers) adds flags/subcommands
  compliance.py venue eligibility table (data/venue_rules.json): executable_venues(), ineligible_reason()
  fees/        venue fee models (Kalshi, Robinhood Rothera/KalshiEX/CDNA, Polymarket, Polymarket US) — Decimal math
  quant/       odds & de-vig, arbitrage evaluation (tie-aware), max-buy price, depth sizing, Kelly, fair value, lines
  venues/      adapters: kalshi.py (public + signed), polymarket.py (Gamma/CLOB), robinhood.py (public web/API), espn.py (live game state), history.py (candles/bars/price history + ESPN play timeline)
  models/      __init__.py = the core dataclasses; wp.py = stdlib win-probability inference (tables in data/nfl_wp_model.json + nfl_wp_rules.json)
  matching/    canonical team/player keys (nfl_teams.json; ncaaf/nba/nhl_teams.json via scripts/build_teams.py --sport …), Eastern-date event keys, cross-venue merge, settlement-rule registry
  scanner.py   sport-wide scan; eventlookup.py single Robinhood event; bridge.py local HTTP server for the overlay
  execution/   Kalshi order plans + gated executor
  strategy/    maker runner (maker.py), in-play lock/steal watcher + execution gates (inplay.py), slate-wide live scanner (live.py), brokers, alerts + journal
  backtest.py  GameReplayer: replay a finished game play-by-play against every price source; tickreplay.py = recorded-tick replay; store.py = SQLite recorder (--record)
  data/        venue_rules.json (eligibility), settlement_rules.json, WP model + rules, margin distributions, team tables
extension/     Chrome MV3 overlay (arb-core.js is the JS twin of fees/ + quant/; background.js consumes the bridge's /analyze and /inplay)
tests/         unittest suite (offline fixtures) + JS tests run by scripts/test_js.sh; fixtures/results/*.json = replay summaries
docs/          VENUES.md (fee facts + sources), SPORTS.md, ARCHITECTURE.md, MODEL.md (measurements), ROADMAP.md, RESEARCH.md + research/ (literature review, ranked candidates)
```

## Commands

```bash
python3 -m unittest discover -s tests -t .     # Python tests (1659)
bash scripts/test_js.sh                        # JS parity + background integration (node or jsc)
python -m arb_engine scan --sport nfl          # live scan (add --books for depth sizing); --sport ncaaf for college football
python -m arb_engine rh-event <robinhood event url>
python -m arb_engine bridge                    # local server the extension uses when running
python -m arb_engine maker --mode paper        # rest Kalshi orders at arb-creating prices (paper by default)
python -m arb_engine live --every 10           # every live game at once: fair per side, cheapest venue, STEAL alerts
python -m arb_engine backtest --week 1         # replay a week: model vs markets, blend fit, STEAL/LOCK simulation
python -m arb_engine record --every 300        # scheduled scans into SQLite; `stats` summarises arb frequency
python scripts/capture_fixtures.py             # refresh offline fixtures from the live APIs
python3 -m arb_engine us-arbs --every 10 --side-cap 25 # read-only US NFL moneyline comparisons; no alerts/orders
```

## The bridge (`arb_engine/bridge.py`)

`GET /analyze?url=…` and `GET /inplay?url=…` on 127.0.0.1:8765 are what the overlay renders in
bridge mode. `extension/background.js` (`fromBridge`, `rowsFromReport`) reads
`analysis.outcomes[].venues[].ineligible` and `analysis.flags` today; `analysis.tie_margin` /
`tie_payout_total`, `view.gated_reasons`, `view.sides[].gated_reasons` / `steal_gated` /
`lock_gated` / `steal_threshold` / `best_ineligible` and `view.executable_venues` are served
beside them for the overlay to render. Rules the bridge keeps (`tests/test_bridge.py` runs the
handler offline):

* every analyzer call passes the environment settings, the resolved executable venues and
  `emit_no_side=True`, so Rothera NO legs (`<contract id>#no`, `meta.side="no"`) are priced
  and Polymarket rows carry `ineligible="not executable"` unless `EXECUTABLE_VENUES` opts in;
* `/inplay` keeps one `FeedFreshness` per event key across polls (`Handler.freshness`) so the
  `feed-stale` / `clock-frozen` / `score-pending` gates work between successive requests,
  and evaluates with `now=time.time()`;
* JSON keys the extension consumes are never renamed; new fields are added beside them
  (`with_overlay_fields`, `with_gate_fields` pin the contract).

## Settings

Every settings key the engine reads is declared **once**, in the module that reads it,
with `config.declare_setting(key, env=..., default=..., cast=..., doc=...)`; the registry
is `config.KNOWN_SETTINGS` (key -> `SettingSpec`, declaration order). Resolution is always

    explicit settings dict  >  environment variable  >  declared default

via `config.setting(settings, key)` (raises `KeyError` for an undeclared key so a typo
surfaces in tests), and `config.load_settings()` (alias `settings_from_env`) bakes every
declared key into the dict handed to each CLI handler and the bridge. Re-declaring a key
identically is a no-op (module reloads); a *conflicting* re-declaration raises so two work
items cannot silently fight over one knob. Modules must import when `config.py` predates the
registry (`try: from .config import declare_setting except ImportError`). Note that
`load_settings()` emits every declared key, so `executable_venues` is `None` when the env
var is unset: `scanner.resolve_executable_venues` and `compliance.executable_venues` read
`None` as *unset* (the table applies) and only the explicit `EXECUTABLE_VENUES=all` (or `*`)
lifts every restriction.

`tests/test_settings_doc.py` imports every module under `arb_engine` and checks this table
against `KNOWN_SETTINGS` both ways: zero undocumented, zero undeclared, env vars matching.
When you declare a key, add its row here.

| Key | Env var | Default | What it does |
|---|---|---|---|
| `robinhood_gold` | `ROBINHOOD_GOLD` | `False` | Price Robinhood's commission at the Gold rate ($0.005 per contract instead of $0.01). |
| `kalshi_rounding` | `KALSHI_FEE_ROUNDING` | `cent` | Kalshi per-order fee rounding: `cent` (up to the cent, conservative) or `centicent`. |
| `polymarket_us_volume_rebate` | `POLYMARKET_US_VOLUME_REBATE` | `0.0` | Polymarket US taker-fee volume rebate as a fraction (0 = none). |
| `polymarket_us_live_trading` | `POLYMARKET_US_LIVE_TRADING` | `0` | US mutations require exactly `1`, `ARB_LIVE_TRADING=1` and explicit confirmation; off by default. No automatic scanner integration. |
| `venue_weights` | — | `None` | `{venue: weight}` override for the consensus fair value; `None` = `quant.fairvalue.DEFAULT_VENUE_WEIGHTS`. Settings dict only. |
| `odds_api_key` | `ODDS_API_KEY` | `None` | The Odds API key for sportsbook consensus (pre-game only); `None` disables it. |
| `rothera_fee_model` | `ROBINHOOD_ROTHERA_FEE_MODEL` | `flat_001` | Rothera exchange fee on Robinhood NFL contracts: `flat_001` ($0.01/contract) or `quadratic` (per order `max(round(0.02·P·(1−P)·C, 2), $0.01)`, Rothera schedule 2026-05-20). |
| `cdna_fee_model` | `CDNA_FEE_MODEL` | `flat_001` | CDNA exchange fee on Robinhood college contracts: `flat_001`, `flat_002` or `weighted_007` (0.07·P·(1−P)·C); unverified until an order ticket is seen. |
| `rothera_no_leg` | `ROTHERA_NO_LEG` | `True` | `scan()`: emit the NO side of each Rothera game contract as its own leg (the tie-aware hedge). |
| `arb_prefer_tie_safe` | `ARB_PREFER_TIE_SAFE` | `True` | In a game that can tie (NFL moneyline), when the cheapest lock loses on a tie (Kalshi YES + Rothera YES pays $0.50 a set), use the cheapest tie-proof set (e.g. the Rothera NO) while it still locks `arb_tie_safe_min_margin`; the ticket says `guaranteed:` or `NOT tie-proof:`. |
| `arb_tie_safe_min_margin` | `ARB_TIE_SAFE_MIN_MARGIN` | `0.01` | Smallest margin (dollars per contract, fees in) at which the tie-proof set is preferred over a cheaper lock that loses on a tie. |
| `line_fair` | `LINE_FAIR` | `False` | Attach `quant.lines` fair values to spread/total events in `scan()` and use them in play (one knob for both). |
| `tennis_thin_book_spread` | `ARB_TENNIS_THIN_SPREAD` | `0.03` | Tennis `thin-book` flag when abs(yes_ask + no_ask − 1) exceeds this. |
| `tennis_thin_book_size` | `ARB_TENNIS_THIN_SIZE` | `20.0` | Tennis `thin-book` flag when the top-of-book size is below this many contracts. |
| `tennis_walkover_p_tour` | `ARB_TENNIS_WALKOVER_P_TOUR` | `None` | Override the tour-tier walkover probability from `settlement_rules.json`. |
| `tennis_walkover_p_challenger` | `ARB_TENNIS_WALKOVER_P_CHALLENGER` | `None` | Override the challenger-tier walkover probability from `settlement_rules.json`. |
| `tennis_walkover_p_itf` | `ARB_TENNIS_WALKOVER_P_ITF` | `None` | Override the ITF-tier walkover probability from `settlement_rules.json`. |
| `history_cache_dir` | `ARB_HISTORY_CACHE_DIR` | `out/cache/history` | Read-through cache for backtest history (Kalshi candles, Polymarket, Robinhood, ESPN). |
| `backtest_bar_mode` | `ARB_BACKTEST_BAR_MODE` | `before` | Market-bar alignment for the replay headline: `before` or `after` the play. |
| `executable_venues` | `EXECUTABLE_VENUES` | `None` | Comma list of venues this account can execute on; overrides `data/venue_rules.json` (table default: kalshi, robinhood); `all` lifts every restriction. |
| `TRADES_CACHE_DIR` | `ARB_TRADES_CACHE_DIR` | `out/cache/trades` | Read-through cache for public trade tapes (event studies). |
| `TICK_REPLAY_STALE_AFTER_S` | `ARB_TICK_REPLAY_STALE_AFTER_S` | `15.0` | Fallback feed-stale threshold for `backtest-ticks` when the live gates are absent. |
| `kalshi_gtd_horizon_s` | `KALSHI_GTD_HORIZON_S` | `3600.0` | Seconds a resting Kalshi order may live before the exchange expires it (capped at kickoff). |
| `wp_kneel_floor_enabled` | `ARB_WP_KNEEL_FLOOR` | `None` | Override `nfl_wp_rules.json` `kneel_floor.enabled` (1/0); unset keeps the table value. |
| `inplay_stale_after_s` | `INPLAY_STALE_AFTER_S` | `15.0` | Seconds without an ESPN state change (while a venue mid moved ≥ 0.02 over the last `max(interval, 10 s)`) before STEAL/LOCK are gated `feed-stale`. |
| `inplay_frozen_s` | `INPLAY_FROZEN_S` | `None` | Seconds of identical ESPN state with the clock running and a venue mid moved ≥ 0.02 since, before STEAL/LOCK are gated `clock-frozen`; unset = `max(3 × poll interval, 30 s)`. |
| `inplay_agreement_gap` | `INPLAY_AGREEMENT_GAP` | `0.12` | Model-vs-market gap above which a STEAL is gated `disagreement` when ESPN's win probability sides with the market (provisional; measure on the first recorded Sunday). |
| `inplay_idle_every_s` | `INPLAY_IDLE_EVERY_S` | `60.0` | Live slate: seconds between ticks while no game is live or within the pre-game window (a recorder can run all week). |
| `lag_lock_watch_s` | `LAG_LOCK_WATCH_S` | `600.0` | LAG lock watch: seconds after a LAG position fills during which the other outcome is watched for a price that locks the pair. |
| `lag_lock_tie_safe` | `LAG_LOCK_TIE_SAFE` | `True` | LAG lock watch: only lock pairs that pay at least $1 on a tie (a Kalshi YES + a Rothera YES pays $0.50). |
| `arb_stake_fraction` | `ARB_STAKE_FRACTION` | `0.2` | Share of the bankroll a BIG ARB ticket is sized to, fees in (a locked set holds its cost until the game ends; on one $500 bankroll over 2026-09-20/21, 20 % per BIG ARB made $187 vs $92 all-in and $111 at 50 %). |
| `arb_stake_fraction_arb` | `ARB_STAKE_FRACTION_ARB` | `0.05` | Share of the bankroll a 1-3c ARB ticket is sized to (legged by hand that tier returned -0.5 % per dollar, Kelly 0; 20 % BIG + 5 % ARB made $169 vs $80 at 20 % on every tier). 0 = do not alert that tier. |
| `arb_push_style` | `ARB_PUSH_STYLE` | `short` | What an ARB / ARB CLOSE push shows on the phone: `short` (each leg's venue, shares, side and price, the cost and profit, a tie warning when a tie loses; Kalshi / Robinhood buttons) or `full` (the whole itemised ticket). The journal always keeps the full ticket. |
| `arb_button_mode` | `ARB_BUTTON_MODE` | `paper` | The "Robinhood done" button on Kalshi + Robinhood ARB pushes (`strategy/arbbutton.py`): `off`; `paper` (practice - at the tap, both venues' live prices are re-read and checked against the alert and the Kalshi buy is simulated against Kalshi's live order book; nothing is sent); `demo` (the Kalshi order goes to the demo exchange); `live` (a real immediate-or-cancel order; also needs `ARB_LIVE_TRADING=1` and the production key). Results come back as ARB FILL pushes. |
| `arb_button_auto_practice_s` | `ARB_BUTTON_AUTO_PRACTICE_S` | `10.0` | Seconds after a real Kalshi + Robinhood arb's button is issued at which a practice tap is simulated: live prices re-read, the Kalshi buy walked against the live book, journalled to `out/orders/arb_button.jsonl` (`scripts/arb_button_report.py` summarises it); never pushed, never an order, and your own tap still works. 0 = off. |
| `arb_confirm_book` | `ARB_CONFIRM_BOOK` | `True` | Before a Kalshi + Robinhood ARB / BIG ARB is pushed, re-read Kalshi's live order book and Robinhood's live quote (the button's tap check, nothing sent). A set that would not lock there at full size is journalled as ARB GONE and not pushed, and that market is not re-checked for 5 s. Kalshi's `/markets` price, which the sweep and the fast lane read, trails its book by 5-10 s while a game moves (2026-09-26: 34 % of busy college reads disagreed). A read that fails pushes as before. |
| `arb_suspect_margin` | `ARB_SUSPECT_MARGIN` | `0.15` | An arb wider than this (dollars per contract, fees in) is journalled as ARB SUSPECT and not pushed (on the 2026-09-24/26 slates such gaps were frozen or mismatched quotes). |
| `arb_max_quote_lag_s` | `ARB_MAX_QUOTE_LAG_S` | `30` | During play, an arb with a leg whose own venue timestamp is older than this is ARB SUSPECT, not pushed (Robinhood's college quotes can sit frozen while the game moves). |
| `arb_push_min_margin` | `ARB_PUSH_MIN_MARGIN` | `0.01` | Live slate: smallest ARB margin (dollars per contract, fees in) that is pushed; smaller ones are journalled as ARB SMALL (replayed by hand, arbs under 1c lost money). |
| `arb_big_margin` | `ARB_BIG_MARGIN` | `0.03` | Live slate: ARB margin from which the push is titled BIG ARB at top priority (replayed by hand, arbs of 3c+ made money). |
| `arb_near_margin` | `ARB_NEAR_MARGIN` | `0.03` | Live slate: how far below a lock (dollars per contract, fees in) still earns an ARB CLOSE alert — the buffer that says "this pair is about to cross". |
| `arb_near_every_s` | `ARB_NEAR_EVERY_S` | `900.0` | Seconds before the same event may send another ARB CLOSE unless the gap shrank by two cents (at 300 s / one cent the college slate sent 279 ARB CLOSE pushes on 2026-09-26 and used up the free ntfy.sh quota). Low-priority pushes (ARB CLOSE, FINAL, LAG...) also stop once fewer than `ARB_ALERT_NTFY_RESERVE` (80) of ntfy's daily messages remain. |
| `leadlag_move` | `LEADLAG_MOVE` | `0.05` | Lead-lag: leader mid move (dollars) within the window that counts as a repricing. |
| `leadlag_window_s` | `LEADLAG_WINDOW_S` | `30.0` | Lead-lag: seconds over which the leader's move and the follower's (non-)move are measured. |
| `leadlag_min_edge` | `LEADLAG_MIN_EDGE` | `0.02` | Lead-lag: minimum leader mid minus follower all-in ask to signal LAG. |
| `leadlag_cooldown_s` | `LEADLAG_COOLDOWN_S` | `60.0` | Lead-lag: seconds before the same (event, follower, side) may signal again unless the edge grew. |
| `leadlag_leaders` | `LEADLAG_LEADERS` | `robinhood,kalshi` | Lead-lag: venues whose repricing may lead a signal (Polymarket is thin and sometimes stale; measure before adding it). |
| `alert_ntfy` | `ARB_ALERT_NTFY` | `None` | ntfy topic name (on ntfy.sh) or full `https://host/topic` URL that receives ARB / LAG / HEDGE NOW pushes. |
| `alert_ntfy_kinds` | `ARB_ALERT_NTFY_KINDS` | `ARB,LAG,HEDGE NOW,TAKER ARB,EXCHANGE PAUSED,HEDGE VENUE NOT EXECUTABLE,FINAL` | Comma list of alert titles pushed to ntfy (add `STEAL` / `LOCK NOW` to opt in). |
| `alert_min_interval_s` | `ARB_ALERT_MIN_INTERVAL_S` | `60.0` | Seconds between two pushes for the same (title, event, side); HEDGE NOW is never throttled. |
| `inplay_quiet` | `INPLAY_QUIET` | `False` | `live`: print only STEAL / LOCK / GATED lines and a one-line summary per tick (`--quiet`). |
| `inplay_delay_haircut_cdna` | `INPLAY_DELAY_HAIRCUT_CDNA` | `0.02` | Extra edge a STEAL on a CDNA-routed Robinhood contract needs, for its 3 s order delay. |
| `inplay_slate_cap` | `INPLAY_SLATE_CAP` | `None` | Dollars the live slate may deploy per tick across every STEAL (default: the bankroll); stakes scale proportionally. |
| `maker_hedge_cash` | `MAKER_HEDGE_CASH` | `250.0` | Max dollars of hand-executed hedge legs the maker may leave resting at once (sum of size × hedge ask). |
| `maker_hedge_venues` | `MAKER_HEDGE_VENUES` | `robinhood` | Comma list of maker hedge venues; naming `polymarket` is the explicit opt-in to a non-executable hedge. |
| `order_ledger_dir` | `ARB_ORDER_LEDGER_DIR` | `out/orders` | Directory of the durable Kalshi order ledgers (`execution/ledger.py`): one SQLite file per environment (`kalshi_<env>_ledger.sqlite3`), shared by every process that sends orders. Tests always pass their own path. |
| `auto_pair_mode` | `ARB_AUTO_PAIR_MODE` | `off` | Dedicated `auto-arb` two-venue runner: `off` or automatic `paper` mode only. `live` reports missing production-pair/settlement capabilities and cannot submit orders, even when manual trading flags are enabled. Paper accounting uses a separate `auto_pair_paper.sqlite3` in the ledger directory. |

| `arb_button_side_cap` | `ARB_BUTTON_SIDE_CAP` | `25` | Maximum dollars per leg of an arbitrage-button ticket, including conservative split-fill fees at the allowed prices. Kalshi rechecks the exchange fee multiplier before sending. Robinhood remains a manual suggested leg; this is not a daily or account-wide cap. |

## CLI plugins (`arb_engine/cli_plugins/`)

Polymarket US account credentials are optional and separate from public scanning.
`python3 scripts/polymarket_us_account.py setup` accepts a hidden Terminal paste and saves
`POLYMARKET_KEY_ID` / `POLYMARKET_SECRET_KEY` in git-ignored `secrets/polymarket_us.env`
(600). These are script credentials, not engine settings. `check` signs only
`GET https://api.polymarket.us/v1/account/balances`, refuses redirects, and prints a
connection summary, not account details or secrets. The separate `us-ioc` plugin reads
the same credentials (or both exported credential variables); signing uses the existing
optional `cryptography` installation.

`us-ioc order --market-slug SLUG --side yes|no --count N --limit P` previews a US IOC
without an account read, ledger write or submission. `--limit` is the purchased side's
cost; the API payload always expresses the long/YES instrument's price. Confirmed orders
re-read NFL identity, kickoff, OPEN book/grid and buying power, then reserve before
sending. `--request-id` is local deduplication, not exchange idempotency: keep it for
recovery, never replace it to retry an unknown send. `ledger` reports both venues' cash
total; `reconcile [--intent-id ID]` reads known US order IDs and updates accounting;
adding `--confirm` permits cancellation of remaining IOC quantity through the gates.
Final REST rows must explicitly state quantities, average long price and commission.
Missing fees are unknown, not zero. A lost exchange ID, crash between reservation and
send, or sticky contradiction requires operator investigation; no automatic activity
matching or unsafe force-release exists. This is a manual buy executor, not automatic
two-venue buying or unwind; unknown settlement still excludes guaranteed-arbitrage claims.

`trade_approval.py` registers `trade-approval arm|status|revoke`. `arm` and `revoke`
are dry-run unless `--confirm`; `status` is read-only and creates no store or account
client. Confirmed arming uses only signed GETs on the fixed production Kalshi account
identity endpoint and Polymarket US balances. Private credential files are never sourced
as shell code. `--hours` defaults to 6 and must be in (0,24]; file overrides are
`--kalshi-env-file` (default `~/.kalshi/prod.env`) and `--us-env-file` (default
`secrets/polymarket_us.env`). Permission is stored privately in
`ARB_ORDER_LEDGER_DIR/standing_approval.sqlite3`, with FULL synchronous DELETE-mode
SQLite transactions. The resolved scope is frozen to Kalshi + Polymarket US; no settings
keys are added. `auto-arb --mode live` reports its read-only permission status but remains
`BLOCKED` even when permission is `ARMED` or manual live flags are enabled. This command
does not switch the default Kalshi demo environment or reconfigure running workers.

`us_arbs.py` adds the **read-only** `us-arbs` command (NFL full-game moneylines):
`--every` defaults to 0 (one scan; repeated scans require at least 5 seconds),
`--contracts` defaults to 100 whole contracts, `--side-cap` to $25 including fees,
`--max-quote-age` to 6 seconds, and `--min-margin` to 0; `--json` emits one report per scan.
There are no new environment settings. Polymarket US's venue table row has
`requires_opt_in: true`: existing live/maker defaults are unchanged. Unknown settlement
produces conditional price candidates, never guaranteed-payoff claims or orders.

`cli.build_parser` imports every public module of the package in name order (`_private`
modules skipped) and calls its `register(subparsers, existing_parsers)`:

* `subparsers` is the root parser's argparse sub-parser action; `existing_parsers` maps
  subcommand name -> `ArgumentParser` for every subcommand registered so far (built-ins
  first, then earlier plugins in name order). Add flags to an existing subcommand through
  `existing_parsers["scan"].add_argument(...)`, or add a whole subcommand with
  `subparsers.add_parser(...)` + `set_defaults(func=handler)`.
* Flag names must be unique (`--<feature>-...`): argparse raises on a duplicate and the CLI
  reports which plugin did it.
* Return `None` or `{subcommand: handler}` to override the dispatch of an existing
  subcommand (wrap the built-in: `cli.cmd_scan` etc.). Handlers take `(args, settings)` with
  `settings = config.load_settings()`; a one-argument `handler(args)` is also accepted. The
  last plugin in name order wins an override.
* Settings keys a plugin needs are declared where they are read, never in `cli.py`.
* A plugin that fails to import or register is reported on stderr and skipped, so one broken
  feature cannot take down `scan`; guard cross-item imports (`try/except ImportError`) and
  keep flag defaults inert. With no plugins present the CLI is byte-for-byte what it was
  (`tests/test_cli_registry.py` pins every built-in `--help` in `tests/fixtures/cli_help`).

## Acceptance discipline: replay first

* A model, blend, gate or fee change is judged on replays of finished games
  (`backtest --week N`, `backtest-ticks` on recorded ticks, `lines-eval`, the eligibility and
  fee-flip reports), with placebos and the alignment controls the harness provides — never
  on a handful of live scans.
* The replay writes a **metrics-only** summary to `tests/fixtures/results/<name>_<item>.json`
  (per-source log-loss / Brier, arb minutes, STEAL ladders, gate counts, fee flips, sample
  sizes). No raw feeds, quotes or play-by-play go in there; the offline inputs live under
  `tests/fixtures/{espn,nflverse,ticks,trades,history}`.
* Every table in `README.md` / `docs/*.md` is rendered from a results fixture; cite the
  fixture name next to the table. Change the fixture, re-render the table; never edit a
  number in the docs by hand. A test that loads the fixture and re-checks its headline
  numbers against the code (`tests/test_fees.py` -> `fee_flip_p10.json`, `tests/test_scanner.py`
  -> `arb_fixture_p09.json`, `tests/test_eligibility_impact.py`, `tests/test_feedparity.py`,
  `tests/test_wp_model.py`) is the acceptance test for the item.
* Refresh the test count in **Commands** whenever you add tests (`python -m unittest discover
  -s tests -t . 2>&1 | tail -3`).

## Conventions

- Prices are dollars per $1-payout contract (`0.52`), never cents, in Python and JS.
- `OutcomeQuote.ask` is always "what you pay to buy this outcome" — adapters fold NO-side
  quotes into the other outcome (a Rothera NO leg keeps `meta.side="no"` and
  `meta.no_of=<team>` so tie payouts and same-book de-dupe stay correct).
- Event keys: `nfl:<CODE>|<CODE>:<YYYY-MM-DD ET>`, `tennis:<surname>|<surname>:<date>`,
  lines append `:spread:<FAV>-<line>` / `:total:<line>` (half-point lines; match on the
  numeric line, never on venue ticker suffixes — Kalshi uses ⌈line⌉, Rothera ⌊line⌋).
- Money math in `Decimal` (Python) / `BigInt` (JS). Do not introduce float rounding into fees.
- Keep functions small and tested; fixtures are trimmed live responses in `tests/fixtures`.
  Tests never touch the network: adapters take a `FakeHttp` / `SequencedFakeHttp`
  (`tests/helpers.py`), the bridge handler is driven without a socket.

## Good next tasks

See `docs/ROADMAP.md`. Highest value: Polymarket CLOB order placement (py-clob-client), a
websocket feed for Kalshi/Polymarket, and sportsbook de-vigged consensus via The Odds API.

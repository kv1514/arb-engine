# arb-engine

Fee-aware arbitrage and fair-value engine for **sports prediction markets**, plus a Chrome
overlay for Robinhood's event contracts. Focus: **NFL** (moneylines, spreads and totals —
every line) and **tennis** (match winners).

Venues today: **Kalshi** (direct API, public + signed), **Polymarket** (Gamma/CLOB public
data), **Robinhood** event contracts (public web/API data; contracts route to **Rothera**,
KalshiEX, ForecastEX or Nadex). Every price is compared *after* each venue's real fee
schedule (`docs/VENUES.md`), because a 1¢ gross gap on a 50¢ contract is a loss once
Kalshi's 1.75¢ taker fee or Robinhood's 2¢ is paid.

```
$ python -m arb_engine scan --sport nfl --cross-only --books
# NFL scan  venues=kalshi,polymarket,robinhood  events=1038 {'total': 518, 'spread': 488, 'moneyline': 32} (live/in-play: 0)  arbs(margin>0.00%, pre-game only)=16

[total] SEA @ ARI total 63.5 (over / under)  [nfl:ARI|SEA:2026-09-20:total:63.5]  venues=kalshi,robinhood
  sum-of-asks=0.960  fee-adjusted margin=  2.1% per $1 payout  | fillable: 450 contracts -> profit $2.85 (0.64% on $447.15)  flags=depth-checked
  Over 63.5              fair=0.064  best=robinhood  all-in=0.054  edge=  1.1%
      robinhood/rothera    ask=0.040 bid=0.030 fee/ct=0.014 all-in=0.054 max-buy(taker)=0.050 max-buy(maker)=0.050 size=5000.0
      kalshi               ask=0.090 bid=0.080 fee/ct=0.006 all-in=0.096 max-buy(taker)=0.070 max-buy(maker)=0.070 size=5020.0
  Under 63.5             fair=0.936  best=kalshi     all-in=0.925  edge=  1.0%
      kalshi               ask=0.920 bid=0.910 fee/ct=0.005 all-in=0.925 max-buy(taker)=0.940 max-buy(maker)=0.940 size=200.0
      robinhood/rothera    ask=0.970 bid=0.960 fee/ct=0.013 all-in=0.983 max-buy(taker)=0.920 max-buy(maker)=0.920 size=500.0
  legs@100: robinhood:Over 63.5@0.04(fee 1.39) + kalshi:Under 63.5@0.92(fee 0.52)  => cost $97.91 for $100 payout, profit $2.09
```

Where the arbs live today: the far tails of totals and spreads, where the quadratic fee is
smallest and Rothera's market maker quotes differently from Kalshi's. They are small
(≈1% on capital, a few dollars each at the available depth) but real and repeatable; the
moneylines are efficient to within fees.

`max-buy` is the number the overlay is built around: the highest price you can pay for this
outcome **on this venue** such that buying the other outcome at its cheapest current ask
elsewhere still locks in your target margin after all fees — as an order that crosses the
book (taker) or as a resting order (maker, lower fees on Kalshi, none on Polymarket).

## What is in the box

Sports: NFL (moneylines, spreads, totals), **college football** (`--sport ncaaf`: Kalshi ×
Polymarket × Robinhood/CDNA, 761 programs), NHL and NBA (`--sport nhl|nba`, team tables ready
for the season) — see [docs/SPORTS.md](docs/SPORTS.md); tennis with per-venue settlement rules.

| Piece | What it does |
|---|---|
| `arb_engine/fees` | Exact (`Decimal`) fee models: Kalshi taker/maker with series multipliers, Robinhood commission (Gold/no Gold) + exchange fee with opt-in Rothera / CDNA schedules, Polymarket sports schedule, Polymarket US theta. Tested against the venues' own tables and worked examples. |
| `arb_engine/quant` | Odds conversion & de-vig (multiplicative / power / Shin / additive), tie-aware fee-adjusted arb evaluation, max-buy price on the tick grid, depth-limited sizing with venue minimums, Kelly + hedge Kelly, consensus fair value, spread/total fair values from margin distributions (`lines.py`), calibration statistics (`calibration.py`: game-cluster bootstrap, isotonic, games-needed), event study. |
| `arb_engine/compliance.py`, `data/venue_rules.json`, `data/settlement_rules.json` | Which venues a US account can execute on (Polymarket is signal-only by default; `EXECUTABLE_VENUES` overrides) and how each venue settles ties, postponements, cancellations, walkovers and retirements, with the rule text pinned by sha256. |
| `arb_engine/venues` | Adapters for Kalshi, Polymarket, Robinhood — moneylines, and every NFL spread/total line (one binary market per line on all three venues); Kalshi RSA-PSS signing for portfolio/orders. |
| `arb_engine/matching` | Canonical NFL team codes (every venue's spelling), tennis surname keys, Eastern-date event keys with market type + line (`nfl:BUF|DET:2026-09-17:spread:BUF-1.5`), cross-venue merge. |
| `arb_engine/scanner.py` | Sport-wide scan: merge → fees → eligibility → arbs/edges/max-buy; an arb must be **fillable** (≥ 1 contract at quoted depth); `--books` runs a second pass with real order books for candidate events and reports the profit-maximising size. Flags live matches, stale snapshots, thin quotes, same-book quotes, tie-rule mismatches, below-minimum legs, settlement mismatches and signal-only venues. |
| `arb_engine/eventlookup.py`, `bridge.py` | One Robinhood event across venues; local HTTP server for the overlay. |
| `arb_engine/execution` | Kalshi order plans + executor with three safety gates (dry-run → demo → prod needs `ARB_LIVE_TRADING=1`). |
| `arb_engine/venues/espn.py`, `arb_engine/models/wp.py` | Live game state from ESPN (score, clock, situation, timeouts, ESPN WP, sportsbook line) behind a `StateGuard` that holds impossible score changes, and the in-game win-probability model (XGBoost on nflverse play-by-play, stdlib inference, dead-ball rules table; `scripts/train_wp_model.py` retrains). |
| `arb_engine/strategy` | **Maker runner**: rests post-only Kalshi orders (with an exchange-side expiry) at the price where a fill *creates* an arb against the cheapest *executable* hedge, re-prices/cancels as the hedge moves, fires a HEDGE-NOW alert (bell, macOS notification, webhook, JSONL journal) with the exact hedge instruction, and cancels everything on a pause or a kill. **In-play watcher / live slate**: fair per side from model + market + ESPN, STEAL / LOCK NOW behind feed-freshness gates (a stale or suspect feed downgrades the alert to `GATED`). Paper broker, demo and live Kalshi brokers. |
| `arb_engine/backtest.py`, `tickreplay.py`, `store.py` | Replay a finished week against Kalshi candles under both alignments, Polymarket history, ESPN and the model with game-cluster intervals and placebo-checked STEAL/LOCK simulations; replay recorded ticks through the watcher with the gates on/off; SQLite recorder for scans, in-play ticks and STEAL observations. |
| `extension/` | Chrome MV3 overlay for `robinhood.com/us/en/prediction-markets/…/events/…`: panel + badges with fair value, all-in cost, edge, max-buy; direct mode or via the bridge. |
| `.mcp.json` | Wires the [mcp-server-kalshi](https://github.com/9crusher/mcp-server-kalshi) MCP server so Claude Code / Codex can browse and (with your keys) trade Kalshi. |

## Install

Python ≥ 3.10, no third-party packages needed for scanning.

```bash
git clone https://github.com/kv1514/arb-engine && cd arb-engine
pip install cryptography          # only for authenticated Kalshi calls
cp .env.example .env              # optional: Kalshi keys, Gold flag
python -m unittest discover -s tests -t .
```

## Use

```bash
python -m arb_engine scan --sport nfl --cross-only    # every moneyline/spread/total line, pre-game fillable arbs
python -m arb_engine scan --sport nfl --cross-only --books --json out/nfl.json   # + real depth for candidates
python -m arb_engine scan --sport nfl --markets moneyline --all --limit 20
python -m arb_engine scan --sport tennis --cross-only --all --limit 10
python -m arb_engine quote --sport nfl "BUF|DET" --markets moneyline   # one game, every venue
python -m arb_engine rh-event "https://robinhood.com/us/en/prediction-markets/nfl/events/september-20-philadelphia-vs-tennessee-sep-20-2026/"
python -m arb_engine rh-event "https://robinhood.com/us/en/prediction-markets/nfl/events/september-20-carolina-vs-atlanta-totals-sep-20-2026/" --all   # every line of a spread/total page
python -m arb_engine fees --venue robinhood --price 0.52 --contracts 100 --gold
python -m arb_engine kelly --bankroll 2000 --fair 0.58 --cost 0.53
python -m arb_engine bridge                           # serves the overlay on 127.0.0.1:8765
```

### In-play: live game state + win-probability model

```bash
python -m arb_engine games                       # this week's NFL games: status, score, situation, spread, model P(home)
python -m arb_engine inplay "https://robinhood.com/us/en/prediction-markets/nfl/events/<game>/" --position robinhood:DEN:0.50:100 --once
python -m arb_engine inplay "<game url>" --position robinhood:DEN:0.50:100 --position robinhood:DEN:0.40:100   # keeps watching
python -m arb_engine live --every 10 --pre-hours 1 --record out/history.db   # the whole slate: every live game, STEAL alerts, ticks recorded
python -m arb_engine live --sport ncaaf --once --pre-hours 6    # college football: same thing for Saturday (65 games matched on 2026-09-18)
python -m arb_engine live --every 10 --bankroll 500 --slate-cap 200 --stale-after 15 --cdna-haircut 0.02 --allowed-venues executable
```

`live` is the Sunday mode: one venue pull and one ESPN scoreboard call per tick, a summary
refresh per live game every 30 s, then the same evaluation as `inplay` for every game at
once (no lots) — fair per side with its three sources, the cheapest venue's all-in, and a
`STEAL` alert the first time a side sits below fair by `--steal-edge` with the model agreeing.

Every tick pulls the venues' quotes **and** ESPN's live game state (score, clock,
possession, down & distance, field position, timeouts, ESPN's own win probability, the
DraftKings line) and scores our **in-game win-probability model** on it. The model is an
XGBoost stack trained on nflverse play-by-play 2016–2024; on the held-out 2025 season it
matches nflfastR's `vegas_wp` (log-loss 0.475 vs 0.477, Brier 0.158 vs 0.159, ECE 0.02) —
see [docs/MODEL.md](docs/MODEL.md). Inference is stdlib (`arb_engine/models/wp.py`).

Fair value in play = blend of **model** (0.55), **market consensus** (0.30, scaled down
when the best book is wide) and **ESPN** (0.15), renormalised over what is available;
pre-game the market alone (with the sportsbook moneyline de-vigged in). The weights and the
policy for changing them are in [docs/MODEL.md](docs/MODEL.md). Per side it prints
what you hold and its all-in average, the fair with its three sources, the cheapest venue,
and — for the side you are short of — the **lock price**: the most you can pay (fees
included) so the pair pays $1 either way for less than you spent. Alerts `LOCK NOW` when
that price is available (with what holding is worth at fair beside it) and `STEAL` when a
side is below fair by `--steal-edge` **and the model agrees** (a stale quote on one venue
cannot trigger it alone). Both pass through **feed gates**: no ESPN change while a venue mid
moves (`feed-stale`), a frozen clock, an old quote, a score whose play has not been published
or a `suspect` state downgrade the alert to `GATED … wait: <reason>`; a CDNA-routed contract
needs 2 extra points of edge for its order delay. Plain-English fee mechanics and the
Chiefs/Broncos worked example: [docs/FEES_EXPLAINED.md](docs/FEES_EXPLAINED.md).

### Backtest: replay a finished week against every price source

```bash
python -m arb_engine backtest --week 1 --season 2026 --bar-mode both --slices --placebo --results-json out/w1.json   # the documented run
python -m arb_engine backtest --sport ncaaf --week 2 --season 2026 --bar-mode both --slices --placebo             # college (86 games)
python -m arb_engine backtest --week 2 --offline --cache-dir out/cache/history                                    # rerun from the cache, no network
python -m arb_engine backtest --pool 'out/week*.json'                                                            # pooled weeks: intervals, games needed, walk-forward fit
python -m arb_engine backtest --espn 401872932 --rh-home <contract id> --rh-away <contract id> --pm-away <token id> --json out/backtest.json
python -m arb_engine lines-eval --week 1 --season 2026                 # spread/total fair values vs Kalshi mids, both alignments
python -m arb_engine record --sport nfl --every 300 --books            # keep scanning on a schedule (Ctrl-C or --hours N)
python -m arb_engine stats --db out/history.db --convergence           # arb share by market × hours-to-kickoff; STEAL ladder convergence
python -m arb_engine backtest-ticks --db out/live.db --gates both      # a recorded game through the watcher, gates on/off, CLV, settled P&L
python -m arb_engine event-study --rows out/w1_full.json --trades 'kalshi=KXNFLGAME-26SEP14DETBUF-BUF@DET|BUF'   # absorption of model moves
python -m arb_engine clv --db out/live.db                              # closing-line value of recorded STEALs
```

`--week N` replays every final of a week with Kalshi (1-minute candles under **two
alignments**: the last candle before the play and the first after it), Polymarket (token ids
from the Gamma slug), ESPN and the model, classifies every play, pools the scores three ways
(all rows, in play, in-play scrimmage — the headline), fits the blend weights, and puts a 90 %
game-cluster bootstrap interval on every comparison. **NFL 2026 week 1, 16 games, 2,263 in-play
scrimmage plays** (P(home) per play, lower is better):

<!-- results:nfl_w1_pooled -->
| source (P(home) per play) | games | scrimmage plays | log-loss | Brier | log-loss, all in-play rows | log-loss, all rows |
|---|---|---|---|---|---|---|
| model (state before the play) | 16 | 2,263 | 0.4060 | 0.1336 | 0.3993 | 0.3974 |
| model (state after the play) | 16 | 2,263 | 0.4022 | 0.1323 | 0.3956 | 0.3937 |
| ESPN win probability | 16 | 2,263 | 0.4205 | 0.1391 | 0.4132 | 0.4109 |
| Kalshi mid, last candle before the play | 16 | 2,261 | 0.4425 | 0.1472 | 0.4332 | 0.4311 |
| Kalshi mid, first candle after the play | 16 | 2,260 | 0.4382 | 0.1456 | 0.4290 | 0.4267 |
| Polymarket last trade | 16 | 2,263 | 0.4415 | 0.1471 | 0.4318 | 0.4297 |
| market consensus | 16 | 2,263 | 0.4417 | 0.1471 | 0.4322 | 0.4301 |
| blend 0.30 market / 0.55 model / 0.15 ESPN | 16 | 2,263 | 0.4173 | 0.1378 | 0.4097 | 0.4077 |
| blend, post-play state and candle | 16 | 2,263 | 0.4146 | 0.1368 | 0.4074 | 0.4054 |
| Kalshi, book ≤ 4¢ wide | 16 | 2,048 | 0.4875 | 0.1625 | 0.4837 | 0.4834 |
| model on those same plays | 16 | 2,048 | 0.4478 | 0.1475 | 0.4450 | 0.4448 |
<!-- /results:nfl_w1_pooled -->

The model scores **0.406 against Kalshi's 0.438–0.443** (the truth is inside that range) with
a game-cluster interval that excludes zero; on the plays where Kalshi's book was ≤ 4¢ wide the
gap is wider (0.488 vs 0.448). The current blend is *worse* than the model alone on this week
(0.417, interval excludes zero) but college week 2 (86 games) shows no difference and 185
earlier college games favoured the blend, so the weights stay put until the season-to-date
interval or two consecutive weeks say otherwise. The same run simulates the watcher's rules
against Kalshi's asks (10 contracts, taker fees, one STEAL per game) under honest pairings and
two placebos: on the NFL every hold-to-settlement P&L interval includes zero and the
shuffled-outcomes placebo produces comparable ROIs, so **the STEAL edge is not demonstrated on
the NFL yet** (roughly 70 games are needed at these effect sizes); on college the real pairing
is +3.5 % / +7.1 % / +5.5 % ROI at 3 / 5 / 8 % edges against a placebo of −23 % / −22 % /
−13 %, intervals still including zero. Every break-even LOCK variant loses on the NFL week
(−4.7 % to −13 %); on college it loses at 2–6 % edges and is positive only at the 10 % edge
(+6.3 % on 25 games). All of it, with the
per-quarter, per-class, ESPN-timing, feed-parity and line-evaluation tables, is in
[docs/MODEL.md](docs/MODEL.md); the tables are rendered from the committed fixtures
(`python scripts/render_results.py --check`, [docs/results/README.md](docs/results/README.md)).

`backtest --espn <id>` replays one game (DET @ BUF 2026-09-17, 189 plays: model 0.070,
Robinhood 0.078, ESPN 0.079, blend 0.078, Kalshi 0.087, Polymarket 0.089 log-loss under the
old after-candle alignment) and counts the minutes a fee-aware arb existed inside Kalshi's book
and across Kalshi × Robinhood (Robinhood history is trades, not the book; Robinhood contract
ids are only known for open events, so `--week` runs without it). `arb_engine/store.py` holds
the SQLite schema (`scans`, `quotes`, `inplay_ticks`, `espn_ticks`, per-venue L1 ticks,
`steal_observations` with a +10 s … +15 min ladder).

### Maker runner

```bash
python -m arb_engine maker --sport nfl --markets total,spread --mode paper --min-margin 0.005 --duration 3600
python -m arb_engine maker --sport nfl --mode paper --hedge-venues robinhood --hedge-cash 250 --allowed-venues executable   # the defaults, spelled out
python -m arb_engine maker --sport nfl --mode demo --confirm --size 50 --max-orders 5 --max-notional 300   # real orders on Kalshi's demo exchange
KALSHI_ENV=prod ARB_LIVE_TRADING=1 python -m arb_engine maker --sport nfl --mode live --confirm --size 20 --max-notional 200
```

What it does every `--interval` seconds: refresh Kalshi (batched) and hedge quotes for the
watchlist, cancel/re-price resting orders whose margin-if-filled decayed, poll fills, and
alert `HEDGE NOW: buy N x <other side> on <venue> at ≤ <price>`; every `--rescan` seconds it
re-runs the cross-venue scan. The price it rests at is
`min(max_buy_maker(hedge ask), Kalshi ask − 1 tick)`, only when the margin-if-filled clears
`--min-margin` and (by default) the price is at or above Kalshi's best bid — resting behind
the bid rarely fills; `--deep-queue` allows it. Limits: `--max-orders`, `--max-notional`,
`--max-per-event`, `--hedge-cash` (dollars of hand-executed hedge legs resting at once, $250
by default) and the Kalshi balance. Hedges are only taken on venues this account can execute on
(`--hedge-venues`, default `robinhood`; a watch whose hedge sits on Polymarket alerts `HEDGE
VENUE NOT EXECUTABLE` instead of resting — on the fixture scans that was 5 of 8 NFL hedges).
Every resting order carries an exchange-side expiry (`min(kickoff, now + 1 h)`), an exchange
pause cancels everything, Ctrl-C cancels everything resting and a restart sweeps orphans. The
hedge leg is manual (Robinhood has no API), so keep sizes at what you can hedge by hand within a
minute.

Kalshi account (needs `KALSHI_API_KEY` + `KALSHI_PRIVATE_KEY_PATH`; demo environment by default):

```bash
python -m arb_engine kalshi balance
python -m arb_engine kalshi order --ticker KXNFLGAME-26SEP20PHITEN-PHI --side-action buy --side yes --count 10 --price 0.72 --post-only            # dry-run
python -m arb_engine kalshi order --ticker ... --confirm                                                                                        # submits (demo)
```

Real-money orders additionally require `KALSHI_ENV=prod` **and** `ARB_LIVE_TRADING=1`. The
signed path (external-api hosts, PSS salt, V2 order / cancel / batched-cancel payloads, int64
expiry) is unit-tested against schema-derived fixtures; `python scripts/kalshi_demo_check.py`
verifies it against the demo exchange with your demo key and `--record` replaces the fixtures
with real responses — not yet run, see [docs/ROADMAP.md](docs/ROADMAP.md).

### Robinhood overlay

1. `chrome://extensions` → **Developer mode** (toggle, top-right) → **Load unpacked** → select the
   folder named **`extension`** *inside* this repo (`…/arb-engine/extension`), not the repo
   root. Run `python3 scripts/check_extension.py` first; it must print `PASS`. Install steps,
   Chrome error strings and debugging: [docs/EXTENSION.md](docs/EXTENSION.md).
2. Open any game/match page under `robinhood.com/us/en/prediction-markets/…/events/…`.
3. The panel (bottom-right) shows per outcome: consensus fair value, each venue's ask/bid,
   fee per contract, all-in cost, and max-buy prices; contract tabs get a badge. On a
   game's **Spread** or **Totals** page it lists every line (arbs first) with your all-in
   cost, the max price to pay here, the cheapest hedge for the other side and the margin.
   Polymarket rows are tagged **`signal only`** (priced into the fair value, never an arb leg)
   unless the popup's "I can trade on Polymarket" is ticked — US accounts cannot use
   polymarket.com. With the bridge running, game pages also get a **LIVE strip**:
   score/clock/situation, model vs market vs ESPN fair per side, and LOCK/STEAL hints for the
   lots you enter in the popup. The strip is feed-gated like the CLI: the bridge keeps a
   per-event `FeedFreshness` across polls, so a `feed-stale` / `clock-frozen` / … signal shows
   as `GATED · wait` instead of `NOW` (see [docs/EXTENSION.md](docs/EXTENSION.md), also for
   "Load unpacked" failures).
   On a **category page** (`…/prediction-markets/nfl/`) every game card's price buttons
   ("PHI - 77¢") get a badge with the consensus fair value and the max price to pay here
   (first 16 games; `ARB +x%` when one exists) and the panel summarises the page.
4. Optional but recommended: run `python -m arb_engine bridge` — the extension detects it and
   lets the Python engine do the modelling (popup → "Local engine bridge").
5. It refreshes itself: every second by default (popup → "Refresh every"), never with two
   requests in flight; the engine re-pulls only the quotes each second and caches the page.
6. **How much to buy** is on the panel: for an arb, the number of contracts the books actually
   hold at those prices and the locked profit after fees for each leg; for a STEAL, enter your
   bankroll in the popup and the alert says how many contracts (fractional Kelly on the fee-inclusive
   edge, capped by what is offered at that ask). Every price shown is all-in — commission plus
   exchange fee here, taker fees on Kalshi/Polymarket — and a "size" column shows what is
   offered at each ask.

Kalshi's API blocks browser origins; in direct mode the extension removes the `Origin`
header on its own requests with a `declarativeNetRequest` rule. If Kalshi rows show an
error, start the bridge.

The overlay is read-only. It never places orders and reads nothing from your account.

### Kalshi MCP server (Claude Code / Codex)

`.mcp.json` registers the [mcp-server-kalshi](https://github.com/9crusher/mcp-server-kalshi)
server (`pip install mcp-server-kalshi`, then it runs as
`python3 -c "from mcp_server_kalshi.server import main; main()"`) with `KALSHI_ENV`,
`KALSHI_API_KEY` and `KALSHI_PRIVATE_KEY_PATH` taken from your environment (defaults to the
demo exchange). Set the variables in your shell; Claude Code picks the server up from the
project and its tools cover markets, order books, rules PDFs, balance, positions and
(with `confirm=true`) orders. For Codex add the same command to `~/.codex/config.toml` under
`[mcp_servers.kalshi]`; if you use `uv`, `uvx mcp-server-kalshi` works too.

## What the numbers mean

* **all-in** = ask + fee per contract at the reference size (fees round up per order, so
  size matters at the extremes).
* **fair** = spread-weighted consensus of every venue's de-vigged mid; **edge** = fair −
  all-in at the cheapest venue. Positive edge is +EV, not risk-free.
* **margin** = 1 − Σ all-in of the cheapest leg per outcome. Positive = arbitrage at that
  size; "fillable" means every leg has ≥ 1 contract at the quoted size, and `--books` shows
  the profit-maximising size against real order books.
* **max-buy (taker / maker)** = the highest price to pay here so the round trip still locks
  `--target-margin` after fees, given the other outcome's cheapest ask elsewhere.
* Flags: `live` (in play — gaps are staleness, excluded by default), `same book as …`
  (Robinhood re-selling Kalshi; never arbed against Kalshi), `thin` (positive margin but
  not fillable), `depth-checked` (real books used), `tie-rule-unverified` (Rothera NFL
  moneyline: the engine reads its terms as "no winner on a tie" but has not seen the text),
  `tie-rule-mismatch` (the chosen legs settle a tie differently; `tie_margin` says what the pair
  pays then), `below-min-size` (a venue minimum exceeds what is offered), `signal-only` (a leg's
  venue is not executable for this account), `settlement-mismatch:<case>` (tennis walkover /
  cancellation / postponement rules differ), `stale-quote` (snapshot older than
  `--max-quote-age`). Spread/total lines are half-points on all three venues, so they cannot push.

## Status (2026-09-19)

Live data verified for all three venues; **721 Python tests** and the JS suites
(**3,769 `arb-core` checks** — 3,650 fee vectors + 54 arb vectors in parity with Python — and
**159 background-worker checks** incl. totals, category pages and signal-only rows) pass;
CI runs them on Python 3.10–3.13 and `scripts/render_results.py --check` keeps every results
table in the docs equal to its committed fixture. What the data has said so far:

* **NFL** moneylines are efficient to within fees; the ~1,000 spread/total lines held 16
  fillable, depth-checked arbs on a Tuesday (Rothera far-tail overs vs Kalshi unders, ≈1 % on
  capital) that were gone by Friday — the maker runner sits at the prices where they reappear.
  With the eligibility table applied, the only arb on the committed fixture scans disappears
  (its cheap leg was Polymarket): what is executable for a US account is Kalshi × Robinhood.
* **In play, NFL 2026 week 1** (16 games, 2,263 scrimmage plays): the win-probability model
  scores 0.406 log-loss against Kalshi's 0.438–0.443 (before / after the play; the truth is in
  that range), 90 % game-cluster interval excluding zero. The current blend is worse than the
  model alone on this week (0.417) but not on college week 2 (86 games, no difference), so the
  weights are unchanged. The **STEAL edge is not demonstrated**: hold-to-settlement P&L
  intervals include zero on the NFL and a shuffled-outcomes placebo does as well; on college the
  real pairing beats its placebo (+3.5 % / +7.1 % / +5.5 % vs −23 % / −22 % / −13 %) with
  intervals that still include zero. Every break-even LOCK variant loses on the NFL week;
  on college only the widest (10 %) break-even lock came out ahead. Spreads and totals: a
  normal margin model scores level with Kalshi in play (0.616 vs 0.622–0.628; totals 0.640 vs
  0.636–0.641) **but its in-play sd floors were picked on those same 16 games** (untuned it
  is 0.632 / 0.718, behind the market), so that is in-sample until another week is scored.
  All of it in [docs/MODEL.md](docs/MODEL.md), one week each.
* **Inputs are checked**: the replay's ESPN-derived state agrees with nflverse on ≥ 99.8 % of
  aligned plays on every field but the clock; ESPN's own win probability is a *post*-play
  number; the college spread rescale changes nothing beyond noise.
* **College football** (Kalshi × Polymarket × Robinhood/CDNA): 263 moneylines and 10,525
  spread/total lines on a Friday, 1,384 lines on all three venues, two fillable tail arbs
  (Polymarket legs, i.e. signal-only for a US account).
* **Tennis** on Robinhood is Kalshi's book re-sold (fee routing only); Kalshi × Polymarket
  pairs settle walkovers differently (Kalshi fair price vs Polymarket 50-50; 3.25 % of tour
  and 1.98 % of challenger matches settle that way) and are gated `walkover-exposed`.
* **NHL/NBA** are wired for the season (NHL preseason already merges Kalshi × Polymarket).
* **Fees**: the exchanges' own schedules (Rothera per-order quadratic, CDNA range) are wired
  as opt-in models; the $0.01/contract default stays until an order ticket says which one
  Robinhood passes on — on the fixtures the flip changes no arb's sign.

**Needs you**: every remaining step is by hand on your own accounts — a Rothera / CDNA
order-ticket fee preview (never submit), the Kalshi demo-key check, a Polymarket US gateway
curl, recording a live Sunday slate, re-fitting the in-play sd floors out of sample, re-running
the walkover shares with `--pages 20`, and the fee-default decision. The list with commands is
in [docs/ROADMAP.md](docs/ROADMAP.md).
The literature review behind the next round of candidates, ranked with offline tests, is in
[docs/RESEARCH.md](docs/RESEARCH.md).

Not investment advice. Prediction-market contracts can lose their full cost; rule
differences (ties, retirements, postponements) can break a "hedge". Verify every fee and
rule on the venue before trading size.

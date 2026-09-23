# Sunday runbook: the first live NFL slate

What to run, in what order, what to look at while it runs, how to read what it prints, what
to keep, and what to do when something breaks. Every `python3 -m arb_engine …` command and
`scripts/sunday.sh` here answer `--help` (`tests/test_preflight.py` checks that; the one
exception is `scripts/check_extension.py`, which takes a folder). Nothing on this page places an order: the maker
runs in paper mode and the live slate only prints and records.

Times below are Eastern. The early window kicks off 13:00, the late window 16:05 / 16:25,
Sunday night 20:20.

## 0. The morning before: preflight (10 s, from the repo root)

```bash
cd ~/Documents/GitHub/arb-engine
export ARB_HTTP_TRANSPORT=curl            # this box's proxy truncates chunked HTTP to urllib
python3 -m arb_engine preflight --sport nfl --date 2026-09-20
```

Twelve rows, one verdict. It is safe to run as often as you like (one Kalshi call, one
Gamma call, the Robinhood category page, one ESPN scoreboard, one adapter fetch each).

| row | PASS means | if it is not PASS |
|---|---|---|
| `python` | 3.10+ (3.13 recommended — what the author runs and CI tests first) | `python3 --version` |
| `imports` | every `arb_engine` module imports, stdlib only (checked in a fresh `python3 -I` so what this shell already imported cannot hide anything) | the detail names the module and the exception; a non-stdlib import is a FAIL (`cryptography` is the one optional package and only a note) |
| `wp-model` | the packaged WP model loads and prices a known Q3 state and a pre-game spread sanely | `arb_engine/data/nfl_wp_model.json` is corrupt or the rules table changed: `git status arb_engine/data`, `python3 -m unittest tests.test_wp_model` |
| `settings` | the executable venue set is the table's (`kalshi,robinhood`) | **WARN "polymarket is executable"**: an `EXECUTABLE_VENUES` from an experiment is still exported (`unset EXECUTABLE_VENUES`, check `.env`). A US account cannot execute there; with it set the scanner will show Polymarket legs and the overlay will drop the `signal only` tag |
| `venue:kalshi` | `/markets?series_ticker=KXNFLGAME&limit=1` answered, with latency | HTTP 403 → user-agent / Origin problem, try `KALSHI_BASE_URL=https://api.elections.kalshi.com/trade-api/v2`; HTTP 0 → the truncating proxy, make sure `ARB_HTTP_TRANSPORT=curl` is exported |
| `venue:polymarket` | Gamma `/events?tag_slug=nfl&limit=1` answered | WARN "no active event" = off-season or renamed tag; the slate still runs (Polymarket is a signal) |
| `venue:robinhood` | the NFL category page fetched from the host (never from `out/cache/`, so the latency is real), parsed (`__NEXT_DATA__`), game winners listed | the page is ~30 MB; > 45 s is a WARN and means the transport is not curl. A FAIL with a `ValueError` about `__NEXT_DATA__` is a truncated page (same fix) |
| `espn` | the scoreboard lists the date's games (ET date; ESPN answers with the whole NFL week, the other days are only counted), kickoffs, and how many carry a spread | 0 games = wrong `--date` (the row says how many games sit on other dates) or ESPN 403 on `site.api.espn.com` (the client falls back to `site.web.api.espn.com` and a plain UA on its own; if all fail the row says which). WARN "no spread yet" = a pre-game without an odds block: the model prices it neutral until one appears |
| `matches` | every game of the date is quoted on Kalshi *and* Robinhood (the executable venues) | a game listed as `unmatched` will print as `no quotes: <game>` in `live` all afternoon. Usually a team-name spelling: `python3 -m arb_engine quote --sport nfl <TEAM>` shows which venue is missing; fix `arb_engine/matching/nfl_teams.json` |
| `bridge` | `/health` answers and its executable venue set equals this shell's | WARN "not running" before `sunday.sh start` is expected. **FAIL "!= this process"**: the bridge was started in another shell with a different `EXECUTABLE_VENUES`; `scripts/sunday.sh restart bridge` |
| `out-dir` | `out/` (and `out/logs`, `out/run`) writable, ≥ 1 GiB free | free space: a Sunday of ticks is ~100 MB |
| `extension` | `scripts/check_extension.py extension` passes | the detail is the checker's last line; `docs/EXTENSION.md` maps Chrome's error strings |

Exit code 0 on PASS or WARN, 2 on FAIL. `--json` prints the same as one JSON document
(`--offline` skips every network row; `--limit N` caps the games matched; `--venue-timeout S`
bounds each adapter fetch). Read every WARN once; start only when you understand each one.

## 1. Start everything: the launcher (12:30 — or leave it running all week)

The recorders idle at one ESPN poll a minute when nothing is live or within the pre-game
window (`INPLAY_IDLE_EVERY_S`), so the launcher can stay up from Thursday to Monday. It runs
an NFL recorder (`live`) and a college one (`live-ncaaf`, `EXTRA_SPORTS=ncaaf` by default;
`EXTRA_SPORTS=""` for none) — Saturday's slate is recorded with the same fast lane, LAG rule,
paper book and pushes, into the same `out/history.db` (its own journal
`out/live_ncaaf_journal.jsonl`).

```bash
BANKROLL=1000 KELLY=0.25 scripts/sunday.sh start
# extra flags for `live` go after --:   scripts/sunday.sh start -- --steal-edge 0.04 --pre-hours 0.5
```

`start` runs preflight again (it refuses on FAIL, and on WARN when `START_ON_WARN=0`), then
starts three processes, each under a supervisor that restarts it 10 s after any exit. It also
runs `caffeinate -i -w <live supervisor>` so the Mac cannot idle-sleep while the recorder is
up (`KEEP_AWAKE=0` skips it) — a closed lid still sleeps, so leave it open and on power; the
first Sunday lost most of the early window to sleep:

| name | command | log | writes |
|---|---|---|---|
| `bridge` | `python3 -m arb_engine bridge --port 8765` | `out/logs/bridge-<date>.log` | nothing (serves the overlay) |
| `live` | `python3 -m arb_engine live --sport nfl --every 5 --record out/history.db --journal out/live_journal.jsonl --bankroll $BANKROLL --kelly $KELLY [--quiet if the build has it] <extra>` | `out/logs/live-<date>.log` | `out/history.db` (espn_ticks, venue L1, steal_observations + ladder, pregame_lines), `out/live_journal.jsonl` |
| `maker` | `python3 -m arb_engine maker --sport nfl --mode paper --size 10 --journal out/maker_journal.jsonl` | `out/logs/maker-<date>.log` | `out/maker_journal.jsonl` (paper fills, HEDGE NOW alerts) |

PIDs live in `out/run/<name>.pid` (supervisor) and `out/run/<name>.child` (the Python
process); the extras you gave after `--` sit in `out/run/live.args`. Every process inherits
`ARB_HTTP_TRANSPORT=curl` and whatever `EXECUTABLE_VENUES`, `ROBINHOOD_GOLD`, `INPLAY_*` you
exported.

```bash
scripts/sunday.sh status            # up / restarting / down per process (+ live's extras) + the last 3 log lines + /health
scripts/sunday.sh logs live         # tail -f the live log (Ctrl-C leaves the process running)
scripts/sunday.sh restart bridge    # bounce one process (after a git pull, or a venue-set change)
scripts/sunday.sh restart live      # same extras as at start (out/run/live.args); `restart live -- <new extras>` replaces them
scripts/sunday.sh preflight         # the report alone (also written to out/logs/preflight-<date>.log)
```

## 1b. Pushes to your phone (ntfy, 2 minutes)

1. Install the **ntfy** app (iOS / Android) or open https://ntfy.sh in a browser, and
   subscribe to a topic name nobody would guess (anyone who knows the name can read it, so
   make it random; no account needed). The current topic is in `out/run/ntfy_topic.txt`.
2. Tell the launcher once — it persists the topic and sends a test push; every later
   `start` / `restart` picks it up (an exported `ARB_ALERT_NTFY` wins over the file):

```bash
scripts/sunday.sh ntfy arb-kv15-7q2x9m          # or a full https://ntfy.example/topic URL
scripts/sunday.sh restart live                  # if the recorders are already running
```

3. What gets pushed, by default: **ARB** (a fresh two-leg lock, fees and depth checked),
   **ARB CLOSE** (within `arb_near_margin`, 3¢ per contract, of locking — the heads-up
   before it crosses), **EXEC ERROR** (the auto-trader's order failed — loud on
   purpose, one per game per minute),
   **HEDGE NOW** (a paper maker fill), **FINAL** (one line per finished game: score, LAG/ARB
   counts, paper-book result), TAKER ARB, EXCHANGE PAUSED. STEAL and LOCK NOW are not pushed
   unless you add them (`ARB_ALERT_NTFY_KINDS=ARB,LAG,STEAL,FINAL`): the first live Sunday
   lost on STEAL. One push per (kind, game, side) per 60 s (`ARB_ALERT_MIN_INTERVAL_S`);
   TAKER ARB and ARB CLOSE collapse to one per *game* (a game has a dozen spread and total
   lines), ARB CLOSE repeats only every `arb_near_every_s` (5 min) or when the gap shrank by
   a cent, and HEDGE NOW is never throttled. The full text is in `out/live_journal.jsonl`
   (`"kind": "alert"`); pushes are logged as `"kind": "ntfy"`, throttled ones as
   `ntfy_throttled`.

   **LAG is not pushed** (since 2026-09-22). A LAG is a one-sided bet that the slower venue
   catches up, not an arbitrage, and the re-run of the first Sunday with executable
   accounting lost ~6c per contract on it (docs/MODEL.md). It keeps running in the
   background - every signal journalled, paper-traded and, with `--execute-lag demo`,
   demo-executed - so the validation games can show whether it pays at the fast lane's 1 s.
   To see LAG pushes anyway: `ARB_ALERT_NTFY_KINDS=ARB,ARB\ CLOSE,LAG,EXEC\ ERROR,HEDGE\ NOW,TAKER\ ARB,FINAL`
   before `sunday.sh restart live`. Each LAG push ends with what the auto-trader did
   (`AUTO (demo): sent IOC buy 50 x ... -> filled 12.00`).

4. **Reading a push.** The phone's bold line is the kind and the sport (`ARB NFL`); the body
   is an itemised receipt per venue, priced at the **ask** (what tapping buy costs now, never
   the mid) and checkable line by line against each venue's order review:

   ```
   NFL - ATL @ GB - ARB +1.3c/ct after fees
   1) KALSHI: buy 340 x Green Bay at the ask $0.56 (56c)
      price: 340 x $0.56 = $190.40
      + Kalshi taker fee: 0.07 x 340 x 0.56 x 0.44 = $5.8643 -> $5.87 (rounded up to the cent)
      = you pay $196.27 ($0.5773 per contract)
   2) ROBINHOOD: buy 340 x Atlanta at the ask $0.39 (39c)
      price: 340 x $0.39 = $132.60
      + Robinhood commission: 0.1 x 340 x 0.39 x 0.61 = $8.0886 -> $8.09 (rounded up), capped at $0.01 x 340 = $3.40
      + Rothera exchange fee: $0.01 x 340 = $3.40 (assumed: Robinhood's 'up to $0.01 per contract' ceiling)
      = you pay $139.40 ($0.4100 per contract)
   total: $196.27 + $139.40 = $335.67 -> pays $340.00 whoever wins = +$4.33 (+1.29% on cost)
   tie: pays $340.00 = +$4.33
   340 ct; bankroll $500.00; fees are entry-only (held to settlement)
   ```

   The count is one number for both legs (a set pays $1 whoever wins) and is already capped
   by the thinner book *and* by the bankroll, fees included (`quant.arbitrage.size_for_budget`).
   Each fee line is the venue's rule at that exact count (`FeeModel.breakdown`, whose items
   sum to the fee): Kalshi's 0.07 x C x p x (1-p) rounded up per order; Robinhood's commission
   0.10 x C x p x (1-p) rounded up and capped at 1c per contract (0.05 with Gold), plus the
   routing exchange's fee. The exchange fee marked *assumed* is Robinhood's published
   "up to $0.01 per contract" ceiling: if Robinhood's order review shows a different
   exchange fee, that line is the one to tell us about (docs/ROADMAP.md, "Needs you").
   "Entry-only" means a contract held to settlement pays no exit fee; selling early pays the
   schedule again. A `tie:` line that says **LOSES on a tie** is a Kalshi-YES + Rothera-YES
   pair, where a tie pays $0.50 total - take the Rothera NO of the same side instead.

   An **ARB CLOSE** body prints what each leg costs now and the price it has to reach, so the
   limit order can be parked before the move:

   ```
   NFL - ATL @ GB - ARB CLOSE -3.7c/ct after fees (not yet a lock)
   KALSHI Green Bay @ 0.62 (all-in 0.6371) - locks at 0.60, 2.0c away, depth 420
   ROBINHOOD Atlanta @ 0.39 (all-in 0.4000) - locks at 0.37, 2.0c away, depth 150
   set costs $1.04/ct with fees; needs $0.04/ct more of move; ready for 150 ct (depth)
   ```

## 1c0. Connect your Kalshi account (5 minutes, done once)

Only you can do the first two steps — they happen while signed in to Kalshi, and the private
key must never be pasted into a chat, a commit or a file in this repo.

1. **Create a key.** Start on the demo exchange (https://demo.kalshi.co, a separate sign-up with
   play money) so the executor can be proven before real money is involved; do the same on
   kalshi.com later. Account → **API Keys** → **Create key**. Kalshi shows a **Key ID** and
   downloads a **private key file** once — it cannot be downloaded again.
2. **Install it with one command, in your own Terminal** (the downloaded file's name, the Key
   ID Kalshi showed, and `demo` or `prod`). It moves the file into `~/.kalshi/` (mode 600),
   writes `~/.kalshi/env` (the ID and a *path*, not the key) and runs the check below:

```bash
scripts/kalshi_install_key.sh ~/Downloads/<the-downloaded-file> <key-id> demo
```

   Never paste the key file's contents into a chat, an issue or a commit. If that happens,
   delete the key on Kalshi (Account → API Keys) and create a new one: a pasted private key is
   in that tool's logs for good, and deleting the key is the only thing that makes the copy
   useless.

3. **Check it again any time** (read-only: one signed balance read, nothing placed or cancelled):

```bash
python3 scripts/kalshi_connect.py
```

   `CONNECTED: cash $…` means the engine can sign as you. A `401` at the last step is almost
   always a demo key on the production host (or the reverse) or a Key ID that belongs to a
   different file; the checker says which. For the two Kalshi MCP servers in `.mcp.json` to see
   the key too, add `[ -f ~/.kalshi/env ] && source ~/.kalshi/env` to `~/.zshrc` and restart
   Claude Code. Those servers are for looking (markets, balance, positions, fills); orders go
   through the engine's own capped executor below.

## 1c. Acting on LAG automatically (needs your Kalshi API key)

A LAG lives ~23 s; reading a push and typing an order is slower. `live --execute-lag` sends
an **immediate-or-cancel buy** of the laggard's own Kalshi market at the ask the signal saw,
sized like the signal and capped (`--lag-max-contracts 50`, `--lag-max-per-game 100`,
`--lag-daily 500`; the caps count what *filled*, so an order that found nothing does not use
them up). Modes: `intent` writes what it would send to
`out/orders/lag_intents.jsonl` (start here; no key needed), `demo` sends to Kalshi's demo
exchange (`KALSHI_ENV=demo` + `KALSHI_API_KEY` + `KALSHI_PRIVATE_KEY_PATH`), `live` sends to
production and also needs `ARB_LIVE_TRADING=1`. The launcher passes extras after `--`:

```bash
scripts/sunday.sh start -- --execute-lag intent          # dry run: intents only
KALSHI_ENV=demo KALSHI_API_KEY=… KALSHI_PRIVATE_KEY_PATH=… scripts/sunday.sh restart live -- --execute-lag demo
```

Read `out/orders/lag_intents.jsonl` after a slate before considering `demo`; the paper book
(`scripts/leadlag_study.py --date <day>`) tells you what those intents would have filled.

## 2. Reload the extension (12:35)

1. `chrome://extensions` → the "Arb Engine Robinhood Overlay" card → the circular reload
   arrow. Do this after every `git pull` that touched `extension/`, and once now so the
   service worker picks up the running bridge.
2. Toolbar popup: **Local engine bridge = auto** (or `required`), bankroll and Kelly fraction
   the same numbers you gave the launcher, "I can trade on Polymarket" **unticked**.
3. Open one game page (`robinhood.com/us/en/prediction-markets/nfl/events/<slug>/`). The
   panel's status line must read `<time> · engine` (bridge mode). A bare time means direct
   mode (the extension's own fee math, no in-play strip, no gates): check
   `curl http://127.0.0.1:8765/health` and `scripts/sunday.sh status`, then reload the card.
4. Keep **one** game tab open at a time: each tab polls the bridge every second and the bridge
   re-pulls Kalshi for it (`docs/EXTENSION.md`, "Refresh rate").

If a card shows a red **Errors** button, `python3 scripts/check_extension.py extension` (it
takes the folder, not `--help`) names the file and `docs/EXTENSION.md` maps the Chrome string
to the fix. Preflight's `extension` row runs the same script.

## 3. What to watch (13:00 → 23:30)

`scripts/sunday.sh logs live`. Each tick (every 5 s) prints one header and one block per game.
The shape (numbers invented, the formats are `strategy/live.py` `format_tick` and
`strategy/inplay.py` `game_line`):

```
13:07:15 9 game(s) priced, 0 without venue quotes
LIVE Q1 09:31 · CHI 0-3 MIN · MIN ball 2nd & 7 · 45 to go · TO 3/3  [gated: feed-stale]
    Chicago          fair 0.612 [mkt 0.605 model 0.618 espn 0.610]  best robinhood  ask 0.590 all-in 0.610  edge +0.3%
    Minnesota        fair 0.388 [mkt 0.395 model 0.382 espn 0.390]  best kalshi     ask 0.360 all-in 0.376  edge +3.1%  GATED
    -> GATED STEAL: wait: feed-stale — Minnesota all-in 0.376 on kalshi vs fair 0.388 (+3.1%) [market 0.40 / model 0.38]
```

* **header**: `N game(s) priced, M without venue quotes`. `M` should be 0 after preflight; a
  game that goes `no quotes:` mid-afternoon means a venue delisted or halted it — nothing to do but note the time.
* **`fair [mkt model espn]`**: the blend and its three inputs. When `model` and `mkt` sit more
  than 0.05 apart the block ends with `sources disagree by 0.xx` — that is exactly the
  dead-ball moment the gates exist for, not a signal.
* **`best <venue> ask all-in edge`**: cheapest executable venue for that side, fee-inclusive.
  `edge` is `fair − all-in`; the STEAL threshold is `--steal-edge` (3 %) plus `+0.02` on a
  CDNA-routed Robinhood contract, and double on a spread/total line.
* **`STEAL … → N ct`**: the side's all-in is below both the blend and the model by the edge
  and every gate is clear. `N` is fractional Kelly on the bankroll, capped by the contracts
  offered and scaled by the slate cap. The replays have *not* demonstrated this edge on the
  NFL (`docs/MODEL.md`); treat the count as a ceiling and the line as something to record.
* **`GATED` / `-> GATED STEAL: wait: <reasons>`**: the signal fired but a freshness gate held
  it. Reasons, from `docs/ARCHITECTURE.md`:

  | reason | what it means right now |
  |---|---|
  | `feed-stale` | a venue mid moved ≥ 0.02 but ESPN's state has not changed for 15 s: the market knows a play the feed has not published. Wait for the state line to move |
  | `clock-frozen` | identical ESPN state over several polls with the clock supposedly running: ESPN is lagging |
  | `quote-old:<venue>` | that venue's quote timestamp is older than the poll interval: stale book, not a price |
  | `score-pending` | the score changed but the last play id did not: the scoring play is half-published; the WP will jump |
  | `suspect` / `review-pending` | ESPN's own state guard (score went backwards / a review or challenge may still change it) |

  A gated line is *information*: the same gap will usually be gone two ticks later. If it
  survives the gate on the next ticks it becomes a plain `STEAL`.
* **`signal only`** (overlay tag; in the CLI the venue simply never becomes `best`): the
  cheapest ask is on Polymarket, which is not executable for a US account. Its price still
  moves `mkt` and `fair`; there is no leg to take.
* **`error:` lines**: one venue failed this tick (the adapter records the exception, the
  other venues still price). Repeated `robinhood: HTTP 0` = the proxy again; repeated
  `kalshi: HTTP 429` = another process is hammering Kalshi (a second `live`, a second tab).
* The overlay's LIVE strip shows the same numbers for the open game, gated the same way
  (`GATED · wait`, hover for reasons).

The maker log prints its watches every rescan (`--rescan 300`) and `HEDGE NOW` when a paper
order fills. Its hedge venues default to Robinhood only; a watch whose only hedge sits on
Polymarket is simply not rested. A `HEDGE VENUE NOT EXECUTABLE` alert at start-up means
`MAKER_HEDGE_VENUES` names a venue the compliance table says you cannot use — unset it.

## 4. What to record (during and after)

The launcher already keeps everything that matters; do not stop `live` between windows.

* `out/history.db` — every ESPN tick, venue L1, STEAL / GATED observation with the
  +10 s … +15 min ladder, pre-game lines. This is the "record a live Sunday slate" row of
  `docs/ROADMAP.md`, "Needs you".
* `out/live_journal.jsonl`, `out/maker_journal.jsonl` — the alert stream with gate reasons.
* `out/logs/*-<date>.log` — stdout/stderr per process, including every supervisor restart.
* By hand, in a text file: for each STEAL you *would* have taken, the time, the game line,
  and the Robinhood order-ticket fee preview if you open one (never submit) — the Rothera
  fee-model flip (`docs/ROADMAP.md`) is still waiting on a ticket.

Quick looks while it runs (all read-only):

```bash
python3 -m arb_engine stats --db out/history.db --convergence     # STEAL ladder: toward/away, CLV per edge
python3 -m arb_engine games --sport nfl --date 2026-09-20 --enrich # ESPN's view of the slate
python3 -m arb_engine rh-event <robinhood event url>               # what the overlay sees for one game
```

## 5. Stop (after the Sunday-night game)

```bash
scripts/sunday.sh stop      # maker first (SIGINT: cancels nothing real in paper mode, flushes the journal), then live, then bridge
scripts/sunday.sh status    # every row "down"
```

`stop` kills the supervisor before the child, so nothing respawns; the supervisor forwards
exactly one SIGINT to its child (a second one would land inside the maker's cancel-all and
abort it). If a child ignores SIGINT for 10 s it gets SIGTERM, then SIGKILL. `stop` also
clears `out/run/live.args`, so the next `start` runs with the extras you give it then.

## 6. What to send me

```bash
tar czf out/sunday-2026-09-20.tgz out/history.db out/live_journal.jsonl out/maker_journal.jsonl out/logs/*2026-09-20*
```

plus the hand notes from step 4 and the answers to: which STEALs you would have taken and
why not; any `no quotes:` game and when it started; any `error:` line that repeated for more
than a minute; the overlay status line when it was wrong. With the database in hand the
replays run offline:

```bash
python3 -m arb_engine backtest-ticks --db out/history.db --gates both --game 'CHI|MIN'
python3 -m arb_engine clv --db out/history.db
python3 -m arb_engine event-study --help
```

## 7. Failure modes from the college run, and the remedy for each

| symptom | cause | remedy now | caught by |
|---|---|---|---|
| every game `no quotes:` although the venues list them | ESPN keys the game on one ET date, the venue on another, or a college team name the table does not know | preflight `matches` lists the unmatched games before kickoff; `quote --sport ncaaf <team>` shows the venue that disagrees; fix the team table | `preflight` row `matches` |
| `GATED STEAL: wait: feed-stale` on nearly every tick of one game | ESPN's college scoreboard updates in bursts (10-30 s) while Kalshi re-prices per play | expected; the gate is doing its job. Use `--stale-after 25` on `live` (`INPLAY_STALE_AFTER_S`) if every game is gated and the state line *is* moving | runbook §3 |
| `clock-frozen` for minutes | ESPN's clock stops at a timeout / review / halftime without a status change | wait; the WP is frozen too, so nothing is priced wrongly | runbook §3 |
| overlay shows `direct`, panel empty on a college page | the bridge is not running, or it was started before the code that resolves CDNA symbols | `scripts/sunday.sh status`, `restart bridge`, reload the extension card | `preflight` row `bridge` |
| overlay prices Polymarket as a leg / no `signal only` tag | `EXECUTABLE_VENUES` exported in the bridge's shell, or "I can trade on Polymarket" ticked | `unset EXECUTABLE_VENUES`, restart the bridge, untick the popup box | `preflight` rows `settings`, `bridge` |
| `robinhood: HTTP 0 …` or a `ValueError` about `__NEXT_DATA__` | the proxy truncated the 30 MB category page | `export ARB_HTTP_TRANSPORT=curl` before starting (the launcher does); the adapter retries via curl on its own for the page but not for the quotes API | `preflight` row `venue:robinhood` |
| `espn: HTTP 403` | Akamai refused `site.api.espn.com` for our UA | the client falls back to `site.web.api.espn.com` and a plain UA and remembers what worked for 60 s; if the row still fails, wait a minute | `preflight` row `espn` |
| `kalshi: HTTP 429` | two processes share the Kalshi budget (a second `live`, a `scan --books`, several overlay tabs) | one `live`, one game tab; `KALSHI_RATE_LIMIT=8` for the extra process | — |
| a process died and nobody noticed | no supervisor | the launcher restarts each process 10 s after any exit and logs `exited rc=… restart in 10s`; `status` shows `restarting` | `sunday.sh` |
| the live log is unreadable at 5 s ticks | no `--quiet` | the launcher passes `--quiet` when the build has it (the journal and the recorder keep everything); otherwise `--every 10` via `EVERY=10` | — |
| `history.db` grew but `stats --convergence` is empty | `live` ran without `--record`, or the ladder never had 15 min of ticks after a STEAL | the launcher always passes `--record out/history.db`; run past the last window | — |
| CDNA (college) STEAL fired at +3 % and the fill would have been 3 s late | Robinhood's CDNA order delay | the `+0.02` haircut (`--cdna-haircut`) is on by default; NFL contracts are Rothera and not affected | — |

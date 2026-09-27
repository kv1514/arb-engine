# Engine audit and execution handoff — 2026-09-26

Repo: `/Users/kv15/Documents/ChatGPT/arbitradge`, branch **`claude/exec-readiness`** (created
this session from `claude/order-buttons` @ `a8b9246`). Not pushed. Not merged into the GitHub
clone (`~/Documents/GitHub/arb-engine`) that runs the live stack. That clone's `main`
(`3ca9684` at 13:00 PT, moved by another session this afternoon) is an ancestor of this branch,
so deploying is a fast-forward. The original prompt for this session is kept at the end.

## Deployment audit (17:10 PDT): what runs, what the runtime files say, how to deploy

This section was written by a separate session, from 16:25 to 17:15 PDT on 2026-09-26.

* **It changed nothing:** not the running stack, not `~/Documents/GitHub/arb-engine`, not the branch's code.
* **It made GET requests only,** against the Kalshi **demo** account. It sent no production request and placed no order.
* **It added two files:** this section and `docs/DEPLOY_RUNBOOK_2026-09-26.md`. The runbook is the step-by-step procedure (stop → reconcile → fast-forward a pinned hash → start → verify → demo check) and supersedes *Exact next steps* 2–4 below.
* **Read-only analysis scripts:** `analysis/{A,B,C}/analyze.py` and `recon/demo_recon.py` in the session scratchpad, `/private/tmp/claude-501/-Users-kv15/70832a7e-…/scratchpad/`. That directory is wiped at reboot, so the numbers are copied here.

### Read this first

* **What is deployed.** `~/Documents/GitHub/arb-engine` runs `main` `3ca9684`; the bridge runs `b831e0d`.
  * This branch is **not deployed**. No process has `arbitradge` as its working directory, and neither checkout has an order-ledger file.
* **Execution is demo only.**
  * `live` and `live-ncaaf` send LAG IOC orders to Kalshi's demo exchange (`--execute-lag demo`). The button and the maker are paper, and the week scanner only alerts.
  * `~/.kalshi` holds only a demo key, and no process has `ARB_LIVE_TRADING` set.
* **The demo account (16:36 and again at 16:47 PDT).**
  * 0 resting orders, 0 open positions, balance $238.24 of play money.
  * All 17 filled orders in the journal match exchange fills, and all 10 resulting positions have settled: +$138.42 net of $8.45 fees.
  * The demo book is not production's, so this is **not** evidence of edge.
* **The Mac slept through most slates** (lid closed, on battery):

  | day | asleep (PDT) |
  |---|---|
  | Thu | 15:17–17:35, 18:18–18:53, 19:46–23:20 |
  | Fri | 13:00–20:16 |
  | Sat | 13:02–16:16 |

  * Saturday's sleep alone cost about 66 in-play game-hours across 38 games (estimate). `caffeinate -i` does not prevent lid-closed sleep.
  * No data from those windows means no data, not "no opportunities".
* **No push has reached the phone since 10:52 PDT.** The ntfy free quota answered 429 from 11:16. As a result, none of the 134 "Robinhood done" buttons issued today could be tapped.
* **Three of this branch's fixes do not take effect in the real run loop.** Per-game decision time, the post-fetch clock refresh and the `polling gap` records depend on an unpinned `tick()`. `run()` calls `tick(t0)`. See *Known issues* in the runbook.
* **Other sessions were committing here during the audit.** This branch went `2414d8b` → `e2dd092` between 16:35 and 16:58, and `claude/micro-audit-3` was pushed from a `/private/tmp` worktree. At 17:08 both sessions were idle and this tree was clean. Deploy a pinned hash (runbook step 0).

### The two checkouts

| | `~/Documents/ChatGPT/arbitradge` | `~/Documents/GitHub/arb-engine` |
|---|---|---|
| role | development (Codex worktrees, Claude sessions) | **deployed**: cwd of every running process |
| HEAD at 17:08 PDT | `claude/exec-readiness` `e2dd092` (docs-only after `a016721`, the last validated code); clean | `main` `3ca9684` (12:22:56) = GitHub `main`; clean |
| relation | `3ca9684` is an ancestor: 46 files, +10,093 / −1,360 | – |
| processes | none; `out/logs`, `out/run`, `out/orders` empty | bridge, live, live-ncaaf, maker, week, caffeinate |
| order records | ledger code present, no ledger file | `out/orders/lag_intents.jsonl` (demo orders), `out/orders/arb_button.jsonl` (paper); no ledger |
| worktrees | 6 Codex worktrees, all clean | none besides itself (the `/private/tmp` one was removed after its push) |
| only on this Mac | `claude/exec-readiness`; `claude/order-buttons` (`a8b9246`, 5 ahead of GitHub); 8 `codex/*`; `stash@{0}` | nothing (all on GitHub) |
| `origin/main` ref | stale (`02cd612`, fetched 09-23) | current |

### What is running (`ps`, `lsof` cwd, `ps eww` filtered to non-secret names)

| process | supervisor → child | child started (PDT) | code at start | command (from `ps`) | mode |
|---|---|---|---|---|---|
| bridge | 60054 → 60058 | Sat 11:02:46 | `b831e0d` | `python3 -m arb_engine bridge --port 8765` | overlay server, no orders |
| live | 32255 → 32259 | Sat 12:23:02 | `3ca9684` | `live --sport nfl --every 5 --record out/history.db --journal out/live_journal.jsonl --bankroll 500 --kelly 0.25 --quiet --fast 1 --execute-lag demo` | LAG IOC to **Kalshi demo**; button paper |
| live-ncaaf | 32408 → 32413 | Sat 12:23:04 | `3ca9684` | the same with `--sport ncaaf --journal out/live_ncaaf_journal.jsonl` | LAG IOC to **Kalshi demo**; button paper |
| week | 32139 → 32143 | Sat 12:23:00 | `3ca9684` | `weekscan --sport nfl --sport ncaaf --every 120 --fast 5 --bankroll 500 --record out/history.db --journal out/week_journal.jsonl --quiet` | alerts only |
| maker | 60164 → 14811 | Sat 16:37:11 (a new child every hour) | `3ca9684` | `maker --sport nfl --mode paper --size 10 --journal out/maker_journal.jsonl` | paper; writes nothing to `history.db` |
| caffeinate | → 32257 | Sat 12:23:02 | – | `caffeinate -i -w 32255` | idle-sleep hold only |

**Common to every process:**

* **Working directory and launch:** `/Users/kv15/Documents/GitHub/arb-engine`, launched by `scripts/sunday.sh`.
  * The last full `start` was at 10:27 PDT, with the week scanner off while its sizing bug was fixed.
  * The 10:56 preflight then failed: the `venue:robinhood` category page timed out after 255 s while the Mac was in idle sleep.
  * So every process was brought up by per-process `restart`: all five at 11:02, then `week`, `live` and `live-ncaaf` again at 11:25, 11:57 and 12:23.
* **Persisted launcher settings:** `out/run/live.args` and `live-ncaaf.args` are `--execute-lag demo`; `bankroll` 500, `kelly` 0.25.
* **Environment:** `KALSHI_ENV=demo`, `KALSHI_PRIVATE_KEY_PATH=…/demo.key`, `KALSHI_API_KEY` set (value not read), `ARB_ALERT_NTFY` set (topic not shown), `ARB_HTTP_TRANSPORT=curl`, `PYTHONUNBUFFERED=1`.
* **Not set:** `ARB_LIVE_TRADING`, `ARB_BUTTON_MODE` (so the button defaults to paper), `KALSHI_BASE_URL`, `EXECUTABLE_VENUES`. Neither checkout has a `.env`.

**Scheduled restarts.** `live --hours` defaults to 8, so the live slates will restart at about 20:23 PDT, which resets the deployed per-process caps. The maker restarts every hour (`--duration 3600`).

**Not started by the launcher:**

* **Four orphaned test supervisors:** `bash scripts/sunday.sh start`, PIDs 25788, 28381, 29273 and 30093, from 2026-09-22/23. Each runs in a deleted `/private/tmp/claude-501/sunday-*` directory and loops `sleep 1` (8–11 CPU-min each). None has a Kalshi environment.
* **Another Claude session's practice loop:** zsh PID 51681. At 17:00:43 it started `arb_button_practice.py --sport ncaaf --phone --pairs 1 --mid --tap-wait 3600` (PID 93349): one paper practice push, then a wait of up to 1 h for your tap.

### Code-version evidence

**When HEAD moved** (`git reflog` of the GitHub clone, PDT):

| time | HEAD |
|---|---|
| 10:52 | `b831e0d` |
| 11:25 | `1ae9ef2` |
| 11:41 | `57c657b` |
| 11:42 | `1c39f82` |
| 11:56 | `5fc9397` |
| 12:22:56 | `3ca9684` |

It has not moved since 12:22:56.

**Which code each process loaded:**

* **week, live and live-ncaaf** started 4–8 s after `3ca9684`. The current maker child started at 16:37.
* **The bridge** started at 11:02 on `b831e0d`. Any module it imported lazily after 11:25 came from a later tree, so it is not pinned to one commit.
* **No source file** under `arb_engine/` or `scripts/` is newer than 12:22.
* **The `.pyc` compile times** (10:52, 11:42, 11:48, 12:20) line up with the commits.

**Order ledgers on disk.** The only ones are temporary: `/private/tmp/claude-501/kalshi_demo_check_*/ledger.sqlite3` (138 directories, from demo checks and tests) and `arb_test_*` ledgers. Both are wiped at reboot.

### Demo exchange reconciliation (GET only; the script refuses anything but a demo host)

**Account and scope.** Host `external-api.demo.kalshi.co`. `~/.kalshi` holds `demo.key` (0600) and `env` (`KALSHI_ENV=demo`, a key ID, the `demo.key` path), and no production key.

| item | exchange (demo), 16:36:35 PDT; rechecked 16:47:48, unchanged |
|---|---|
| balance | $238.24 play money; portfolio value $0 |
| resting orders / open positions | **0 / 0** |
| fills since 2026-09-22 | 26: 18 from the stack's LAG executor (17 orders, 585 ct, 10 tickers in 8 games), 8 from `kalshi_demo_check.py` round trips (PIT/CLE, 12:25–12:38 PDT) |
| journal ↔ exchange | all 17 journal orders with `fill_count > 0` are in `/portfolio/fills`; no stack fill missing from the journal |
| settlements | all 10 LAG positions: cost $248.13, revenue $395.00, fees $8.45 → +$138.42 (play money) |
| fees | all taker, centicent-precise (e.g. $0.7207 for 50 @ $0.71; the engine's `cent` default says $0.73) |

**The journal's prices are not fill prices.** The deployed executor journals `filled_notional` as filled contracts × its limit (the production ask the signal saw). Demo fills happen at the demo book's own prices:

| ticker | limit | demo fill |
|---|---|---|
| ATL | 0.87 | 0.31 |
| CLEM | 0.64–0.76 | 0.53 |
| TEX | 0.84 | 0.71 |
| COLO | 0.24 | 0.11 |

Summed over the 17 fills, that is $335.36 at the limit against $248.13 paid.

**The local files cannot resolve exposure on their own.** From them, 351 ct (ATL, CLEM, COLO, VT/BC) look unresolved, because the recorder slept through those finals. The exchange shows all settled.

### Runtime evidence from the stack's own files (read-only)

The windows are PT days, "since the previous audit" (after 11:54 PDT) and "current deployment" (from 12:23 PDT). "Markets" include spread and total lines; "games" strip them.

**Orders sent to the Kalshi DEMO exchange** (`lag_intents.jsonl`, 536 rows at 16:50, every one `mode=demo`; $ at the limit)

| window | entries sent (ct / $) | filled orders (ct / $) | zero-fill | skipped: unverified Robinhood settlement rule | lock IOCs |
|---|---|---|---|---|---|
| Thu 09-24 | 4 (200 / $107.50) | 1 (50 / $43.50) | 3 | 0 | 457, 0 filled |
| Fri 09-25 | 3 (139 / $99.64) | 3 (139 / $99.64) | 0 | 11 | 0 |
| Sat 09-26 | 44 (1,662 / $816.86) | 13 (396 / $192.22), 2 partial | 31 | 17 | 0 |
| current deployment | 17 (732 / $350.18) | 1 (50 / $42.00) | 16 | 11 | 0 |
| all | 51 entries to 19 games | 17 in 8 games | 34 | 28 | 457 |

* **Every row is a moneyline,** so markets equal games for orders.
* **The lock storm is still the only one.** ATL|GB on Thursday, 19:20–19:30 PDT: 457 IOCs averaging 47 a minute, $1,954 attempted, 0 filled.
* **Ambiguous responses.**
  * The journals and logs show none: 0 `error` rows, 0 missing `order_id`, 0 unknown `fill_count`, 0 `remaining_count > 0`, 0 EXEC ERROR pushes, and no order-code traceback. All 24 tracebacks are KeyboardInterrupt at restarts.
  * That is an absence of recorded failures, not proof. The deployed `HttpClient` re-POSTs an identical order on a timeout, 429 or 5xx. The executor journals a failed request as `error` without charging its caps, and a process killed in the middle of a POST journals nothing.
  * Two risky windows occurred: live-ncaaf restarted 11 s after two WAKE fills (12:23), and 2 orders went out during a 37 s dark wake at 13:20.
  * The exchange reconciliation found nothing unaccounted for.
* **Repeated signals became repeated orders.**
  * In 11 clusters, the same game, ticker and limit went out again within 10 s. Each was a new signal whose edge had grown, not a retry.
  * 4 of the extra orders filled: COLO 2×50, WAKE 2×50, VT 2×1, CLEM 50 then 39 a second later.
  * Two games were bought on both sides (VT|BC, WAKE|LOU).
  * The ledger's duplicate key includes the signal time, so it would not stop these.
* **Caps.**
  * No process exceeded its caps.
  * Across the 28 live/live-ncaaf process starts since 09-24, 09-26 *attempted* $816.86 against the $500/day cap. LOU|WAKE took 11 entries from 5 process instances.
  * The ledger's cross-process budgets fix this.
* **Fees and cancels.**
  * The deployed executor records no fees; the exchange has them (above).
  * There were no explicit cancels. The exchange cancelled the unfilled remainders of 493 IOCs.
  * `lag_locks` still shows 7 demo rows `watching`, all stale: lock watches are in memory and a restart drops them.

**Paper and practice only**

* **The "Robinhood done" button:** 134 issued, 110 confirm checks, 83 withdrawn (74 because Kalshi was short at the limit), 42 auto-practice runs (16 would have locked), 0 taps.
  * 13 listen errors, all during sleep or within 271 s of a wake.
  * 14 buttons were issued more than 60 s after their `created` time (worst +4,219 s), all across sleeps.
  * Every row that carries a mode is paper.
* **The maker:** paper only; 1,657 rests with `paper-` ids since 09-18, none since Thursday.
  * Two HEDGE NOW pushes for *paper* fills (09-24 19:18 PDT) do not say "paper".

**Alerts versus pushes** (alerts are signals, not trades)

| journal · window | BIG ARB | ARB | ARB CLOSE | SMALL / SUSPECT / GONE | pushed† | throttled | withheld | push errors |
|---|---|---|---|---|---|---|---|---|
| live-ncaaf · Sat | 58 | 44 | 504 | 79 / 10 / 47 | 346 | 41 | 220 | 18 × 429 |
| live-ncaaf · current | 1 | 1 | 126 | 25 / 6 / 45 | 0 | 0 | 142 | 2 × 429 |
| week · Sat | 408 | 133 | – | 138 / 36 / 48 | 93 | 416 | 0 | 32 × 429 |
| week · current | 10 | 15 | – | 42 / 18 / 47 | 0 | 0 | 0 | 25 × 429 |

† The process *believed* it pushed. Those processes predate `b831e0d`, whose curl fallback recorded a 429 as sent, so no push row is verified as delivered.

* **Nothing has been delivered since 10:51:57 PDT.** 50 ARB/BIG ARB attempts got 429. Every ARB CLOSE/FINAL since then was withheld, with ntfy reporting 0 remaining. The quota resets at 00:00 UTC (17:00 PDT).
* **Markets versus games, Saturday:**

  | process · tier | rows | markets | games |
  |---|---|---|---|
  | week · BIG ARB | 408 | 132 | 17 |
  | week · ARB | 133 | 116 | 19 |
  | live-ncaaf · ARB CLOSE | 504 | 48 | 48 |

* **Alerted economics are not results.**
  * Of week's BIG ARB rows on Saturday, 287 rows across 126 markets are plausible (cost ≤ $101, ≤ 1,000 contracts, margin ≤ 15c). Their median is 7.22c, and the first alert per market claims $564.45. None was bought.
  * 85 rows above 15c (max 35.3c; SHSU|TTU) and 122 above 1,000 contracts (max 250,000) all predate the 10:52 fix.
* **Repeats.**
  * The same-process 60 s push throttle held.
  * 14 ARB CLOSE re-alerts beat the 900 s cooldown, 9 of them across a sleep.
  * 3,358 of 3,370 early near-lock repeats follow a sleep: the pinned tick clock again.
  * 40 same-title, same-game pushes came from two processes less than 60 s apart, all before 11:54. The throttle is still per process.
* **Journal and log volume.**
  * The week journal is 99.4 % near-lock `info` rows (267,960; about 32 MB per awake day), and `Alerter.events` keeps every one in memory.
  * Logs: 4,595 DNS errors, all right after wakes; 2,535 of them in the 5 minutes after the 16:18 wake.
  * 66 Kalshi 429s, 55 while awake: the 1 s fast lane and the trade-print polling are being rate-limited.
  * No "database is locked".

**Recorder timing and coverage** (`history.db`, read-only)

* **Cadence.**
  * Full ticks run at 19–47 % of the 5 s target: p50 interval 9 s NFL, 21–29 s ncaaf; tick start to first write is 7.2 s / 24.6 s.
  * The 1 s fast lane is on target (p50 1.01 s).
* **Gaps.**
  * Every live-recorder gap over 120 s overlaps sleep.
  * The previous audit's 131 gaps across 20 games reproduce. Since then there have been 278 gaps across 49 games, 225 of them overlapping sleep.
  * Every per-game gap outside sleep comes from 6 episodes: 3 restarts, 2 post-wake stalls and 1 timeout.
  * The largest awake gap: the week scanner was deliberately off from 10:24 to 11:02 (1,664 s).
* **Quote age.**
  * Kalshi and Polymarket `*_quote_time` are **NULL in 100 %** of 256,578 `inplay_ticks` rows; the code never sets them.
  * Robinhood in play, fast lane: p50 5.2 s, p90 149 s, over 30 s in 22 % of rows. In the current window: p50 4.9 s, p90 69 s, 19 %.
  * Robinhood's "no timestamp" is −6,795,364,578 (Go's zero time) in 30.5 % / 17.6 % of home / away rows, and must be read as missing.
  * Full ticks store the tick time, not the fetch time.
* **Coverage.**
  * Saturday: 76 ncaaf games in play. live-ncaaf recorded 55; the week scanner covered 1,482 markets in 71 games. 20 FCS games are not on ESPN, so the live slate never sees them.
  * In-play game-hours lost to sleep (estimate): Saturday 76.3 of 130.9; in the current window 66.2 of 86.2 (77 %).
* **Games stuck "live".**
  * `live=1` after the game ended: ATL|GB and CCU|LIB 6.1 h, CAL|CLEM 4.0 h (new).
  * Cause: `tick()` returns early when no game is wanted, before `_live_priced` is rebuilt. The idle loop then ran every 5 s instead of 60 s.
  * Only 23 of 61 games since 09-24 ever got an ESPN `final` row.
* **Size.** `history.db` is 6.02 GB (+54 MB WAL) and grows about 1.2 GB per hour with ~20 live games. 37 GiB is free; no write contention.

### Missing evidence (absent is not success)

1. Kalshi and Polymarket venue timestamps, so their quote age cannot be measured.
2. Per-fill price and fee in local files (the deployed executor discards them). Only the exchange has them; they are reconciled above as of 16:47.
3. The HTTP status of each order POST attempt, and any order POSTed but never journalled. The exchange shows none unaccounted for as of 16:47, but later orders need the same check.
4. Push delivery: no row proves delivery, and none has been delivered since 10:52.
5. Local finals for 4 games (the Mac was asleep); only exchange settlement shows them.
6. Anything during sleep: about 3 h on Saturday and about 9 h on Thursday/Friday while games were live. Nothing recorded means no evidence either way about opportunities.
7. The throttle's `now` is not journalled; the stale-clock attribution comes from the code.
8. Week-scanner venue errors (not logged). Fast-lane errors are deduplicated.
9. Time-in-force per order row (IOC by code only), which process issued each button, and the listener's uptime.
10. Production behaviour of any kind: nothing ran there, and demo prices are not production prices.
11. Robinhood settlement rules. Their being unverified is why 28 LAG entries were skipped.
12. Why no executor rows exist before 09-24 18:59 PDT. Demo execution was on from 09-22 21:30, and there were no live games Tue/Wed, but that is not proven.

### Exact next steps

1. **Now, if tonight's slate matters:** put the Mac on AC and keep the lid open. It was on battery at 48 % at 16:30.
2. **Optional cleanup:**
   * `kill -TERM 25788 28381 29273 30093`, the orphaned test supervisors (check their cwd first; runbook §10).
   * `kill 51681 93349` if you don't want the practice push.
3. **Decide the known issues** in the runbook, above all #1: `run()` pins the tick clock, so the per-game time, clock refresh and polling-gap fixes are inert in production. The branch owner should fix it, add a test that goes through `run()`, re-run the suite, and only then pin `DEPLOY`. Deploying without it is safe on demo; those symptoms just continue.
4. **Deploy with `docs/DEPLOY_RUNBOOK_2026-09-26.md`, steps 0–8, at a quiet moment:**
   * `sunday.sh stop` **before** the fast-forward, so no process runs mixed code. The per-process restarts in *Exact next steps* below would leave the bridge and maker on old modules.
   * Reconcile on the demo exchange before stopping.
   * Deploy a pinned hash, and verify the commit, environment and account.
   * Run `kalshi_demo_check.py`, then watch `kalshi ledger` for the first hour.
5. **Pushing to GitHub is your decision** (public repo). Push `claude/exec-readiness`, and if you want them kept off this Mac, the 8 `codex/*` branches. Make the runbook's bundles regardless.
6. **Before 2026-10-08:** merge the `claude/micro-audit-3` and `claude/exec-readiness` evaluator lines (see 6a below), then decide H3 strictness and freeze.
7. **Not now, before production:**
   * a production fee check (centicent);
   * the Robinhood/Rothera settlement rules;
   * a shared ntfy throttle plus a quota plan (the free 250/day is gone by late morning);
   * keep-awake on AC;
   * ledger seeding if deploying mid-day.

## Watch-key identity audit (session c1ab42, 17:30 PDT): maker limits and ledger attribution

Branch **`claude/watchkey-audit`** off `claude/exec-readiness` `3c6b232`, commit `91ab70d`, built in
its own worktree. Not pushed, not deployed, not yet merged into `claude/exec-readiness`. It touches
`strategy/maker.py`, `strategy/broker.py`, `cli.py` (one keyword), `cli_plugins/maker_flags.py`,
`scripts/kalshi_demo_check.py` (one call), README and AGENTS test count, and the new
`tests/test_watch_identity.py`. It does not touch `execution/ledger.py`, which two other sessions
are editing. **1012 Python tests OK** on `91ab70d`.

**The finding.** The watch key is `f"{event_key}|kalshi:{outcome}"`, and event keys contain `|`
themselves (`nfl:BUF|DET:2026-09-20:spread:BUF-1.5`). Both readers cut at the first `|`, which kept
`nfl:BUF`: no opponent, no date, no market.

* **Maker limit.** `MakerRunner.reconcile` counted resting orders under `nfl:BUF`, but looked the
  limit up under the full event key. The two never matched, so the count restarted from zero every
  pass. `max_per_event` (default 1) held within one pass only. On the next pass, the other outcome of
  the same market could rest beside the first.
* **Ledger attribution.** `KalshiBroker.place` booked `event_key = game_key = "nfl:BUF"`, which put
  every BUF game, market and date in one bucket, and `nfl:ARI|BUF` under `nfl:ARI`. The maker sets no
  per-game budget, so this corrupted attribution and `exposure(game_key=...)`, not an enforced cap.

**What changed.**

* **Parsing.** `broker.watch_identity` splits on the last `|kalshi:`; a key without it is kept whole.
  `order_identity` returns (market = the full event key, game = `game_event_key`).
* **Explicit identity.** `place(..., event_key=, game_key=)` on every broker. `RestingOrder` carries
  both (`.identity`), and ledger rows store the market and the game.
* **Two limits.** `max_per_event` counts one market (a moneyline, one spread line or one total line).
  The new `max_per_game` counts every market of one game. It is **off by default (0)**; the flag is
  `--max-per-game`, added through the maker CLI plugin so the built-in help is unchanged.
* **What is counted.** The runner's own resting orders, plus `KalshiBroker.untracked_open()`: maker
  orders the ledger still holds open that this process did not place. That covers a dead
  predecessor's order whose recovery cancel failed or is unconfirmed, a lost create answer, and
  another live maker's orders.
  * Their identity is read from the full watch key each row keeps in `detail.watch`, so rows written
    before this fix, with cut columns, are read correctly. Those rows are not rewritten.
  * If the ledger read fails, nothing new rests that pass.
* **Demo check.** `kalshi_demo_check.py` passes its identity explicitly; the ledger value stays
  `democheck:<ticker>`.

**Tests** (`tests/test_watch_identity.py`, 9). 7 of the 8 behaviour tests fail on `3c6b232`:

* both outcomes of one moneyline, a spread, a total, the same team's next game and a game where it
  sorts second, over 3–4 reconciliation passes;
* a whole-game limit that counts every market of that game and nothing else;
* an order whose cancel failed still counts;
* ledger rows keep market and game through repeated poll/reconcile, and exposure splits exactly by
  game;
* restart with a failed recovery cancel: the market stays blocked until the next reconcile books
  the order's end;
* another live maker's orders count;
* a legacy row with cut columns;
* the CLI wiring.

**Open.**

* **Whole-game limit.** Decide whether to turn it on (`--max-per-game 1` or `2`). Off matches what
  the code intended before, but lets up to `--max-orders` rest across one game's spread and total
  lines.
* **Merge.** Merge into `claude/exec-readiness` next to the ledger sessions' work; conflicts are
  expected only in the AGENTS and README counts and in this document.

## Follow-up round (same day, evening): reproduced findings fixed, ledger hardened

Branch `claude/exec-readiness`, commits `5dfae25` → `a016721` on top of `2414d8b`. Still not
pushed and not deployed. Validation on `a016721`: **1003 Python tests OK**; JS parity 3650 fee +
54 arb vectors with 0 mismatches, `ok 3769` + `ok 159` checks, extension PASS;
`render_results.py --check` OK; `git diff --check` clean. **Kalshi demo exchange**
(`kalshi_demo_check.py`, demo only): **ALL PASS twice**, the second run with `--fill
--confirm-demo`. No production request or order was made.

| hash | what |
|---|---|
| `5dfae25` | **Finding 1.** `cancel_all(sweep=True)` ignored truncation: reproduced with 20 cursor pages, the sweep returned "complete" without the fallback. Now a truncated or failed listing runs `DELETE /portfolio/events/orders`, a complete re-listing must show the book empty (polled up to `settle_s`), the outcome is in `last_sweep` (`SweepReport`), and anything short of complete raises `SweepIncomplete` naming every order that may still rest. **Finding 2.** `env_host_problem` accepted `http://` (and user-info, other ports and paths, query, fragment) on recognised hosts. `endpoint_problem` now requires exactly `https://<known host>/trade-api/v2`, and the client refuses to sign any request to a non-HTTPS endpoint. |
| `2079269` | **Fee multiplier.** `fee_bound` defaulted to 1x and nobody passed the real one. Every live Kalshi series is 1, 0.5 or 0 today (14,394 series), but a 2x series would pay $0.35 where $0.20 was reserved. The multiplier is now required: read from `GET /series` on the trading exchange, taking the larger of that and the quote's value; unknown → no order. The adapter now marks an assumed 1x. |
| `af3ef98` | **Account binding.** The ledger stores SHA-256 fingerprints of the key id and of `GET /communications/id`, never the identifiers. A ledger belongs to one account: another account's client is refused. Only a client provably of the sending account can release a missing order. Key rotation on the same account carries on (`keys` map). New command `kalshi release --intent-id … --reason … --confirm` is the recorded escape hatch. |
| `57140f2` | **Maker through the ledger.** `KalshiBroker.place` records the intent before sending. A lost answer → unknown → no new placements until found; found resting and untracked → cancelled. `poll` books order rows (`apply_row`). `recover()` (run by the maker runner at start) cancels a dead maker's resting orders; a live one's are left alone. `tests/__init__.py` stops tests writing `out/orders`. |
| `a016721` | Demo check step 6c drives `KalshiBroker` through the ledger on the real demo exchange. |

**Answers to the investigation items**

* **Account vs environment.** Now bound to the account.
  * `GET /communications/id` is documented as "a public communications ID which is used to identify the user".
  * On demo it was stable across calls, and order rows carry a matching `user_id`.
  * Rotation is handled by the account fingerprint. It could not be tested live: there is only one demo key. If the id ever changed with the key, the ledger would refuse the new key (fail safe), and `kalshi release` or a new `ARB_ORDER_LEDGER_DIR` gets out.
* **Maker path.** Covered by these tests (fake exchange with resting orders, cancels, partial maker fills):
  * accepted-but-timeout → blocked → found → cancelled → done → unblocked;
  * a request that never arrived is released;
  * a restart cancels a dead predecessor's orders and books 4 fills at their cost;
  * a live predecessor is left alone;
  * partial fills are booked, then finished after the cancel.
* **Higher multipliers.** A property test covers multipliers 0–3, many limits and counts, every split and both roundings: charged ≤ reserved. The old 1x bound is shown to under-reserve a 2x market. The executor reserves at the exchange's 2x when the quote says 1x, and the reconciled 2x fee lands inside the reservation.

**Remaining gaps from this round**

* **Subaccounts.** The identity is per account, not per subaccount (orders use subaccount 0).
* **Owner liveness uses same-host PIDs** (checked here: a dead PID reads as gone, anything uncertain as alive). A maker that died on another host is never treated as gone, so its orders wait for their GTD expiry (≤ 1 h).
* **Shutdown timing.** `MakerRunner.shutdown` cancels without re-reading. The ledger shows those orders `accepted` until the next `recover()` books them. That doesn't matter for the LAG or button budgets, because budgets are per strategy.
* **Multiplier cache.** The multiplier is cached for 1 h, so a series change is picked up within the hour.
* **Sweep verification.** It trusts a complete listing within `settle_s` (10 s), while Kalshi's cancel-all is asynchronous. A late straggler is reported as unresolved (`SweepIncomplete`), not hidden.

## Read this first

* **The running live stack still runs the old executor.** It was started from the GitHub
  clone (`main` `1ae9ef2`–`3ca9684`) with `--execute-lag demo` and has per-process caps, lock
  legs retried every second, and no ledger. Nothing in this branch reaches it until it is merged there and the
  processes are restarted (next steps 1–3). I did not restart anything: it is your running
  system.
* **Demo-order readiness is verified against Kalshi's demo exchange.** This covers the ledger,
  the LAG executor's own path, real fills and real fees; details are under *Validation*.
  Production was never touched: no production request, no production order.
* **Kalshi's demo charges fees rounded up to the centicent, not the cent.** One contract at
  $0.56 was charged $0.0173, not the $0.02 the engine assumes by default. The default stays
  `cent` (the conservative choice) until a production fill agrees; see *Findings*.
* **The test fold stays sealed.** I ran only `--fold discovery`. I did not run
  `--freeze-spec`: freezing the spec is your decision.

## Commits (oldest first)

| hash | what |
|---|---|
| `b1c4d6c` | Merge GitHub `main` `1ae9ef2` (week-scan fix, "Robinhood done" button, practice taps) into the Codex audit line (`84806e4`, `8f326e7`, `a50e4e9`, `3ce3c97`, `a8b9246`). The only conflict was AGENTS.md's test count. |
| `a5f9dbb` | Paperexec WIP `1076f1a` integrated. The book identity known at decision time is authoritative; placeholder ids never count; "guaranteed" needs two known, distinct books; tie sums are Decimal. |
| `91ac811` | Recorder WIP `1a44841` integrated, with a fix: it stamped *every* fetched quote as freshly observed. A Robinhood contract the quote refresh did not answer keeps its cached catalogue time (up to 30 min old), and is now recorded with that time and `approx_time=1`, `refreshed=0`. |
| `3211f95` | **Durable order ledger** (`arb_engine/execution/ledger.py`), wired into the LAG executor, lock legs, the button and manual orders. Adds the host/environment gate and the manual-ops audit fixes. |
| `dc1c315` | Fixes from the runtime logs: each game in a tick decides at its own time; lock legs are spaced; stuck lock watches are swept; `logged_ts` added; polling gaps are journalled. |
| `835daec` | Demo check: lost-answer recovery through the ledger, plus an opt-in real fill with a fee comparison. Real fill fixtures recorded. The centicent finding is documented in `docs/VENUES.md`. |
| `daa7d60` | Demo check drives `strategy/lagexec.py` itself: signal → ledger → IOC → reconcile → a bounded lock leg → flatten. |
| `2cf14c0` | Selective port of `claude/micro-audit-2`, with look-ahead and accounting fixes. Discovery was re-run; the numbers are identical to the branch's. |
| `235be2d` | This handoff. |
| `bac2b46` | Merge GitHub `main` `3ca9684`, the other session's afternoon work (ticker-pair aliases, Polymarket US fee date, live book check before ARB pushes). Only the test counts conflicted. `ArbButton.confirm` only simulates. |

## Validation (all run this session; the suite, JS and render checks again on `bac2b46`)

* **Tests and checks:**
  * `python3 -m unittest discover -s tests -t .`: **975 tests OK** on `bac2b46` (113 s; 962 on `2cf14c0` before the merge).
  * `bash scripts/test_js.sh`: fee vectors 3650 checked with 0 mismatches, arb vectors 54 checked with 0 mismatches, `ok 3769 checks`, `ok 159 checks`, and the extension check PASS.
  * `python3 scripts/render_results.py --check`: OK.
  * `git diff --check`: clean.
* **Demo exchange** (play money; `~/.kalshi/env` read by the shell and never printed):
  * `scripts/kalshi_connect.py --env demo`: PASS. The first signed read returned a transient HTTP 500; two retries passed on both demo hosts.
  * `scripts/kalshi_demo_check.py`: **ALL PASS**, three runs. The last used `--fill --confirm-demo`. Each step's result:
    * Resting orders, the exchange-side expiry, and single and batched cancels all pass.
    * The IOC order shape passes.
    * **Lost answer:** an IOC answer thrown away on purpose blocks new exposure. The order is then found in `GET /portfolio/orders?ticker=&min_ts=` by its `client_order_id`, which unblocks. IOC orders that filled nothing are listed, status `canceled`.
    * **Engine path:** the LAG executor sent 1 contract at $0.56 through the ledger (153 ms). It was reconciled at the exchange's figures: $0.56 cost and $0.0173 fee.
    * `buy_lock`, asked for 5 contracts, was bounded to the 1 held. A second lock was refused ("1.00 of 1.00 contracts already hedged").
    * Both legs were sold back.
    * **Fee check:** the order row, the `/fills?order_id=` rows and the create answer (`average_fee_paid=0.0173`) agree. All are centicent-rounded.
* **Discovery replay** (the 15 NFL games of 2026-09-20/21, read-only on the live DB, 57 s):
  * Every metric is identical to `claude/micro-audit-2`'s own replay.
  * The frozen models are byte-identical.
  * Only the provenance and the code/spec hashes changed.

## What changed, by priority

**Priority 1 — recoverable, bounded execution** (`3211f95`, `dc1c315`, `daa7d60`)

* **The order ledger.** One SQLite file per Kalshi environment (`out/orders/kalshi_<env>_ledger.sqlite3`, setting `order_ledger_dir`), shared by every process.
  * **Before any request:** the intent and its worst-case cost are written and reserved against the daily and per-game budget in one `BEGIN IMMEDIATE` transaction. The worst case is count × limit plus a provable taker-fee bound (count × the one-contract fee at `min(limit, 0.5)`, rounded up per contract).
  * **On the order:** it carries the ledger's `client_order_id`, and a signal already sent is refused as a duplicate.
  * **Budgets are sums over the ledger**, fees included. They therefore survive restarts and span the NFL and college processes. The per-game cap now spans a game's markets.
  * **Unknown outcomes block new exposure** until reconciliation finds the order on the exchange. "Unknown" means: a timeout, a 5xx after `HttpClient` re-sent the POST, a 409, a response without an order id, or a pending intent whose process died or is past 300 s. A truncated listing never releases anything. A never-accepted order is released only after complete listings (30 s; one listing for a plain 4xx).
  * **Accepted orders are read back:** `fill_count_fp`, `taker|maker_fill_cost_dollars` and `taker|maker_fees_dollars`, with the fills cross-check keeping the larger fee. These actuals replace the reservation.
  * **Failure handling:** if the ledger cannot be opened, or a write fails after a send, the executor sends nothing more (`blocked`). A JSONL journal that cannot be written is counted and alerted once.
  * **Restart:** a restart first reconciles whatever was left open.
* **Lock legs.** A lock leg is exempt from the budgets, but it buys only what the entry *verifiably* filled minus what is already hedged or unresolved, at most 3 attempts, spaced 10 s apart. The lock book asks only for the unhedged remainder and closes the watch when the executor refuses.
  * Evidence: the live demo stack sent **457 zero-fill lock IOCs in 10 minutes** for one position on 2026-09-24.
* **Gates.** Every mutation, and `KalshiBroker`, goes only to a known Kalshi host of the client's environment. `KALSHI_BASE_URL` can no longer point the demo gates at production, and an unknown host is refused. `--execute-lag demo|live` requires a demo or prod client respectively.
* **Manual ops** (`cli_plugins/kalshi_ops.py`):
  * NaN and inf are refused before any client exists. Before, a NaN count slipped past the cap.
  * The cap is Decimal and states that it includes fees.
  * Confirmed orders go through the ledger.
  * Exit codes: 3 when blocked, 4 when the outcome is unknown.
  * `orders` and `fills` report `truncated`.
  * New commands: `kalshi ledger` and `kalshi reconcile`.
* **Tests** (`tests/test_order_ledger.py` and updated executor, lock, button and ops tests) cover:
  * accepted but timed out, 409, blank answer, a request that never arrived, truncated listings, read lag;
  * a dead process's in-flight order recovered at start, and a live process's pending order not treated as lost;
  * journal failure and ledger failure;
  * partial fills, zero fills, fills disagreeing with the order row, and order rows without cost fields;
  * 8 concurrent writers against one budget, and environment mismatch;
  * the fee-bound property.

**Priority 2 — runtime evidence and observability** (`dc1c315`; analysis read-only)

* Structured records now carry:
  * `req_ts`, `resp_ts`, `latency_ms`, `quote_age_s` and `signal_age_s`;
  * the intent id and `client_order_id`, the state (`accepted` / `ambiguous` / `rejected` / `done`) and the reason;
  * fills, average fill price and fee, with the reconciled cost and fees in the ledger and its `events` table;
  * `logged_ts`, and `polling gap` records.
* `kalshi ledger` shows the unresolved exposure. Nothing private goes into the repo: the ledger and journals live in the git-ignored `out/`.
* `balance.json` was deliberately not re-recorded.
* Regression tests derived from the observed failures:
  * the lock storm;
  * the stale tick clock;
  * cached Robinhood prices recorded as fresh;
  * a test that wrote `out/orders/arb_button.jsonl` into this repo, now fixed.

**Priority 3 — recorder and model audit** (`a5f9dbb`, `91ac811`, `2cf14c0`)

* **WIPs:**
  * `1076f1a` and `1a44841` are integrated, the latter with the fix above.
  * `5e3a857` is not reused: its items (Holm family, a bootstrap p-value, the H3 identity gate, H2 recovery, symmetric trade targets, chronological fits, the H4 split) are all covered by the ported code, and the sign-flip test replaces its centered-bootstrap p-value.
  * `738e678` was not touched.
* **`claude/micro-audit-2`, independently reviewed and then ported selectively.**
  * `6b7ef41` was dropped as superseded.
  * I kept HEAD's atomic loop, `settlement_for`, and the `no_of` guard; the branch had dropped the last two.
  * Fixed on top of the branch:
    * **A look-ahead in the complement selector.** Outcome sets were collected from the whole replay, so a later row with a third outcome label changed earlier complements. The selector now uses the outcomes observed so far, restricted to a moneyline key's two named outcomes.
    * Fees come from the traded venue, not the contract series' first row (which can be Robinhood's resale of a Kalshi book).
    * The side is part of the exposure key.
    * The evaluator uses `settlement_for`.
    * H3-lock hedges must be able to fill before the remainder's exit is decided.
    * Locked pairs are valued by their settlement (else a tie-safe pair's $1, else unresolved) instead of a flat $1.
    * The test-open guard also reads test runs recorded only in the audit log.
  * The review found the sign-flip test correct: exact up to 16 games, games not trades flipped.
  * All 18 of the branch's audit tests pass (before the port, only 4 of them passed on this branch), plus 7 new regression tests.
* **Discovery conclusions are unchanged.** Every speculative candidate loses after fees at 30 s:

  | candidate | 30 s, per contract |
  |---|---|
  | random buy | −3.9c [−4.2, −3.5] |
  | H1 momentum | −3.2c [−3.9, −2.5] |
  | H2 dip | −5.7c |
  | H2 recovery | −5.0c |
  | M prototype | −3.7c |

  * **H3 lead-lag:** 0 registered decisions under strict settlement identity (primary p = 1.0, reject). The any-settlement diagnostic is +2.66c [+1.15, +4.05] on 148 orders; it is a diagnostic only, not evidence.
  * **H4:** 162 arbs, all "speculation" because Rothera's rules are unverified; the win case is +0.94c [−0.28, +2.38].
  * Discovery is descriptive, not evidence.

## Findings from the running stack's own files

Read-only snapshot of `~/Documents/GitHub/arb-engine/out`, about 18:40–18:54 UTC on 2026-09-26. `history.db` was opened with `mode=ro`.

* **LAG executor** (all demo):
  * 497 intent rows: 457 lock IOCs, at least 24 `SUBMITTED` entries, 14 skipped for an unverified Robinhood settlement rule.
  * No `error` rows, and no duplicate order ids.
  * 8 of the 24 entries filled, for $170.66 filled notional.
  * No cap would have been exceeded, even summing across restarts: the highest day was $99.64, on CAL|CLEM, where the cap visibly bound.
* **The lock storm:**
  * One position (ATL|GB, 2026-09-24, 50 @ 0.87) got 457 IOC lock orders in 10 minutes, all zero fills, $1,954 attempted.
  * The likely reason nothing filled: the demo book is not the production book.
  * Fixed as described above.
* **Stale tick clock:**
  * Four buttons were created 815–821 s in the past, already past their 180 s TTL.
  * Auto-practice ran 34–37 s after issue instead of 10 s.
  * All 308 college ticket leg ages printed "0s ago".
  * 3 ARB CLOSE re-alerts beat the 900 s cooldown.
  * Fixed: each game now decides at its own time.
* **Alerts and cooldowns:**
  * Within one process the cooldown otherwise held: the 300 s / +1c rule was never violated, and the 60 s ntfy throttle held.
  * **40 same-title, same-game pushes came from two different processes less than 60 s apart.** The throttle is per process; *not fixed*.
  * 15 ARB and BIG ARB pushes were lost to HTTP 429 (the ntfy quota).
  * Week-scanner "arbs" above 15c (max 35c, mostly SHSU|TTU lines) all predate the suspect filter.
* **Polling gaps:**
  * 131 gaps over 30 s across 20 games in 3 days. 101 of them have no writes at all, which looks like the Mac sleeping.
  * These are now journalled as `polling gap` records.
* **Open, not fixed:**
  * `live=1` ticks continued for ATL|GB and CCU|LIB 6 h after ESPN's last row.
  * Kalshi and Polymarket `quote_time` are NULL in 100 % of recent `inplay_ticks`.
* **Button:** 21 issued, 0 taps, 12 auto-practice runs, all paper. `would_lock` was true in 3 of 12.
* **Clone B's `out/`:** all of the prior note's claims hold. `week_journal` has 373 ARB CLOSE rows over 191 keys; 182 of those keys were repeated 21–37 s apart at identical margins.

## The execution gap: Kalshi is automatic, Robinhood is manual

Only Kalshi has an order API the engine can use. Robinhood's prediction markets (Rothera, CDNA and KalshiEX-routed) have no API, so every Robinhood leg is bought by a person.

**What the engine does with each leg:**

* It can reserve, send, reconcile and bound the **Kalshi** leg (ledger, IOC, caps).
* For the **Robinhood** leg it can only *read* the quote. It never learns the price you actually paid or the size you got.

**Consequences:**

* A "Robinhood done" tap tells the engine to buy Kalshi against a Robinhood fill it cannot verify.
* If the Kalshi IOC misses or fills partly, the ARB FILL push reports the unhedged Robinhood contracts. Selling them, or waiting, is your decision.
* An order whose outcome is unknown is now reported as UNKNOWN ("check Kalshi before selling the Robinhood leg").
* Tie-proofness of a Kalshi + Rothera pair depends on Rothera's settlement rules, which are still unverified. That is why every discovery arb is classed as speculation.
* The demo exchange proves the plumbing, not the prices: its book is not production's.

## Unfinished work and known gaps

1. **Not deployed.** The live stack runs `1ae9ef2` without any of this (next steps 1–3).
2. ~~The maker's resting orders are not in the ledger~~ - done in `57140f2` (see the follow-up round above).
3. **The ntfy throttle is per process.** Two processes pushed the same game less than 60 s apart 40 times. A shared throttle (a small SQLite file in `out/run/`) would fix it.
4. **Fee rounding.** Demo charges centicent. Confirm on one production fill, then consider switching the `kalshi_rounding` default. That changes fee vectors, so follow AGENTS rule 1 and re-run `gen_fee_vectors.py` and `test_js.sh`.
5. **Microstructure test fold.** It is still unfrozen, and the H3 identity strictness is your decision. The test fold starts 2026-10-08 (TB @ DAL). Run `--freeze-spec` only after deciding.
6. **Not investigated:** the stuck `live=1` ticks after final, and the NULL Kalshi/Polymarket `quote_time` in `inplay_ticks`.
6a. **Two microstructure evaluator lines now exist.** Another session built
   `claude/micro-audit-3` (on GitHub; `24adfb5`, spec v5) from `micro-audit-2` + `main`. Its
   fixes: one decision per instant in `arb_scan`, `h3_lock_trades` and `select_trades`,
   deterministic `_dedupe`, and H4 denominators. It lacks this branch's port fixes: causal
   complements, traded-venue fees, the side in the exposure key, H3-lock hedge causality
   and settlement valuation, and the legacy-log test guard. It also lacks Codex's
   `84806e4` / `a50e4e9` / `1076f1a` (`settlement_for`, `no_of` guard, decision-time book
   identity). The two overlap in `h3_lock_trades` and `select_trades`. Merge them by hand,
   keeping every fix from both, re-run discovery, and do it **before** the test fold opens on
   2026-10-08.
7. **Demo balance.** This session's checks sent a few $0.01 demo IOCs (nothing filled), three filled 1-contract round trips and one lock leg, all sold back; together well under $1 of play money including fees.

## Exact next steps

> For deploying, steps 2–4 below are superseded by `docs/DEPLOY_RUNBOOK_2026-09-26.md`.
> That runbook stops the whole stack *before* the fast-forward, so no process runs mixed code, and adds:
>
> * reconciliation on the demo exchange first;
> * a pinned hash;
> * checks of the commit, environment and account;
> * the demo check;
> * recovery and shutdown.
>
> The expected test count is now 1003. Step 5 and *Also open* still stand.
> See *Deployment audit (17:10 PDT)* at the top.

```bash
# 1. Look at the branch (this clone)
cd /Users/kv15/Documents/ChatGPT/arbitradge && git log --oneline 3ca9684..claude/exec-readiness

# 2. Bring it to the GitHub clone that runs the stack (fast-forward while its main is still 3ca9684;
#    if --ff-only refuses, main moved on: merge FETCH_HEAD instead and re-run the tests)
cd ~/Documents/GitHub/arb-engine && git fetch /Users/kv15/Documents/ChatGPT/arbitradge claude/exec-readiness && git merge --ff-only FETCH_HEAD
python3 -m unittest discover -s tests -t . 2>&1 | tail -3        # expect 1003 OK

# 3. Restart each process, one at a time (`sunday.sh stop` stops everything and deletes *.args)
scripts/sunday.sh restart live -- --execute-lag demo
scripts/sunday.sh restart live-ncaaf -- --execute-lag demo
scripts/sunday.sh restart week
python3 -m arb_engine kalshi ledger        # the demo ledger: state, committed today, block if any

# 4. Re-verify demo after deploying (demo only; --fill spends <= $0.95 play money and sells it back)
set -a; eval "$(sed -n 's/^export //p' ~/.kalshi/env)"; set +a
python3 scripts/kalshi_demo_check.py --fill --confirm-demo

# 5. Before 2026-10-08: decide H3 identity strictness, then freeze (discovery-only command; never --fold test first)
python3 scripts/microstructure_eval.py --fold discovery --db out/history.db --freeze-spec
```

Also open:
* **Pushing.** Push `claude/exec-readiness` to GitHub if you want it there; the keychain token works with `-c credential.helper=osxkeychain`.
* **Production fee check.** Compare one production order review with `docs/VENUES.md` (centicent).
* **Robinhood fee check.** Compare a Robinhood order review's exchange fee with the assumed $0.01 per contract.

---

## Original prompt for this session

Inspected HEAD: `3ce3c97`, branch `claude/order-buttons`. Main checkout was clean.

### Prompt for Claude

Read AGENTS.md, README.md, docs/VENUES.md and docs/ARCHITECTURE.md. Complete the engine's
execution-readiness work and independently audit the microstructure experiment. Preserve
existing work. Use an isolated branch/worktree from the current integration HEAD. Do not
interpret passing tests or alert counts as evidence of profitable execution.

First inspect and reconcile these existing commits, selectively rather than blindly:

- Integrated: `84806e4` atomic microdata and normalized book-specific settlement;
  `8f326e7` manual Kalshi operations; `a50e4e9` decision-time paper arb book identity;
  `3ce3c97` shard-aware cancellation and failed-cancel visibility.
- Unintegrated audit branch: `claude/micro-audit-2`, especially `9774a7d` and `2d28b24`.
  It changes microdata, evaluator, manifest and discovery reporting together. Compare
  settlement compatibility with `settlement_for` on current HEAD; preserve normalized
  Robinhood NO semantics. Review the complement selector's use of outcomes collected
  from the entire replay for future dependence. Do not open the test fold.
- Preserved WIP `1076f1a` on `codex/paperexec-audit`, worktree
  `/Users/kv15/.codex/worktrees/paperexec-audit/arbitradge`.
- Preserved WIP `1a44841` on `codex/recorder-poller-audit`, worktree
  `/Users/kv15/.codex/worktrees/recorder-poller-audit/arbitradge`.
- Older evaluator WIP `5e3a857` on `codex/evaluator-fixes` explicitly has a known
  focused-suite failure. Inspect before reusing. These WIPs have not been validated in
  this handoff session. Never merge `738e678` wholesale: its settlement inversion was faulty.

Priority 1: make automatic execution recoverable and bounded.

Audit `strategy/lagexec.py`: sent_notional/per_game/day are process-local; restart resets
caps. `_journal` swallows OSError. A submission exception returns without reserving possible
exposure, although the server may have accepted the order. Introduce durable intent and
budget reservation before submission, stable client_order_id, reconciliation of ambiguous
responses via orders/fills, persisted daily/per-game limits, and restart recovery. Account
for actual fees and fills. Block new exposure if durable recording or reconciliation fails.
Test accepted-but-timeout, duplicate signal, process restart, journal failure, partial fill,
unknown response, concurrent writers, and demo/live environment mismatch. Lock legs need
verified existing inventory and bounded retries so they cannot overhedge.

Audit manual operations too: numeric inputs must be finite, caps must use Decimal and
clearly state whether fees are included, blocked commands must return nonzero status,
and account reads must expose pagination truncation. Verify endpoint/environment agreement
when KALSHI_BASE_URL overrides the host. Preserve confirmation and production opt-in gates.

Priority 2: analyze runtime evidence and improve observability.

Available local evidence: out/week_journal.jsonl has 373 records, all ARB CLOSE, across
191 distinct event keys, from 2026-09-23 19:29:56.676662 UTC to 19:30:56.120449 UTC.
All recorded margins are negative: -0.0299 to -0.0091 dollars per contract/set.
This is near-opportunity logging, not executed trades or realized profits. Investigate
repeated alerts, cooldown behavior and market-level versus game-level counts before
changing alert thresholds. out/logs and out/run are empty. out/eval_log.jsonl contains
six discovery records, one lacking git/time provenance. No live fill evidence was found
in these inspected paths. Do not imply all other locations were searched.

Add structured records for request/receipt time, quote age, gate reasons, intent id,
submission outcome, fills, fees, unresolved exposure, reconciliation, and polling gaps.
Keep credentials/private account details out of committed logs. Add offline regression
fixtures derived from failures, not fabricated empirical performance.

Priority 3: finish recorder and model verification.

Review preserved recorder/paperexec WIPs; test exact versus approximate receipt timing,
carried/partial/failed updates, public-print pagination and restart, bounded paper entry
and exit windows, duplicate liquidity, partial unwinds and unresolved inventory. Independently
review the external evaluator audit's frozen spec, game folds, sign-flip/bootstrap inference,
complement purchases and fee accounting. Discovery is 2026-09-20/21; keep test sealed.
Use replay evidence before changing model weights or sizing. Retain momentum signal-only/off.

Deliver a reproducible demo-readiness path using the existing scripts/kalshi_connect.py and
scripts/kalshi_demo_check.py. Validate credentials locally without printing secrets. Demo
orders require explicit demo confirmation; do not submit production orders as an audit step.
Kalshi has an order API integration; Robinhood remains manual. Explain this execution gap.

Stdlib only in arb_engine except optional cryptography. Preserve fee formulas and all
execution gates. Feature work goes through plugins, not cli.py/config.py. Document settings
in AGENTS.md. Results fixtures are metrics-only; render documentation with render_results.py.
Run focused adversarial tests, full Python suite, JS parity, render_results.py --check and
git diff --check. Update the test count. Commit coherent changes regularly and maintain
this handoff with hashes, unresolved failures and exact next commands before stopping.

### Validation provenance

Prior integrated validation at `3ce3c97`: 871 Python tests; JS 3769 + 159 checks;
render check and diff check passed. Those checks were reported in the previous session,
not rerun for this documentation-only handoff. No account request or order was made here.

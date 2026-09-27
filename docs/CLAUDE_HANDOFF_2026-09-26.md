# Engine audit and execution handoff — 2026-09-26

Repo: `/Users/kv15/Documents/ChatGPT/arbitradge`, branch **`claude/exec-readiness`** (created
this session from `claude/order-buttons` @ `a8b9246`). Not pushed. **Deployed at `bf7d2d5`** to
the GitHub clone (`~/Documents/GitHub/arb-engine`) that runs the live stack, at 19:27 PDT, as a
fast-forward from `3ca9684` (see *Deployed* below). The original prompt for this session is kept
at the end.

**Sections added on the evening of 2026-09-26.** Each was written by its own session; code
commits are listed in merge order:

1. *Deployment audit*: docs only.
2. *Lock legs* (`75a17e0`).
3. *Watch-key identity audit* (`91ab70d`, merged as `58aa18f`).
4. *Ledger evidence* (`f4b3356`).
5. *Historical trade tapes* (`c2cd4d8`).
6. *H4 same-timestamp ordering* (`5480dfc`).
7. *Fill evidence only grows* (`8522074`, release follow-up `aa7dda7`, 2026-09-27): the ledger defects from the audit of
   `bf7d2d5`. **Not deployed**; the GitHub clone still runs `bf7d2d5`.
8. *Historical trade tapes, round 3* (`d161cf0`, 2026-09-27): a second review's findings, in
   the trade-tape section. Research code only; nothing the live stack runs.

Deploy with `docs/DEPLOY_RUNBOOK_2026-09-26.md`.

## Fill evidence only grows (`8522074`, session db5ad6, 2026-09-27)

The ledger defects from the audit of `bf7d2d5`, fixed and tested offline.

* **What this round did not touch.**
  * No order was sent and nothing was enabled.
  * The only exchange calls were GETs on the Kalshi **demo** account (fill and order
    listings, to verify API shapes).
  * No evaluator file was edited, and the test fold stays sealed.
* **Integration.** It sits on the peer rounds merged as fast-forwards: `f74cb19` / `d67aef0`
  (trade tapes, session c1ab42) and `9fb8e98` (H4, session 89f99c).

### What was wrong

`_apply_order` could erase fills already reported. The steps:

1. Reserve a 1-contract LAG IOC.
2. `accepted(…, {"order_id": "o1", "fill_count": "1"})`.
3. Apply the row `{"status": "canceled", "fill_count_fp": "0", "remaining_count_fp": "0"}`,
   with a client whose `fills_v2` lists one fill for `o1`.

The result was `done: 0 filled, $0 + $0 fees`, and `fills_v2` was never called: fills were
read only when a row reported some. The same hole let a later row reporting fewer fills
(10 → 4) finish at the lower count. A lock leg already sized on 10 was then over-hedged.

### The rules now

The module docstring (6. and 7.) and AGENTS.md rule 3a state them in full.

1. **Fill evidence only grows.**
   * Every count an exchange answer shows is kept in `fill_seen`, with its source in
     `fill_source`, and survives restarts. The sources are the create answer (even a non-final
     one), order rows, and fills listings (a lower bound even when truncated).
   * A count below it, or order row and fills listing apart, makes the intent `contradicted`
     (`fill_state`).
   * A contradicted intent releases nothing: an IOC goes back to its whole worst case, no
     lock leg is sized on it, and the LAG executor pushes one EXEC ERROR per intent.
   * It clears only when the answers agree again at or above the highest count seen.
   * A row that says an IOC may still fill (no terminal status, some or no remaining
     quantity) also puts it back at its whole worst case.
2. **Finishing needs the fills listing.** `done` needs a final order row whose count no
   earlier answer exceeded, plus a complete, correctly scoped fills listing that agrees with
   it. That applies to zero-fill cancellations too (an empty complete listing).
   * **Complete:** every page read, no error.
   * **Correctly scoped:** only this order's rows, each on the intent's market ticker and
     `book_side`, every fill with an id, and duplicates identical.
   * **An order row must name the intent's market, book side and `client_order_id`.**
   * **A listing that fails** (missing, failed, truncated, or rows of other orders) releases
     nothing, and the intent is read again.
   * **Trailing fills.** A listing that trails the row confirms nothing; it is no longer
     finished at the row's count.
3. **Lock legs only on verified fills.** `verified` means a final row and a complete listing
   agree; `corrected` is an operator's correction.
   * A create answer alone, an order row alone, or a trailing listing sizes no lock leg.
   * The caller (`buy_lock`) and the ledger (`_lock_check`) both check this.
   * Earlier lock legs and exits that are not verified count at their full size. A refusal
     they cause says `waiting - … not reconciled yet`, which is not terminal, so the lock
     book keeps watching instead of ending the watch.
4. **Exchange corrections are explicit.**
   * The ledger never takes a lower count by itself.
   * `kalshi correct --intent-id X --reason "…"` is a dry run; add `--confirm` to book
     (`OrderLedger.accept_correction`). It re-reads the order and books only a final row
     and a complete listing that agree on a count below the one seen. It records the
     evidence it overrides and the reason, and sets `fill_state = corrected`.
   * `kalshi release` now refuses an intent that showed fills.
5. **Compare-and-set writes.**
   * `rejected`, `ambiguous` and `done` apply only from the states they expect. A stale
     decision is recorded as `stale-write-ignored` and overwrites nothing.
   * `done` and every hold re-check `fill_seen` inside the write, so another reader's newer
     count wins.
   * A create answer arriving after a release reopens the intent: the exchange has the order.
6. **Finite times.** `reserve` refuses a non-finite `quote_ts` or `now`: NaN, ±inf, a
   string or a bool. A NaN quote time used to pass the lock leg's freshness check. The
   other entry points raise `LedgerError`.
7. **Re-sent POSTs.**
   * `HttpClient` records `attempts` on an `HttpError`, and `refusal_hint(e)` returns the
     status only for a single-attempt 4xx.
   * A 4xx answering a re-send of a POST the exchange already accepted used to release that
     order after one listing, under read lag (audit h05). Now the full not-found window
     applies.
   * `KalshiClient.paged` raises on a page without its rows list. An empty listing is `[]`
     (checked on demo), so a malformed 200 can no longer prove an order absent.

**Fact checked on demo (GET only), now in `docs/VENUES.md`.** A YES sell's order row says
`side: yes, outcome_side: no`, and its fills say `side: no, outcome_side: no`. Only
`book_side` (`ask`) names the direction, so the scope check compares `book_side`, never the
legacy fields. Comparing `side` would have held every sell as contradicted.

### The independent audit (session c1ab42, repro scripts run against this tree)

**Fixed:**
* **h01** (zero-fill row skips fills): contradicted, not done.
* **h02 and s03** (count decreases, over-hedge): held, and no lock is sized.
* **h03 and h03b:**
  * a later row showing fills raises the booked count;
  * a resting 3/7 row puts the IOC back at its whole worst case.
  * **Case 2** ("canceled, 0 filled, 10 remaining" after a final 0/0 create answer) held at
    $0 until the follow-up commit after `1edc7b0`. Now any remaining quantity above zero, even
    beside a terminal status, puts an IOC back at its whole worst case until a consistent
    final row and a complete listing arrive (`RemainingQuantityTests`).
* **h05** (4xx under read lag) for re-sent POSTs, via `attempts`. The script's synthetic
  single-attempt 400 is still read as a refusal.
* **h07** (row identity): market, book side and `client_order_id` are checked.
* **h08:** fully fixed only at `aa7dda7`; at `8522074` just H8a and H8c were.
  * **H8a / H8c:** a release of an intent that showed fills is refused.
  * **H8a' (`aa7dda7`):** any *accepted* intent is refused too. Released, a resting maker
    order was skipped by every later reconcile and cancel, stayed on the book and filled
    while the ledger said $0. A restarted maker now takes it off the book.
  * **H8b (`aa7dda7`):** a client whose account cannot be proved to be the sender's is
    refused, the rule `reconcile` already had. `kalshi release` with a key whose account
    was unreadable used to release another account's unknown order.
* **h09** (stale release over `done`): ignored.
* **Caller layer:** `buy_lock`'s own verified-fill check duplicates the ledger's. It is now
  tested on its own (`CallerLayerTests`: no positions read, no reservation for an unverified
  or contradicted entry), so removing it fails a test.
* **s16** (terminal while unresolved): now a wait. **Residual,** re-run at `1ef5d71` with the
  auditor's `s16_verified.py`: when a lock leg's answer was lost and reconciliation later
  finds it filled, the lock book ends the watch as "closed with 0 of 10 hedged" (status
  `expired`).
  * The ledger has the leg `done` with 10, and nothing is over- or under-hedged.
  * `LagLockBook.locked_contracts` counts only fills from its own SUBMITTED records.
  * That log line could lead a person to hedge by hand again. A fix would read the verified
    lock legs from the ledger when a watch closes.
* **r01** (NaN `quote_ts`): refused. The script pins its own checkout; run on this tree
  through a copy.

**Not addressed** (for the next round):
* **s15:** the exchange-holding cap in `buy_lock` is per market, so two entries on one
  ticker share one position.
* **s17:** another entry's NO lock on the same ticker counts as this entry's exit. That is
  conservative, so it under-hedges.
* **h06:** `min_ts` clock skew.
* **h10:** `KalshiBroker.poll` reads a missing fill count as 0, in memory only; the ledger
  is unaffected.
* **h11–h16:** no bound on a row's fill cost, and the lows.
* **Single-attempt 4xx (h05's synthetic case):** still read as a definitive refusal.
* **The auditor's non-finite-time sweep at `f496d78`,** outside the ledger (message of
  2026-09-27, not verified here):
  * `arbalert.py` (`if qt and now - qt > lag`): a NaN, +inf, far-future or microsecond
    quote time gives a BIG ARB push instead of SUSPECT. This is the phone-button path.
  * The in-play STEAL quote-old gate: a NaN, +inf or future quote time reads as age 0.
  * `arbbutton`: a spec issued at NaN never expires.
  * `KALSHI_GTD_HORIZON_S=inf`: an OverflowError after `reserve` leaves a pending row that
    blocks exposure for 300 s.
  * `LEADLAG_WINDOW_S=nan`: 10 IOC orders instead of 1.
  * The scanner's stale flag misses NaN, +inf, future and zero timestamps.
  * `reserve(now=<a finite past time>)` books onto another day and escapes the daily cap.
    That is an API hazard only: live callers pass the wall clock, and the tests' own
    `Clock(1000.0)` is such a time, so a range check needs its own round.

### Tests

`tests/test_ledger_fill_evidence.py` has 35 adversarial tests:
* the reported reproduction;
* decreasing counts (final, resting, down then up);
* zero after positive;
* duplicate, conflicting and id-less fills;
* fills of other orders, markets and book sides;
* a YES sell's real fill shape;
* a page failing mid-listing, a page without rows, a truncated listing, and an orders page
  without rows that never releases;
* restart with a later lower row, and restart after a lost answer;
* non-finite times at the boundary and at the caller;
* four legitimate zero-fill cancellations (IOC, maker, lost answer, failing listing) that
  resolve normally;
* locks on unverified, trailing, contradicted and corrected entries;
* the correction and release rules, and `kalshi correct` dry-run and confirm;
* stale writes, and a late answer after a release;
* a re-sent POST's 400 end to end;
* a 300-case seeded property test: evidence never shrinks, exposure never falls below the
  highest count seen, and `done` only on agreeing complete evidence.

**Existing tests changed** where the stricter rules apply:
* Finishing tests now pass the agreeing fills listing.
* Lock tests reconcile their entries first; the lock book's tests reconcile each tick, as
  the live loop does.
* The fake exchanges list resting fills, and each order's row and fills.
* The offline demo check serves per-order fills.
* The recorded demo row carries the intent's own `client_order_id`.

### Validation (exact)

Run on `8522074`, the tree combined with the peers' `d67aef0`:
* `python3 -m unittest discover -s tests -t .`: **Ran 1136 tests in 115.344s, OK** (1101 +
  35), about 116 s wall-clock with the Mac awake.
* `bash scripts/test_js.sh`: fee vectors 3650 checked, 0 mismatches; arb vectors 54, 0
  mismatches; `ok 3769 checks`; `ok 159 checks`; extension PASS (0 warnings).
* `python3 scripts/render_results.py --check`: results blocks OK.
* `git diff --check`: clean.
* **Not run:** the live demo order check (`scripts/kalshi_demo_check.py`). It sends demo
  orders, and this round sends none. It reconciles its entry to `done` before the lock leg,
  so it should pass under the new rules; run it before deploying.

### Limitations

* **One extra GET per finished order** (`/portfolio/fills?order_id=`). While the fills
  endpoint fails (demo's intermittent 500s), orders stay `accepted` longer.
  * A zero-fill IOC keeps exposure $0 from its final create answer.
  * An IOC with fills keeps them at the limit plus the fee bound.
  * Lock legs wait.
* **An order already `done` is never re-read,** so an exchange correction after it finished
  goes unnoticed. Kalshi's correction notices are not consumed.
* **A single-attempt 4xx is still trusted** as a refusal after one complete listing past
  `settle_s`.
* **Rows finished before this change** have no `fill_state`. As entries they are never
  hedgeable, and as exits or lock legs they count in full.
* **Cost bounds (h11) are not checked.** A row's fill cost is not compared with count ×
  limit.

### Next steps

1. **Run the live demo check before deploying:**
   `python3 scripts/kalshi_demo_check.py`, then the same with `--fill --confirm-demo`.
   Demo play money only, with explicit demo confirmation.
2. **Deploy** `8522074` (or later) with the runbook. Its step 8 now covers `contradicted` rows
   and `kalshi correct`. The deployed demo ledger's rows migrate in place: new columns, no
   rewrite.
3. **Address s15** (a per-market holding cap shared across entries), **h10** and **h11**
   (cost bounds).
4. **Keep the Mac awake, lid open and on AC,** during any deploy or live slate. See the
   corrected note under *Deployed*.

## Deployed: `bf7d2d5` on the GitHub clone (19:27 PDT, session db5ad6)

The runbook was followed step by step, except the power check. Execution stays on Kalshi
**demo**; nothing turned production on.

* **Power (step 0.4) failed and was overridden.**
  * The Mac was on battery: 14 %, 1 h 08 min left, Low Power Mode on.
  * The deploy adds no order risk. A stopped stack sends nothing, and the new code records
    every order in the ledger before sending it.
  * **Plug the Mac in.** `caffeinate -i` stops neither a battery shutdown nor lid-closed sleep.
* **Backups (step 1).**
  * `~/Documents/arb-backups/2026-09-26/{arbitradge-all,arb-engine-all}.bundle`, both verified.
    The uncommitted-work patch is empty.
  * The rollback point is the local tag `pre-deploy-2026-09-26` → `3ca9684` in the clone.
* **Demo account before the stop (step 2).**
  * 0 resting orders (not truncated).
  * 8 open positions from the day's LAG entries: OKST 50 / WVU 50, TROY 28 / USU 24,
    ARIZ 28 / WSU 6, MSST 145, USC 120.
  * Cash $5.32, portfolio $237.24.
  * No LAG fill in the 10 minutes before the stop.
* **Stop (step 3).** 18:47:08–18:47:19: all five down, no engine process, `caffeinate` gone.
  0 resting after the stop.
* **Fast-forward (step 4).**
  * `main` moved from `3ca9684` to `bf7d2d5` (50 files), and the tree is clean.
  * Suite: `Ran 1054 tests in 114.685s`, **OK**.
  * JS: fee vectors 3650 / 0 mismatches, arb vectors 54 / 0, `ok 3769`, `ok 159`, extension PASS.
    Render check OK.
  * No new `sunday.sh start` orphans (only the four known ones).
  * **The suite process ran 21 minutes wall-clock** (18:47:38–19:08:57).
    * **Corrected on 2026-09-27: it was sleep, not a hang.** `pmset -g log` shows the Mac in
      *Clamshell Sleep* at 18:48:47 (lid closed, on battery at 14 %), then asleep from
      18:50:45 to the 19:08:44 dark wake. The run finished right after that wake, and
      unittest's timer does not count sleep.
    * The earlier guess here, a thread spinning at shutdown, was wrong. The same suite takes
      ~115 s wall-clock awake.
    * No demo order was created in that window.
    * The stack started at 19:27 also ran mostly in dark wakes while the lid stayed closed.
* **Start (step 5).**
  * `BANKROLL=500 KELLY=0.25 scripts/sunday.sh start -- --execute-lag demo`, 19:27:29–19:27:40.
  * Preflight: GO WITH WARNINGS (9 pass, 3 warn). The warnings: no NFL game today, so no ESPN
    matches; the bridge was not up yet.
  * The stack was down for 40 minutes in total, mostly for the slow suite.
* **Verify (step 6).**
  * **Processes.** All five up, started 19:27:33–19:27:40, with cwd = the clone at `bf7d2d5`.
  * **Environment.** `live` and `live-ncaaf` carry `KALSHI_ENV=demo` and none of
    `ARB_LIVE_TRADING` / `ARB_BUTTON_MODE` / `KALSHI_BASE_URL` / `EXECUTABLE_VENUES`.
  * **Logs.** `LAG execution: demo (caps: 50 ct/order, $100/game, $500/day)` twice.
  * **Sleep.** `caffeinate -i -w` holds on the new live supervisor.
  * **Keys.** `~/.kalshi` holds `demo.key` and `env` only; `kalshi_connect.py --env demo` PASS.
  * **Ledger.** `kalshi ledger`: env demo, `blocked` None, 0 intents. It is bound to the demo
    account by fingerprint (`meta.account_fp`).
  * **Recovery.** Each live process journaled a `recover` row (`reconciled: []`, `blocked: null`).
  * **Errors.** No Traceback, UNKNOWN or EXEC ERROR since the start.
* **Demo order check (step 7).** Not re-run from the clone. Session 89f99c ran it on a clean
  export of `bf7d2d5` at 18:44–18:45: ALL PASS, plain and `--fill`. See *Ledger evidence →
  Validation*.
* **First orders on the deployed code (20:42 PDT).**
  * Two LAG IOCs: 50 × `KXNCAAFGAME-26SEP26AFANEV-NEV` YES @ $0.43, both unfilled (canceled by
    the exchange).
  * The ledger took each one reserved → accepted (`final: true`) → done, from the order row,
    within 25 s.
  * `blocked` None, nothing open, `committed_today` lag $0.
* **Watch the demo cash.**
  * **Cash recovered by 20:43.** It was $5.32 at the deploy; settlements brought it to $79.32
    (portfolio $171.33, 3 open positions).
  * When cash runs short, most demo orders will be refused for lack of balance.
    `LagExecutor` does not read the balance before sending.
  * **Each refusal starts as UNKNOWN.**
    * An exception from the create call is booked as **ambiguous**, with the HTTP status as
      its `hint` (`lagexec.py` `_send`).
    * The executor pushes "LAG auto-trade (demo) outcome unknown", and new LAG orders are
      blocked.
  * **The next complete order listing without the order releases it as `rejected`**
    (`ledger.py` `_reconcile_one`):
    * after 2 s for a 4xx other than 409/429;
    * otherwise after 30 s and two listings.
  * Demo's intermittent HTTP 500s on reads can stretch a block.
  * Top up the demo account, or run `--execute-lag intent`, to avoid a stream of these.
  * The runbook's step 8 (first hour) applies as written.

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

## Lock legs: the budget exemption only for proven hedges (`75a17e0`, session db5ad6)

**What was wrong.** Reproduced in memory: `OrderLedger.reserve(parent_id=...)` granted a
budget-exempt 10-contract "lock" on `KXNFLGAME-26SEP21BUFMIA-BUF` (game B) against a filled KC
entry in game A. A direct `LagExecutor.buy_lock` sent both a same-side addition (more KC)
and an unrelated-game order priced on a quote 900 s old. The entry's quantity bounded the
count, but nothing checked the relationship between the two contracts.

**Which layer checks what.**

| layer | checks |
|---|---|
| caller: `LagExecutor.buy_lock` | the quote is Kalshi's, for the entry's market (quote event = position event = entry event), no older than `lock_quote_max_age_s` (10 s), priced at or under its ask; the lock contract's settlement identity comes from the registry's *verified* rule (unknown → refused); the exchange still shows the entry's contracts (`GET /portfolio/positions?ticker=`; unreadable → refused, fewer → capped) |
| ledger: `OrderLedger._lock_check` (atomic with the reservation) | the entry is this account's verified purchase with a recorded settlement identity, and not itself a lock; same market (event key and Kalshi event ticker); no same-side addition (not the entry's own contract or outcome; outcomes named by the moneyline key); complementary payoffs (tie payouts known and summing to ≥ $1 where the market can tie; a NO of the entry's own market qualifies); the claimed quote time within 10 s; no *related* order of unknown outcome (the entry, its lock legs, anything on either ticker); remaining inventory = fill − exits since the entry (any strategy) − every earlier lock leg; attempt cap |

**Unrelated unknown orders.** An unknown order in another game still blocks new exposure,
but not a proven hedge (test
`test_a_legitimate_hedge_goes_out_while_an_unrelated_order_is_unknown`).

**Transient refusals.** The lock book now ends a watch only on terminal refusals (attempts
used up, nothing left to hedge). A transient one, such as a stale quote or a failed read,
keeps watching.

**Validation (`75a17e0`).**
* **Tests:** 1030 Python tests pass, 27 of them new in `tests/test_lock_identity.py`.
* **Checks:** JS parity (3650 + 54 vectors, 0 mismatches, `ok 3769` + `ok 159`),
  `render_results.py --check` and `git diff --check` all pass.
* **Kalshi demo check** (`--fill --confirm-demo`): **ALL PASS** on the run after two
  transient demo HTTP 500s. Those runs stopped before any order, or had the lock refused
  fail-closed on the failed positions read. The passing run shows:
  * the entry recorded its settlement identity;
  * the exchange showed 1.00 held;
  * the lock (5 requested) was bounded to 1 and sent;
  * a second lock was refused with "nothing left to hedge (filled 1.00, exited 0, hedged or
    unresolved 1.00)".

**Gaps.**
* **Inventory is attributed conservatively.**
  * Sells or opposite-side buys on the entry's ticker count as exits of *every* entry on
    that ticker (never under-counted).
  * The exchange-position cap is per ticker, not per entry.
* **Old entries are never hedged.** Entries written before this commit carry no settlement
  identity, so the exemption refuses to hedge them.
* **Pushes block hedges.** A market that can push but whose registry rule states no push
  payout (e.g. Kalshi NFL spreads on integer lines) is never hedged under the exemption.

## Watch-key identity audit (session c1ab42, 17:30 PDT): maker limits and ledger attribution

Branch **`claude/watchkey-audit`** off `claude/exec-readiness` `3c6b232`, commit `91ab70d`, built in
its own worktree. Not pushed, not deployed, not yet merged into `claude/exec-readiness`. It touches
`strategy/maker.py`, `strategy/broker.py`, `cli_plugins/maker_flags.py` (`cli.py` is untouched, AGENTS rule 8),
`scripts/kalshi_demo_check.py` (one call), README and AGENTS test count, and the new
`tests/test_watch_identity.py`. It does not touch `execution/ledger.py`, which two other sessions
are editing. **1012 Python tests OK** on `91ab70d`; merged with `claude/exec-readiness` `14ada17`
(the lock-identity work): **1039 OK**.

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

## Ledger evidence: a reservation is released only on final evidence (`f4b3356`, session 89f99c)

This round covers `arb_engine/execution/ledger.py`: `accepted`, `_apply_order` and the fills
cross-check, plus two new module helpers, `order_evidence` and `fills_evidence`. It made no
schema change. The APIs the other rounds depend on are unchanged: `rows()`, the row keys,
`reserve(...)`, and the lock-leg check.

### What was wrong

**The two reported defects**, reproduced offline on `3c6b232`:

* **Missing status and remaining.** `apply_row` with an order id and `fill_count=0`, but no
  `status` and no remaining quantity, marked the intent `done`. A resting 10-lot's $6.20
  reservation was freed while the order could still fill.
* **Missing fees.** `status=executed`, `fill_count=1`, `taker_fill_cost_dollars=0.50` and no
  fee fields finalized with $0 fees. The $0.02 fee bound was dropped: exposure $0.50 instead
  of $0.52.

**The same class of defect, found in the audit:**

* **Truncated listings looked complete.** The fills cross-check went through `fills_v2`,
  whose paging stops at 20 pages without saying so.
* **Duplicate fills were summed twice.**
* **Rows without an `order_id` were counted** as this order's.
* **More fills than the order row still finished the intent.**
* **Absent fields were read as zero.** A missing fee field read as $0 fees.
* **Partial fills were booked too early.** An IOC create answer without a remaining quantity
  booked its fill count. A partial fill then released the unfilled remainder's reservation at
  acceptance.

### The rules now

| evidence | result |
|---|---|
| terminal status (executed / canceled), remaining explicitly 0, fill count within the order, cost stated, fees stated by the row or by a complete fills listing | `done`: the reservation is replaced by the actual cost plus fees |
| no status, an unknown or open one, remaining missing or above 0, fill count missing or outside the order, `executed` short of the order, another initial count, cost or fees billed on an unfilled row | stays open (`held` event; `reason` says why), reservation unchanged |
| final fills, but fees (or cost) absent | stays open. An IOC keeps fills × limit + `fee_bound`; any other order keeps its whole worst case. It is read again, so late fees land |
| fees stated as `"0.000000"` | zero fees are final. Blank or absent is unknown |
| fills listing truncated, failed, with unattributed or malformed rows | establishes nothing. A complete order row still finishes on its own |
| fills listing shows more contracts than the row (even on a truncated page), or one fill id with two contents | contradiction: stays open. An IOC's fill count goes back to unverified: the whole worst case counts, and no lock leg is sized on it |
| fills listing shows fewer than the row (it trails) | a complete row finishes; the event is noted `fills_trail` |
| the same fill id listed twice with the same contents | counted once |
| IOC create answer | fill count booked only with an explicit remaining 0, or when every contract filled (that releases nothing) |
| `max_checks` reads without final evidence | the bounded reservation stays. `kalshi reconcile` now passes `recheck_exhausted=True` and reads it again |

**Real data** (demo, GET only, 2026-09-26):

* 300 canceled and 31 executed order rows: every one carried the status,
  `remaining_count_fp "0.00"` and all four dollar fields.
* 39 fills: none duplicated, all with `fee_cost` and a `fill_id`.

Real reconciliation is therefore unchanged. The recorded fixtures still reach `done`
(`test_real_demo_rows_are_final`).

### Tests

14 new tests in `tests/test_order_ledger.py::EvidenceTests`. On the pre-fix ledger, 13 of them
fail (49 failures and 1 error across subtests).

| requested case | tests |
|---|---|
| missing or unknown status | `test_a_row_without_status_or_remaining_quantity_releases_nothing`, `test_an_unknown_or_open_status_is_not_final` |
| missing remaining quantity | `test_a_missing_or_open_remaining_quantity_is_not_final`, `test_a_create_answer_books_an_iocs_fills_only_when_nothing_can_remain` |
| delayed fees | `test_absent_fees_keep_the_fee_bound_until_they_are_reported`, `test_fees_come_from_a_complete_fills_listing_when_the_row_has_none`, `test_late_fills_and_fees_are_reconciled_even_after_polling_gave_up` |
| absent vs explicit zero fees | `test_explicitly_reported_zero_fees_are_final` |
| truncated fill pages | `test_a_truncated_fills_listing_establishes_nothing` |
| duplicate fills | `test_duplicate_fills_are_counted_once` |
| contradictory order/fill totals | `test_order_and_fill_totals_that_contradict_release_nothing` |
| incomplete responses cannot free budget | `test_incomplete_answers_never_free_budget` |

**How the budget test shows it.** It runs 15 incomplete variants, each after both a final and a
non-final create answer:

* none reaches `done`;
* exposure never falls below $0.52;
* a second order that needs the room is refused on a per-game budget of $1.018;
* after the complete answer, the same second order is accepted;
* with the fee booked as zero, as the pre-fix ledger did, it would have been accepted every time.

**Other new tests:** `test_the_manual_reconcile_reads_exhausted_orders_again` (the CLI flag) and
`test_real_demo_rows_are_final` (recorded fixtures).

**Changed:**

* `test_an_order_row_without_cost_fields_…` now expects `accepted` at $31.00, where it was
  `done` at the limit plus the bound. It expects `done` at the actual cost once the cost fields
  arrive.
* The test `FakeKalshi` serves fills through `paged`, so truncation is known. It now also has
  fill ids and the `omit`, `fills_hidden` and `extra_fills` switches.

### Validation

On `f4b3356`, which is `04639ac` plus this commit:

* 1053 Python tests OK (109 s).
* JS parity: 3650 fee and 54 arb vectors with 0 mismatches, `ok 3769` + `ok 159`, extension PASS.
* `render_results.py --check` OK and `git diff --check` clean.
* No new orphaned launcher supervisors.
* Unchanged: the fee formulas (`fees/`), `fee_bound`, and every gate (host and environment,
  account binding, budgets, lock proof).

**Kalshi demo check on `bf7d2d5`: ALL PASS on both runs** (18:44–18:45 PDT, demo play money
only).

* **Plain run.** Lost-answer recovery, the LAG executor's IOC and the maker's resting bid all
  reconciled to `done`, with 0 filled.
* **`--fill --confirm-demo` run.**
  * The LAG entry (1 @ $0.57) and step 7's fill reconciled from the exchange at $0.57 plus a
    $0.0172 fee: centicent, and within the $0.02 bound.
  * Step 7's fill reached `done` on its first read. The order row and a complete fills listing
    agreed: 1 fill, $0.0172.
  * The lock leg was bounded to the 1 contract held, and a second lock was refused.
  * Everything was sold back, and 0 orders were resting at the end.
* **Cost.** The check's 6 fills cost $0.13 of play money in spread and fees. A read-only
  before/after comparison showed nothing of the check left open, and none of the running
  stack's positions changed in that window.
* **The demo account is nearly out of play money.** It held $5.45 before the check, because
  the live stack's evening LAG entries hold 8 positions (about $225). The check itself needs
  about $2.

### Gaps

* **A complete row can finish unchecked.** When the fills listing is truncated or fails, the
  order row finishes the intent on its own. Only a listing that shows *more* than the row
  blocks it.
* **Order rows need an explicit remaining quantity,** even when fully executed. Real rows
  always carry one. Create answers accept a full fill without it.
* **`done` is final.** A fill that later appeared on a terminal order would not be booked.
  Kalshi does not add fills to terminal orders.
* **Automatic polling still stops after 120 reads.** The bounded reservation then stays until
  `kalshi reconcile`.
* **Still open from the deployment audit:**
  * the pinned tick clock in `LiveSlate.run()` (`live.py:496`);
  * a re-signalled LAG buying full size again within seconds;
  * finished games left `live=1`.

### Next steps

1. **Demo check: done on `bf7d2d5`, ALL PASS on both runs** (see *Validation*). Run it again
   after deploying (runbook step 7). The demo account had $5.45 of play money left, so wait
   for open positions to settle first.
2. **Deploy with the runbook**, with `DEPLOY` = the tip; the code was validated at `f4b3356`.
3. **In the first hour, watch for `accepted` rows** whose reason reads "… filled, no …: fills
   kept at the limit plus the fee bound". On Kalshi's real answers they should not appear. If
   they persist, run `python3 -m arb_engine kalshi reconcile`.

## Historical trade tapes (`c2cd4d8`, review fixes `faaf6c0` and `d161cf0`, session c1ab42)

`arb_engine/venues/trades.py` is the **research** tape client used by `event-study`. The live
recorder's print poller (`strategy/fastlane.py`) is a separate client and is unchanged. The
work is on branch `claude/trades-cache`; round 3 (`d161cf0`) sits on `claude/exec-readiness`
`f496d78`.

### What was wrong (reproduced at `c13a49c` through the public API only)

A fake of the cursor-paginated trades endpoint serving 30 prints over 3 pages:

| case | at `c13a49c` | now |
|---|---|---|
| `max_pages` used up with a cursor left | 10 of 30 prints returned, no error, cached | `IncompleteTape`; progress kept |
| offline replay afterwards | the 10 served as the tape | `IncompleteTape` |
| the same query with `max_pages=50` | still 10 of 30, backlog never fetched | 30 of 30, fetching only pages `c1`, `c2` |
| overlapping pages | 2 duplicate trade ids kept | 0 |
| one trade id, two prices | both kept | `ConflictingTrades` |
| windows [100.2, 200.7] then [100.9, 200.1] | one cache file; the second answered with the first's prints | separate keys |
| window ending 200.5 | API asked `max_ts=200` | asked 99 … 202, trimmed exactly |
| `"NaN"` price string | parsed as `nan` | row rejected |
| old cache file, offline | served, completeness unknown | never served; the error names it |

### The rules now (after round 3)

* **Completeness is explicit.** Tapes carry schema 3, `complete`, `reason`, `pages`,
  `next_cursor`, `seen_cursors`, `pass_started_at`, `duplicates`, `rejected` and `restarts`.
  Only a complete tape is returned, online or offline.
* **Closed windows only.** A tape is complete only if `max_ts` is set and at least `settle_s`
  (default **15 min**) before the pass's first page. An open or open-ended window is refused
  before any request. The margin covers late publication by the venue and a local clock running
  ahead; the rule trusts the local clock to within it.
* **Resuming.** The page budget running out, a failed page or a crash keep an incomplete tape
  and raise `IncompleteTape`; the next call resumes from the saved cursor with its own budget.
  It starts a fresh pass instead when:
  * the venue rejects the cursor (HTTP 400/404/410/422; 429/401/403/408 keep it);
  * the saved pass began before the close by this client's margin, or later than now;
  * the pass ended on a repeated cursor, a conflict or unreadable rows.
  Progress is checkpointed after page 1 and every 10 pages, and every write is atomic.
* **Fresh passes are judged alone.** A fresh pass starts empty and is not compared with earlier
  passes: the venue's current answer is the tape.
* **Deduplication.** Kalshi prints are deduplicated by `trade_id` within a pass. The same id
  with a different payload in one pass raises `ConflictingTrades` once; the next consistent pass
  heals. A row is rejected, and its tape is never complete, if it has no id, belongs to another
  ticker, or has a time, price or size that is missing or non-finite, a price outside [0, 1], or
  a size that is not positive.
* **Windows.** `min_ts` and `max_ts` must be finite, with min ≤ max. The API window is widened
  by a second on each side and trimmed exactly.
* **Cache files.** `<venue>-<market>-<sha256 of the exact query>`. On every read,
  `trades.tape_from_doc` checks the query, every field's type and range, and for a complete tape
  the closed-window rule with the reader's `settle_s`. Writes hold a per-tape `flock` (opened
  read-only, waited on for at most 30 s); a complete tape that cannot be cached is returned
  with a logged warning.
* **Polymarket.** No new features: a used-up page budget raises instead of being cached as
  complete. There is no dedupe (a transaction hash can carry several prints) and no resume.

### Round 2: an independent review of `c2cd4d8`, fixed in `faaf6c0`

A reviewer that had not seen the implementation tried to break it
(`audit_scratch/tapes_review` in the session scratchpad). It found ways a truncated tape was
still served as complete. All are fixed:

* **Malformed pages (High).** The real `HttpClient` returns `{}` for an empty 200 body, and the
  fetch took that (or an error object, or a page without a cursor) as the last page. It now
  requires a `trades` list and a string `cursor`; anything else is a failed page.
* **Open windows (High).** A window still open when fetched was cached as complete forever.
  Now a tape is complete only if `max_ts` is set and at least `settle_s` (60 s in round 2,
  15 min since round 3) before the pass's first page. An open or open-ended window is never
  complete. Since `max_ts` bounds the query, a pass over a closed window reads a fixed set,
  which is what makes resuming sound.
* **Fresh passes (Medium).** A fresh pass now starts empty, so phantom prints no longer survive
  and a single conflict no longer wedges every later call. (Round 2 still used old prints as
  conflict evidence; round 3 dropped that.)
* **4xx on resume (Medium).** Only HTTP 400/404/410/422 restart the pass; 429/401/403/408 keep
  the cursor.
* **Polymarket key (Medium).** Polymarket's `condition_id` is now part of the key.
* **Lows:**
  * an unwritable cache now says progress was not saved;
  * `event-study` exits with the reason, and has `--max-pages`;
  * cache files are validated field by field;
  * `store_complete` validates its input;
  * conflicts just outside the window are caught;
  * a migration script replaces the one-liner.
* **Concurrency.** A per-tape `flock` around the "never replace a complete file" check and the
  write took a threaded run from 93 downgrades to 0.

**Evidence:**

* The reviewer's scripts show each fix.
* Its fuzz with every window closed (`r12b`): 1,500 of 1,500 seeds complete, 623 kills
  mid-fetch and 356 inside the atomic write, **0 violations**. (An earlier figure here, 32,317
  calls, came from its first fuzz, where only 1,047 of 1,500 seeds could complete because the
  rest used `max_ts=None`.)
* A threaded closed-window run: 4,596 complete tapes, 0 wrong, 0 downgrades.

### Round 3: a second review of `faaf6c0`, fixed in `d161cf0`

The same reviewer attacked the round-2 logic (`audit_scratch/tapes_review/s01`–`s08`). It found
one Medium and five Lows, all fixed:

* **Complete files were never re-checked (Medium).** `_read` accepted any `complete: true` file
  whose fields type-checked. Round-1 files (cut short by an empty 200 body, or fetched while the
  window was open) were served as complete, offline and online, and the migration script called
  them current. Now:
  * the schema is 3, so schema 2 files are not read and an offline miss names them;
  * a complete file needs `max_ts`, and a pass start that closes the window under the reader's
    `settle_s`;
  * `store_complete` needs a window closed now.
* **The closed-window margin (Low).**
  * `settle_s` must be finite and ≥ 0 (NaN or −120 used to make an open window "closed").
  * The default is 15 min instead of 60 s.
  * An open window is refused before any request; round 2 paged the whole tape first.
  * A saved pass is resumed only if it began after the close and not later than now, so a
    doomed pass is no longer resumed to its end and then redone.
  * A non-finite clock is refused.
* **Conflict evidence across passes (Low).** A permanent correction raised twice before
  healing, or never, depending on how `max_pages` split the pass, and a 503 erased the evidence.
  Evidence is now kept within one pass only (see *The rules now*).
* **The lock (Low).**
  * A read-only lock file used to block every save; the lock is now opened read-only.
  * `flock` could wait forever; it now waits at most `lock_timeout_s` (30 s).
  * A complete tape that could not be cached was silently not cached; it now logs a warning.
  * The docstring now says that without `fcntl` (Windows) the guard is best effort.
* **The validator (Low).** A stored price `"0.5"` was served as text and broke
  `as_home_prices`. Prices, sizes and times must now be numbers, prices in [0, 1], Kalshi sizes
  above 0, and side and trade id text; the parsers reject the same rows.
* **The migration script (Low).** It looked only at `schema`. It now:
  * runs the client's own checks (`tape_from_doc`) and prints what fails;
  * checks that each file name is the key of its query;
  * moves schema 2 files;
  * refuses `--to` equal to `--cache-dir` (which used to rename files in place on every run).
* **Info.** `event-study --max-pages 0` was ignored and `-1` escaped as a traceback; both are
  usage errors now.

**Evidence (reviewer scripts re-run on `d161cf0`):**

* `s01`: round-1 files are no longer served; offline names the old file; the migration dry run
  lists them.
* `s03`: a correction raises `ConflictingTrades` once, then 30 of 30. A split pass, a whole pass
  and a 503 then retry all end complete at the corrected price.
* `s04`:
  * 5 processes: 0 wrong, 0 downgrades.
  * 6 threads: 0 wrong, 0 downgrades.
  * Without `fcntl`: 19 downgrades (documented as best effort).
  * With a read-only lock file, saves work.
* `s05`: 0 of 400-plus code-written files rejected, and no mutation to text or out-of-range
  values is accepted. Mutations to other valid values (another in-window time, a price of 0 or
  1, another side string) are served as written.
* `s07` (live venue, 400 seeds each), with no lag, a clock 30 s slow, lag ≤ 45 s, lag ≤ 150 s
  and a clock 120 s fast: **0 wrong tapes each**. Round 2 had 86 and 89 wrong in the last two.
  The margin's limit (200 seeds each): lag ≤ 600 s and a clock 600 s fast give 0 wrong; lag
  ≤ 1,500 s gives 162 wrong and a clock 1,200 s fast gives 88. Lag plus clock skew must stay
  under `settle_s`.
* `s08`: an open window costs 0 requests. The doomed-pass case takes 10 requests instead of 20.
  `--max-pages 0` and `-1` exit 2.
* `r3_margin_and_resume.py`:
  * a saved pass stamped 10,000 s ahead is not resumed;
  * the margin edge is exact (900.0 s closed, 899.999999 s open);
  * `store_complete` refuses open and open-ended windows.

### Cache migration and invalidation

* **Older files are never used.** This covers two kinds of file:
  * the pre-schema format (`<venue>-<market>-<int>-<int>.json`), which cannot show whether its
    fetch ran out of pages;
  * schema 2 files (rounds 1 and 2), which were never re-checked against the closed-window rule.

  Online, the tape is fetched whole under the schema 3 key. Offline, `event-study --offline`
  exits and names the old file. Nothing is deleted automatically.
* **Rebuild offline replays online, after the window has closed.** Run each event study once
  without `--offline`, at least 15 minutes (`settle_s`) after the window's end. `event-study`'s
  window ends 30 minutes after the game's last recorded row, so wait at least **45 minutes** after
  it; earlier, the command exits with the time the window counts as closed. A Kalshi tape longer
  than the page budget (default 50 pages) stops with "page budget … used up"; run the same
  command again and it resumes. A Polymarket tape cannot resume, so raise `--max-pages` instead.
* **Move unusable files aside** with the script. It dry-runs by default, handles errors file by
  file, never overwrites, and never deletes. Using the client's own checks, it moves:
  * old-format and schema 2 files;
  * schema 3 files that fail a check, or whose name is not their query's key;
  * unreadable and non-object `.json` files.

  It leaves current complete and resumable files, and dot-files, alone. `--to` must not be the
  cache directory.

  ```bash
  python3 scripts/migrate_trade_cache.py --cache-dir out/cache/trades --to out/cache/trades-legacy
  ```

  Add `--apply` to move the files it lists.
* **Invalidating one tape.** Delete its `.json` file. The name starts with a sanitised, possibly
  shortened `<venue>-<market>` prefix followed by a digest of the exact query; a dot-file
  `.lock` beside it is only the write lock. `IncompleteTape.tape` and `TradesClient.kalshi_tape`
  expose the record.
* **Fixtures.** `TradesClient.store_complete(venue, market, min_ts, max_ts, trades)` validates
  and records a tape known to be complete. `tests/test_tickreplay.py` uses it.

### Tests and validation (on `faaf6c0`; round 3 below)

* **New tests.** `tests/test_trade_tapes.py` (35 at `faaf6c0`, 41 at `d161cf0`) covers:
  * the page budget, and a second request with a larger budget and with the same budget;
  * failed pages (HTTP and non-HTTP);
  * restart from a checkpoint after the process was killed;
  * a stale cursor restarting from the top;
  * repeated cursors;
  * overlap and same-second dedupe, conflicts and rejected rows;
  * window edges, key collisions, a mismatched stored query and non-finite windows;
  * old files online and offline;
  * atomic writes, shuffled pages, and the Polymarket budget;
  * round 2:
    * malformed pages, including the real client's empty body;
    * open and still-open windows;
    * phantom removal and conflict healing;
    * 4xx statuses on resume;
    * the Polymarket condition id;
    * damaged cache files, `store_complete` input, and an unwritable cache;
    * the late-writer guard, the migration script, and the CLI;
  * round 3:
    * open windows refused before any request;
    * saved passes that cannot end complete not resumed;
    * a complete file re-checked with the reader's margin;
    * bad margins, timeouts and clocks;
    * fresh passes judged alone;
    * text or out-of-range print fields;
    * a read-only lock file, a stuck lock, and the uncached-tape warning;
    * schema 2 files;
    * the migration script's checks and its `--to` guard;
    * `--max-pages` usage errors.
* **The old code.** It has no `IncompleteTape`, so the new file cannot import there. The
  behavioural proof is the reproduction above: `repro_trades.py` in the session scratchpad,
  using only `kalshi_trades` and `parse_kalshi_trade`.
* **Changed tests.** `test_trades`' window test now asks for its fixture's own ticker (the old
  code had filed another ticker's prints under `"T"`), and `test_tickreplay` seeds with
  `store_complete`.
* **Validation.** **1101 Python tests OK** (with `5480dfc`'s evaluator work); JS parity 3650 fee + 54 arb vectors, 0 mismatches,
  `ok 3769`, `ok 159`, extension PASS; `render_results.py --check` OK; `git diff --check` clean.
* **Round 3 (`d161cf0`).**
  * **Against `f496d78`, 13 of the 41 tests fail**: 10 on behaviour, and 3 because they use the
    new `lock_timeout_s` / `schema=` parameters. For those three the old behaviour is shown by
    `s01` (round-1 files served) and `s04` (a lock without a timeout).
  * **1142 Python tests OK** (118 s); the 14 ResourceWarnings are the same test-code sites as
    before, none from the tape code.
  * JS parity 3650 fee + 54 arb vectors, 0 mismatches, `ok 3769`, `ok 159`, extension PASS;
    `render_results.py --check` OK; `git diff --check` clean.
* **Evidence.** The tests are offline and prove behaviour only. No real Kalshi tape was fetched
  in any round, so the venue's actual cursor, `min_ts` / `max_ts` inclusivity, page-overlap
  behaviour and publication lag are unverified. The widening and the dedupe are built to hold
  either way; the 15-minute margin is an assumption about lag and clock skew, not a measurement.

## H4 same-timestamp ordering: one instant, one decision (`5480dfc`, session 89f99c)

### Reproduction

The case: synthetic independent books (Kalshi A, Rothera B), compatible settlement, a $0.50
tie payout on each leg, zero fees.

| t | A ask | B ask |
|---|---|---|
| 0 | .60 | .50 |
| 1 | .40 | .70 |

Run from a checkout root: `python3 repro_h4.py` (session scratchpad; the same case is
`test_the_audit_case_fires_in_neither_order_and_a_real_move_in_both`).

| code | t=1 fed as [A, B] | t=1 fed as [B, A] |
|---|---|---|
| `bf7d2d5` (and `c13a49c`, identical evaluator) | 1 signal: t=1, margin .10, guaranteed-eligible | none |
| `5480dfc` | none | none |

**Why:** `arb_scan` decided after every row. With A's row first, A's new .40 met B's stale .50.

### Fix: every path installs the whole instant, then decides once

The paths, and what each one now does:

| path | change |
|---|---|
| `microdata.resolve_instant` / `observation_instants` | one observation per contract identity per instant |
| `microdata.build` | installs each batch and voids its conflicts, then computes features |
| `arb_scan` | one decision per instant; the pair by economics; fills from de-duplicated series |
| `select_trades` | one exposure at one instant goes to the cheapest bought contract, then a direct buy; was the outcome-name sort |
| `h3_lock_trades` | hedge quotes of one instant chosen together; was the first by contract name |
| `select()` | deterministic order |

**The policy** is documented in `docs/MICROSTRUCTURE_INTERFACES.md`, "One instant, one batch,
one decision":

* **Routes:** a direct route beats a resale route.
* **Identical rows:** count as one observation.
* **Conflicts:** rows of the same identity, instant and route that disagree void that
  contract from `t` until its next clean observation. It is then no decision, leg, fill,
  label, cross-book comparison or complement.
* **Choices:** made by economics, with identity used only on an exact economic tie.
* **Late hedges:** a hedge quote too late to fill before the remainder's exit ends the watch
  at that instant, before anything is sent.

**Preserved:** decision-time book identities, settlement checks, tie handling, fee models and
paper-execution latencies.

### Tests

`tests/test_microstructure_instants.py`, 12 tests:

* the audit case in both orders, and a real move in both;
* all 24 permutations of a 4-row instant;
* book, venue and outcome renames;
* every decision replayed from rows up to its own instant;
* future-append invariance;
* resale routes and same-book pairs;
* identical duplicates;
* conflicts in every order;
* conflicted instants never filled;
* `build` voids;
* same-instant exposures;
* the cheapest hedge whatever the book is called.

**11 of the 12 fail on `bf7d2d5`.**

### Validation on `5480dfc`

**Tests and checks** (on the combined tree: `f74cb19` plus this round)

| check | result |
|---|---|
| `python3 -m unittest discover -s tests -t .` | 1086 tests OK in 117 s of test time. The run took 361 s of wall-clock because the Mac was in lid-closed sleep for part of it (`pmset -g log`: sleep at 00:20:16, dark wakes until 00:25). `unittest` times itself with a clock that stops during sleep, so this was not a shutdown tail. |
| `bash scripts/test_js.sh` | fee vectors 3650 with 0 mismatches, `ok 3769`, `ok 159`, extension PASS |
| `python3 scripts/render_results.py --check` | OK |
| `git diff --check` | clean |

**Discovery replay**

* **Data:** the 15 NFL games of 2026-09-20/21, read-only from `~/Documents/GitHub/arb-engine/out/history.db`.
* **Logs:** both runs are appended to `~/Documents/ChatGPT/arbitradge/out/eval_log.jsonl`.
* **No `--freeze`, and the test fold was not opened.**

**Commands:**

```bash
# baseline: unchanged evaluator, from a git-archive export of bf7d2d5 (its log row has git_head null)
python3 scripts/microstructure_eval.py --fold discovery --db ~/Documents/GitHub/arb-engine/out/history.db --results <scratch>/discovery_before.json --log ~/Documents/ChatGPT/arbitradge/out/eval_log.jsonl
# after: the same command from the worktree that became 5480dfc
python3 scripts/microstructure_eval.py --fold discovery --db ~/Documents/GitHub/arb-engine/out/history.db --results <scratch>/discovery_after.json --log ~/Documents/ChatGPT/arbitradge/out/eval_log.jsonl
```

**Provenance.** The after run's log row says `git_head` c13a49c, because the fix was not yet
committed. Its spec hash, `a29bf2673e75`, equals the hash of the committed `5480dfc` tree.

**Results**

* **The baseline reproduces the committed fixture exactly:** all 3,587 values of
  `tests/fixtures/results/micro_discovery.json`. The fixture's hashes predate this work: spec
  `52cab7ec…`, against `e3b49c2c…` for the baseline run.
* **After the fix, 24 of the 3,587 values change.** All of them are the selection split,
  "long" against "via complement", repeated at each horizon:

  | selection | long before → after | via complement before → after |
  |---|---|---|
  | H1 momentum | 141 → 194 | 61 → 8 |
  | H3 diagnostic, any settlement | 82 → 97 | 66 → 51 |
  | H3 diagnostic, tie-matched | 11 → 16 | 10 → 5 |

  Mirror decisions at one instant used to be labelled by outcome name and now go to the
  direct buy. The same contracts are bought at the same times and prices.
* **Nothing else changes:** no trade count, bought contract, return, test, decision, and no H4
  number. H4 has 162 signals, with 0 field differences across the 162 records.
* **Row order on real data** (recorded, reversed and shuffled rows):
  * the new code gives identical H4 records and H1 selections in all three orders;
  * the old code's H4 records came out in a different order each time, with identical content
    once sorted;
  * the old H1 selection was already order-free.
* **The synthetic fold is identical before and after.**

**Why discovery barely moves**

* All 68,735 discovery observations are legacy full ticks (about 5.4 s, `approx_time=1`).
* 14,256 of 14,281 executable instants carry at least 2 contracts, and 5,877 carry both
  outcomes on at least 2 books.
* There are 0 duplicate or conflicting contract-instants.
* The other book's previous quote is more than 2 s old at every new tick, so the row-by-row
  defect could not produce a phantom here. It bites on 1 s fast-lane data.

### Affected claims: not overwritten, your approval needed

**One rendered claim changes.** In `docs/MODEL.md`, table `micro_discovery_selection`, row
"H1 momentum":

* "bought itself" goes from 141 to 194, and "bought its complement" from 61 to 8;
* trades stay 202;
* every other rendered number is unchanged.

**Its provenance** is `tests/fixtures/results/micro_discovery.json`. Its `_fixture` note says it
was replayed on 2026-09-26 on this branch under spec v4, spec hash `52cab7ec…`.

**The H3-diagnostic splits** change the same way, but they are not rendered anywhere.

**Other results fixtures are unaffected:**

* `micro_synthetic` is identical before and after;
* `scripts/arb_backtest.py` (`arb_backtest_w2`) imports only the unchanged `tie_value`;
* `scripts/leadlag_study.py` (`leadlag_nfl_2026_w2`) and `render_results.py` only name the
  evaluator in text.

**The live stack is unaffected too.** No live engine module imports `quant.microdata` or the
evaluator, so no redeploy is needed.

**To apply after approval.** Replace the fixture's `_fixture` text with a note of this replay:
the date, the commit and the spec hash `a29bf2673e75`. Then:

```bash
python3 scripts/microstructure_eval.py --fold discovery --db ~/Documents/GitHub/arb-engine/out/history.db --results tests/fixtures/results/micro_discovery.json
python3 scripts/render_results.py --write && python3 scripts/render_results.py --check
```

### Differences from `claude/micro-audit-3` `b1238ce`, for the merge of the two evaluator lines

| topic | `b1238ce` | here |
|---|---|---|
| conflicting rows | picks one, the smallest canonical JSON | voids the contract (more conservative) |
| `select_trades` second key | tie payout | a direct buy |
| H3-lock hedges | also requires `settlement_relation == identical` | not adopted: a new exclusion, outside this defect; decide when merging |
| too-late hedges | — | end the watch at that instant, before anything is sent |

### Still empirically unverified

* **The effect on 1 s fast-lane data** (recorded from 2026-09-24). The validation fold was not
  run here, and the test fold stays sealed.
* **Conflicting same-identity observations.** None exist in discovery, so the policy is
  exercised only synthetically.
* **Direct versus resale disagreement.** Whether Kalshi-direct and Robinhood KX rows ever
  disagree at one instant in real data.
* **How often same-instant mirror decisions occur** on fast data.

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

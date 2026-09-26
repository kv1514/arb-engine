# Engine audit and execution handoff — 2026-09-26

Repo: `/Users/kv15/Documents/ChatGPT/arbitradge`, branch **`claude/exec-readiness`** (created
this session from `claude/order-buttons` @ `a8b9246`). Not pushed. Not merged into the GitHub
clone (`~/Documents/GitHub/arb-engine`, `main` @ `1ae9ef2`) that runs the live stack. The
original prompt for this session is kept at the end.

## Read this first

* **The running live stack still runs the old executor.** It was started from the GitHub
  clone with `--execute-lag demo` and has per-process caps, lock legs retried every second,
  and no ledger. Nothing in this branch reaches it until it is merged there and the
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

## Validation (all run this session, on `2cf14c0` unless noted)

* **Tests and checks:**
  * `python3 -m unittest discover -s tests -t .`: **962 tests OK** (108 s).
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
2. **The maker's resting orders** (`KalshiBroker`, `maker --mode kalshi`) are not in the ledger. It keeps its own sweep and expiry, and runs in paper mode. Integrate them before any real maker mode.
3. **The ntfy throttle is per process.** Two processes pushed the same game less than 60 s apart 40 times. A shared throttle (a small SQLite file in `out/run/`) would fix it.
4. **Fee rounding.** Demo charges centicent. Confirm on one production fill, then consider switching the `kalshi_rounding` default. That changes fee vectors, so follow AGENTS rule 1 and re-run `gen_fee_vectors.py` and `test_js.sh`.
5. **Microstructure test fold.** It is still unfrozen, and the H3 identity strictness is your decision. The test fold starts 2026-10-08 (TB @ DAL). Run `--freeze-spec` only after deciding.
6. **Not investigated:** the stuck `live=1` ticks after final, and the NULL Kalshi/Polymarket `quote_time` in `inplay_ticks`.
7. **Demo balance.** This session's checks sent a few $0.01 demo IOCs (nothing filled), three filled 1-contract round trips and one lock leg, all sold back; together well under $1 of play money including fees.

## Exact next steps

```bash
# 1. Look at the branch (this clone)
cd /Users/kv15/Documents/ChatGPT/arbitradge && git log --oneline 1ae9ef2..claude/exec-readiness

# 2. Bring it to the GitHub clone that runs the stack (fast-forward while its main is still 1ae9ef2;
#    if --ff-only refuses, main moved on: merge FETCH_HEAD instead and re-run the tests)
cd ~/Documents/GitHub/arb-engine && git fetch /Users/kv15/Documents/ChatGPT/arbitradge claude/exec-readiness && git merge --ff-only FETCH_HEAD
python3 -m unittest discover -s tests -t . 2>&1 | tail -3        # expect 962 OK

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

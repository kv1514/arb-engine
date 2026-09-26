# Engine audit and execution handoff — 2026-09-26

Repo: `/Users/kv15/Documents/ChatGPT/arbitradge`. Inspected HEAD: `3ce3c97`, branch
`claude/order-buttons`. Main checkout was clean. Recheck before editing.

## Prompt for Claude

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

## Validation provenance

Prior integrated validation at `3ce3c97`: 871 Python tests; JS 3769 + 159 checks;
render check and diff check passed. Those checks were reported in the previous session,
not rerun for this documentation-only handoff. No account request or order was made here.

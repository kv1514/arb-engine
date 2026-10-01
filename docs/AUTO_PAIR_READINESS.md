# Two-venue execution handoff

This branch contains a gated manual Polymarket US IOC executor and shared production
Kalshi/US cash ceilings. The new `auto-arb` command is **paper-only**, default off.
Neither its `live` mode nor manual live flags enable a paired production transport.
No account credentials, running worker configuration or live orders were changed.

`trade-approval` now supplies one-time, revocable standing permission: default dry-run,
authenticated production account/key binding, 6-hour default / 24-hour maximum lifetime,
spec/evidence hash, exact single-use causal pair permits and restart-safe cash holds.
There is no LLM approval fallback and no error becomes "allow". Its separate private
SQLite budget holds at most $25 per venue / $50 combined including entry and contingency
fees, conservatively for the store's lifetime; expiry, unused permission and re-arming
never reset room. It sends no orders and does not enable manual live flags or old workers.
No real account policy was armed during development. The live readiness report can read
this permission status but stays blocked regardless of whether it is armed.

Implemented paper behavior: causal, raw-book Decimal planning; known compatible
settlement only; $25/leg including fees and $50 held cash; durable reservations and
liquidity accounting; US-first partial IOC, fill-sized Kalshi hedge, bounded US
unwinds; unresolved inventory blocks new admissions. Paper accounting is a separate
SQLite file, never real inventory evidence. Synthetic regression tests exercise
compatible terms; the current real US settlement registry does not admit pairs.

The next production checkpoint adds non-sending `USPairOrder`, strict final order
evidence, complete receipt-timed inventory pagination, a standing-policy dispatch
guard, and transport-boundary expiry checks after signing. None of these alone
implements paired production submission. Robinhood event-contract order support
is unverified and its automated pairs remain blocked before either leg sends.

The dedicated Kalshi single-attempt IOC/FOK path is now implemented and tested:
`KalshiExecutor.execute(..., one_send=True, not_after=deadline)`, with optional
constructor `ledger_path` / `clock` / test transport. It requires literal confirmation
and the exact account-bound pending ledger reservation even on demo. Signing happens
after a permanent send claim; host, key, plan, ledger path, gates and deadline are
fenced, and one stdlib POST cannot redirect, retry, invoke curl or switch hosts.
The executor durably records acceptance/ambiguity; failures and crashes keep cash
and cannot be resent. Final fills still require existing reconciliation. This is
an opt-in internal interface, not a connected automatic paired sender. Existing
legacy manual/strategy mutation transports are unchanged. US requests likewise
refuse host/key/seed/fingerprint changes during signing.
Shared cash sums also reject each negative/nonfinite component independently.
Legacy finished Kalshi rows missing explicit cost/fees retain their original
reservation; contradictions do not release it. These are budget-safety fixes,
not evidence of real fills or a completed paired coordinator.

`PairReservationLedger` now supplies atomic parent/both-child accounting staging
in the shared production file, including contingency fee cash and conservative
production bounds. Concurrent/restarted callers count those holds. It adds no
same-pair exemption to existing senders: staged children are not sendable. Only
expired, wholly unclaimed staging can release its local cash without deleting
game/permit IDs. Staging uses a reservation guard, leaving the dispatch guard
unclaimed; later revocation still blocks submission. Do not wire an order callback
directly after admission or call these accounting records verified inventory.

## Published recovery interfaces

* `USPairOrder(USOrderPlan, action='buy'|'sell')`: whole-contract automatic LIMIT/IOC;
  `payload()` uses long-price mapping for either side; `worst_cost(now)` includes
  entry cash/fees or sale fees only, never anticipated proceeds.
* `order_evidence(plan, raw_order, expected_id, final_read=True)`: exact immutable
  terms, count, price-limit and money checks. Returns quantity, remaining, cash,
  fees, final and verified; absent money is None, create evidence never verified.
  The parent ledger must enforce monotonic evidence and sticky contradictions.
* `read_inventory(client, slug, side, clock=..., max_pages=20)`: complete scoped
  positions pages only; returns key fingerprint, signed net, available quantity,
  request/receipt/deadline. Missing or stale evidence raises. It does not prove
  this pair owns inventory; reserve sales only against independently verified excess.
* `ApprovalStore.dispatch_guard(permit_id, binding, plan_digest)`: after `consume`,
  serializes one initial durable claim against policy revocation/re-arming. Hold
  policy before production-ledger lock, not over network. Production permit IDs
  must be unique; guard rollback cannot undo a production claim in another database.
* `PairReservationLedger(USOrderLedger).admit(approval_store, consumed, binding)`:
  stages both child terms/cash from the policy's exact stored plan, with policy
  before production locks. The shared ledger must be authenticated/bound first.
  `get`/`status` report accounting-only state. `abandon_staged(pair_id)` releases
  only expired, provably unclaimed staging; there is no send/hedge/exit API yet.
* `PolymarketUSTradingClient._create(..., not_after=deadline)` checks deadline,
  clock and live gates immediately before transport, including slow signer time.
  `positions_page(slug, cursor=...)` preserves the whole response and signs the
  bare path while encoding query parameters. Neither method retries.

## Remaining production requirements

1. Capture each product's binding rules, including cancellation, postponement,
   expiry, ties and official-result exceptions. Record their provenance/hash in
   the settlement registry; incompatible exceptional payoffs remain conditional.
   Do not replace this requirement with an operator `verified=True` boolean.
2. Extend the implemented atomic parent/both-child staging to real child-intent
   transfer and send claims in that same production transaction. Each executor deliberately
   blocks unfinished exposure from the other venue; do not remove that safeguard
   globally. Any same-pair exemption must prove parent/child ownership, account,
   contract identity, fill evidence and available cash under the transaction lock.
3. Implement verified US position inventory and bounded reduce-only-equivalent
   sales for LONG/SHORT contracts, with ledgered sends and correct long-price
   mapping. Autonomous orders must use the appropriate venue order indication,
   not blindly reuse the manual IOC payload. Document API evidence limitations. Cancellation acknowledgement is
   not fill finality; missing fees/IDs or contradictory evidence retain reservations.
4. A production pair coordinator must persist each dispatch before sending, reconcile
   after crashes, never resend an unknown US order, size hedges on verified fills,
   and recover partial hedges/exits without consuming displayed liquidity twice.
   Recovery may reduce existing exposure during a halt; it may not authorize new bets.
   Its atomic send claim must also fence the standing policy's generation, revocation,
   deadline and account binding; consuming a permit alone does not prevent revocation
   between consumption and submission. Do not reinterpret permission holds as production
   order reservations or silently bypass either venue's existing live gates.
5. Independently audit the observed US order/fill/position schemas read-only before
   relying on them unattended. Consolidate old workers onto one code revision,
account binding and ledger path before enabling any production automation.
6. Add offline adversarial tests for timeouts/crashes between both legs, account/key
   changes, partial fills, fee changes, concurrent admission, cancellation races and
   worst-case fees/cash caps. Run the complete validation set below. A passing suite
   does not establish live fill quality, profitability or universal settlement safety.
7. Connect only the new dedicated single-attempt Kalshi interface after the child
   ownership/transfer proof above is implemented. `HttpClient(retries=0)` is NOT sufficient:
   `KalshiClient._request` can retry a connection failure on its legacy host, and
   `HttpClient._request_retrying` can retry an `IncompleteRead` through curl inside
   the same attempt. The existing urllib/curl paths also follow redirects (curl
   uses `-L`) and curl headers appear in argv. Do not count one executor call as one
   HTTP send, forward signed headers through redirects, or infer refusal from a
   later duplicate-key 4xx. The dedicated path needs a post-signing deadline gate,
   exact production host, secret-safe headers and crash-safe non-retryable claims.
   Those legacy transports remain unchanged. The new opt-in interface implements
   these transport requirements; it does not implement child transfer or paired
   recovery, and no automatic sender is connected yet.

## Integration ownership

The main `claude/exec-readiness` checkout has a separate uncommitted `pmus_ops.py`,
`pmus_ledger.py`, `execution/polymarket_us.py` and ledger edits. Preserve them; compare
their evidence contracts before choosing one implementation. Do not run two order
ledgers for the same account or merge executors by removing gates. This work stays
on `codex/polymarket-us-execution`; it does not edit the evaluator, manifest, MODEL
tables or metrics fixtures. Existing workers do not pick up these commits automatically.

## Copy-paste integration prompt

> Fetch `origin/codex/polymarket-us-execution` and inspect the real branch/status before
> changing anything. Read AGENTS.md, README.md, docs/VENUES.md, docs/ARCHITECTURE.md and
> this handoff. Preserve the dirty `claude/exec-readiness` checkout and compare its US
> executor/ledger evidence contracts in isolated worktrees; do not run parallel ledgers
> or remove unresolved-order safeguards to make a pair submit. The standing-approval
> primitive is committed, but no policy or trading worker was activated. Finish the
> remaining production requirements above: verified market-specific settlement,
> transfer the existing shared pair/contingency staging to exact real child intents
> and independently validated send claims, account-bound US inventory/exits,
> partial-fill/crash recovery, and a revocation/expiry fence at the durable send claim.
> Reuse the tested Kalshi `one_send=True, not_after=deadline` interface with the
> same ledger path, never the retrying legacy client. It claims once and records
> acceptance/ambiguity; it does not itself validate paired-child ownership or
> verified hedge inventory. Those proofs must precede connecting the sender.
> Keep $25 per leg and $50 aggregate including fees, dry-run by default, stdlib-only
> engine code and offline tests. Never infer an unknown order absent or resend it.
> Do not submit live orders, expose credentials, edit cli.py/config.py, overwrite
> evaluator/manifest/results/MODEL work, or open the test fold. Distinguish implemented
> and tested behavior from real fill/settlement/profit evidence. Run and report the
> exact validation commands below, update the test count, commit coherent changes and
> push the owned branch. Do not describe paper execution or standing permission as
> enabled live autotrading.
> Robinhood event-contract automated order capability is unverified: block any
> such pair before its first leg, preserve quotes/manual support, and do not invent
> endpoints or automate an unsupported browser order flow. Public documentation
> evidence and backend interfaces are recorded in VENUES/ARCHITECTURE above.

## Validation commands

```bash
python3 -m unittest tests.test_shared_cash_integrity tests.test_kalshi_once tests.test_pair_reservations tests.test_us_pair_primitives tests.test_polymarket_us_execution tests.test_standing_approval tests.test_order_ledger
python3 -m unittest discover -s tests -t .
bash scripts/test_js.sh
python3 scripts/render_results.py --check
git diff --check
python3 -m arb_engine auto-arb
python3 -m arb_engine auto-arb --mode live
python3 -m arb_engine trade-approval arm
python3 -m arb_engine trade-approval status
```

Live auto-arb deliberately exits 3 with `BLOCKED`; the default command exits 0
with `OFF`. Default permission arming is `DRY_RUN`; a missing store's status is `UNARMED`.
These diagnostics do not authenticate accounts, fetch prices, create stores or send
orders. Only confirmed permission arming makes authenticated account GETs.

Validation on 2026-09-30: focused suite 167 tests in 4.644s, OK; full suite 1565 tests in
127.498s, OK. Twenty-four pair-reservation regressions cover atomic both-child
staging, shared cash accounting, restart/concurrent admission, account changes,
fee/cap conflicts, local expiry, partial transaction failures and policy rollback
after production commit. They do not submit or reconcile real paired children.
Forty primitive regressions cover buy/sell complement mapping,
hand-calculated fees, missing and contradictory evidence, complete positions pagination,
slow signatures, removed live gates and consumed-permit dispatch/revocation races.
Sixty earlier standing-permission regression tests cover lifetime/account binding,
expiry during lock waits/verification/serialization, receipt deadlines, concurrent
single use, cumulative fees/caps, restart/re-arm persistence, code/evidence changes,
private stores and account-only mocked CLI calls. JS: 3650 fee vectors and 54 arbitrage vectors without mismatches,
3769 core checks, 159 background checks, syntax check without warnings. Renderer
and whitespace checks pass. All new execution tests are synthetic/offline; there
is no empirical live-fill or profitability result and no test-fold evaluation.

Single-attempt/cash-integrity checkpoint, 2026-09-30: the focused command above
ran 272 tests in 4.586s, OK. The final full suite, with code frozen throughout,
ran 1608 tests in 122.631s, OK. Twenty-nine new Kalshi transport tests and thirteen
cash-integrity tests cover durable/restarted/concurrent claims, no retries after
timeouts/incomplete reads, redirect refusal, missing order IDs, signing/account
callback races, immutable paths/terms, conservative legacy fees and corrupt cash.
One additional US regression checks signing-time account/host changes. An earlier
full run was invalidated by edits to hash-pinned code while it ran; the approval
hash correctly refused it. The fresh final run above passed without relaxing that
guard. JS counts and renderer/whitespace checks remain as stated above. Default
`auto-arb` is OFF, live mode remains BLOCKED (exit 3), permission arming defaults
to DRY_RUN, and status is UNARMED. No accounts, policies, workers or orders were
activated. Production pair transfer/reconciliation/recovery and settlement/schema
verification remain open requirements, not a completed production executor.

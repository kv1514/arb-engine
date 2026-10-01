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

## Remaining production requirements

1. Capture each product's binding rules, including cancellation, postponement,
   expiry, ties and official-result exceptions. Record their provenance/hash in
   the settlement registry; incompatible exceptional payoffs remain conditional.
   Do not replace this requirement with an operator `verified=True` boolean.
2. Add atomic reservations for both child intents and contingency exits to the
   existing shared production SQLite transaction. Today each executor deliberately
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
> atomic shared pair/contingency reservations, account-bound US inventory/exits,
> partial-fill/crash recovery, and a revocation/expiry fence at the durable send claim.
> Keep $25 per leg and $50 aggregate including fees, dry-run by default, stdlib-only
> engine code and offline tests. Never infer an unknown order absent or resend it.
> Do not submit live orders, expose credentials, edit cli.py/config.py, overwrite
> evaluator/manifest/results/MODEL work, or open the test fold. Distinguish implemented
> and tested behavior from real fill/settlement/profit evidence. Run and report the
> exact validation commands below, update the test count, commit coherent changes and
> push the owned branch. Do not describe paper execution or standing permission as
> enabled live autotrading.

## Validation commands

```bash
python3 -m unittest tests.test_standing_approval tests.test_auto_arb tests.test_settings_doc tests.test_us_arbitrage tests.test_polymarket_us_execution
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

Validation on 2026-09-30: focused suite 170 tests in 2.450s, OK; full suite 1501 tests in
118.253s, OK. Sixty standing-permission regression tests cover lifetime/account binding,
expiry during lock waits/verification/serialization, receipt deadlines, concurrent
single use, cumulative fees/caps, restart/re-arm persistence, code/evidence changes,
private stores and account-only mocked CLI calls. JS: 3650 fee vectors and 54 arbitrage vectors without mismatches,
3769 core checks, 159 background checks, syntax check without warnings. Renderer
and whitespace checks pass. All new execution tests are synthetic/offline; there
is no empirical live-fill or profitability result and no test-fold evaluation.

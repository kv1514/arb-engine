# Execution audit and integration handoff — 2026-09-30

## Outcome and scope

Recovery-evidence, cash-accounting and public-quote selection fixes are implemented
on `codex/polymarket-us-execution`, based on `1ac352e`. They add 16 narrowly owned
adversarial tests. No venue fee formula, execution eligibility override, credentials,
live switch or standing permission was changed. No live order or cancellation was sent.

The main checkout remains on `claude/exec-readiness` at `c227387`, with its existing
dirty ledger and untracked US execution files preserved. Claude's separate local
`claude/kalshi-auto` advanced from `5f0873e` to `a878f99` during this audit. It was
reviewed in a clean detached worktree, not merged. At the fetch used for this audit,
`origin/claude/exec-readiness` was still `c227387`, `origin/main` was `3ca9684`, and
`a878f99` was a local commit, not a verified GitHub push.

This is a focused execution/candidate audit, not certification of the entire repo.
The recorder, public-print cadence, microstructure evaluator and empirical model
performance have not been exhaustively revalidated in this pass. The experiment
test fold and Claude-owned evaluator, manifest, results fixtures and MODEL tables
were not opened or edited.

## Implemented fixes on the Codex branch

- `execution/pair_recovery.py`: an incomplete receipt cannot increase or change
  previously final verified counts or money while retaining inventory verification.
  Such changes preserve the maximum observed lower bounds and become sticky
  contradictions; they cannot size a hedge. Monetary evidence is retained even
  when cumulative quantity is missing. Missing money is unknown, not zero.
- Every incomplete recovery receipt advances the durable event clock fence.
  Clock rollback after an incomplete observation is rejected.
- `execution/shared_limits.py`: each parent's US/Kalshi cash charge is at least
  its observed purchase/fee lower bound. All US exit commissions count; sale
  proceeds never create capacity. Contradicted/incomplete overruns remain charged.
  Negative/nonfinite components and unknown recovery roles fail closed.
- Actual exposure above $50 blocks new claims but does not disable status or
  reconciliation. This preserves a way to investigate an overrun.
- US unwind commands expire at the earlier of book and inventory receipt deadlines,
  including preparation delays. Available exchange bid timestamps must be within
  ten seconds before receipt and cannot be future/nonfinite.
- `quant/us_arbitrage.py`: the latest causal receipt is selected before checking
  liquidity. A newer failed/carried row invalidates older cheap liquidity. Equal-time
  conflicting price/depth rows are excluded, not selected by cheapest price or
  input order. Future appends remain invisible. Conflicting venue kickoff/game
  identities are rejected instead of accepting whichever identity merges first.

Synthetic Decimal examples now retain exposure of `$9.87` instead of `$9.71`
after an excess exit commission, and `$60.20` after a large observed commission
overrun. These are hand-constructed accounting tests, not measured real charges.

Interfaces are unchanged except `PairRecovery._parent(..., claim=True)` is used
internally for new claims. The command deadline is now the minimum of book and
inventory deadlines. Downstream dispatch must preserve that deadline, never recompute
it from a later time. Commands still explicitly carry `send_authorized=False`.
There are no new settings.

## Unresolved findings in Claude's local `a878f99`

These are independently reproduced with temporary fixture ledgers, fake clients,
fake signing and fake transports. They do not establish that a real account suffered
the failure. The original offline tests passed despite these cases.

### P1 — US dispatch lacks an after-preparation deadline/stop fence

`arb_engine/venues/polymarket_us_trading.py:174-206` checks the deadline before
signing, then signs, serializes and sends without a final deadline, host, key or stop
recheck. The new stop checks in the ledger/executor are upstream of this work.

A fake signer advanced the clock from 100 to 107 and created a temporary test stop
file, with a deadline of 106. The fake transport was still called once:

```text
fake_transport_calls=1, deadline=106.0, after_sign=107.0,
stop_present_at_send=True
```

Preserve the Codex branch's after-signing account/host/deadline fences when integrating
Claude's transport changes. Add the stop fence at the last dispatch boundary too;
do not rely solely on a check before signing or a ledger wait.

### P1 — autonomous Kalshi uses cached freshness and the legacy send path

`arb_engine/strategy/lagexec.py:878` calculates quote age once. Reconciliation
(`:897`), fee lookup (`:912`) and reservation (`:928`) can delay the send, but `:904`
checks the cached age. `_send` (`:652-662`) calls `execute(confirm=True)` without a
decision deadline or the one-send transport. `execution/kalshi.py:164` goes through
the legacy `create_order` client, whose retry/fallback behavior is not appropriate
for autonomous paired dispatch.

Using `tests.test_kalshi_auto.AutoBase`, delaying a successful `ledger.reserve`
by ten simulated seconds produced:

```text
status=SUBMITTED, cached_quote_age=0.0, actual_age_at_send=10.0,
max_age=5.0, fake_orders=1
```

Route autonomous requests through the durable single-attempt path with the original
decision deadline and recheck after every blocking preparation boundary. A reservation
is not permission to send on an expired quote. Preserve ambiguity after an attempted
request; do not resend. Claude's feature is a single-leg LAG strategy with a later
lock watch, not a coordinated guaranteed two-venue arbitrage executor.

### P1 — unknown US sends can lose their cash hold without order-level proof

`arb_engine/execution/polymarket_us_ioc.py:545-579` releases a claimed unknown send
after 300 seconds if open orders and the activity history show no matching trade.
The returned position is not used to refuse release. A fixture lost create response,
empty paginated activities and a simulated net position of ten produced:

```text
charge_before=2.08, charge_after=0, state=missed,
simulated_net_position=10
```

An empty activity listing is not affirmative evidence that the request was never
accepted. Polymarket US documents `eof` as the last pagination page, and
`maxBlockTime` as the maximum synchronous response wait. Neither description promises
that all earlier requests/activities have completed or provides an authoritative
absence certificate. This is an inference about insufficient evidence, not an
observed exchange-consistency incident. Sources checked 2026-09-30:
[Get Activities](https://docs.polymarket.us/api-reference/portfolio/get-activities),
[Create Order](https://docs.polymarket.us/api-reference/orders/create-order).

Keep attempted unknown sends reserved until uniquely scoped final order evidence
or other documented authoritative proof is available. Never automatically adopt a
manual order merely because market, size, price and time happen to match.

### P2 — shared cash accepts offsetting negative components

`arb_engine/execution/shared_limits.py:172-191` validates only the total after
summing raw per-intent values. A temporary database with a positive US charge of
24.70 and a malformed negative charge of -10 reported:

```text
reported_exposure=14.70, positive_cash_lower_bound=24.70,
new_24_dollar_hold_problem=None
```

Each component must fail closed before summation. This reproduces robustness against
malformed persisted state, not a normal exchange response writing a negative value.
The Codex branch validates components and charges parent reservations plus observed
recovery overruns; do not replace it wholesale with Claude's older summation while
merging the new stop/pinning/lock-cap features.

## Validation performed

In the isolated Codex worktree:

```bash
python3 -m unittest tests.test_pair_recovery_audit tests.test_pair_recovery tests.test_shared_cash_integrity
# 68 tests in 3.391s; OK

python3 -m unittest tests.test_us_quote_selection_audit tests.test_us_arbitrage tests.test_auto_arb tests.test_pair_recovery_audit tests.test_pair_recovery tests.test_shared_cash_integrity
# 135 tests in 4.105s; OK

python3 -m unittest discover -s tests -t .
# Frozen-code final run: 1675 tests in 128.431s; OK

bash scripts/test_js.sh
# 3650 fee cases, 54 arbitrage cases: zero mismatches
# 3769 arb-core checks; 159 background checks; syntax PASS, zero warnings

python3 scripts/render_results.py --check
# results blocks: OK

python3 -m unittest tests.test_settings_doc
# 5 tests; OK

git diff --check
# clean

python3 -m arb_engine auto-arb --mode off
# OFF, live_enabled=false, orders_submitted=0; exit 0

python3 -m arb_engine auto-arb --mode live
# BLOCKED, live_enabled=false, orders_submitted=0; expected exit 3

python3 -m arb_engine trade-approval status
# UNARMED, approval_active=false, execution_enabled=false; exit 0

python3 scripts/check_pair_settlement.py
# guaranteed_payoff_verified=false; BLOCKED; expected exit 3
```

Before the fixes, the new 11 recovery tests and five quote-selection tests reproduced
failures. An intermediate full-suite run was invalidated by an intentional
standing-approval code-hash fence because source changed during the run. It ran
1670 tests with three failures and 46 errors. The final run above started fresh
with code held fixed; no hash check or safety test was weakened to make it pass.

In the clean detached Claude audit worktree at `a878f99`:

```bash
python3 -m unittest tests.test_kalshi_auto tests.test_polymarket_us_recovery tests.test_polymarket_us_execution tests.test_preflight
# 137 tests in 99.645s; OK
```

The additional reproductions above still fail their intended safety properties.
Claude's entire branch suite was not run here. A passing suite is neither proof of
live settlement compatibility nor of profitability, leakage freedom or correct
account routing. No authenticated account was inspected in this audit.

## Remaining production gaps

- Durable recovery is still a non-sending outbox. Parent-to-child ownership transfer,
  shared reservation accounting and production dispatch are not integrated. Parent
  holds must not double-count child holds or disappear between transfer steps.
- Exact matched-market settlement compatibility is not established. The captured
  product-family examples have different postponement windows (48 hours vs two
  weeks) and independent discretionary payoff exceptions. Half-dollar tie payouts
  alone do not prove a guaranteed pair. Do not promote descriptions/FAQs to a
  verified execution rule.
- Account-scoped live reconciliation/positions evidence has not been independently
  verified. Unknown attempts stay held. Robinhood automatic event-contract order
  support remains unverified and must refuse before another venue leg is sent.
- There is no new empirical profit, real fill-rate or experiment evidence in this
  commit. Missing matches/settlement proof are reasons to exclude, not permission
  to label speculation guaranteed.

## Copy-paste Claude integration prompt

```text
Repo: /Users/kv15/Documents/ChatGPT/arbitradge

Read AGENTS.md, README.md, docs/VENUES.md, docs/ARCHITECTURE.md and
docs/EXECUTION_AUDIT_2026_09_30.md on origin/codex/polymarket-us-execution.
Inspect current branches, status and worktrees. Preserve the dirty main checkout.
Fetch origin without pulling/resetting; work in an isolated integration worktree
from an explicitly recorded agreed commit. Do not overwrite another agent's files.

Integrate the reviewed recorder/accounting/recovery/one-send fixes from
origin/codex/polymarket-us-execution deliberately, not by choosing one side wholesale
in conflicts. Review a878f99 and 5f0873e separately. In particular:

1. Preserve per-component finite/nonnegative cash validation, durable parent holds,
   observed cash/fee overruns and non-sending recovery outbox ownership. Retain new
   Claude kill/pinning/lock-cap behavior only with combined adversarial tests.
2. Reproduce and fix US signing past its original deadline and a kill switch set
   during preparation. Recheck deadline, stop, host, key/account and immutable plan
   at the final send boundary. A failed/ambiguous send is never retried.
3. Reproduce the ten-second reservation delay still sending a five-second Kalshi
   quote. Route autonomous sends through the durable one-send transport, with the
   original decision deadline; legacy retry/fallback paths are not substitutes.
4. Remove absence/time-based release of attempted unknown US sends. Empty activities,
   EOF or maxBlockTime do not prove nonacceptance. Keep cash held; require uniquely
   scoped authoritative final evidence for adoption/release. Positions conflicts,
   delayed activity, identical manual orders, restart and contradictory reads need
   tests. Do not weaken evidence gates just to get the suite green.
5. Before connecting pair child transports, publish the atomic parent-to-child
   ownership/reservation interface and prove crash/restart, duplicate dispatch,
   partial entry/hedge, failed-leg unwind and commission-overrun behavior offline.
   Unwind expiry is min(book deadline, inventory deadline), not a recomputed clock.
6. Keep guaranteed-only pair admission. Exact matched-market rules, tie, cancellation,
   postponement, OT and discretionary exceptions must be compatible and documented.
   Do not mark a family description verified or mislabel single-leg LAG as paired arb.

Stdlib only in arb_engine except existing optional signing crypto. Preserve fee
formulas. Dry-run remains default. Keep $25 per leg fee-inclusive and $50 aggregate;
unknown/contradicted attempts consume their full safe bound. No credential sourcing,
private account calls, approval arming, live flag activation, orders/cancellations,
or experiment test-fold opening during development. Do not edit cli.py/config.py
for feature work. No L2/websocket/maker expansion. Do not hand-edit MODEL/result tables.

Add narrowly owned adversarial tests, then freeze source while running the full
Python suite (standing approval pins code hashes), JS parity, render_results.py
--check and git diff --check. Update the actual test count after integration.
Commit coherent changes and push the integration branch. Return hashes, exact
commands/results, unresolved blockers and what is implemented vs tested vs empirical.
Do not claim live automatic paired execution is ready until every blocker is proven.
```

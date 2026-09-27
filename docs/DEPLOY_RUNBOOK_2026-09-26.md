# Deployment runbook: `claude/exec-readiness` to the running stack (demo only), 2026-09-26

This runbook moves the durable order ledger and the executor fixes from the development checkout
to the checkout the running stack uses. It then restarts the stack **with execution still on
Kalshi's demo exchange**.

* **No step turns production on.** Nothing here sets `KALSHI_ENV=prod`, `ARB_LIVE_TRADING`,
  `ARB_BUTTON_MODE=live` or `--execute-lag live`, and nothing installs a production key. The
  *Production stays off* section at the end shows how to check that.
* **The evidence behind each step** is in `docs/CLAUDE_HANDOFF_2026-09-26.md`, section
  *Deployment audit (17:10 PDT)*.
* **Every step says what to expect.** If a result differs, stop at that step.

```bash
GH=~/Documents/GitHub/arb-engine          # deployed checkout: cwd of every running process
AT=~/Documents/ChatGPT/arbitradge         # development checkout, branch claude/exec-readiness
BK=~/Documents/arb-backups/2026-09-26     # backups, outside both repos
```

## 0. Go / no-go (read-only)

**1. Nobody is editing.**

Other agent sessions committed to `$AT` until 16:58 PDT, and to a `/private/tmp` worktree of
`$GH`. At 17:08 both were idle, `$AT` was clean, the worktree was gone, and
`claude/micro-audit-3` was on GitHub (`24adfb5`). Check again:

```bash
git -C "$AT" status --porcelain     # must print nothing
git -C "$AT" log -4 --format='%h %ci %s' claude/exec-readiness
git -C "$GH" worktree list          # only $GH itself
```

**2. Pick the commit and pin it by hash.**

* The code was last validated at `a016721`: 1003 Python tests, JS parity, render check, and the
  demo check ALL PASS twice (per the branch's own handoff).
* The commits after it (`d725316`, `e2dd092` and the audit's docs commit) change only documents.
* Pin the hash so a later commit cannot slip in:

```bash
DEPLOY=$(git -C "$AT" rev-parse claude/exec-readiness)          # or a hash you choose
git -C "$AT" diff --stat a016721 "$DEPLOY" -- arb_engine scripts tests extension   # must print nothing (docs-only after a016721)
git -C "$AT" merge-base --is-ancestor 3ca9684 "$DEPLOY" && echo "fast-forward from 3ca9684: OK"
```

**3. The deployed checkout is what this runbook assumes:**

```bash
git -C "$GH" rev-parse --abbrev-ref HEAD   # main
git -C "$GH" rev-parse --short HEAD        # 3ca9684 (= GitHub main)
git -C "$GH" status --porcelain            # nothing
```

**4. Power: on AC, lid open.**

On 2026-09-26 the Mac slept, lid closed and on battery, from 13:02 to 16:16 PDT. That lost about
66 in-play game-hours. `caffeinate -i` (the launcher's hold) does not prevent lid-closed sleep.

```bash
pmset -g batt | head -2                    # "Now drawing from 'AC Power'"
```

**5. Timing.** The stop-to-start gap is a few minutes, and LAG signals in that gap are lost.

A restart also drops two kinds of in-memory state. Neither the deployed code nor the branch reloads
them from `lag_locks`:

* the lock watch of any LAG entry filled in the last 10 minutes (`lag_lock_watch_s` = 600);
* every outstanding "Robinhood done" button (paper).

Deploy when no entry has filled in the last 10 minutes:

```bash
python3 - <<'EOF'
import json, time
rows = [json.loads(l) for l in open("/Users/kv15/Documents/GitHub/arb-engine/out/orders/lag_intents.jsonl") if l.strip()]
print(sum(1 for r in rows if float(r.get("fill_count") or 0) > 0 and time.time() - r["ts"] < 600), "LAG fills in the last 10 min")
EOF
```

The live slates also end themselves after 8 h (`live --hours` defaults to 8), and the supervisor
restarts them 10 s later. The processes started at 12:23 PDT therefore restart at about
20:23 PDT. On the deployed code, each such restart resets the per-process caps.

## 1. Preserve both checkouts (no working tree or branch is changed)

```bash
mkdir -p "$BK"
git -C "$AT" bundle create "$BK/arbitradge-all.bundle" --all    # every branch, tag and refs/stash
git -C "$GH" bundle create "$BK/arb-engine-all.bundle" --all
git bundle verify "$BK/arbitradge-all.bundle" >/dev/null && git bundle verify "$BK/arb-engine-all.bundle" >/dev/null && echo bundles OK
git -C "$AT" diff > "$BK/arbitradge-uncommitted.patch"          # empty when step 0.1 passed
git -C "$GH" tag pre-deploy-2026-09-26 3ca9684                   # the rollback point (local tag)
```

**Only on this Mac, all in `$AT`** (checked against GitHub at 17:09 PDT):

* `claude/exec-readiness`;
* `claude/order-buttons` (`a8b9246`, 5 commits ahead of GitHub's `6676ebc` and contained in `claude/exec-readiness`);
* 8 `codex/*` branches;
* `stash@{0}` (the Sep 18/22 overlay work).

Everything in `$GH` is on GitHub. The bundles protect against a bad deploy, not a lost disk.
Pushing is optional and publishes to the public repo, so it is your decision:

```bash
git -C "$AT" -c credential.helper=osxkeychain push origin claude/exec-readiness
```

## 2. Reconcile before stopping (read-only)

**Exchange side (demo).** Nothing may be resting, and nothing unexplained may be open. The
branch's `kalshi` command reads `~/.kalshi/env` itself and prints no key. `balance`,
`positions`, `orders` and `fills` only send GET requests.

```bash
cd "$AT"                                      # clean and at $DEPLOY (step 0)
python3 -m arb_engine kalshi balance
python3 -m arb_engine kalshi positions        # expect no market_positions with a non-zero position; "truncated": false
python3 -m arb_engine kalshi orders           # resting: expect "count": 0 and "truncated": false
```

**What the demo account showed at 16:36 and 16:47 PDT:**

* no open positions and no resting orders;
* a balance of $238.24 (play money);
* 26 fills since 2026-09-22. Those include all 17 filled orders in the journal; the other 8 were
  the afternoon's `kalshi_demo_check.py` round trips.

If a position is open, it is a demo LAG entry. It settles by itself; write it down. A *resting*
order should not exist, because LAG sends IOC only. If one does, cancel it on demo:

```bash
python3 -m arb_engine kalshi cancel --order-id <id> --ticker <ticker> --confirm
```

**Engine side.** The deployed code has no ledger; its journal is the only local record:

```bash
tail -n 3 "$GH/out/orders/lag_intents.jsonl" | cut -c1-220
```

* **What the old executor records:**
  * Every `SUBMITTED` row carries an `order_id`.
  * When the order request raises, the old executor journals `status: "error"` and pushes EXEC ERROR.
* **What it misses:**
  * It does not charge an errored order to its caps, and it never looks the order up.
  * A journal write that fails is dropped silently.
  * A process killed in the middle of a POST writes nothing.
* **What that means:** the exchange listing above is the only proof that no order is unaccounted
  for. None were, as of 16:47.

**The journal's `filled_notional` is not what was paid.** It is filled contracts × the *limit*.
Demo fills happen at the demo book's own prices, so the exchange shows $248.13 against the
journal's $335.36 for the 17 filled orders.

## 3. Stop the whole stack

Stop everything before the merge. A process that keeps running while the tree changes under it
imports new modules lazily and runs mixed code. The bridge already does: it started at 11:02 on
`b831e0d`.

```bash
cd "$GH" && scripts/sunday.sh stop          # week, live-ncaaf, maker, live, bridge; deletes out/run/*.args
scripts/sunday.sh status                    # every row "down"
pgrep -fl "arb_engine (live|weekscan|maker|bridge)" || echo "no engine processes"
```

Then repeat the exchange check in step 2. Expect still 0 resting orders and the same positions.

## 4. Deploy the pinned commit (fast-forward only)

```bash
cd "$GH"
git fetch "$AT" claude/exec-readiness                 # brings $DEPLOY's objects
git merge --ff-only "$DEPLOY"                         # main moves to exactly $DEPLOY
test "$(git rev-parse HEAD)" = "$(git -C "$AT" rev-parse "$DEPLOY")" && echo "HEAD = DEPLOY"
git status --porcelain                                # nothing
python3 -m unittest discover -s tests -t . 2>&1 | tail -3     # "Ran 1003 tests ... OK" (AGENTS.md at a016721)
bash scripts/test_js.sh 2>&1 | tail -4                # 0 mismatches; ok 3769 / ok 159; extension PASS
python3 scripts/render_results.py --check             # OK
pgrep -fl "sunday.sh start"                           # only the four known orphans (see Shutdown), no new ones from the suite
```

## 5. Start with the same settings (demo execution)

```bash
cd "$GH"
BANKROLL=500 KELLY=0.25 scripts/sunday.sh start -- --execute-lag demo
```

Preflight can FAIL only because the Robinhood NFL catalogue timed out. In that case, start the
processes one at a time. `restart` works on a stopped process and reads `BANKROLL`/`KELLY` from
`out/run/`:

```bash
scripts/sunday.sh restart bridge
scripts/sunday.sh restart live -- --execute-lag demo
scripts/sunday.sh restart live-ncaaf -- --execute-lag demo
scripts/sunday.sh restart maker
scripts/sunday.sh restart week
```

## 6. Verify the commit, the environment and the account

```bash
cd "$GH"
scripts/sunday.sh status                                  # 5 rows "up"
git log -1 --format='%h %ci' HEAD                         # = $DEPLOY; every process below started after the merge
for n in bridge live live-ncaaf maker week; do p=$(cat out/run/$n.child); echo "$n pid=$p started=$(ps -o lstart= -p $p) cwd=$(lsof -a -p $p -d cwd -Fn | sed -n 's/^n//p')"; done
for n in live live-ncaaf; do ps eww -o command= -p "$(cat out/run/$n.child)" | tr ' ' '\n' | grep -E '^(KALSHI_ENV|ARB_LIVE_TRADING|ARB_BUTTON_MODE|KALSHI_BASE_URL|EXECUTABLE_VENUES)='; done
                                                          # expect exactly "KALSHI_ENV=demo" twice and nothing else
grep -h "LAG execution" out/logs/live-$(date +%F).log out/logs/live-ncaaf-$(date +%F).log | tail -2
                                                          # "LAG execution: demo (caps: 50 ct/order, $100/game, $500/day)"
ls ~/.kalshi                                              # demo.key env: no production key on this Mac
python3 scripts/kalshi_connect.py --env demo              # read-only: PASS and the demo balance
python3 -m arb_engine kalshi ledger                       # env "demo", "blocked": null, "open": []
```

**About the ledger:**

* The first ledger use creates `out/orders/kalshi_demo_ledger.sqlite3`. It binds it to the demo
  account by fingerprints only (key id and `GET /communications/id`, hashed).
* The ledger starts empty. Budgets are sums over the ledger, so the demo fills made today before
  the deploy are not counted: $158.96 plus $5.28 in fees, on 8 tickers in 6 games, on 2026-09-26
  local. The $100/game and $500/day caps therefore start from 0 for the rest of the day. That is
  acceptable on demo.

## 7. Run the existing demo verification path

Demo play money only. Never add `--record` (it rewrites test fixtures) or `--env prod`.

```bash
cd "$GH"
python3 scripts/kalshi_connect.py --env demo                              # read-only check
set -a; eval "$(sed -n 's/^export //p' ~/.kalshi/env)"; set +a            # the demo check reads KALSHI_* from the environment
python3 scripts/kalshi_demo_check.py
python3 scripts/kalshi_demo_check.py --fill --confirm-demo                # optional
python3 -m arb_engine kalshi orders                                       # "count": 0: nothing left resting
python3 -m arb_engine kalshi ledger                                       # the engine's ledger is untouched by the check
```

**What the plain check does** (it must end ALL PASS):

* places $0.01 test orders, then single and batched cancels;
* checks the IOC order shape;
* checks lost-answer recovery through the ledger;
* drives the LAG executor and the maker's `KalshiBroker`, all on a *temporary* ledger.

**What `--fill --confirm-demo` adds:** one real demo fill of at most $0.95 of play money, with a
fee comparison (demo charges centicent), then sells it back.

## 8. The first hour after the deploy

```bash
python3 -m arb_engine kalshi ledger | python3 -c 'import json,sys; d=json.load(sys.stdin); print("blocked:", d.get("blocked"), "| states:", d.get("by_state"), "| today:", d.get("committed_today"), "| open:", len(d.get("open") or []))'
grep -hE "LAG auto-trade|UNKNOWN|blocked|EXEC ERROR|Traceback" out/logs/live*-$(date +%F).log | tail -20
tail -n 5 out/orders/lag_intents.jsonl | cut -c1-220
```

`blocked` must stay `None`. `committed_today` counts actual fill cost plus fees from the
exchange, not the limit.

## 9. Recovery

**Step 4's `--ff-only` refuses (main moved).** Run `git merge "$DEPLOY"`, resolve, and re-run
every check in step 4. Or stop and ask.

**Tests fail after the merge.** Go back to the old code and run step 5:

```bash
git -C "$GH" reset --keep pre-deploy-2026-09-26
```

The old code has no ledger and uses per-process caps.

**`kalshi ledger` shows `blocked` (an order's outcome is unknown).**

1. Run `python3 -m arb_engine kalshi reconcile`. It sends only GETs and writes only the local ledger. Repeat after 30 s.
2. If it is still blocked, look for the order yourself: `python3 -m arb_engine kalshi orders --status all` and `python3 -m arb_engine kalshi fills`.
3. Only if the exchange provably has no such order: `python3 -m arb_engine kalshi release --intent-id <id> --reason "<what you checked>" --confirm`. Without `--confirm` it is a dry run.

While it is blocked, new LAG orders are refused, which is the safe state. To keep recording
without execution in the meantime:

```bash
scripts/sunday.sh restart live-ncaaf -- --execute-lag intent
```

**A process crash-loops** (`exited rc=… restart in 10s` repeating). Read
`tail -50 out/logs/<name>-$(date +%F).log`. To stop only that process:
`kill -TERM $(cat out/run/<name>.pid)`; its TERM trap stops the child.

**Anything unexpected in the demo order flow.** Stop order flow first, then check what rests:

```bash
kill -TERM $(cat out/run/live.pid) $(cat out/run/live-ncaaf.pid)
python3 -m arb_engine kalshi orders
python3 -m arb_engine kalshi cancel-all --confirm     # demo, only if something rests
```

**The Mac slept.** The processes resume by themselves on wake. For a few minutes DNS fails
(`Could not resolve host`); about 2,500 log lines are normal. See the gap with
`pmset -g log | grep -E "Entering Sleep|Wake from"`.

**Roll back after a bad hour.** The old code ignores the ledger file; keep the file.

```bash
scripts/sunday.sh stop
git -C "$GH" reset --keep pre-deploy-2026-09-26
BANKROLL=500 KELLY=0.25 scripts/sunday.sh start -- --execute-lag demo
```

**No execution at all, keep recording.** Run
`scripts/sunday.sh restart live -- --execute-lag off`, and the same for `live-ncaaf`.

## 10. Shutdown

```bash
cd "$GH" && scripts/sunday.sh stop && scripts/sunday.sh status     # all "down"; *.args deleted; bankroll/kelly/topic kept
pgrep -fl "arb_engine" || echo "no engine processes"
pmset -g assertions | grep -c "caffeinate command-line tool"       # 0 once the live supervisor is gone
python3 -m arb_engine kalshi orders                                # 0 resting on demo
```

**Leftovers the launcher did not start.** Check each one before killing it.

**The four orphaned test supervisors.**

* **What they are:** `bash scripts/sunday.sh start`, PIDs 25788, 28381, 29273 and 30093, from 2026-09-22/23. Each runs in a deleted `/private/tmp/claude-501/sunday-*` directory.
* **What they do:** each spawns `sleep 1` forever and has burned 8–11 CPU-minutes. They have no Kalshi environment and cannot reach `out/`.

```bash
for p in 25788 28381 29273 30093; do printf "%s " $p; lsof -a -p $p -d cwd -Fn | sed -n 's/^n//p'; done   # expect the deleted temp dirs
kill -TERM 25788 28381 29273 30093
```

**Another Claude session's practice loop.**

* **What it is:** zsh PID 51681.
* **What it does:** at 17:00:43 PDT it started `scripts/arb_button_practice.py --sport ncaaf --phone --pairs 1 --mid --tap-wait 3600` (PID 93349). That pushes one **practice** button (`ArbButton("paper")`; nothing goes to Kalshi) and waits up to an hour for your tap.

If you don't want it: `kill 51681 93349`.

## Known issues that ship with this deploy

None of these blocks a demo deploy. Decide on them before any production use.

1. **The run loop pins the tick clock, so three of the branch's fixes are inert in production.**
   * `LiveSlate.run()` calls `self.tick(t0)` (`arb_engine/strategy/live.py:496`), and `tick()` treats any explicit time as a pinned replay clock (`pinned = now`, line 249).
   * The skipped fixes are: the post-fetch clock refresh, per-game decision time, and the `polling gap` records.
   * The tests call `tick()` with no argument, so they pass. The stale-clock symptoms (buttons issued up to 4,219 s after their `created` time, cooldowns measured against an old clock) will therefore continue.
   * Likely fix: call `self.tick()` in `run()` and keep `t0` for pacing, plus a test that goes through `run()`.
2. **A signal re-fired with a larger edge becomes a second full-size order within seconds.**
   * 11 clusters so far; 4 of the extra orders filled (COLO 2×50, WAKE 2×50, VT 2×1, CLEM 50 then 39 a second later).
   * The ledger's duplicate key includes the signal time, so it does not stop these; only the $100/game budget does.
3. **A finished game stays "live" in the fast lane after the slate goes quiet.**
   * `tick()` returns early when no game is wanted, before `_live_priced` is rebuilt (`live.py:219-220` vs `:276` on `3ca9684`; `:274` vs `:342` on the branch).
   * The fast lane then keeps recording the finished game with `live=1`: 6.1 h for ATL|GB and CCU|LIB, 4.0 h for CAL|CLEM. Idle ticks also run every 5 s instead of 60 s.
4. **The ntfy push throttle is per process.** 40 duplicate cross-process pushes, and the free quota (250 a day) runs out by late morning. No push has been delivered since 10:52 PDT on 09-26, so no button could be tapped.
5. **Kalshi and Polymarket `quote_time` are never recorded,** so their quote age cannot be measured. Robinhood's "no timestamp" arrives as −6,795,364,578 (Go's zero time) and has to be read as missing.
6. **The recorder grows about 1.2 GB per hour with ~20 live games,** measured at 16:41–16:45 PDT; `history.db` is 6.0 GB and 37 GiB was free. At that rate the free space lasts about 30 live hours, so a full college Saturday plus an NFL Sunday uses roughly half of it. Watch `df -h ~`.

## Production stays off, and how to check it

* **No production key.** `~/.kalshi` holds only `demo.key` and `env` (`KALSHI_ENV=demo`). Production execution would need you to install a production key yourself (`scripts/kalshi_install_key.sh … prod`).
* **The environment.**
  * Every process runs with `KALSHI_ENV=demo` and the demo key path.
  * None of `ARB_LIVE_TRADING`, `ARB_BUTTON_MODE`, `KALSHI_BASE_URL` or `EXECUTABLE_VENUES` is set.
  * There is no `.env` in either checkout.
  * Step 6 re-checks all of this.
* **The code paths.**
  * The launcher passes `--execute-lag demo`.
  * `live` mode refuses to run without `ARB_LIVE_TRADING=1`, and on the branch also without a production client, a production host, and a ledger bound to that account.
  * The maker is hard-coded to `--mode paper`, and the button defaults to paper.
* **Turning production on is not part of this deploy.** It needs a production key, `KALSHI_ENV=prod`, `ARB_LIVE_TRADING=1`, and `--execute-lag live` or `ARB_BUTTON_MODE=live`, each set by hand.

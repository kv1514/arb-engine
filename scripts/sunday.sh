#!/usr/bin/env bash
# Sunday launcher: preflight, then the bridge, the live slate and the paper maker under a
# restart-on-crash supervisor (10 s backoff), logs under out/logs/, PIDs under out/run/.
#
#   scripts/sunday.sh start [-- <extra args for live>]   preflight (refuses on FAIL), then start all (idle sleep held off while live runs)
#   scripts/sunday.sh stop                                stop every supervisor and its child
#   scripts/sunday.sh status                              what is running, log tails
#   scripts/sunday.sh preflight                           the readiness report only
#   scripts/sunday.sh restart <bridge|live|maker> [-- <extra args for live>]
#                                                         bounce one process (e.g. after a code pull);
#                                                         live keeps the extras given at start unless new ones follow --
#   scripts/sunday.sh logs [name]                         tail -f the log(s)
#   scripts/sunday.sh ntfy <topic>                        set the ntfy topic (persisted), send a test push
#
# Environment knobs (all optional):
#   SPORT=nfl  EXTRA_SPORTS=ncaaf (a live-<sport> recorder per extra sport; "" for none)
#   DATE=<YYYY-MM-DD ET, default today>  BANKROLL=<dollars, default 1000>
#   KELLY=0.25  EVERY=5  MAKER_SIZE=10  BRIDGE_PORT=8765  PREFLIGHT_LIMIT=16  BRIDGE_WAIT_S=10
#   EXECUTABLE_VENUES / ROBINHOOD_GOLD / INPLAY_* pass straight through to the engine.
#   START_ON_WARN=1 (default) — a WARN verdict still starts; FAIL never does.
#
# Every process runs with ARB_HTTP_TRANSPORT=curl (the proxy on this box truncates chunked
# HTTP/1.1 to urllib; curl speaks HTTP/2). Nothing here places orders: the maker is paper.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
export ARB_HTTP_TRANSPORT="${ARB_HTTP_TRANSPORT:-curl}"
export PYTHONUNBUFFERED=1

SPORT="${SPORT:-nfl}"
BANKROLL="${BANKROLL:-1000}"
KELLY="${KELLY:-0.25}"
EVERY="${EVERY:-5}"
MAKER_SIZE="${MAKER_SIZE:-10}"
BRIDGE_PORT="${BRIDGE_PORT:-8765}"
PREFLIGHT_LIMIT="${PREFLIGHT_LIMIT:-16}"
START_ON_WARN="${START_ON_WARN:-1}"
BACKOFF_S="${BACKOFF_S:-10}"
BRIDGE_WAIT_S="${BRIDGE_WAIT_S:-10}"
PY="${PYTHON:-python3}"
LOGS="$ROOT/out/logs"
RUN="$ROOT/out/run"
# Extra sports get their own live recorder (live-<sport>): college on Saturdays by default.
EXTRA_SPORTS="${EXTRA_SPORTS-ncaaf}"
NAMES="bridge live maker"
for _s in $(echo "$EXTRA_SPORTS" | tr ',' ' '); do [ -n "$_s" ] && [ "$_s" != "$SPORT" ] && NAMES="$NAMES live-$_s"; done
# The ntfy topic persists in out/run/ntfy_topic.txt (`sunday.sh ntfy <topic>` sets it); an
# exported ARB_ALERT_NTFY wins.
if [ -z "${ARB_ALERT_NTFY:-}" ] && [ -f "$ROOT/out/run/ntfy_topic.txt" ]; then export ARB_ALERT_NTFY="$(cat "$ROOT/out/run/ntfy_topic.txt")"; fi
# Kalshi credentials (the key ID and the *path* to the private-key file, never the key itself)
# may live in ~/.kalshi/env as `export KALSHI_...=...` lines; an exported variable wins. The
# file is parsed, not sourced, so nothing in it can run. Check with scripts/kalshi_connect.py.
if [ -f "$HOME/.kalshi/env" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#export }"
    case "$line" in KALSHI_[A-Z_]*=*) ;; *) continue ;; esac
    name="${line%%=*}"; value="${line#*=}"; value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
    case "$value" in "~/"*) value="$HOME/${value#\~/}" ;; esac
    [ -z "$(printenv "$name")" ] && export "$name=$value"
  done < "$HOME/.kalshi/env"
fi

usage() { sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

today_et() {
  local d
  d="$("$PY" -c 'from arb_engine.preflight import today_et; print(today_et())' 2>/dev/null | tail -1)"
  case "$d" in [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) echo "$d" ;; *) date +%F ;; esac
}

# --- the three commands ------------------------------------------------------------------------
# The live journal / recorder paths are the ones the runbook tells you to send back.
cmd_for() {
  local name="$1"; shift
  case "$name" in
    bridge) echo "$PY -m arb_engine bridge --port $BRIDGE_PORT" ;;
    live|live-*)
      local quiet="" fast="" sport="$SPORT" journal="out/live_journal.jsonl"
      if [ "$name" != "live" ]; then sport="${name#live-}"; journal="out/live_${sport}_journal.jsonl"; fi
      if "$PY" -m arb_engine live --help 2>/dev/null | grep -q -- '--quiet'; then quiet=" --quiet"; fi
      # the fast lane (1 s Kalshi + Robinhood refreshes between full ticks) when the build has it; FAST=0 disables
      if [ "${FAST:-1}" != "0" ] && "$PY" -m arb_engine live --help 2>/dev/null | grep -q -- '--fast'; then fast=" --fast ${FAST:-1}"; fi
      echo "$PY -m arb_engine live --sport $sport --every $EVERY --record out/history.db --journal $journal --bankroll $BANKROLL --kelly $KELLY$quiet$fast ${*:-}" ;;
    maker) echo "$PY -m arb_engine maker --sport $SPORT --mode paper --size $MAKER_SIZE --journal out/maker_journal.jsonl" ;;
    *) echo "unknown process: $name" >&2; return 1 ;;
  esac
}

# --- supervisor ---------------------------------------------------------------------------------
# One background bash loop per process: run the command, log its exit, sleep, run again. The
# loop's PID is out/run/<name>.pid, the current child's out/run/<name>.child, and the extra
# args `start` was given for live sit in out/run/live.args so `restart live` reuses them.
# `stop` kills the loop first (so it cannot respawn); the loop's TERM trap sends the child ONE
# SIGINT, so the maker cancels its resting orders and the live slate flushes its journal
# inside a single KeyboardInterrupt (a second SIGINT would land inside that handler and abort
# the cleanup). The loop detaches from the caller's stdio (its output goes to the log), so
# `start` returns even when stdout is a pipe.
#
# bash makes every asynchronous child *ignore* SIGINT, and an ignored SIGINT survives exec:
# Python then never installs KeyboardInterrupt, so Ctrl-C-style shutdown (the maker's
# cancel-all, the journal flush) would silently not happen. Each child is therefore started
# through a stdlib shim that resets SIGINT to default and execs the real command (same PID).
SHIM_PY="${SHIM_PY:-python3}"
RESET_SIGINT='import os, signal, sys; signal.signal(signal.SIGINT, signal.SIG_DFL); os.execvp(sys.argv[1], sys.argv[1:])'

supervise() {
  local name="$1" cmd="$2" date="$3"
  local log="$LOGS/$name-$date.log"
  (
    child=""
    # TERM (from `stop`, or a stray kill) ends the loop and takes the child with it, so a
    # killed supervisor never orphans a Python process; the backoff sleep is a background
    # job under `wait` so the trap runs at once instead of after the sleep.
    trap '[ -n "$child" ] && kill -INT "$child" 2>/dev/null; exit 0' TERM
    while :; do
      echo "[$(date '+%F %T')] sunday.sh: starting $name: $cmd" >> "$log"
      # shellcheck disable=SC2086  # $cmd is a command line; word-splitting it is the point
      "$SHIM_PY" -c "$RESET_SIGINT" $cmd >> "$log" 2>&1 &
      child=$!
      echo "$child" > "$RUN/$name.child"
      wait "$child" ; rc=$?
      child=""
      rm -f "$RUN/$name.child"
      echo "[$(date '+%F %T')] sunday.sh: $name exited rc=$rc; restart in ${BACKOFF_S}s" >> "$log"
      sleep "$BACKOFF_S" & wait $!
    done
  ) </dev/null >/dev/null 2>&1 &
  echo $! > "$RUN/$name.pid"
  echo "  started $name (supervisor pid $!, log $log)"
  # Hold off idle sleep while the supervisor lives (a closed lid still sleeps: keep it open
  # and on power). The first Sunday's recorder lost hours to sleep; the -w form releases the
  # assertion by itself when the supervisor exits, so stop/restart leave nothing behind.
  if [ "$name" = "live" ] && [ "${KEEP_AWAKE:-1}" != "0" ] && command -v caffeinate >/dev/null 2>&1; then  # one hold is enough: the others live as long
    caffeinate -i -w "$(cat "$RUN/$name.pid")" </dev/null >/dev/null 2>&1 &
    echo "  caffeinate -i holding idle sleep off while live runs (KEEP_AWAKE=0 disables)"
  fi
}

alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }

health() { curl -s -m 2 "http://127.0.0.1:$BRIDGE_PORT/health" >/dev/null 2>&1; }

# Poll /health for up to $1 seconds after starting the bridge (it imports the engine and the
# WP model first); the overlay's "reload the extension" step needs it answering.
wait_health() {
  local i
  for ((i = 0; i < ${1:-10}; i++)); do
    if health; then echo "  bridge /health answering on :$BRIDGE_PORT"; return 0; fi
    sleep 1
  done
  return 1
}

pid_of() { [ -f "$RUN/$1.pid" ] && cat "$RUN/$1.pid"; }
child_of() { [ -f "$RUN/$1.child" ] && cat "$RUN/$1.child"; }

stop_one() {
  local name="$1" sup child
  sup="$(pid_of "$name" || true)"; child="$(child_of "$name" || true)"
  # One SIGINT only: a live supervisor forwards it from its TERM trap; the direct INT is for
  # an orphaned child whose supervisor is already gone.
  if alive "$sup"; then kill -TERM "$sup" 2>/dev/null
  elif alive "$child"; then kill -INT "$child" 2>/dev/null; fi
  if alive "$child"; then
    for _ in 1 2 3 4 5 6 7 8 9 10; do alive "$child" || break; sleep 1; done
    alive "$child" && kill -TERM "$child" 2>/dev/null
    sleep 1
    alive "$child" && kill -KILL "$child" 2>/dev/null
  fi
  rm -f "$RUN/$name.pid" "$RUN/$name.child"
  echo "  stopped $name"
}

status_one() {
  local name="$1" sup child state="down"
  sup="$(pid_of "$name" || true)"; child="$(child_of "$name" || true)"
  if alive "$sup" && alive "$child"; then state="up (pid $child)"
  elif alive "$sup"; then state="restarting (supervisor $sup, no child)"
  fi
  local extra=""
  [ -s "$RUN/$name.args" ] && extra=" [$(cat "$RUN/$name.args")]"
  printf '  %-7s %s%s\n' "$name" "$state" "$extra"
  local log
  log="$(ls -t "$LOGS"/"$name"-*.log 2>/dev/null | head -1)"
  if [ -n "$log" ]; then tail -n 3 "$log" | sed 's/^/          | /'; fi
}

# --- preflight ------------------------------------------------------------------------------------
run_preflight() {
  local date="$1"
  mkdir -p "$LOGS"
  local log="$LOGS/preflight-$date.log"
  echo "# preflight $SPORT $date (copy in $log)"
  "$PY" -m arb_engine preflight --sport "$SPORT" --date "$date" --bridge "http://127.0.0.1:$BRIDGE_PORT" --limit "$PREFLIGHT_LIMIT" --bankroll "$BANKROLL" --kelly "$KELLY" 2>&1 | tee "$log"
  local rc="${PIPESTATUS[0]}"
  if [ "$rc" -eq 0 ] && [ "$START_ON_WARN" != "1" ] && grep -q '^VERDICT WARN' "$log"; then
    echo "preflight WARN and START_ON_WARN=$START_ON_WARN: treating as FAIL"; return 3
  fi
  return "$rc"
}

# --- main -----------------------------------------------------------------------------------------
action="${1:-}"
[ $# -gt 0 ] && shift
case "$action" in
  -h|--help|help|"")
    usage; [ -n "$action" ] && exit 0 || exit 1 ;;
  preflight)
    run_preflight "${DATE:-$(today_et)}"; exit $? ;;
  start)
    DATE="${DATE:-$(today_et)}"
    mkdir -p "$LOGS" "$RUN"
    for n in $NAMES; do
      if alive "$(pid_of "$n" || true)"; then echo "$n is already running (sunday.sh status); stop it first"; exit 1; fi
    done
    extra=""
    if [ "${1:-}" = "--" ]; then shift; extra="$*"; fi
    run_preflight "$DATE"; rc=$?
    if [ "$rc" -ne 0 ]; then echo "preflight FAIL (rc=$rc): not starting. Fix the FAIL rows above and run again."; exit 2; fi
    echo "preflight ok; starting $NAMES (logs: $LOGS, pids: $RUN)"
    printf '%s' "$extra" > "$RUN/live.args"
    supervise bridge "$(cmd_for bridge)" "$DATE"
    wait_health "$BRIDGE_WAIT_S" || echo "  bridge /health not answering on :$BRIDGE_PORT yet (see $LOGS/bridge-$DATE.log); live and maker start anyway"
    supervise live "$(cmd_for live "$extra")" "$DATE"
    supervise maker "$(cmd_for maker)" "$DATE"
    for n in $NAMES; do
      case "$n" in live-*) printf '%s' "$extra" > "$RUN/$n.args"; supervise "$n" "$(cmd_for "$n" "$extra")" "$DATE" ;; esac
    done
    [ -n "${ARB_ALERT_NTFY:-}" ] && echo "  ntfy pushes -> ${ARB_ALERT_NTFY} (ARB / LAG / HEDGE NOW)"
    echo "watch:  $0 logs live      status:  $0 status      stop:  $0 stop" ;;
  stop)
    rev=""; for n in $NAMES; do rev="$n $rev"; done   # children first, bridge last
    for n in $rev; do stop_one "$n"; done
    rm -f "$RUN"/*.args ;;
  ntfy)
    t="${1:-}"; [ -z "$t" ] && { echo "usage: $0 ntfy <topic-or-url>   (current: ${ARB_ALERT_NTFY:-none})"; exit 1; }
    mkdir -p "$RUN"; printf '%s' "$t" > "$RUN/ntfy_topic.txt"; export ARB_ALERT_NTFY="$t"
    url="$t"; case "$t" in http://*|https://*) ;; *) url="https://ntfy.sh/$t" ;; esac
    curl -s -m 8 -X POST -H "Title: Arb Engine" -H "Tags: white_check_mark" --data-binary "Connected: ARB / LAG / HEDGE NOW pushes from the live slates will arrive here." "$url" >/dev/null && echo "test push sent to $url; restart live processes to pick the topic up: $0 restart live" || echo "push failed (no network?)"; ;;
  restart)
    n="${1:-}"; [ -z "$n" ] && { echo "restart which? $NAMES"; exit 1; }
    shift
    case " $NAMES " in *" $n "*) ;; *) echo "unknown process: $n ($NAMES)"; exit 1 ;; esac
    DATE="${DATE:-$(today_et)}"; mkdir -p "$LOGS" "$RUN"
    # The extras `start` was given survive a restart (they are what the operator is running
    # with); new ones after -- replace them for this and later restarts.
    extra=""
    if [ "${1:-}" = "--" ]; then shift; extra="$*"; printf '%s' "$extra" > "$RUN/$n.args"
    elif [ -f "$RUN/$n.args" ]; then extra="$(cat "$RUN/$n.args")"; fi
    stop_one "$n"; supervise "$n" "$(cmd_for "$n" "$extra")" "$DATE" ;;
  status)
    echo "sunday.sh status ($ROOT, transport $ARB_HTTP_TRANSPORT)"
    for n in $NAMES; do status_one "$n"; done
    if health; then echo "  bridge  /health answers on :$BRIDGE_PORT"; else echo "  bridge  /health NOT answering on :$BRIDGE_PORT"; fi ;;
  logs)
    n="${1:-}"
    ls "$LOGS"/*.log >/dev/null 2>&1 || { echo "no logs under $LOGS yet"; exit 1; }
    if [ -n "$n" ]; then tail -n 50 -f "$(ls -t "$LOGS"/"$n"-*.log | head -1)"; else tail -n 20 -f "$LOGS"/*.log; fi ;;
  *)
    echo "unknown action: $action"; usage; exit 1 ;;
esac

#!/usr/bin/env bash
# Runs the extension's JS tests with node if available, else macOS's bundled JavaScriptCore.
#   tests/arb-core.test.js   — fee parity with the Python models + arb math
#   tests/background.test.js — background worker against recorded venue responses
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMPD="${TMPDIR:-/tmp}"
run_js() {
  if command -v node >/dev/null 2>&1; then node "$1"; else
    JSC=/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc
    [ -x "$JSC" ] || { echo "neither node nor jsc found"; exit 1; }
    "$JSC" "$1"
  fi
}

# 1) arb-core parity
T1="$TMPD/arbcore-test-$$.js"
{
  printf 'const FEE_VECTORS = '; cat "$ROOT/tests/fixtures/fee_vectors.json"; printf ';\n'
  # arb vectors (tie payouts, ticks, size steps) from the Python arbitrage module, when present
  if [ -f "$ROOT/tests/fixtures/arb_vectors.json" ]; then printf 'globalThis.ARB_VECTORS = '; cat "$ROOT/tests/fixtures/arb_vectors.json"; printf ';\n'; fi
  cat "$ROOT/extension/arb-core.js" "$ROOT/tests/arb-core.test.js"
} > "$T1"
run_js "$T1"; rm -f "$T1"

# 2) background integration (fixtures embedded as a JSON object of file contents)
T2="$TMPD/background-test-$$.js"
{
  printf 'const FIXTURES = '
  python3 - "$ROOT" <<'PY'
import json, os, sys
root = sys.argv[1]
d = os.path.join(root, "tests", "fixtures", "ext")
out = {name: open(os.path.join(d, name), encoding="utf-8").read() for name in sorted(os.listdir(d))}
out["nfl_teams.json"] = open(os.path.join(root, "extension", "nfl_teams.json"), encoding="utf-8").read()
print(json.dumps(out))
PY
  printf ';\n'
  cat "$ROOT/tests/background.stubs.js" "$ROOT/extension/arb-core.js"
  sed 's/^importScripts("arb-core.js");//' "$ROOT/extension/background.js"
  cat "$ROOT/tests/background.test.js"
} > "$T2"
run_js "$T2"; rm -f "$T2"

# 3) static extension self-check (manifest, referenced files, permissions, JS syntax)
python3 "$ROOT/scripts/check_extension.py" "$ROOT/extension"

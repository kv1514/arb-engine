#!/usr/bin/env bash
# Builds dist/arb-engine-extension.zip with the extension's files at the zip root
# (so "Load unpacked" on the unzipped folder, or a Web Store upload, finds manifest.json).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/dist/arb-engine-extension.zip"
python3 "$ROOT/scripts/check_extension.py" "$ROOT/extension"
mkdir -p "$ROOT/dist"
rm -f "$OUT"
( cd "$ROOT/extension" && zip -q -X -r "$OUT" . -x '.*' '*/.*' '*.DS_Store' '__MACOSX/*' )
echo "wrote $OUT"
unzip -l "$OUT" | tail -n +2

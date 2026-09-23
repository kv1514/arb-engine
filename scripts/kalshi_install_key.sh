#!/usr/bin/env bash
# Install a Kalshi API key you just created, then check it (read-only).
#
#   scripts/kalshi_install_key.sh <downloaded-key-file> <key-id> [demo|prod]
#
# Moves the private-key file Kalshi downloaded into ~/.kalshi/<env>.key (mode 600, directory
# 700), writes ~/.kalshi/env (the key ID and the *path* to the file, mode 600) and runs
# scripts/kalshi_connect.py. The key's contents are never printed, copied or sent anywhere by
# this script; only its first line is checked to make sure it is a private key.
# Run it in your own Terminal: the key belongs on this disk and nowhere else — never paste
# it into a chat, an issue, a commit or a message.
set -euo pipefail
file="${1:-}"; key_id="${2:-}"; env="${3:-demo}"
die() { echo "kalshi_install_key: $*" >&2; exit 2; }
case "$file" in -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;; esac
[ -n "$file" ] && [ -n "$key_id" ] || die "usage: $0 <downloaded-key-file> <key-id> [demo|prod]"
case "$env" in demo|prod) ;; *) die "env must be demo or prod (got '$env')" ;; esac
[[ "$key_id" =~ ^[A-Za-z0-9-]+$ ]] || die "the key ID should look like 1a2b3c4d-...: letters, digits and dashes only"
[ -f "$file" ] || die "no such file: $file"
head -1 "$file" | grep -q -- "-----BEGIN .*PRIVATE KEY-----" || die "$file does not start with a PRIVATE KEY header: not the key file Kalshi downloaded"

umask 077
mkdir -p "$HOME/.kalshi"; chmod 700 "$HOME/.kalshi"
dest="$HOME/.kalshi/$env.key"
mv "$file" "$dest"; chmod 600 "$dest"
printf 'export KALSHI_ENV=%s\nexport KALSHI_API_KEY=%s\nexport KALSHI_PRIVATE_KEY_PATH=%s\n' "$env" "$key_id" "$dest" > "$HOME/.kalshi/env"
chmod 600 "$HOME/.kalshi/env"
echo "installed: key file -> $dest (600), settings -> $HOME/.kalshi/env (600), env=$env"
[ "${KALSHI_INSTALL_NO_CHECK:-0}" = 1 ] && exit 0
cd "$(dirname "$0")/.." && exec python3 scripts/kalshi_connect.py --env "$env"

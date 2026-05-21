#!/usr/bin/env bash
set -e
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
exec python3 "$REPO_ROOT/anvil/launch/preload/preload.py" "$REPO_ROOT/game/sbox-server" "$@"

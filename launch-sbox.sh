#!/usr/bin/env bash
set -e

ANVIL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$ANVIL_DIR/.." && pwd)"
PATCH_BIN="$ANVIL_DIR/patches/bin"
GAME_BIN="$REPO_ROOT/game/bin/linuxsteamrt64"

for so in "$PATCH_BIN"/*.so; do
    [ -f "$so" ] || continue
    LD_PRELOAD="$so:${LD_PRELOAD}"
done
export LD_PRELOAD
export LD_LIBRARY_PATH="$GAME_BIN:${LD_LIBRARY_PATH}"

exec "$REPO_ROOT/game/sbox" "$@"

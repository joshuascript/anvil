#!/usr/bin/env bash
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PATCHES_BIN="$REPO_ROOT/anvil/patch/bin"
GAME_DIR="$REPO_ROOT/game"

if [ -d "$PATCHES_BIN" ]; then
    for so in "$PATCHES_BIN"/*.so; do
        [ -f "$so" ] && LD_PRELOAD="${LD_PRELOAD:+$LD_PRELOAD:}$so"
    done
    export LD_PRELOAD
fi

export LD_LIBRARY_PATH="$GAME_DIR/bin/linuxsteamrt64:$LD_LIBRARY_PATH"
exec python3 "$REPO_ROOT/anvil/launch/preload/inotify.py" "$GAME_DIR/sbox-server" "$@"

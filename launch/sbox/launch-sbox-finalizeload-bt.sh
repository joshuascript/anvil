#!/usr/bin/env bash
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PATCHES_BIN="$REPO_ROOT/anvil/patch/bin"
GAME_DIR="$REPO_ROOT/game"
GDB_DIR="$REPO_ROOT/anvil/debug/logs"

# Load all patches except finalizeload — the jge must remain intact for the
# breakpoint in gdb-finalizeload-bt.py to fire.
if [ -d "$PATCHES_BIN" ]; then
    for so in "$PATCHES_BIN"/*.so; do
        [[ "$so" == *finalizeload* ]] && continue
        [ -f "$so" ] && LD_PRELOAD="${LD_PRELOAD:+$LD_PRELOAD:}$so"
    done
    export LD_PRELOAD
fi

export LD_LIBRARY_PATH="$GAME_DIR/bin/linuxsteamrt64:$LD_LIBRARY_PATH"
export SBOX_TRACE_DIR="$GDB_DIR"

mkdir -p "$GDB_DIR"

exec python3 "$REPO_ROOT/anvil/launch/preload/inotify.py" \
    gdb \
    --readnever \
    -iex "set debuginfod enabled off" \
    -ex "handle SIG34 nostop noprint pass" \
    -ex "handle SIG35 nostop noprint pass" \
    -ex "source $REPO_ROOT/anvil/debug/scripts/gdb/gdb-auto-bt.py" \
    -ex "source $REPO_ROOT/anvil/debug/scripts/gdb/gdb-finalizeload-bt.py" \
    -ex "run" \
    --args "$GAME_DIR/sbox" "$@"

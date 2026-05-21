#!/usr/bin/env bash
set -e
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
GDB_SCRIPTS="$REPO_ROOT/anvil/debug/scripts/gdb"
exec python3 "$REPO_ROOT/anvil/launch/preload/preload.py" \
    gdb \
    --readnever \
    -iex "set debuginfod enabled off" \
    -ex "handle SIG34 nostop noprint pass" \
    -ex "handle SIG35 nostop noprint pass" \
    -ex "source $GDB_SCRIPTS/gdb-auto-bt.py" \
    -ex "source $GDB_SCRIPTS/capture_steam_callback.py" \
    -ex "run" \
    --args "$REPO_ROOT/game/sbox" "$@"

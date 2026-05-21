#!/usr/bin/env bash
# Launches sbox under gdb with the wrapper object layout probe.
# Installs a breakpoint at libengine2.so+0x34d160 (wrong vtable[6] target),
# dumps the wrapper object fields on each hit, then continues.
# Output shows which field of the wrapper holds the raw ISteamHTMLSurface ptr.
set -e
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
GDB_SCRIPTS="$REPO_ROOT/anvil/debug/scripts/gdb"
exec python3 "$REPO_ROOT/anvil/launch/preload/preload.py" \
    gdb \
    --readnever \
    -iex "set debuginfod enabled off" \
    -iex "set pagination off" \
    -ex "handle SIG34 nostop noprint pass" \
    -ex "handle SIG35 nostop noprint pass" \
    -ex "handle SIGSEGV nostop noprint pass" \
    -ex "source $GDB_SCRIPTS/probe_wrapper_layout.py" \
    -ex "run" \
    --args "$REPO_ROOT/game/sbox" "$@"

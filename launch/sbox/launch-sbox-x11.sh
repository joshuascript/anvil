#!/usr/bin/env bash
set -e
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

export SDL_VIDEODRIVER=x11
export GDK_BACKEND=x11
export QT_QPA_PLATFORM=xcb

exec python3 "$REPO_ROOT/anvil/launch/preload/preload.py" "$REPO_ROOT/game/sbox" "$@"

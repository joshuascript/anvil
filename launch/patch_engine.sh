#!/usr/bin/env bash
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SHIMS="$REPO_ROOT/anvil/patch"
OUT_DIR="$REPO_ROOT/anvil/patch/bin"
GAME_BIN="$REPO_ROOT/game/bin/linuxsteamrt64"
VIDEO_TXT="$REPO_ROOT/game/core/cfg/video.txt"

mkdir -p "$OUT_DIR"

for src in "$SHIMS"/*.c; do
    [ -f "$src" ] || continue
    name="$(basename "$src" .c)"
    echo "Building $name.so..."
    gcc -shared -fPIC -O2 -o "$OUT_DIR/$name.so" "$src" -ldl \
        -L"$GAME_BIN" -Wl,--no-as-needed -Wl,-rpath,"$GAME_BIN"
done

if [ -f "$VIDEO_TXT" ]; then
    echo "Resetting video.txt windowing flags..."
    sed -i 's/\("setting\.coop_fullscreen"[ \t]*\)"[^"]*"/\1"0"/' "$VIDEO_TXT"
    sed -i 's/\("setting\.nowindowborder"[ \t]*\)"[^"]*"/\1"0"/' "$VIDEO_TXT"
fi

echo "Done."

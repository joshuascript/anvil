#!/usr/bin/env bash
set -e

ANVIL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$ANVIL_DIR/.." && pwd)"
SHIMS="$ANVIL_DIR/patches"
OUT_DIR="$ANVIL_DIR/patches/bin"
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

ENGINE_JSON="$REPO_ROOT/game/config/convar/engine.json"

if [ -f "$ENGINE_JSON" ]; then
    echo "WARNING: bloom is currently broken by default on Linux. Disabling r_bloom..."
    python3 - "$ENGINE_JSON" <<'EOF'
import json, sys, time
path = sys.argv[1]
with open(path) as f:
    data = json.load(f)
key = "convar.r_bloom"
if key in data:
    data[key]["Value"] = "False"
else:
    data[key] = {"Value": "False", "Timeout": int(time.time()) + 86400 * 30, "DeleteAt": 0}
with open(path, "w") as f:
    json.dump(data, f, indent=2)
EOF
fi

echo "Done."

#!/usr/bin/env python3
"""
preload.py

Entry point for all launch scripts. Does two things in order:

  1. Patches — loads anvil/patches/bin/*.so into LD_PRELOAD and sets
     LD_LIBRARY_PATH / SBOX_TRACE_DIR for the sbox session.

  2. Preload scripts — discovers every other *.py file in this directory
     (alphabetically) and calls its setup() function to mutate the environment.

Then execs the game command passed as argv.

Usage:
    python3 preload.py <command> [args...]

Env vars:
    ANVIL_SKIP_PATCHES  Comma-separated name fragments to exclude from
                        LD_PRELOAD (e.g. "finalizeload" to skip
                        libsbox_finalizeload_patch.so).
"""

import glob
import importlib.util
import os
import subprocess
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ANVIL_ROOT = os.path.normpath(os.path.join(_SCRIPT_DIR, "..", ".."))
_REPO_ROOT  = os.path.normpath(os.path.join(_SCRIPT_DIR, "..", "..", ".."))
_PATCH_BIN  = os.path.join(_ANVIL_ROOT, "patches", "bin")
_GAME_BIN   = os.path.join(_REPO_ROOT, "game", "bin", "linuxsteamrt64")
_TRACE_DIR  = os.path.join(_ANVIL_ROOT, "debug", "logs")


# --- Patches ------------------------------------------------------------------

def _load_patches() -> None:
    skip = [s.strip() for s in os.environ.get("ANVIL_SKIP_PATCHES", "").split(",") if s.strip()]

    if os.path.isdir(_PATCH_BIN):
        for so in sorted(glob.glob(os.path.join(_PATCH_BIN, "*.so"))):
            name = os.path.basename(so)
            if any(fragment in name for fragment in skip):
                print(f"[preload] Skipped {name} (ANVIL_SKIP_PATCHES)", file=sys.stderr)
                continue
            existing = os.environ.get("LD_PRELOAD", "")
            os.environ["LD_PRELOAD"] = f"{so}:{existing}" if existing else so
            print(f"[preload] Loaded patch {name}", file=sys.stderr)

    existing_lib = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = f"{_GAME_BIN}:{existing_lib}" if existing_lib else _GAME_BIN

    if not os.environ.get("SBOX_TRACE_DIR"):
        os.environ["SBOX_TRACE_DIR"] = _TRACE_DIR
    os.makedirs(os.environ["SBOX_TRACE_DIR"], exist_ok=True)


# --- Preload scripts ----------------------------------------------------------

def _run_preload_scripts() -> None:
    self_name = os.path.basename(__file__)
    scripts = sorted(
        f for f in os.listdir(_SCRIPT_DIR)
        if f.endswith(".py") and f != self_name and not f.startswith("_")
    )
    for filename in scripts:
        path = os.path.join(_SCRIPT_DIR, filename)
        spec = importlib.util.spec_from_file_location(filename[:-3], path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if hasattr(mod, "setup"):
            mod.setup()


# ------------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <command> [args...]", file=sys.stderr)
        sys.exit(1)

    _load_patches()
    _run_preload_scripts()

    proc = subprocess.run(sys.argv[1:])
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()

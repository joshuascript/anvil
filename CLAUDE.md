# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Anvil is a patch toolkit that makes s&box run natively on Linux without Proton. It provides:
- **LD_PRELOAD patches** — compiled C shims that fix ABI mismatches and engine crashes at runtime
- **Launch scripts** — always use these instead of the `game/sbox` binary directly
- **Debug utilities** — GDB scripts and ELF binary analysis tools for crash investigation

The primary native target is `game/bin/linuxsteamrt64/libengine2.so`.

## Key Commands

```bash
# Compile all patches (run after any change to patches/*.c)
bash anvil/launch/patch_engine.sh

# Launch the game with all patches active
bash anvil/launch/sbox/launch-sbox.sh

# Launch under GDB for crash capture
bash anvil/launch/sbox/launch-sbox-gdb.sh

# Analyze a crash session
python3 anvil/debug/scripts/gdb/parse_crashes.py debug/logs/<YYYYMMDD_HHMMSS>/
# writes debug/logs/<session>/analysis.md
```

All paths above are relative to the repo root (`sbox-public/`).

## Before Investigating Any Crash

**Always read `llmcontext/` first.** Prior analysis may already identify the root cause, fix, and verification result. Check:
- `llmcontext/<short_name>.md` — crash root causes and fixes
- `llmcontext/assertions/<name>.md` — native assertion dialogs
- `llmcontext/libsbox_<name>_patch.md` — patch rationale and offset history

Do not mark a fix as confirmed or write a Verification section in llmcontext until the user has explicitly confirmed the fix worked (e.g. "that fixed it", "no more crashes"). A clean GDB session is not confirmation.

## Architecture

### Patches (`patches/`)

Each `.c` file compiles to `patches/bin/<name>.so` and is auto-loaded by every launch script via `LD_PRELOAD`. Each patch hooks `dlopen` to fire when the target `.so` loads, then prints `[<name>_patch] installed — ...` to stderr on success, or a mismatch warning if the binary has changed.

All patches use dynamic scanning — no hardcoded offsets remain. Each patch self-locates its target at runtime and prints a mismatch warning to stderr if the binary has changed in a way that breaks the pattern.

| Patch | Strategy | Test script |
|-------|----------|-------------|
| `libsbox_finalizeload_patch.c` | Pattern scan in `.text` | `test_finalizeload_patch.py` |
| `libsbox_lightmapuv_patch.c` | Data-anchored table scan | `test_lightmapuv_patch.py` |
| `libsbox_htmlcb_patch.c` | Pattern scan in `.text` | `test_htmlcb_patch.py` |
| `libsbox_casemap.c` | libc syscall interposition | *(no binary offsets — no test needed)* |

#### Checking for stale patches after an engine update

Run all test scripts before launching:

```bash
python3 anvil/debug/scripts/binaryscan/test_finalizeload_patch.py
python3 anvil/debug/scripts/binaryscan/test_lightmapuv_patch.py
python3 anvil/debug/scripts/binaryscan/test_htmlcb_patch.py
```

Each script exits 0 (`PASS  all checks`) if the patch will apply cleanly, or exits 1 with a `FAIL` line describing what broke. A failing script means the corresponding C patch needs its pattern updated before the next launch. Use `pattern_scan.py` and `cross_ref.py` to locate the new pattern context around the affected instruction.

The offset history and patch rationale for each patch live in the corresponding `llmcontext/libsbox_<name>_patch.md`.

### Launch Scripts (`launch/sbox/`)

All scripts exec `launch/preload/preload.py`, which loads `patches/bin/*.so` into `LD_PRELOAD`, sets `LD_LIBRARY_PATH` and `SBOX_TRACE_DIR`, then runs every `*.py` file in `launch/preload/` alphabetically by calling its `setup()` function. To add a new preload step, drop a `.py` file with a `setup()` there.

The finalizeload backtrace script (`launch-sbox-finalizeload-bt.sh`) sets `ANVIL_SKIP_PATCHES=finalizeload` before calling `preload.py`, which causes it to omit `libsbox_finalizeload_patch.so` so the assertion `jge` is reachable and the GDB breakpoint fires.

### GDB Scripts (`debug/scripts/gdb/`)

Source inside GDB, not executed directly:
```
(gdb) source anvil/debug/scripts/gdb/gdb-auto-bt.py
(gdb) run
```

`gdb-auto-bt.py` — automated SIGSEGV capture; writes `debug/logs/<session>/crash_NNN.txt` and resumes automatically. CLR/JIT crashes are forwarded to the CLR so managed exceptions are logged to `debug/logs/nullrefs_<timestamp>.log`.

### Binary Scan Scripts (`debug/scripts/binaryscan/`)

Operate on ELF files on disk with no running process. Primary tool for finding and verifying patch offsets:

```bash
# Wildcard byte-pattern search
python3 anvil/debug/scripts/binaryscan/pattern_scan.py <binary> "<pattern>" [--resolve] [--context N]

# Find cross-references to a file offset
python3 anvil/debug/scripts/binaryscan/cross_ref.py <binary> <offset> [--type call]

# Find function prologues (endbr64)
python3 anvil/debug/scripts/binaryscan/find_funcs.py <binary> [--near 0xOFFSET]
```

### llmcontext Files

Each file covers one issue: **Problem**, **Root Cause**, **Fix** (with code snippets), **Verification**. One issue per file — create a new file when the root cause, crash site, or subsystem differs, even with similar symptoms. Name files after the root cause, not the symptom.

Update llmcontext whenever: a new root cause is identified, a fix is applied, a patch offset goes stale, a fix regresses, or a new assertion is observed.

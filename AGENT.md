# anvil — Tool Reference

All paths below are relative to `anvil/`.

---

## Directory Overview

| Directory | Purpose |
|-----------|---------|
| `debug/scripts/gdb/` | GDB Python scripts — source inside a running GDB session |
| `debug/scripts/binaryscan/` | Standalone ELF analysis tools — no running process needed |
| `debug/logs/` | GDB trace output and CLR nullref logs |
| `patches/` | LD_PRELOAD runtime patches for `libengine2.so` and other natives |
| `patches/bin/` | Compiled `.so` patch binaries (auto-loaded by launch scripts) |
| `launch/sbox/` | Launch scripts for sbox and GDB sessions |
| `launch/preload/` | Python preload scripts — run before the game to configure system state |
| `llmcontext/` | Markdown write-ups of past bugs, crashes, and patch rationale |

---

## LLM Context (`llmcontext/`)

Markdown write-ups of past bugs, crashes, patches, and assertions. **Always
read relevant files here before investigating a known crash type** — prior
analysis may already identify the root cause and fix.

### When to update

Update or create a `llmcontext/` file whenever:

- A new root cause is identified (even if not yet fixed)
- A fix is applied — record what was changed and where
- A patch offset goes stale and is corrected
- A previously confirmed fix regresses — add a note with the new session and what was missed
- An assertion is newly observed — add it to `llmcontext/assertions/`

**Write the Verification section and mark a fix as solved only after the user
has verbally confirmed the fix worked** (e.g. "that fixed it", "no more
crashes", "confirmed"). Do not mark a fix confirmed based on a clean GDB session
alone — always wait for explicit user confirmation.

After applying any fix, always ask the user: "Did that fix it?" (or equivalent)
before updating llmcontext with a confirmed solution.

### File conventions

| Content | Location |
|---------|----------|
| Crash root cause + fix | `llmcontext/<short_name>.md` |
| Native patch rationale | `llmcontext/libsbox_<name>_patch.md` |
| Assertion dialogs | `llmcontext/assertions/<assertion_name>.md` |

Each file should cover: **Problem**, **Root Cause**, **Fix** (with code
snippets), and **Verification** (which GDB session confirmed it).

### One issue per file

Each distinct issue gets its own file. When a fix is found:

- **Check existing files first.** If the issue is a direct extension of a known
  bug (same root cause, same crash site, missed code path), add it to that
  file's Fix section.
- **Create a new file** if the issue has a different root cause, crash site, or
  subsystem — even if it produces similar symptoms. Name it after the root cause,
  not the symptom (e.g. `citizen_animation_nan.md`, not `animation_crash2.md`).

When in doubt, create a new file. Splitting is easier than untangling a file
that covers two unrelated bugs.

---

## Launch Scripts

All launch scripts live in `launch/sbox/`. Each one computes `REPO_ROOT` and
execs `launch/preload/preload.py`, which handles patch loading and all preload
setup before starting the game.

### `launch/sbox/launch-sbox.sh`

Standard launch. Execs `game/sbox` via `preload.py`.

### `launch/sbox/launch-sbox-gdb.sh`

Same as above but launches under GDB with `gdb-auto-bt.py` active.

### `launch/sbox/launch-sbox-server.sh`

Dedicated server variant (`game/sbox-server`).

### `launch/sbox/launch-sbox-finalizeload-bt.sh`

Runs sbox under GDB with `gdb-finalizeload-bt.py` and `gdb-auto-bt.py` both
active. Sets `ANVIL_SKIP_PATCHES=finalizeload` so `preload.py` omits
`libsbox_finalizeload_patch.so`, leaving the `jge` assertion intact for the
breakpoint to fire.

### `launch/sbox/launch-sbox-capture-steam-callbacks.sh`

Runs with `capture_steam_callback.py` active for Steam IPC reverse-engineering.

### `launch/sbox/launch-sbox-probe-wrapper.sh`

Runs sbox under GDB with `probe_wrapper_layout.py` for ISteamHTMLSurface
wrapper object layout inspection.

---

## Preload Scripts

All launch scripts exec `launch/preload/preload.py`, which does two things:

1. **Patch loading** — loads `patches/bin/*.so` into `LD_PRELOAD`, sets
   `LD_LIBRARY_PATH` and `SBOX_TRACE_DIR`. Respects `ANVIL_SKIP_PATCHES`
   (comma-separated name fragments to exclude).

2. **Script discovery** — imports every other `*.py` file in `launch/preload/`
   alphabetically and calls its `setup()` function.

To add a new preload step, drop a `.py` file in `launch/preload/` with a
`setup()` function. No launch script changes needed.

### `launch/preload/gameoverlay.py`

Finds `gameoverlayrenderer.so` across common Steam install locations (native,
Flatpak, package-manager) and prepends it to `LD_PRELOAD`. Warns but continues
if the file is not found.

### `launch/preload/inotify.py`

Temporarily raises `fs.inotify.max_user_watches` (written to
`/proc/sys/fs/inotify/max_user_watches`) for the duration of the session,
then restores the original value on exit.

**Why this is needed:** `libengine2.so` and `libtier0.so` watch game content
directories via inotify. On Debian the default limit (8192) is exhausted by a
full sbox session, causing the engine to fall back to polling via `readdir()`.
That polling loop races with internal hash table lookups on .NET thread pool
threads, producing a SIGSEGV in `libengine2.so`.

On launch the script prints the current and required values, explains the risk,
and asks `[y/N]`. Declining skips the change and continues to launch with a
warning. The original value is always restored on exit via `atexit` and signal
handlers (`SIGTERM`, `SIGINT`, `SIGHUP`).

---

## Patches

Built with:

```bash
bash anvil/launch/patch_engine.sh   # compiles all *.c in patches/ → patches/bin/*.so
```

All patches are auto-loaded via `LD_PRELOAD` by every launch script. Each patch
hooks `dlopen` so it can fire at exactly the right moment after the target `.so`
loads.

Each patch prints a confirmation line to stderr on startup:

```
[<name>_patch] installed — ...
```

If verification fails (binary version mismatch after an engine update):

```
[<name>_patch] unexpected bytes at ... — patch skipped (binary version mismatch?)
```

---

### `patches/libsbox_finalizeload_patch.c`

Suppresses the spurious `Assert( pLoadingResource->GetExtRefDepth() > GetExtRefDepth() )`
in `resourcesystem/loadingresource.cpp:1194`. Fires when a resource dependency
fails to load (file not found) and its `ExtRefDepth` is left uninitialised.

**Note:** this assertion shows as a native dialog (`Assertion Failed / Always Ignore`),
not a SIGSEGV — it does **not** appear in GDB crash logs.

Uses a **dynamic pattern scan** of the executable PT_LOAD segment — no hardcoded
offset to maintain. The found offset is printed to stderr on each launch for
reference. See the offset history table in the patch source.

---

### `patches/libsbox_lightmapuv_patch.c`

Adds `LightmapUV` to the `SemanticNameToUsage()` lookup table in
`librendersystemvulkan.so`. Without this, lightmapped mesh rendering hits
`Unknown semantic name 'LightmapUV'`, produces an invalid `VkVertexInputAttributeDescription`,
and crashes inside `libnvidia-glcore.so`.

Uses **hardcoded offsets** (loop bound byte + table slot). Verify after engine
updates — see `llmcontext/libsbox_lightmapuv_patch.md` for the offset history
and verification command.

---

### `patches/libsbox_htmlcb_patch.c`

Fixes an ABI mismatch in `libengine2.so`'s Steam HTML surface callback. On Linux,
`steamclient.so` passes the browser handle as an integer in RSI; the engine
expects a pointer and dereferences it → SIGSEGV.

Uses a **hardcoded offset**. Verify after engine updates — see
`llmcontext/libsbox_htmlcb_patch.md` for the offset history and verification
command.

---

### `patches/libsbox_casemap.c`

LD_PRELOAD shim that resolves wrong-cased file paths for s&box on Linux.
The engine was written for Windows (case-insensitive NTFS); on Linux, paths
like `addons/base/assets` fail when the real path is `addons/base/Assets`.

Intercepts `fstatat`, `openat`, `inotify_add_watch`, `open`, `open64`,
`fopen`, `fopen64`, `freopen64`, `stat`, `stat64`, `lstat64`, and `access`.
For paths under the game directory it walks each path segment, scans the
parent directory once via `opendir`/`readdir`, caches a `lowercase→real`
mapping, and resolves case-insensitively. A negative cache prevents
re-walking confirmed misses.

**Overlap note:** `libtier0.so` now exports `FioFindFileInDirCaseInsensitive`
which covers the same problem for tier0-routed calls. `libengine2.so` does
**not** use that function — it calls `opendir`/`readdir`/`scandir` directly
from glibc — so the shim still provides full coverage for engine file IO.

---

## GDB Scripts

These scripts must be **sourced inside GDB**, not run directly:

```
(gdb) source anvil/debug/scripts/gdb/<script>.py
(gdb) run
```

---

### `debug/scripts/gdb/gdb-auto-bt.py`

Automated SIGSEGV capture. Installs a stop handler that fires on every
SIGSEGV, classifies the crash as either native (PC in a named `.so`) or
JIT/CLR (PC in an anonymous mmap), writes a full backtrace to a timestamped
session directory, then resumes automatically.

- **CLR/JIT crashes** (NullReferenceException): signal is forwarded back so
  the CLR's own handler can convert it to a managed exception.
- **Native crashes**: execution is continued past the fault.

Output lands in `debug/logs/<YYYYMMDD_HHMMSS>/crash_NNN.txt`. Override with
`SBOX_TRACE_DIR=<path>`.

Each file contains:
- `thread apply all bt`
- `info registers`
- `x/16i $pc-24`
- `info proc mappings`

---

### `debug/scripts/gdb/capture_steam_callback.py`

Intercepts `Steamworks_Dispatch_OnClientCallback` and dumps the raw bytes of
each Steam callback struct as it arrives. Useful for reverse-engineering Steam
IPC struct layouts (e.g. HTML surface callbacks with 32-bit Linux string
pointers).

Output goes to `debug/logs/callbacks_<timestamp>/captures.txt`. Configure
`TARGET_TYPES` at the top to restrict which callback type IDs are intercepted.
`MAX_CAPTURES` (default 40) prevents log flooding.

Re-source after the engine starts if the symbol isn't visible at load time.

---

### `debug/scripts/gdb/gdb-finalizeload-bt.py`

Traces every hit of the `FinalizeLoadRequest()` depth-ordering assertion in
`libengine2.so` without stopping execution. Uses the same dynamic pattern scan
as `libsbox_finalizeload_patch` to locate the `jge` at runtime, then installs
a silent breakpoint there.

On each hit records:
- `pLoadingResource->ExtRefDepth` (`$eax` at the jge)
- `this->ExtRefDepth` (`*($rbx + 0x6c)`)
- Whether the assertion would pass or fail
- Full thread backtrace and registers

Output goes to `debug/logs/finalizeload_<timestamp>/captures.txt`. Cap is
`MAX_CAPTURES = 40`. Install alongside `gdb-auto-bt.py` to correlate assertion
hits with any subsequent SIGSEGV crashes.

**Note:** run with `libsbox_finalizeload_patch` **disabled** (remove it from
`LD_PRELOAD`) so the assertion path is actually reachable; otherwise the jge is
NOPed out and the breakpoint never fires.

---

### `debug/scripts/gdb/probe_wrapper_layout.py`

One-shot layout probe for the `ISteamHTMLSurface` wrapper object in
`libengine2.so`. Waits for `libengine2.so` to load, then breaks at a hardcoded
offset (`FUNC_OFFSET = 0x34d160`) and prints RDI/RSI/RDX/RCX, 8 wrapper object
fields at RDI with ELF VMA annotations, and vtable slots `[0..7]`. Stops after
`MAX_HITS` (default 2).

---

### `debug/scripts/gdb/parse_crashes.py`

Aggregates a session directory of `crash_NNN.txt` files into a single
LLM-readable markdown analysis file.

```bash
python3 anvil/debug/scripts/gdb/parse_crashes.py debug/logs/<session>/
python3 anvil/debug/scripts/gdb/parse_crashes.py debug/logs/<session>/ --show-unresolved
```

Output includes a unique named-call table, per-crash summaries with registers,
deduplicated thread call chains, and disassembly/mapping appendices. Writes to
`<session>/analysis.md` if no output path is given.

---

## Binary Scan Scripts

All scripts in `debug/scripts/binaryscan/` operate on ELF binaries on disk —
no running process needed. Typical target: `game/bin/linuxsteamrt64/libengine2.so`.

---

### `debug/scripts/binaryscan/pattern_scan.py`

Wildcard byte-pattern search across an ELF binary's raw bytes.

```bash
python3 anvil/debug/scripts/binaryscan/pattern_scan.py <binary> "<pattern>" [options]
# Wildcards: ?? (full), D? (low nibble), ?0 (high nibble)
# Options: --start 0xOFFSET, --end 0xOFFSET, --limit N, --resolve, --context N
```

---

### `debug/scripts/binaryscan/vtable_dump.py`

Dumps C++ vtable slot contents from a PIE ELF binary using `R_X86_64_RELATIVE`
relocations.

```bash
python3 anvil/debug/scripts/binaryscan/vtable_dump.py <binary> <file_offset> [--resolve] [--count N]
python3 anvil/debug/scripts/binaryscan/vtable_dump.py <binary> --list [--min-slots N] [--resolve]
```

---

### `debug/scripts/binaryscan/slot_diff.py`

Compares `nativeInit` function-pointer slot tables between two ELF builds.

```bash
python3 anvil/debug/scripts/binaryscan/slot_diff.py <binary_a> <binary_b> [--slots 2315-2340] [--all] [--tsv]
```

---

### `debug/scripts/binaryscan/find_funcs.py`

Finds function start addresses by scanning for `endbr64` (`F3 0F 1E FA`) prologues.

```bash
python3 anvil/debug/scripts/binaryscan/find_funcs.py <binary> [--resolve] [--near 0xOFFSET] [--window N]
```

---

### `debug/scripts/binaryscan/cross_ref.py`

Finds all RIP-relative instructions (`call`, `jmp`, `lea`, `mov`, `movq`) that
reference a given file offset.

```bash
python3 anvil/debug/scripts/binaryscan/cross_ref.py <binary> <offset> [--resolve] [--type call]
```

---

### `debug/scripts/binaryscan/decode_nativeinit.py`

Decodes SSE2-optimised function-pointer table fills. Reconstructs `slot → func_offset`
mappings from `PUNPCKLQDQ/MOVUPS` pairs.

```bash
python3 anvil/debug/scripts/binaryscan/decode_nativeinit.py <binary> [--slots 2315-2340] [--base-reg rdi]
```

---

### `debug/scripts/binaryscan/decode_thunks.py`

Decodes C++ vtable-dispatch thunks to extract the vtable byte offset each thunk
calls into.

```bash
python3 anvil/debug/scripts/binaryscan/decode_thunks.py <binary> <offset> [<offset> ...]
# Read offsets from stdin:
python3 ... decode_nativeinit.py <binary> | awk '{print $2}' | python3 ... decode_thunks.py <binary> --stdin
```

---

### `debug/scripts/binaryscan/elf_relocs.py`

Queries ELF RELA relocations by address, range, type, or target.

```bash
python3 anvil/debug/scripts/binaryscan/elf_relocs.py <binary> --at <offset>
python3 anvil/debug/scripts/binaryscan/elf_relocs.py <binary> --range <start>-<end>
python3 anvil/debug/scripts/binaryscan/elf_relocs.py <binary> --target <func_offset> [--resolve]
python3 anvil/debug/scripts/binaryscan/elf_relocs.py <binary> --type 8 [--section .rela.dyn]
```

---

## Log Directory (`debug/logs/`)

Not cleaned automatically — sessions accumulate and can be diffed across runs.

### Crash sessions — `debug/logs/<YYYYMMDD_HHMMSS>/`

One directory per GDB run, created by `gdb-auto-bt.py`. Each SIGSEGV generates
a numbered file. Analyze with:

```bash
python3 anvil/debug/scripts/gdb/parse_crashes.py debug/logs/<session>/
# writes debug/logs/<session>/analysis.md
```

### Steam callback captures — `debug/logs/callbacks_<YYYYMMDD_HHMMSS>/`

Written by `capture_steam_callback.py`. Contains `captures.txt` with hex dumps
and field offset tables for each intercepted Steam callback struct.

### CLR NullRef logs — `debug/logs/nullrefs_<YYYYMMDD_HHMMSS>.log`

Managed stack traces written by external tooling (not the GDB scripts). Each
entry is a timestamped `NullReferenceException` call chain. Corresponds to
`jit/clr` crashes captured by `gdb-auto-bt.py` — the CLR converts the forwarded
SIGSEGV into a managed exception and logs the managed stack here.

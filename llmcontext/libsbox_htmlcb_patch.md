# ISteamHTMLSurface Callback ABI Patch (libsbox_htmlcb_patch)

## Problem

`libengine2.so` contains a C++ callback registered with `steamclient.so` for HTML surface
events. On Linux, `steamclient.so` invokes this callback with the browser handle integer
in RSI (e.g. 2, 8), but the function expects RSI to be a nullable pointer to navigation
data. The function checks `test rsi, rsi; je <skip>` — the non-zero handle passes the null
check — then dereferences `mov (%rsi),%rsi`, treating the integer as a pointer → SIGSEGV.

## Crash signature

```
#0  ?? () from libengine2.so     ← native crash
rsi = 0x2                        ← browser handle integer, not a pointer
```

## Fix

`anvil/patches/libsbox_htmlcb_patch.c` — LD_PRELOAD shim that interposes `dlopen`. When
`libengine2.so` is loaded, scans the executable PT_LOAD segment for a unique 8-byte pattern
and replaces the 3-byte `mov (%rsi),%rsi` (`48 8b 36`) with `xor rsi,rsi` (`48 31 f6`).
This zeroes RSI at that point, equivalent to the null branch the function already handles.

## Dynamic scan strategy

The patch locates the crash instruction at runtime by scanning the executable PT_LOAD
segment of `libengine2.so` for this 8-byte pattern:

```
74 03           je +3           (skip 3 bytes — length of mov (%rsi),%rsi)
48 8b 36        mov (%rsi),%rsi  ← crash instruction at pattern+2
4c 8d 2d        lea r13,[rip+…] (ISteamHTMLSurface global — immediately follows)
```

The `74 03` displacement is fixed because it skips exactly the 3 bytes of `48 8b 36`.
The `4c 8d 2d` (lea r13 RIP-relative) is characteristic of this specific callback.
The 8-byte combination is unique in `libengine2.so` (1 hit verified 2026-05-20).

The patch verifies the expected bytes (`48 8b 36`) before writing. If verification fails
(binary version mismatch), it prints a warning to stderr and skips — the game still runs
but the crash reappears.

## Why dlopen hook, not constructor

sbox does not link `libengine2.so` as a direct ELF dependency — it `dlopen`s it at runtime.
A plain `__attribute__((constructor))` fires before `libengine2.so` exists in the process,
so `dl_iterate_phdr` finds nothing. The `dlopen` interpose fires at exactly the right time.

## Validation

Run after any engine update to verify the patch will work:

```bash
python3 anvil/debug/scripts/binaryscan/test_htmlcb_patch.py
```

Expected output: `PASS  all checks`. If the pattern is not found, `libengine2.so` has
changed significantly. Use `pattern_scan.py` to locate the new pattern context around the
crash instruction (look near any `test rsi,rsi` followed by `je +3; mov (%rsi),%rsi`).

## Offset history (for reference — no longer needed at runtime)

The crash instruction moved when `libengine2.so` was updated (+0x20 due to `endbr64` +
stack canary prologue added to the function). The dynamic scan handles future moves
automatically without requiring an offset update.

| Date       | Pattern addr | Crash insn | Notes |
|------------|-------------|------------|-------|
| 2026-05-18 | `0x34d184`  | `0x34d186` | Original |
| 2026-05-19 | `0x34d1a4`  | `0x34d1a6` | After update; `endbr64` + stack canary added (+0x20) |

## Build and deployment

```bash
bash anvil/launch/patch_engine.sh   # builds all .so files in anvil/patches/bin/
```

Auto-loaded by all launch scripts via `LD_PRELOAD` from `anvil/patches/bin/`.

Successful patch prints to stderr:
```
[htmlcb_patch] installed — pattern@0x34d1a4  insn@0x<runtime_addr>  (mov rsi,[rsi] → xor rsi,rsi)
```

If the pattern is not found:
```
[htmlcb_patch] pattern not found — patch not installed
```

If the bytes at the found location are unexpected:
```
[htmlcb_patch] unexpected bytes at pattern+2: XX XX XX — binary version mismatch?
```

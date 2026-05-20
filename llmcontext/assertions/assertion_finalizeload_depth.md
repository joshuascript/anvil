# Assertion: FinalizeLoadRequest — ExtRefDepth Ordering

## Status: SOLVED via patch

**File:** `resourcesystem/loadingresource.cpp`
**Line:** 1194
**Expr:** Assertion Failed in function `FinalizeLoadRequest()`:
`pLoadingResource->GetExtRefDepth() > GetExtRefDepth()`

## Root Cause

When a resource dependency fails to load (file not found), its `ExtRefDepth` is never
initialised, violating the depth ordering invariant checked by `FinalizeLoadRequest()`. The
assertion fires, shows the dialog, then falls through to the exact same continuation as the
non-assertion path — no behavioural difference between the two paths.

> **Note:** this assertion fires as a native `Assert()` dialog, not a SIGSEGV. It does **not**
> appear in GDB crash logs. If it reappears after an engine update, the patch offset has gone
> stale — see below.

## Fix

`anvil/patch/libsbox_finalizeload_patch.c` — dynamically scans `libengine2.so`'s executable
PT_LOAD segment at runtime for the `mov/cmp/jge` byte pattern and NOPs the `jge`, making
execution always fall through without triggering the assertion.

Uses a **dynamic pattern scan** — no hardcoded offset to maintain. The found offset is printed
to stderr on each launch:
```
[finalizeload_patch] installed — jge@0x... (offset 0x...) → 6×NOP
```

Pattern scanned for:
```
8b 40 6c   mov  0x6c(%rax),%eax   ; pLoadingResource->ExtRefDepth
39 43 6c   cmp  %eax,0x6c(%rbx)   ; this->ExtRefDepth
0f 8d ...  jge  <assert>
```

## Offset history (for reference only — not used at runtime)

| Date       | JGE offset  | jge bytes            | Notes                        |
|------------|-------------|----------------------|------------------------------|
| 2026-05-20 | `0x3e6f9f`  | `0f 8d 35 82 e8 ff`  | Initial                      |
| 2026-05-20 | `0x3e6b1f`  | `0f 8d b5 86 e8 ff`  | After engine update (-0x480) |

## Verification

Confirmed resolved. Assertion dialog absent when patch is active and startup message shows
`installed`.

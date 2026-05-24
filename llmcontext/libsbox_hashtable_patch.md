# libsbox_hashtable_patch — hash table bucket crash (.NET TP Worker)

## Problem

SIGSEGV on a `.NET TP Worker` thread during gameplay (content loading phase).
Identical PC and register values across all observed sessions.

- **Crash PC:** `libengine2.so+0x15bc0c0` (`mov rbx,[r13]`)
- **r12 at crash:** `0x672244bb` — same hash key in every crash, across different users
- **r13 at crash:** computed slot address, unmapped at time of access
- **Faulting thread:** `.NET TP Worker`

Affected users: ligmoidprime (two sessions, same crash site)

## Root Cause

The engine maintains an incremental-rehashing hash table in libengine2.so. The
lookup function reads the bucket index using the capacity (`rcx`) from one
moment in time, then reloads the bucket array pointer (`rax`) from the struct
field `[rbx]` immediately after. A concurrent resize between those two loads
can leave the computed slot `rax + rdx*8` pointing past the end of (or into
freed pages of) the newly-allocated array:

```asm
mov    ecx, [rbx+0x1c]     ; capacity (old or second table)
; ... hash computation ...
div    rcx                  ; rdx = hash % capacity   <- capacity snapshot
mov    rax, [rbx]           ; reload array ptr        <- may be new, smaller array
mov    r15, [rbx+0x8]       ; reload [rbx+8]
movslq edx, edx
lea    r13, [rax+rdx*8]    ; slot = new_array + old_index * 8
mov    rbx, [r13]           ; CRASH — slot is unmapped
test   rbx, rbx
je     <empty_bucket>
```

The struct at `rbx` holds two capacities:
- `[rbx+0x1c]` — old / second-table capacity (used in the second div path)
- `[rbx+0x24]` — current / first-table capacity (used in the first div path)

A `jae` branch at `rbx+0x24` vs a threshold selects which capacity is used.
The race fires when the table shrinks between the div and the array-pointer
reload — the index `rdx` is valid for the old capacity but out of range for
the new smaller array.

The hash key `0x672244bb` appears identically across all crashes, suggesting
a specific engine lookup (likely a registered type or UGC package) consistently
races with a resize triggered by content loading.

## Fix

`patches/libsbox_hashtable_patch.c` — trampoline patch on `libengine2.so`.

Replaces the 5-byte patch site (`lea r13` + first byte of `mov rbx,[r13]`)
with a `jmp rel32` to a trampoline. The trampoline guards the slot dereference
with two bounds checks:

```asm
; trampoline (28 bytes, mmap'd near patch site)
cmp    edx, [rbx+0x1c]       ; within old/second capacity?
jb     .proceed
cmp    edx, [rbx+0x24]       ; within current/first capacity?
jb     .proceed
jmp    safe_exit              ; out of bounds for both -> empty bucket path
.proceed:
lea    r13, [rax+rdx*8]      ; original instruction
mov    rbx, [r13]            ; original crash instruction
jmp    resume                 ; -> test rbx,rbx
```

If `edx` (bucket index) exceeds both stored capacities the lookup exits via
the existing null-bucket path, returning no match. This is conservative: it
can produce a false-negative during an active resize if the index is
legitimately valid in one table but not the other, but that is safe — a missed
lookup is preferable to a crash.

**Pattern** (wildcard at je displacement):

```
8b 4b 1c                 mov ecx,[rbx+0x1c]
4c 89 e0                 mov rax,r12
31 d2                    xor edx,edx
48 f7 f1                 div rcx
48 8b 03                 mov rax,[rbx]
4c 8b 7b 08              mov r15,[rbx+0x8]
48 63 d2                 movslq edx,edx
4c 8d 2c d0              lea r13,[rax+rdx*8]   <- patch site (5 bytes)
49 8b 5d 00              mov rbx,[r13]
48 85 db                 test rbx,rbx
74 ??                    je <safe_exit>
```

Confirmed unique (1 hit) in libengine2.so at file offset `0x15bc0a7`.

## Offset History

| Date       | Pattern addr | Patch site  | Notes   |
|------------|-------------|-------------|---------|
| 2026-05-21 | `0x15bc0a7` | `0x15bc0bc` | Initial |
| 2026-05-21 | `0x15c29a7` | `0x15c29bc` | Engine update |

## Trigger Identified

Managed logs from ligmoidprime confirm the crash is triggered by `PreJITAsync`
in `Sandbox.ReflectionUtility` (`engine/Sandbox.Reflection/Utility.cs:249`).

`PreJITAsync` pre-JIT-compiles all engine methods on `.NET TP Workers` to avoid
hitches during gameplay. When it hits a method that calls into libengine2.so, it
executes that code path — including the hash table lookup for `0x672244bb` — while
the main thread is still in the middle of its startup registration pass. That is
the race.

Timeline from managed log:
```
04:50:39.748  [Generic] Took 2.4643008s to get steam auth ticket
04:50:40.145  SIGSEGV on .NET TP Worker  (PreJITAsync racing hash table init)
```

A `libgdi32.dll` DllNotFoundException also appears in the log (GDI P/Invoke not
present on Linux) but is unrelated to the crash — it is caught and non-fatal.

**Upstream fix**: `PreJITAsync` should not start until after the engine completes
its initialization pass. Starting it before the hash table is fully populated
exposes any TP Worker that touches a libengine2.so code path to this race.

## Verification

**Patch insufficient — backed up, not active.**

Second session from ligmoidprime (2026-05-22, session `8s7mw8k`) confirmed both
`.so` patches were loaded. The crash still occurred on a `.NET TP Worker`.

Key differences from original crash:
- **Crash PC:** `0x7ff7ccbc2013` — inside the mmap'd trampoline (not in
  libengine2.so directly). The `jmp 0x7ff7d0bc29c4` at PC+4 returns to
  libengine2.so, confirming the trampoline was reached.
- **Hash key (r12):** `0x64dc8d0b` — different from the original `0x672244bb`,
  meaning a different lookup hit the same race.
- **Faulting instruction:** `mov rbx,[r13]` — same crash instruction, now
  executing inside the trampoline's `.proceed` path.
- **r13 at crash:** `0x7ff7200424a8` — unmapped.

The bounds checks (`cmp edx,[rbx+0x1c]` / `cmp edx,[rbx+0x24]`) both passed,
allowing `.proceed` to execute `lea r13,[rax+rdx*8]` and then crash on
`mov rbx,[r13]`. The index `edx` was within at least one stored capacity, but
`rax` (the array pointer) had already been replaced by a resize — so
`rax + edx*8` pointed past the end of the newly-allocated array.

The bounds check approach guards against out-of-range indices but cannot
protect against the array pointer itself being stale. The race window between
the capacity snapshot and the array-pointer reload is narrower than assumed.

**Root cause not addressed.** The correct fix is upstream in managed code:
`PreJITAsync` must not start until engine initialization (and hash table
population) is complete. The native bounds check is insufficient.

`patches/libsbox_hashtable_patch.c` renamed to
`patches/libsbox_hashtable_patch.c.bak` — excluded from compilation.

## Upstream Fix Applied (2026-05-22)

`engine/Sandbox.Reflection/Utility.cs` — `PreJITAsync` now gates on a
`volatile bool _engineReady`. Assemblies queued before the gate opens are held
in a `ConcurrentQueue<Assembly>` and drained by `SignalEngineReady()`.

`engine/Sandbox.Engine/Core/Bootstrap.cs` — `ReflectionUtility.SignalEngineReady()`
called after `IGameInstanceDll.Current.Initialize()` returns, once all engine
subsystems have finished initialising.

**Confirmed fixed.** User reported all PreJIT errors stopped after the change.
No native patch required.

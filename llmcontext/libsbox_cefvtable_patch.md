# libsbox_cefvtable_patch — CEF browser vtable crash (Sandbox gamemode)

## Problem

SIGSEGV on the main `sbox` thread when loading the Sandbox gamemode. Crash is
reproducible across sessions; identical PC and `rax` value each time.

- **Crash PC:** `libengine2.so+0x34df38` (`mov rsi,[rax]`)
- **`rax` at crash:** `0x454d4f5248435f46` (ASCII: `F_CHROME` — CEF type sentinel)
- **Faulting thread:** `sbox` (main thread)
- **Stack:** 2 frames — `libengine2.so` at frame 0, null return address at frame 1

Log entries immediately preceding the crash:

```
[Generic] Didn't enter lobby in a reasonable time!
[engine/Engine] [Steam] [S_API WARN] CClientUGC::SetRankedByTrendDays() failed ...
```

## Root Cause

The Sandbox gamemode tries to create a lobby for multiplayer. When the lobby
times out, the engine tears down a CEF browser object. Concurrently, UGC/
workshop callbacks complete and try to call a virtual method on that same
object through a raw pointer that was not cleared.

CEF writes the `F_CHROME` type tag (`0x454d4f5248435f46`) into the memory slot
that previously held the vtable pointer. The engine guards against null but not
against this sentinel, so the double-dereference crashes:

```asm
mov    (%rsi),%rax      ; rsi -> torn-down object; loads F_CHROME into rax
test   %rax,%rax        ; non-zero — null branch NOT taken
je     <safe_exit>
mov    (%rax),%rsi      ; CRASH: dereferences 0x454d4f5248435f46
...
call   *0x118(%rax)     ; intended vtable dispatch (never reached)
```

This is a Linux-specific timing issue. On Windows the message pump serialises
these two paths; on Linux they land on the main thread in an order the engine
does not handle.

## Fix

`patches/libsbox_cefvtable_patch.c` — trampoline patch on `libengine2.so`.

Replaces the 5-byte `test rax,rax / je` sequence at the patch site with a
`jmp rel32` to a small mmap'd trampoline. The trampoline adds a sentinel check
after the existing null check:

```asm
; trampoline (33 bytes, mmap'd near patch site)
test   rax, rax
je     safe_exit           ; null guard (original behaviour)
movabs r11, 0x454d4f5248435f46
cmp    rax, r11
je     safe_exit           ; F_CHROME sentinel guard (new)
jmp    crash_insn          ; clean — continue with mov rsi,[rax]
```

Pattern scanned dynamically at runtime (no hardcoded offsets). Trampoline
allocated using `MAP_FIXED_NOREPLACE`, scanning outward from the patch site in
both directions at exponentially increasing offsets (4KB, 8KB, 16KB … 1GB)
until a free page within ±2 GB is found. A plain `mmap` hint was insufficient —
on the first attempt the kernel ignored the hint and placed the trampoline ~2 GB
out of `jmp rel32` range.

**Pattern** (wildcards at je displacement bytes):

```
48 8b 06                 mov rax,[rsi]
48 85 c0                 test rax,rax    <- patch site (5 bytes)
74 ??                    je <safe_exit>
48 8b 30                 mov rsi,[rax]   <- crash instruction
48 85 f6                 test rsi,rsi
74 ??                    je <safe_exit>
48 8b 06                 mov rax,[rsi]
48 89 7d e8              mov [rbp-0x18],rdi
ff 90 18 01 00 00        call [rax+0x118]  <- vtable slot anchor
```

Confirmed unique (1 hit) in libengine2.so at file offset `0x34df30`.

## Offset History

| Date       | Pattern addr | Patch site | Notes   |
|------------|-------------|------------|---------|
| 2026-05-21 | `0x34df30`  | `0x34df33` | Initial |
| 2026-05-21 | `0x34df70`  | `0x34df73` | Engine update |

## Verification

Not yet confirmed. Awaiting user report that Sandbox no longer crashes on lobby
timeout.

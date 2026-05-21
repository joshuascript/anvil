# Shockwave VFX Filter — Null Dereference SIGSEGV (JIT/CLR)

## Problem

Recurring SIGSEGV in JIT-compiled managed code on the main `sbox` thread. Two identical crashes
per session, ~3–4 minutes apart.

Session `20260520_231504`: 2 crashes — SIGSEGV #1 at 23:20:51, SIGSEGV #2 at 23:24:27.

## Crash Site

```
PC:     0x7fff8a0023a3  (memfd:doublemapper — JIT code)
Thread: sbox (LWP 219584) — main thread
```

```asm
0x7fff8a00239d:  mov  %r14,%rdi    ; rdi = r14 = 0x0
0x7fff8a0023a0:  mov  %r12,%rsi
=> 0x7fff8a0023a3:  cmp  %edi,(%rdi)  ; CRASH: dereference null rdi
```

`r14` is null at the crash site. The JIT has no null guard here — it trusted the reference
was non-null. The backtrace is fully unresolved (all `??`) because the entire call chain is
in JIT-emitted code.

Registers consistent across both crashes (same code path, different object instances):

| Register | Crash #1 | Crash #2 | Notes |
|----------|----------|----------|-------|
| `rdi` | `0x0` | `0x0` | null — direct crash cause |
| `r14` | `0x0` | `0x0` | source of null |
| `rip` | `0x7fff8a0023a3` | `0x7fff8a0023a3` | identical |
| `rax` | `0x7ff82bdb046f` | `0x7ff82bdb046f` | identical |
| `r13` | `0x7ff885e77438` | `0x7ff885e77438` | identical |
| `rbx` | differs | differs | different managed object instance |
| `rsi` | differs | differs | different managed object instance |

The two crashes are the same JIT code path invoked for two separate `shockwave.vmat` instances
(e.g., two concurrent particle effects).

## Root Cause

The native assertion `VfxEvalExpression()` for the `Filter` expression of `materials/shockwave.vmat`
fires in `ExecuteMaterialCommandBuffers()` (`material_command_buffer.cpp:1507`). When this evaluation
fails, the native pipeline returns null/error through the material command buffer back into managed
code. The managed callback expects a valid object at the call site at `0x7fff8a0023a3` but receives
null → SIGSEGV.

The `VfxEvalExpression` failure is itself caused by `ResourceHandleToData()` failures
(`texturebase.cpp:3526`) — a texture referenced by the Filter expression cannot be loaded on Linux.

See `llmcontext/assertions/assertion_vfxeval_shockwave_filter.md` for the assertion detail.

## Fix Direction

Patch `ExecuteMaterialCommandBuffers` to make the `VfxEvalExpression` failure non-fatal (same
trampoline strategy as `libsbox_finalizeload_patch.c`). This would prevent the null from propagating
into managed code.

Longer term: investigate why the Filter texture fails to load (`ResourceHandleToData` failure) —
likely a missing or unsupported texture on Linux.

## Verification

Not yet confirmed resolved.

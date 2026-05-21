# Assertion: ExecuteMaterialCommandBuffers — VfxEvalExpression Filter

## Status: OPEN — no patch yet

**File:** `material_command_buffer.cpp`
**Line:** 1507
**Expr:** Assertion Failed in function `ExecuteMaterialCommandBuffers():`
`false`
**Message:** `Error Calling VfxEvalExpression() for Filter in material "materials/shockwave.vmat"`

## Root Cause

`VfxEvalExpression()` fails when evaluating the `Filter` expression for `materials/shockwave.vmat`.
The failure is almost certainly downstream of `ResourceHandleToData()` failures — the filter
expression references a texture that cannot be loaded on Linux, so the expression returns false.

The assertion at line 1507 is fatal in the sense that it propagates the failure back through the
material command buffer pipeline into managed code, eventually causing a null dereference SIGSEGV.
See `llmcontext/shockwave_vfxeval_null_crash.md` for the crash detail.

## Fix Direction

Two approaches:

1. **Patch the assertion** — scan `libengine2.so` for the assert pattern in
   `ExecuteMaterialCommandBuffers` at line 1507 and NOP/redirect it, same strategy as
   `libsbox_finalizeload_patch.c`. This stops the crash but leaves the material broken silently.

2. **Fix the texture load** — identify which texture the Filter expression resolves to and
   determine why `ResourceHandleToData()` fails for it on Linux. May be a missing file, an
   unsupported format, or a path resolution issue.

## Verification

Not yet confirmed resolved.

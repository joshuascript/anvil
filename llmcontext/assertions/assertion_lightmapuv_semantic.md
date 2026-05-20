# Assertion: SemanticNameToUsage — Unknown 'LightmapUV'

## Status: SOLVED via patch

**File:** `vulkan/InputSystemVulkan.cpp`
**Line:** 53
**Expr:** Assertion Failed in function `SemanticNameToUsage()`: Unknown semantic name `'LightmapUV'`

## Root Cause

`librendersystemvulkan.so` contains a 16-entry lookup table in `SemanticNameToUsage()` that
maps vertex semantic names to Vulkan usage values. `LightmapUV` — used for baked lightmap UV
coordinates — is absent from the table.

When a lightmapped mesh is rendered, the assertion fires, returns a garbage usage value,
produces an invalid `VkVertexInputAttributeDescription`, and the NVIDIA driver crashes inside
`libnvidia-glcore.so` on a `GlobPool` worker thread.

## Fix

`anvil/patch/libsbox_lightmapuv_patch.c` — extends the loop bound from 16 → 17 and writes a
new slot 16 entry mapping `"lightmapuv"` to the TEXCOORD usage class (`field8 = 0x322`,
`field12 = 0x14`).

See `llmcontext/libsbox_lightmapuv_patch.md` for full offset details and verification command.

> **Offset note:** patch targets hardcoded offsets in `librendersystemvulkan.so`. Verify after
> engine updates — see the offset history table in `libsbox_lightmapuv_patch.md`.

## Verification

Confirmed resolved once patch is active. Crash signature (`libnvidia-glcore.so` SIGSEGV on
`GlobPool` thread) absent in sessions after patch was applied.

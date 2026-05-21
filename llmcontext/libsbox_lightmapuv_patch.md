# LightmapUV Semantic Patch (libsbox_lightmapuv_patch)

## Problem

`librendersystemvulkan.so` contains a 16-entry lookup table in `SemanticNameToUsage()`
(`vulkan/inputlayoutvulkan.cpp`) that maps vertex semantic names to Vulkan usage values.
`LightmapUV` — a Source 2 semantic used for baked lightmap UV coordinates — is absent
from this table.

When a lightmapped mesh is rendered:
1. `SemanticNameToUsage("LightmapUV")` asserts: `Unknown semantic name 'LightmapUV'`
2. Returns garbage/uninitialised usage value
3. Produces an out-of-range `VkVertexInputAttributeDescription`
4. NVIDIA driver crashes inside `libnvidia-glcore.so` on `GlobPool` worker thread

## Crash signature

```
SIGSEGV in libnvidia-glcore.so.580.142
Faulting thread: GlobPool/8
rax=0x0  rbp=0x0  (crash at function entry, stack corrupted by invalid Vulkan state)
[vkps] Update thread also running libnvidia-glcore — pipeline state compilation active
```

## Table structure

File/VA offset: `0x737560` — 16 entries × 16 bytes each:
- `[0..7]`  : pointer to semantic name string (R_X86_64_RELATIVE relocation)
- `[8..11]` : uint32 field8 (usage class)
- `[12..15]`: uint32 field12 (index base offset)

Loop bound check: `0x1292f8: 41 83 fd 10  cmp r13d, 0x10` — asserts when r13 reaches 16.

## Fix

Two runtime patches applied via LD_PRELOAD after `librendersystemvulkan.so` loads:

### Patch 1 — extend loop bound
Change byte at `0x1292fb` from `0x10` → `0x11` (allows 17 loop iterations).

### Patch 2 — add entry 16
Write to table slot 16 at offset `0x737660`:
- String pointer → static `"lightmapuv"` in patch .so
- field8  = `0x322` (same as `texcoord` entry — TEXCOORD usage class)
- field12 = `0x14`  (same as `texcoord` — index base offset)

`LightmapUV` maps to the TEXCOORD semantic class, consistent with how Source 2
represents lightmap UVs on D3D11 (TEXCOORD with a high index).

## Verification

Patch prints on startup:
```
[lightmapuv_patch] installed — base=0x...  cmp_byte=0x... (0x10→0x11)  entry16=0x... → "lightmapuv"
```

Verification failure (binary version mismatch) prints:
```
[lightmapuv_patch] unexpected byte 0xXX at 0x...+0x1292fb — patch skipped (binary version mismatch?)
```

## Offset history

| Date       | CMP_BOUND_OFFSET | TABLE_ENTRY16_OFFSET | Notes   |
|------------|------------------|----------------------|---------|
| 2026-05-20 | `0x1292fb`       | `0x737660`           | Initial |

## Build and deployment

```bash
bash anvil/launch/patch_engine.sh   # builds all .so files in anvil/patches/bin/
```

Auto-loaded by all launch scripts via `LD_PRELOAD` from `anvil/patches/bin/`.

# LightmapUV Semantic Patch (libsbox_lightmapuv_patch)

## Problem

`librendersystemvulkan.so` contains a 16-entry lookup table in `SemanticNameToUsage()`
(`vulkan/inputlayoutvulkan.cpp`) that maps vertex semantic names to Vulkan usage values.
`LightmapUV` — used by Source 2 lightmapped meshes — is absent from this table.

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
```

## Table structure

The semantic lookup table is at VMA `0x737560` (file offset `0x736560` — note the 0x1000
delta from the `.data.rel.ro` segment mapping: VMA `0x7340a0`, file offset `0x7330a0`).

16 entries × 16 bytes:
- `[0..7]`  : pointer to semantic name string (R_X86_64_RELATIVE relocation)
- `[8..11]` : uint32 field8 (usage class)
- `[12..15]`: uint32 field12 (index modifier)

| Slot | String                  | f8     | f12    |
|------|-------------------------|--------|--------|
|  0   | position                | 0x0000 | 0x0000 |
|  1   | blendweight             | 0x0001 | 0x0000 |
|  2   | blendindices            | 0x0002 | 0x0000 |
|  3   | normal                  | 0x0003 | 0x0000 |
|  4   | psize                   | 0x0004 | 0x0000 |
|  5   | texcoord                | 0x0005 | 0x0000 |
|  6   | tangent                 | 0x0006 | 0x0000 |
|  7   | binormal                | 0x0007 | 0x0000 |
|  8   | tessfactor              | 0x000a | 0x0000 |
|  9   | positiont               | 0x0008 | 0x0000 |
| 10   | color                   | 0x0009 | 0x0000 |
| 11   | tangents                | 0x0006 | 0x0000 |
| 12   | tangentt                | 0x0007 | 0x0000 |
| 13   | specular                | 0x0009 | 0x0001 |
| 14   | wrinkle                 | 0x0005 | 0x0008 |
| 15   | vertexpainttintcolor    | 0x0005 | 0x0000 |

There are **two functions** that loop over this table — both share the same table:

| Function | Counter reg | Loop bound VMA | Notes |
|----------|-------------|----------------|-------|
| `0x128d10` | `%ebx` | `0x128d39` | Simple lookup, returns field8 |
| `0x1292dd` | `%r13d` | `0x1292fb` | Extended; also uses field12 |

## Fix

`anvil/patches/libsbox_lightmapuv_patch.c` — LD_PRELOAD shim that fires when
`librendersystemvulkan.so` is `dlopen`'d. Uses a **data-anchored** scan strategy:

### 1. Table location (data-anchored, not code-anchored)
Scans non-exec PT_LOAD segments for three consecutive 16-byte entries matching:
- `[0]` ptr → `"position"`,    field8=0, field12=0
- `[1]` ptr → `"blendweight"`, field8=1, field12=0
- `[2]` ptr → `"blendindices"`,field8=2, field12=0

This anchors on source-level string literals in `.rodata` — stable across recompilations
regardless of register allocation, instruction encoding, or ASLR.

### 2. Entry 16 write
Writes to `table_base + 256` (slot 16):
- string ptr → `"lightmapuv"` (static string in the patch .so)
- field8 = `0x0005` (TEXCOORD usage class — matches `texcoord` entry)
- field12 = `0x0000` (no index modifier)

Entry is written unconditionally once the table is found (graceful degradation).

### 3. Loop bound patching
Scans `.text` for RIP-relative LEAs that resolve to `table_base`. Within ±512 bytes
of each LEA, patches any `cmp $0x10, %<reg>` followed by a conditional jump:
`0x10` → `0x11`. Covers all register choices the compiler might use.

## Validation

Run after any engine update to verify the patch will work:

```bash
python3 anvil/debug/scripts/binaryscan/test_lightmapuv_patch.py
```

Expected output: `PASS  all checks`. If `find_table` fails, the semantic strings
themselves changed (unlikely). If `find_bounds` fails, update the patch or investigate
manually — the table entry will still be written, only the loop bound needs fixing.

## Observed offsets (librendersystemvulkan.so, 2026-05-20)

| Item | VMA | Notes |
|------|-----|-------|
| Table base | `0x737560` | VMA; file offset `0x736560` (delta 0x1000) |
| Entry 16 | `0x737660` | slot 16 of the table |
| Bound A | `0x128d39` | in function `0x128d10` (`%ebx` counter) |
| Bound B | `0x1292fb` | in function `0x1292dd` (`%r13d` counter) |

## Prior errors (corrected)

Earlier versions of this document stated:
- `TABLE_ENTRY16_OFFSET = 0x737660` with `field8=0x322, field12=0x14` — **incorrect values**.
  These were read from FILE offset `0x737560` which equals VMA `0x738560`, landing in
  `CUtlRBTree` assertion data rather than the semantic table. The correct texcoord values
  are `field8=0x0005, field12=0x0000`.
- Only one loop bound was patched (`0x1292fb`). The second bound at `0x128d39` was missed.

## Build and deployment

```bash
bash anvil/launch/patch_engine.sh   # builds all .so files in anvil/patches/bin/
```

Auto-loaded by all launch scripts via `LD_PRELOAD` from `anvil/patches/bin/`.

Successful patch prints to stderr:
```
[lightmapuv_patch] table at offset 0x737560
[lightmapuv_patch] entry 16 written at offset 0x737660 → "lightmapuv" (field8=0x0005 field12=0x0000)
[lightmapuv_patch] bound patched at offset 0x128d39 (0x10→0x11)
[lightmapuv_patch] bound patched at offset 0x1292fb (0x10→0x11)
[lightmapuv_patch] installed — 2 loop bound(s) patched
```

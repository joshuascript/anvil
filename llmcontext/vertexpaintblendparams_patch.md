# VertexPaintBlendParams Semantic Patch — IN PROGRESS / UNSOLVED

## Problem

`vulkan/inputlayoutvulkan.cpp:53` asserts:
```
SemanticNameToUsage(): Unknown semantic name 'VertexPaintBlendParams'
```
Tracked in `game/ignored_assertions.txt` as `1779314907,86400,53,vulkan/inputlayoutvulkan.cpp`.

Same class of fix as `LightmapUV` — extend the 16-entry semantic lookup table in
`SemanticNameToUsage()` and increment the loop bound.

---

## Binary: `game/bin/linuxsteamrt64/librendersystemvulkan.so`

---

## ✅ Confirmed: Semantic table at `0x736560`

16 entries × 16 bytes. Found via `elf_relocs.py` (R_X86_64_RELATIVE relocations
on the string pointer field). Strings verified by reading file offsets directly.

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

`VertexPaintBlendParams` is NOT in this table (confirmed: string absent from binary).

Entry 16 would be at `0x736660` — needs verification that bytes there are safe to
overwrite (may contain convar assertion data — see investigation notes below).

---

## ✅ Confirmed: Loop bound byte

`0x1292f8: 41 83 fd 10  cmp r13d, 0x10` — byte to patch is at `0x1292fb = 0x10`.

---

## ⚠️ UNSOLVED: Which function owns this loop?

### The contradiction

The existing `libsbox_lightmapuv_patch.md` doc and the C patch both say the semantic
table is at `0x737560` and entry 16 at `0x737660`. But:

- The disassembly at `0x1292dd` does `lea 0x60e27c(%rip),%r12 # 737560` — loads
  `0x737560` as the base of the loop's comparison table.
- Raw bytes at `0x737560` contain **CUtlRBTree assertion data** (strings like
  `../public/tier0/utlrbtree.h`, `m_Elements.IsValidIterator(...)`), NOT semantic
  name strings.
- The actual semantic name strings with correct field8/field12 values are at `0x736560`.

### Two references to the assertion string

`"Unknown semantic name '%s'"` is at file offset `0x6461a1`. Two RIP-relative
references found:

1. **`0x128d63`**: `48 8d 35 37 d4 51 00` → `lea 0x51d437(%rip),%rsi # 6461a1`
   — **NOT yet disassembled. Likely the real SemanticNameToUsage.**

2. **`0x1296d8`**: `48 8d 35 c2 ca 51 00` → `lea 0x51cac2(%rip),%rsi # 6461a1`
   — This is the assertion fail path of the loop at `0x1292f8`. But this loop's
   table base is `0x737560` (wrong data). Could be a different function that
   coincidentally loops to 16 and uses the same assertion message.

### What needs to happen next

1. **Disassemble around `0x128d63`** — this is likely the real `SemanticNameToUsage`.
   Find the loop bound byte and table base address for THAT function.

2. **Verify `0x736560` table is referenced** from the 0x128d63 function (expected).

3. **Determine if `0x737560` loop (at `0x1292f8`) is a separate function** — if so,
   the existing LightmapUV patch target may be wrong and the LightmapUV fix may not
   actually be active.

4. **Check `0x736660`** (16-byte area after the confirmed table) for safe overwrite.

5. Once the correct function is identified, update the patch C file with:
   - Correct `TABLE_ENTRY16_OFFSET` (expected `0x736660`)
   - `CMP_BOUND_OFFSET` — may be the same `0x1292fb` or may change
   - `VertexPaintBlendParams` entry: f8 = `0x0005` (texcoord class, same as
     `vertexpainttintcolor`), f12 = `0x0000`

---

## ⚠️ RELATED: LightmapUV patch may need re-verification

`libsbox_lightmapuv_patch.md` documents `TABLE_ENTRY16_OFFSET = 0x737660`. If the
real table is at `0x736560`, the LightmapUV entry 16 should be at `0x736660`. The
current patch may be writing to the wrong slot.

Determine definitively which table the semantic lookup function uses before committing
offsets. Both patches should write to the same table.

---

## Files to update when solved

- `anvil/patch/libsbox_lightmapuv_patch.c` — update offsets, add second entry
- `anvil/llmcontext/libsbox_lightmapuv_patch.md` — update offset history table
- This file — mark resolved, fill in verified offsets

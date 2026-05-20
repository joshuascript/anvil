# Assertion Failures

## Assertion 1 — SOLVED via patch

**File:** `vulkan/InputSystemVulkan.cpp`  
**Line:** 53  
**Expr:** Assertion Failed in function `SemanticNameToUsage()`  
Unknown semantic name `'LightmapUV'`

**Fix:** `anvil/patch/libsbox_lightmapuv_patch.c` — extends the semantic lookup table and loop bound at runtime. See `llmcontext/libsbox_lightmapuv_patch.md` for details.

> **Offset note:** patch targets offsets in `librendersystemvulkan.so`. Verify after engine updates — see the offset history table in `libsbox_lightmapuv_patch.md`.

---

## Assertion 2 — SOLVED via patch

**File:** `resourcesystem/loadingresource.cpp`  
**Line:** 1194  
**Expr:** Assertion Failed in function `FinalizeLoadRequest()`:  
`pLoadingResource->GetExtRefDepth() > GetExtRefDepth()`

**Fix:** `anvil/patch/libsbox_finalizeload_patch.c` — NOPs the `jge` that gates the assertion so execution always falls through to the normal continuation path (no behavioural difference).

> **Offset note:** this assertion does **not** appear in GDB crash logs — it fires as a native `Assert()` dialog, not a SIGSEGV. The patch targets a hardcoded offset in `libengine2.so` that **shifts whenever the engine updates**. If the dialog reappears after an update, re-run the byte search and update `JGE_OFFSET` and `expected[]` in the patch source, then rebuild:
> ```bash
> python3 -c "
> import re, sys
> data = open('game/bin/linuxsteamrt64/libengine2.so','rb').read()
> pat = bytes([0x8b,0x40,0x6c,0x39,0x43,0x6c,0x0f,0x8d])
> pos = data.find(pat)
> print(f'jge offset: 0x{pos+6:x}  bytes: {data[pos+6:pos+12].hex(\" \")}')
> "
> gcc -shared -fPIC -O2 -o anvil/patch/bin/libsbox_finalizeload_patch.so \
>     anvil/patch/libsbox_finalizeload_patch.c -ldl -lpthread
> ```
> See the offset history table in `libsbox_finalizeload_patch.c` for past values.

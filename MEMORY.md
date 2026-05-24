# Memory

## Patch workflow & maintenance

Patches in `anvil/patches/` are compiled to `anvil/patches/bin/` and auto-loaded via `LD_PRELOAD` by all launch scripts. Build all patches with `bash anvil/launch/patch_engine.sh`.

**Why:** libengine2.so and other native .so files update periodically and shift function offsets. Patches that use hardcoded offsets (libsbox_htmlcb_patch, libsbox_lightmapuv_patch) need the offset verified after each engine update. The finalizeload patch was converted to dynamic pattern scan so it never needs updating.

**How to apply:** After an engine update, if a patched assert/crash reappears, check the patch's stderr output on launch for "unexpected bytes — patch skipped (binary version mismatch?)". If seen, find the new offset with the binary search one-liner in `llmcontext/assertions/assertion_lightmapuv_resource_load.md` and update the patch source + rebuild.

**Key offset indicator:** `libengine2.so` modify time — compare against patch `.so` modify time. If engine is newer, offsets may be stale.

Patches that don't catch in GDB (Assert dialogs vs SIGSEGV) require this manual workflow — they don't appear in `debug/logs/` crash sessions.

## Active investigations

- [Particle sprite renderer — ResourceHandleToData invalid handle](llmcontext/particle_texture_handle_invalid.md) — per-frame SeriousWarning from invalid anonymous texture handle in IBatchedParticleSpriteRenderer; native site at librendersystemvulkan.so:0x102199; fix not yet confirmed

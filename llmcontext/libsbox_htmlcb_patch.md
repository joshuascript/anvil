# ISteamHTMLSurface Callback ABI Patch (libsbox_htmlcb_patch)

## Problem

`libengine2.so` contains a C++ callback registered with `steamclient.so` for HTML surface
events. On Linux, `steamclient.so` invokes this callback with the browser handle integer
in RSI (e.g. 2, 8), but the function expects RSI to be a nullable pointer to navigation
data. The function checks `test rsi, rsi; je <skip>` — the non-zero handle passes the null
check — then dereferences `mov (%rsi),%rsi`, treating the integer as a pointer → SIGSEGV.

## Crash signature

```
#0  ?? () from libengine2.so     ← native crash
rsi = 0x2                        ← browser handle integer, not a pointer
```

## Fix

`anvil/patch/libsbox_htmlcb_patch.c` — LD_PRELOAD shim that interposes `dlopen`. When
`dlopen("libengine2.so", ...)` returns, it locates the function via `dl_iterate_phdr` and
replaces the 3-byte `mov (%rsi),%rsi` (`48 8b 36`) with `xor rsi,rsi` (`48 31 f6`). This
zeroes RSI at that point, equivalent to the null branch the function already handles.

The patch verifies the expected bytes before writing. If verification fails (binary version
mismatch), it prints a warning to stderr and skips — the game still runs but the crash
re-appears.

## Why dlopen hook, not constructor

sbox does not link `libengine2.so` as a direct ELF dependency — it `dlopen`s it at runtime.
A plain `__attribute__((constructor))` fires before `libengine2.so` exists in the process,
so `dl_iterate_phdr` finds nothing. The `dlopen` interpose fires at exactly the right time.

## Offset history

The crash instruction offset changes when `libengine2.so` is updated. Always verify with:

```python
python3 -c "
with open('game/bin/linuxsteamrt64/libengine2.so','rb') as f:
    f.seek(CRASH_INSN_OFFSET)
    print(f.read(3).hex())  # must be '488b36'
"
```

| Date | Offset | Notes |
|------|--------|-------|
| 2026-05-18 | `0x34d186` | Original |
| 2026-05-19 | `0x34d1a6` | After libengine2.so update; function gained `endbr64` + stack canary prologue (+0x20) |

Current offset in source: `CRASH_INSN_OFFSET` in `anvil/patch/libsbox_htmlcb_patch.c`.

## Build and deployment

```bash
bash anvil/launch/patch_engine.sh   # builds all .so files in anvil/patch/bin/
```

The launch scripts (`anvil/launch/launch-sbox.sh`, `launch-sbox-capture-steam-callbacks.sh`)
auto-load all `.so` files from `anvil/patch/bin/` via `LD_PRELOAD`.

Successful patch prints to stderr at startup:
```
[htmlcb_patch] installed — base=0x...  patched=0x...  (mov rsi,[rsi] → xor rsi,rsi)
```

Failed verification prints:
```
[htmlcb_patch] unexpected bytes at 0x...+0x... — patch skipped (binary version mismatch?)
```

If you see the mismatch warning, find the new offset with `objdump -d` around the old
offset and update `CRASH_INSN_OFFSET`, then rebuild.

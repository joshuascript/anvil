# Bloom Ghosting Artifact (bloom_artifact_patch)

## Problem

On Linux with s&box rendering at 1280×720 in a windowed session, the bloom post-process
produces 5–6 translucent ghost copies of the scene translated roughly 2/3 of the way
inward and upward. The artifact only appears in **Lit** mode (normal full lighting pipeline)
and disappears when bloom is disabled with `r_bloom 0`.

## Root Cause

The bloom composite pixel shader (`postprocess_bloom.shader`) computes screen-space UVs as:

```hlsl
float2 vScreenUv = (input.vPositionSs.xy + g_vViewportOffset) / g_vViewportSize;
```

`g_vViewportSize` is a global set by the engine's native render system. On Linux/Vulkan,
this appears to be set to the **physical display resolution** (e.g. 1920×1080) rather than
the game's render viewport size (1280×720). With a 1920×1080 display and 1280×720 render:

- UVs only span 0..0.666 across the viewport instead of 0..1
- The bloom texture is sampled with a 2/3-scale UV offset
- With `g_sBilinearMirror` sampling, the out-of-range region wraps, producing ghost copies
- The "2/3 of the way" offset matches exactly: 1280/1920 = 0.666

The fix is to use `input.vTexCoord` (set correctly by the engine's `Blit` mechanism) instead
of the computed UV.

## Fix (source change — not yet compiled)

In `postprocess_bloom.shader` PS block, change:

```hlsl
// Before
float2 vScreenUv = (input.vPositionSs.xy + g_vViewportOffset) / g_vViewportSize;

// After
float2 vScreenUv = input.vTexCoord;
```

The modified source is saved at `llmcontext/shaders/postprocess_bloom.shader`.
The original compiled binary is saved at `llmcontext/shaders/postprocess_bloom.shader_c.original`.

## Compilation Problem

s&box only auto-recompiles `.shader` → `.shader_c` on **Windows** (inside the editor).
On Linux, the compiled `.shader_c` is used as-is and there is no automatic recompilation.

### Attempts to compile on Linux

#### 1. `game/bin/managed/ShaderCompiler` (Linux ELF, .NET 10)
- Fails at startup: `ToolAppSystem.InitEnginePaths()` only checks for `bin\managed`
  (Windows backslash), never matches on Linux → throws `"Unknown Location"`.
- Fix applied to `engine/Sandbox.AppSystem/ToolAppSystem.cs`: added Linux branch
  checking `bin/managed` and setting `LD_LIBRARY_PATH` to `bin/linuxsteamrt64/`.
- After rebuild, the native engine tried to dlopen from `bin/managed/` instead of
  `bin/linuxsteamrt64/` — RPATH in the binary overrides `LD_LIBRARY_PATH`.
- **Status: blocked by native library resolution.**

#### 2. `game/bin/win64/resourcecompiler.exe` via wine
Requires:
- `-game <directory>` (not a file path — the directory containing `gameinfo.gi`)
- A `gameinfo.gi` at the game root defining search paths
- `assettypes.txt` discoverable at `EXECUTABLE_PATH` (the exe's directory)

Progress made:
- Created `game/gameinfo.gi` with correct search paths
- Confirmed `-game` takes a directory, not the `.gi` file path
- Initialization succeeds (assettypes.txt loaded, filesystem mounted)
- Compile itself fails silently: `"Attempting to compile shader resource ... without buffer [FAIL]"`
- No error detail even with `-v`; output is swallowed inside `vfx_vulkan.dll` / DXC

Hypothesis: `vfx_vulkan.dll` fails to initialize the Vulkan pipeline compilation context
under wine (no real GPU/Vulkan available), causing DXC invocation to silently abort.

#### 3. SPIR-V direct patching
- `spirv-tools` installed (`spirv-dis`, `spirv-as`, `spirv-opt`)
- `.shader_c` is a Source 2 resource file with a `SPRV` block at offset `0x110`
- Program table: FEATURE at `+0x48`, VS at `+0x1339`, PS at `+0x16d9` (9896 bytes)
- Each program is in `vcs2` format (Valve Compiled Shader v2, version 65)
- No raw SPIR-V magic (`0x07230203`) found — data is likely in a custom VCS2 combo
  encoding, possibly LZ4-compressed per static/dynamic combo
- **Status: format not yet fully reversed; patching not attempted.**

## Files

| File | Description |
|------|-------------|
| `llmcontext/shaders/postprocess_bloom.shader` | Modified source (vTexCoord fix applied) |
| `llmcontext/shaders/postprocess_bloom.shader_c.original` | Original compiled binary (backup) |
| `game/addons/base/Assets/shaders/postprocess_bloom.shader` | Active source (same as above) |
| `game/addons/base/Assets/shaders/postprocess_bloom.shader_c` | Active compiled (unpatched) |
| `game/gameinfo.gi` | Created for resourcecompiler; safe to delete |
| `game/assettypes.txt` | Copy of bin/assettypes.txt placed here during testing; safe to delete |
| `game/core/assettypes.txt` | Copy placed during testing; safe to delete |
| `game/bin/win64/assettypes.txt` | Copy placed during testing; safe to delete |

## Next Steps

1. **SPIR-V path**: Reverse the `vcs2` combo format to locate and extract the PS SPIR-V
   blob, patch it using `spirv-dis` / `spirv-as`, and reinsert.
2. **DXC path**: Write a small C program using `libdxcompiler.so` (present at
   `game/bin/linuxsteamrt64/libdxcompiler.so`) to compile the PS HLSL directly to
   SPIR-V, then inject into the `vcs2` structure.
3. **Native patch path**: Write an LD_PRELOAD patch that intercepts the bloom composite
   draw call and overrides `g_vViewportSize` / `g_vViewportOffset` to correct values
   at runtime, without recompiling the shader.
4. **Windows compile path**: Compile on Windows or in a Windows VM with the s&box editor.

Option 3 (LD_PRELOAD runtime override) may be the most tractable on Linux without
a working shader compiler pipeline.

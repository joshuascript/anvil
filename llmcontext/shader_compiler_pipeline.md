# Shader Compiler & Content Builder Pipeline

## Overview

s&box compiles `.shader` source files into `.shader_c` binary resource files. The build pipeline
has two distinct stages:

1. **ShaderProc** — packs native `.fxc` include files into a C++ header (build-time, not relevant
   to shader compilation on Linux)
2. **BuildShaders** — actually compiles `.shader` → `.shader_c` using `ShaderCompiler` + native
   `libvfx_vulkan.so`

Content building (`BuildContent`) is a separate step that uses `contentbuilder.exe` (Windows only
tool, invoked by `SboxBuild`).

---

## Key Files

| File | Role |
|------|------|
| `engine/Tools/ShaderCompiler/Program.cs` | Entry point for the shader compiler tool |
| `engine/Tools/SboxBuild/Steps/BuildShaders.cs` | SboxBuild step that invokes ShaderCompiler |
| `engine/Tools/SboxBuild/Steps/BuildContent.cs` | SboxBuild step that invokes contentbuilder.exe |
| `engine/Sandbox.Engine/Systems/Render/ShaderCompile/ShaderCompile.cs` | Core compile logic |
| `engine/Sandbox.Engine/Systems/Render/ShaderCompile/ShaderSource.cs` | Parses `.shader` files, detects stale `.shader_c` |
| `engine/Sandbox.Engine/Systems/Render/ShaderCompile/ProgramSource.cs` | Compiles individual shader programs (VS/PS/CS etc.) |
| `engine/Sandbox.Engine/Systems/Render/ShaderCompile/ShaderCompileContext.cs` | Wraps `IShaderCompileContext` native handle |
| `engine/Sandbox.Engine/Systems/Render/ShaderCompile/ShaderPreprocessor.cs` | `#include` expansion preprocessor |
| `engine/Sandbox.Engine/Systems/Render/ShaderCompile/ShaderCompileOptions.cs` | Options struct (ForceRecompile, SingleThreaded, ConsoleOutput) |
| `engine/Sandbox.Engine/Core/Interop/CreateInterface.cs` | `NativeLibrary`-based Valve CreateInterface loader |
| `engine/Sandbox.AppSystem/ToolAppSystem.cs` | Bootstraps engine for standalone tool processes |
| `game/bin/managed/ShaderCompiler` | **Linux ELF** — the compiled ShaderCompiler binary |
| `game/bin/linuxsteamrt64/libvfx_vulkan.so` | Native Vulkan shader compiler library (Linux) |
| `game/bin/linuxsteamrt64/libfilesystem_stdio.so` | Native filesystem library (Linux) |
| `game/bin/win64/vfx_vulkan.dll` | Windows version of vfx_vulkan |
| `game/bin/win64/contentbuilder.exe` | Content builder (Windows only) |

---

## Compilation Pipeline (managed side)

### 1. ShaderCompiler entry (`Program.cs`)

```
ShaderCompiler [file.shader ...] [-f] [-s] [-q]
  -f   force recompile (even if .shader_c is up to date)
  -s   single-threaded
  -q   quiet (no console output)
  (no args) = compile all *.shader files recursively from CWD
```

- Enumerates `.shader` files under working directory, skips `download/`, `templates/`, hidden dirs
- Wraps in `ToolAppSystem` (bootstraps engine + filesystem + resourcecompiler)
- Calls `ShaderCompile.Compile(absolutePath, relativePath, options, token)` for each file
- On failure, exits with code 1

### 2. ToolAppSystem bootstrap (`ToolAppSystem.cs`)

- **Critical Linux problem**: `InitEnginePaths()` checks for `bin\managed` using Windows backslash,
  then adds `bin\win64` to `PATH`. This needs to be circumvented for Linux.
- Calls `SourceEnginePreInit()` on the native engine
- Adds `resourcecompiler` system (`ResourceCompilerSystem001`)

### 3. ShaderCompile static constructor (`ShaderCompile.cs:72-96`)

Runs once on first use:
```csharp
string dllName = "vfx_vulkan.dll";
native = NativeEngine.CreateInterface.LoadInterface(dllName, "VFX_DLL_001");
var createinterface = NativeEngine.CreateInterface.GetCreateInterface("filesystem_stdio.dll");
native.Init(createinterface);
```

On Linux, `NativeLibrary.TryLoad("vfx_vulkan.dll")` will try to resolve `libvfx_vulkan.so` from
`LD_LIBRARY_PATH`. The `.so` exists at `game/bin/linuxsteamrt64/libvfx_vulkan.so`.

### 4. ShaderSource.Read() (`ShaderSource.cs`)

- Reads `.shader` file line by line
- Detects program blocks: `VS`, `PS`, `CS`, `GS`, `PS_RENDER_STATE`, `RTX`
- Checks if `.shader_c` is out of date via `vfx.LoadFromCompiledUnlessOutOfDate()`

### 5. ProgramSource.Compile() (`ProgramSource.cs`)

For each program (VS/PS/CS etc.):
1. Calls `ShaderTools.MaskShaderSource()` to extract only the relevant program block
2. Runs `ShaderPreprocessor` to expand `#include` directives
3. Gets static+dynamic combo list from the VFX native object
4. Compiles each combo in parallel via `ShaderCompile.CompileSingleCombo()`
5. Aggregates results into `CVfxByteCodeManager`

### 6. CompileSingleCombo (`ShaderCompile.cs:177`)

```csharp
native.CompileShader(context, staticCombo, dynamicCombo, vfx.native,
    VfxCompileTarget_t.SM_6_0_VULKAN, programType, useShaderCache, 0)
```

Calls into `libvfx_vulkan.so` → `IVfx::CompileShader`. Targets `SM_6_0_VULKAN` (SPIR-V via DXC).

### 7. Output serialization (`ShaderSource.Serialize()`)

Binary format written to `file.shader_c`:
```
[ProgramHeader_t × VFX_PROGRAM_MAX]   -- offsets + sizes per program type
[program data for each present program]
[SPIRV total size uint]
[JSON source dict OR 0 int]           -- source omitted for core assets
```

Then wrapped by `IResourceCompilerSystem.GenerateResourceBytes()` into a Valve resource file.

### 8. CompileResourceFile (`ShaderCompile.cs:168`)

```csharp
static unsafe byte[] CompileResourceFile(string filename, byte[] data)
{
    fixed (byte* dataPtr = data) {
        using CUtlBuffer buffer = IResourceCompilerSystem.GenerateResourceBytes(
            filename, (IntPtr)dataPtr, data.Length);
        return buffer.ToArray();   // calls Base() + TellMaxPut()
    }
}
```

---

## Native Interface Summary

All these are loaded from `libvfx_vulkan.so` (Linux) / `vfx_vulkan.dll` (Windows):

| Interface | Key Methods |
|-----------|-------------|
| `IVfx` (obtained via `CreateInterface("VFX_DLL_001")`) | `Init(factory)`, `CompileShader(...)`, `CreateSharedContext()`, `ClearShaderCache()` |
| `IShaderCompileContext` | `SetMaskedCode(string)`, `Delete()` |
| `CVfxByteCodeManager` | `Create()`, `OnStaticCombo(id)`, `OnDynamicCombo(result)`, `Reset()`, `Delete()` |

Loaded from `libengine2.so` (via the function table at engine init, slots ~1312-1316, 2286-2287, 2383-2386):
| Interface | Key Methods |
|-----------|-------------|
| `IResourceCompilerSystem` | `GenerateResourceBytes(filename, ptr, len)` → `CUtlBuffer` |

`filesystem_stdio` is passed to `IVfx.Init()` as a `CreateInterface` factory pointer — the shader
compiler uses it internally to read shader include files.

---

## SboxBuild Steps

### BuildShaders (`Steps/BuildShaders.cs`)

```csharp
string shaderCompilerPath = Path.Combine(gameDir, "bin", "managed", "shadercompiler.exe");
// Runs with: "* [-f]"   (wildcard = all shaders, -f = force)
Utility.RunProcess(shaderCompilerPath, arguments, workingDirectory: gameDir);
```

- **Hardcoded `.exe` extension** — needs to become `ShaderCompiler` (no extension) on Linux
- On CI, fails if any shader was recompiled (shaders must be pre-compiled before commit)

### BuildContent (`Steps/BuildContent.cs`)

```csharp
string contentBuilderPath = Path.Combine(gameDir, "bin", "win64", "contentbuilder.exe");
Utility.RunProcess(contentBuilderPath, "-b", gameDir);
```

- Windows-only tool, no Linux equivalent
- Compiles non-shader content assets (textures, models, etc.)

---

## Linux Status

| Component | Status |
|-----------|--------|
| `game/bin/managed/ShaderCompiler` | ✅ Linux ELF binary already exists |
| `game/bin/linuxsteamrt64/libvfx_vulkan.so` | ✅ Exists |
| `game/bin/linuxsteamrt64/libfilesystem_stdio.so` | ✅ Exists |
| `game/bin/linuxsteamrt64/libresourcecompiler.so` | ✅ Compiled stub exists (source deleted — see below) |
| `anvil/launch/shaders/compile_shaders.py` | ✅ Launcher script exists and works through engine init |
| `ToolAppSystem.InitEnginePaths()` | ✅ Circumvented via `game/bin/bin\managed/` directory trick |
| `BuildShaders.cs` shaderCompilerPath | ❌ Hardcoded `.exe` extension |
| `contentbuilder.exe` | ❌ Windows only, no Linux port |
| Native include resolution (addon shaders) | ❌ Blocker — see below |
| Missing `.fxc` files (`math_general.fxc` etc.) | ❌ Blocker — not in public repo |

---

## Linux Invocation Strategy (no managed code changes)

### The `bin\managed` directory trick

`ToolAppSystem.InitEnginePaths()` checks:
```csharp
if (exePath.EndsWith("bin\\managed", StringComparison.OrdinalIgnoreCase))
```

On Linux, `\` is a valid filename character (not a path separator). So we create a directory
literally named `bin\managed` inside `game/bin/`. The binary at
`game/bin/bin\managed/ShaderCompiler` satisfies the check because on Linux:
- `Path.GetDirectoryName("…/game/bin/bin\managed/ShaderCompiler")` = `"…/game/bin/bin\managed"`
- `"…/game/bin/bin\managed".EndsWith("bin\\managed")` = `true` (C# `\\` = literal backslash)
- `new DirectoryInfo("…/game/bin/bin\managed").Parent` = `…/game/bin`
- `.Parent.Parent` = `…/game` ← correctly becomes gameRoot

The binary and all managed DLLs must be **copied** (not symlinked) from `game/bin/managed/`
to `game/bin/bin\managed/` — symlinks fail because .NET reads `/proc/self/exe` which resolves
through the symlink.

### Engine path normalization issue

The native engine normalizes `\` → `/` in path strings internally. So after `Plat_SetModuleFilename`
is called with the binary's path, the engine sees the module directory as `game/bin/bin/managed/`
(forward slashes). When the engine loads its systems, it searches for `.so` files in
`game/bin/bin/managed/` — which must exist as a real directory containing symlinks to
`game/bin/linuxsteamrt64/*.so`.

Required symlinks at `game/bin/bin/managed/` (forward slash directory):
```
→ game/bin/linuxsteamrt64/libengine2.so
→ game/bin/linuxsteamrt64/libtier0.so
→ game/bin/linuxsteamrt64/libvfx_vulkan.so
→ game/bin/linuxsteamrt64/libfilesystem_stdio.so
→ game/bin/linuxsteamrt64/libmaterialsystem2.so
→ game/bin/linuxsteamrt64/librendersystemempty.so
→ game/bin/linuxsteamrt64/libresourcecompiler.so  ← stub goes here too
... (all .so files)
```

Also needs `libresourcecompiler.so` placed in `game/bin/linuxsteamrt64/` directly, since
`LD_LIBRARY_PATH` includes that directory.

### Minimal manual invocation (once stub is built)

```bash
cd game/bin/bin\managed   # literal backslash in directory name
LD_LIBRARY_PATH=../../linuxsteamrt64:$LD_LIBRARY_PATH ./ShaderCompiler [options]
```

Or from a Python script that sets up the environment.

---

## CUtlBuffer — Native Struct Layout (reverse engineered from libengine2.so)

`CUtlBuffer` is 0x50 (80) bytes total, allocated with `operator new(0x50)`.

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| 0x00 | 8 | `data` (void*) | `Base()` reads this (nativeFunctions[1258]) |
| 0x08 | 4 | `nAllocationCount` | from CUtlMemory |
| 0x0C | 4 | `nGrowSize` | Dispose checks `<= 0x3FFFFFFF`: if true, frees `data` via MemAlloc_Free |
| 0x10 | 16 | (padding/other fields) | |
| 0x20 | 4 | `put_pos` (int) | `TellMaxPut()` reads this (nativeFunctions[1259]) |
| 0x24 | 44 | (remaining fields) | |

**CUtlBuffer function pointers in `libengine2.so`:**

| nativeFunctions slot | Function | VA in libengine2 | Behavior |
|---------------------|----------|-----------------|---------|
| 1256 | `CUtlBuffer_Create` | 0x3567b0 | `operator new(0x50)` + `CUtlBuffer(0,0,0)` ctor |
| 1257 | `CUtlBuffer_Dispose` | 0x361ae0 | if offset 0x0C ≤ 0x3FFFFFFF: free data; then `operator delete(ptr, 0x50)` |
| 1258 | `CUtlBuffer_Base` | 0x34c7b0 | `mov (%rdi),%rax` — reads qword at offset 0x00 |
| 1259 | `CUtlBuffer_TellMaxPut` | 0x34c7c0 | `mov 0x20(%rdi),%eax` — reads dword at offset 0x20 |

**Stub `GenerateResourceBytes` must return a malloc'd 0x50-byte struct** with:
- `[0x00]` = pointer to VRF-wrapped data (also malloc'd, will be freed by Dispose)
- `[0x0C]` = 0 (nGrowSize=0 → Dispose WILL free the data buffer — desired behavior)
- `[0x20]` = size of VRF data

---

## IResourceCompilerSystem — nativeFunctions Slots

From `engine/Sandbox.Engine/Interop.Engine.cs` line 16939:

```csharp
IResourceCompilerSystem.__N.g_pRsrcCmplrSyst_GenerateResourceFile   = nativeFunctions[1566];
IResourceCompilerSystem.__N.g_pRsrcCmplrSyst_GenerateResourceFile_1 = nativeFunctions[1567];
IResourceCompilerSystem.__N.g_pRsrcCmplrSyst_GenerateResourceBytes  = nativeFunctions[1568];
```

C ABI signatures:
```
slot 1566: (IntPtr path, IntPtr data, int size, int unknown) -> int
slot 1567: (IntPtr path, IntPtr data, int size) -> void
slot 1568: (IntPtr path, IntPtr data, int size) -> IntPtr (CUtlBuffer*)
```

The engine populates slots 1566-1568 when `libresourcecompiler.so` is loaded via
`CAppSystemDict::AddSystem("resourcecompiler", "ResourceCompilerSystem001")`.
If the library fails to load, these slots remain NULL and calling them throws.

The engine loads `libresourcecompiler.so` from its module path, which (after `\`→`/`
normalization) is `game/bin/bin/managed/` OR from `LD_LIBRARY_PATH`.

---

## IResourceCompilerSystem Vtable Layout (to implement in stub)

The engine calls `CreateInterface("ResourceCompilerSystem001")` on the loaded library,
then extracts methods and places them into nativeFunctions[1566-1568]. The vtable must
implement the full `IAppSystem` base interface plus resource compiler methods.

Expected vtable order (IAppSystem inherits first):
```
[0]  Connect(CreateInterfaceFn factory) -> bool
[1]  Disconnect() -> void
[2]  QueryInterface(const char* name) -> void*
[3]  Init() -> InitReturnVal_t  INIT_OK=1, INIT_FAILED=0  (Valve enum — opposite of POSIX)
[4]  PostInit() -> void
[5]  Shutdown() -> void
[6+] IResourceCompilerSystem-specific methods (stubs must return void* NULL, not void)
```

The engine does NOT call these vtable methods directly from managed code — it reads them
during system initialization and registers wrappers into the nativeFunctions table.

---

## Valve Resource Format (VRF) — `.shader_c` Binary Layout

Fixed 272-byte header overhead + variable SPRV block:

```
[16 bytes]  File header
  DWORD file_size        (total file size)
  WORD  hdr_version      (12)
  WORD  version          (3)
  DWORD blocks_offset    (8, relative to this field)
  DWORD block_count      (3)

[36 bytes]  Block directory (3 × 12 bytes each)
  CHAR[4] name           ("REDI", "DATA", "SPRV")
  DWORD   rel_offset     (relative to this DWORD field's own position)
  DWORD   size

[220 bytes] REDI block (resource editor info)
  Fixed template, all zeros except:
  +0x58 (from block start): 4-byte CRC fingerprint of the SPRV data

[0 bytes]   DATA block (empty for shaders)

[N bytes]   SPRV block — raw output of ShaderSource.Serialize()
```

Total fixed overhead: 272 bytes = 0x110.

The CRC at REDI+0x58 is the 4-byte fingerprint; the exact algorithm is unknown but can be
copied from an existing `.shader_c` with matching SPRV content or computed.

**Confirmed VRF math:**
- `unlit.shader_c`: 272 + 0x2890 = 10,656 bytes ✓
- `postprocess_bloom.shader_c`: 272 + 0x3D81 = 16,017 bytes ✓

---

## Shader File Locations

| Location | Contents |
|----------|----------|
| `game/core/shaders/` | Core engine shaders, `.shader` + `.shader_c` pairs |
| `game/addons/base/Assets/shaders/` | Base addon shaders |
| `game/addons/menu/Assets/shaders/` | Menu shaders |
| `game/templates/` | Template shaders (skipped by ShaderCompiler) |

---

## ShaderPreprocessor Include Resolution

1. Try `{shaderDir}/{includeFile}` relative to the including file
2. Fall back to `shaders/{includeFile}` in mounted filesystem
3. Core content includes can be ignored (`IgnoreCoreIncludes = true` during compilation)
4. Include guard via path tracking (each file included only once per compile)

---

## What the Python Launcher Does (`anvil/launch/shaders/compile_shaders.py`)

```
python3 anvil/launch/shaders/compile_shaders.py [-f] [path/to/file.shader ...]
  -f   force recompile
  (no args) = compile all stale shaders under game/
```

Steps executed each run:
1. Builds `libresourcecompiler.so` via `build_resourcecompiler.sh` if stale (checks source mtime)
   — **NOTE: `anvil/patches/libresourcecompiler.c` was deleted**; the compiled `.so` in
   `linuxsteamrt64/` is the only copy. The build step is now a no-op until the source is restored.
2. Creates `.dll` symlinks in `game/bin/linuxsteamrt64/` so `NativeLibrary.TryLoad("x.dll")` finds
   them: `vfx_vulkan.dll → libvfx_vulkan.so`, `filesystem_stdio.dll → libfilesystem_stdio.so`
3. Populates `game/bin/bin\managed/` (literal backslash dir) with a copy of `ShaderCompiler` and
   all managed `.dll` files from `game/bin/managed/`
4. Resolves shader paths to absolute paths (required — ShaderCompiler matches full paths)
5. Runs `game/bin/bin\managed/ShaderCompiler` with `LD_LIBRARY_PATH` set to `linuxsteamrt64/`
   and `LD_PRELOAD` cleared (standalone tool, no game patches)

---

## `libresourcecompiler.so` Stub — What It Does

Source (`anvil/patches/libresourcecompiler.c`) **was deleted**. The compiled binary remains at
`game/bin/linuxsteamrt64/libresourcecompiler.so`.

The stub exports `CreateInterface("ResourceCompilerSystem001")` returning an object with this
vtable:

```
[0]  Connect(factory) -> bool    (returns 1)
[1]  Disconnect() -> void
[2]  QueryInterface(name) -> void*
[3]  Init() -> int               MUST return 1 (INIT_OK). Valve enum: 0=INIT_FAILED, 1=INIT_OK
                                 (opposite of POSIX — returning 0 silently aborts startup)
[4]  PostInit() -> void
[5]  Shutdown() -> void
[6..13]  stubs — MUST return void* NULL, not void. Garbage rax → engine tries to load
                 "???" modules as dependencies and crashes.
[14] GenerateResourceBytes(this, data, size) -> CUtlBuffer*
```

`GenerateResourceBytes`:
- Receives compiled shader bytes (the managed `path` arg is overwritten by libengine2's wrapper
  before reaching the vtable; slot receives `(this, data, size)`)
- Wraps data in VRF format (272-byte header + SPRV block)
- Returns a 0x50-byte malloc'd `CUtlBuffer`-compatible struct:
  - `[0x00]` = pointer to VRF data (freed by Dispose if `grow_size ≤ 0x3FFFFFFF`)
  - `[0x0C]` = `0x40000000` — `> 0x3FFFFFFF` so Dispose skips freeing this pointer
  - `[0x20]` = VRF data size

---

## Native Include Resolution — Current Blocker

### Problem

`ShaderCompile.CompileShader()` calls `vfx.LoadFromSource(absolutePath)` — a **native call** into
`libvfx_vulkan.so` (`vfx_common.cpp`). This native call resolves `#include` directives using the
engine's `IFileSystem`, **not** OS file access. `IFileSystem` is initialized by `ToolAppSystem`
with only `game/core/` mounted. Addon shader directories are not mounted.

When compiling `game/addons/base/Assets/shaders/postprocess_bloom.shader`:
- Includes `postprocess/shared.hlsl` (relative include in COMMON block)
- Native IFileSystem searches in `game/core/postprocess/shared.hlsl` → **not found**
- `LoadFromSource` returns false → managed code logs "is it in an assets folder?" and aborts

**Note:** The managed `ShaderPreprocessor` runs *after* `LoadFromSource` and also resolves
includes, but through `ShaderCompile.FileSystem` (the managed virtual FS). The native step
must succeed first.

### Partial Fix Applied (testing)

Symlinking `game/core/postprocess → game/addons/base/Assets/shaders/postprocess/` lets the native
IFileSystem find `postprocess/shared.hlsl`. After this fix, the next error is:

```
vfx_common.cpp(5467): Error opening file "math_general.fxc"!
```

### Include chain for `postprocess_bloom.shader`

```
postprocess_bloom.shader (COMMON block)
  └─ postprocess/shared.hlsl
       ├─ system.fxc        ✅ game/core/shaders/system.fxc
       ├─ common.fxc        ✅ game/core/shaders/common.fxc
       └─ sbox_shared.fxc   ✅ game/core/shaders/sbox_shared.fxc
            └─ vr_common.fxc ✅ game/core/shaders/vr_common.fxc
                 ├─ system.fxc    ✅
                 ├─ common.fxc    ✅
                 └─ math_general.fxc  ❌ MISSING — not in repo
```

### Missing `.fxc` Files

These files are referenced by core `.fxc` includes but do **not** exist in the public repo:

| File | Referenced by |
|------|---------------|
| `math_general.fxc` | `vr_common.fxc`, `vr_environment_map.fxc`, `vr_common_vs_code.fxc`, `vr_common_ps_code.fxc` |
| `instancing.fxc` | `vr_common_vs_code.fxc` |
| `vs_decompress.fxc` | `vr_common_vs_code.fxc` |
| `morph.fxc` | `vr_common_vs_code.fxc` |
| `transform_buffer.fxc` | `skinning_cs.shader` |
| `octohedral_encoding.fxc` | `skinning_cs.shader` |

These are the VFX standard library headers. The compiled `.shader_c` files in `game/core/shaders/`
(e.g. `skinning_cs.shader_c`) were built on Windows where the full SDK was available. On Linux
with only the public repo, the sources are absent.

**Origin hypothesis:** These files are part of a private Steam depot or internal SDK that is not
distributed with the public repo. They are NOT embedded in `libvfx_vulkan.so` (confirmed via
`strings`). They must be present on disk for `vfx_common.cpp` to read during `LoadFromSource`.

### Candidate Fix Approaches

1. **Extract from existing `.shader_c` files**: non-core shaders embed HLSL source in the SPRV
   block (JSON dict). Could reconstruct include chain content this way.
2. **Locate via Steam depot / VPK**: check if a downloadable depot provides these files.
3. **Create stubs**: minimal placeholder `.fxc` files so `LoadFromSource` can parse structure
   without full semantics — risky if native code evaluates content, not just presence.

---

## Remaining Work

1. **Missing `.fxc` files** — highest priority blocker. Need `math_general.fxc` and related
   files in `game/core/shaders/` for `LoadFromSource` to succeed on any shader that uses
   `vr_common.fxc` (which is most shaders).

2. **Addon include mounting** — the Python script needs to automatically symlink addon shader
   directories into `game/core/` so the native IFileSystem can find them. Currently done
   manually (testing only).

3. **`libresourcecompiler.c` source** — deleted. The compiled `.so` still exists. If the binary
   ever needs rebuilding, the C source must be recreated from the documentation above.

4. **End-to-end test** — once include issues are resolved, verify the VRF output from the stub
   produces a valid `.shader_c` that the game runtime accepts.

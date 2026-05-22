# win64 Tool Reference

All tools live in `game/bin/win64/`. All confirmed Wine-runnable unless noted.
Goal: identify which are needed on Linux, which can be eliminated, and what replaces each.

---

## contentbuilder.exe

**Purpose:** Valve Content Builder — top-level asset pipeline orchestrator. Scans all content,
resolves dependency order, and dispatches compile jobs (via subprocesses) for every asset type.
Uses a checksum-based dirty system to skip up-to-date assets.

**Wine status:** ✅ Works. Auto-detects mod from CWD (`game/bin/win64` → mod `core`).

**Key flags:**
```
-b                build out-of-date content
-lb               list out-of-date content (dry run)
-f                force recompile (ignore checksums)
-fshallow         force top-level only
-include <ext>    restrict to a resource type (e.g. -include vfx, -include vtex)
-exclude <ext>    skip a resource type
-path <path>      only build content under this path
-spewallcommands  print every subprocess command it runs (useful for debugging)
-spewallcompiles  print stdout from every subprocess
-proc <n>         max parallel compile jobs (default 22)
-v                verbose
-game <path>      explicit path to gameinfo.gi (auto-detected from CWD if omitted)
```

**Shader relevance:** Compiles `.shader` → `.shader_c` when invoked with `-include vfx`
(or with no `-include`, as part of a full build). Calls `resourcecompiler.exe` internally
for each asset. `contentbuilder.exe -b -include vfx -f` is the all-shaders force-recompile path.

**Linux replacement:** None needed long-term — will be superseded by running `ShaderCompiler`
(Linux ELF) with `libvfx_vulkan.so` once `ToolAppSystem` Linux path is fixed. In the interim,
the Wine invocation of contentbuilder or resourcecompiler is the workaround.

---

## resourcecompiler.exe

**Purpose:** Low-level single-asset compiler. Takes one or more source files and compiles them
to their target format. Called by `contentbuilder.exe` for each individual asset. Can be invoked
directly to compile specific files without the full content pipeline.

**Wine status:** ✅ Works.

**Key flags:**
```
-i <file>         input source file (wildcards ok, can also be bare arg at end)
-filelist <file>  text file listing input files, one per line
-r                recursive search if wildcards specified
-game <path>      path to gameinfo.gi; derived from input path if omitted
-f                force recompile
-fshallow         force top-level, compile children only if needed
-v                verbose
-pc               target Windows PC (default)
```

**Shader relevance:** **Primary tool for individual shader compilation.**
Direct invocation to compile a single shader:
```bash
wine resourcecompiler.exe -i path/to/file.shader -game game/core
```
This is what the Python script should call per-shader. No need to invoke contentbuilder.

**Linux replacement:** `ShaderCompiler` (Linux ELF) + `libvfx_vulkan.so` when ToolAppSystem
Linux path is patched.

---

## dmxconvert.exe

**Purpose:** Converts DMX (Valve Data Model) files between encodings and formats. DMX is the
intermediate format used throughout the Valve asset pipeline for models, animations, maps, etc.

**Wine status:** ✅ Works.

**Key flags:**
```
-i <file>   input file
-o <file>   output file (overwrites input if omitted, for dmx-to-dmx)
-ie <enc>   input encoding hint
-oe <enc>   output encoding (keyvalues, keyvalues2, binary, binary_seqids, etc.)
-of <fmt>   output format (dmx, model, vtex, vmap, vanim, etc.)
-r          recursive on wildcards
-upconvert  auto-checkout and convert all DMX files
```

**Supported encodings:** `keyvalues`, `keyvalues2`, `keyvalues2_flat`, `keyvalues2_noids`,
`binary`, `binary_seqids`, `actbusy`, `commentary`, `vmt`, `tex_source1`

**Shader relevance:** None. Purely a model/animation/data interchange tool.

**Linux replacement:** Not needed for shader pipeline. Long-term: can be called via Wine or
replaced by an open-source DMX library if model import becomes necessary.

---

## fbx2dmx.exe

**Purpose:** Converts FBX model files to DMX format for import into the Valve model pipeline.
Intermediate step before `resourcecompiler.exe` compiles a `.vmdl`.

**Wine status:** ✅ Works.

**Key flags:**
```
-i <file>               input FBX file
-o <file>               output DMX file (defaults to same name, .fbx extension)
-a                      convert animation (default: convert model geometry)
-msp <path>             material search path for remapping materials
-up <axis>              up axis: x, y, z, -x, -y, -z (default: y)
-fp <parity>            forward parity: even, odd, -even, -odd (default: x)
-s <scale>              scale factor
-ufc                    underscores in delta names = corrective states
-v                      increase verbosity (stackable)
```

**Shader relevance:** None. Model import tool only.

**Linux replacement:** Not needed for shader pipeline. For model import, Blender's FBX export
or `assimp` can produce DMX-compatible intermediates via `dmxconvert`.

---

## obj2dmx.exe

**Purpose:** Converts Wavefront OBJ files to DMX format. Same role as `fbx2dmx.exe` but for
OBJ inputs. Feeds the `.vmdl` model compilation pipeline.

**Wine status:** ✅ Works (confirmed by help text; requires `-game` for mod context).

**Key flags:**
```
-h | -help   print help
<filename>   bare OBJ filename as positional arg
-game <mod>  specify mod (required if not inferrable from file path)
```

**Shader relevance:** None. Model import only.

**Linux replacement:** Not needed for shader pipeline. `assimp` or Blender can produce DMX
or compatible formats directly.

---

## vrad2.exe

**Purpose:** Legacy distributed lightmap baker (Source 2 generation 2). Bakes static lighting
and light probe volumes for world geometry. Takes a compiled map file as input. Part of the
world/map compilation pipeline, invoked after Hammer compiles a `.vmap`.

**Wine status:** ✅ Binary runs but requires a compiled map file + game context to do anything
useful.

**Key flags (from strings):**
```
-silent              suppress output
-interactive         interactive mode
-tests               run internal tests
-dumptrace           dump ray trace debug data
-dumppointsamples    dump point sample debug data
-dumpphotons         dump photon map debug data
-skipphotons         skip photon map pass
-sse2 / -sse3 / -sse4 / -avx   SIMD level override
```

**Shader relevance:** None. Lightmap baking only.

**Linux replacement:** Not needed unless doing world lighting bakes. If needed, run via Wine.
No open-source equivalent supports the Source 2 lightmap format.

---

## vrad3.exe

**Purpose:** Newer distributed lightmap and lighting baker (Source 2 generation 3). Script-driven
(takes a `-script <vrad3_script_file>` argument). Handles lightmap baking with path tracing,
OIDN denoising, and directional lighting. Invoked by the map compilation pipeline via a generated
script file.

**Wine status:** ✅ Binary runs. Requires `-script` arg pointing to a vrad3 script file generated
by the map compiler.

**Key flags:**
```
-script <file>   required — path to the vrad3 script file
-game <path>     gameinfo.gi path
-vulkan          force Vulkan renderer
```

**Script commands (partial):** `lightmap_size`, `lightmap_load_block`,
`lightmap_generate_samples_from_packing_geometry`, `lightmap_pathtrace`, `lightmap_directlight`,
`lightmap_image_filter_oidn`, `lightmap_image_write_*` — all lightmap baking operations.

**Shader relevance:** None. World lighting only.

**Linux replacement:** Not needed for shader pipeline. Would need Wine for map baking workflows.

---

## vsopen.exe

**Purpose:** Opens a file at a specific line in a running Visual Studio instance via the VS
automation API. Used by the editor's "open in IDE" feature when running on Windows.

**Wine status:** ⚠️ Runs but non-functional — requires a live Visual Studio COM server which
Wine's COM stack cannot provide.

**Usage:**
```
vsopen.exe <installationPath> <solutionPath> [fileName lineNumber]
```

**Shader relevance:** None. Editor IDE integration only.

**Linux replacement:** Replace with a script that invokes `code`, `rider`, or `nvim` at a
given file+line. The editor UI that calls this will need to be made platform-aware.

---

## vswhere.exe

**Purpose:** Microsoft Visual Studio Locator. Queries the VS Setup configuration store (COM)
to find installed Visual Studio instances, their paths, versions, and installed components.
Used by SboxBuild to find MSBuild and the Windows SDK.

**Wine status:** ❌ Non-functional under Wine — COM class `{177f0c4a-...}` not registered.
Returns empty output.

**Shader relevance:** None. Windows build toolchain discovery only.

**Linux replacement:** Not needed. On Linux, `dotnet` and the managed build system don't use
MSBuild or the Windows SDK. SboxBuild's `dotnet run` path already works on Linux without this.

---

## crashpad_handler.exe

**Purpose:** Sentry/Crashpad crash report handler. Spawned as a subprocess by the engine at
startup to catch unhandled exceptions and upload crash dumps to the Sentry error tracking service.
Runs as a background monitor process alongside the engine.

**Wine status:** Not applicable — this is launched by the engine process, not manually.

**Key flags:**
```
--database=PATH          crash report storage directory
--url=URL                Breakpad-compatible crash upload endpoint
--annotation=KEY=VALUE   add annotation to all reports
--attachment=FILE_PATH   attach file to reports
--monitor-self           run a second handler to catch crashes in the first
--no-rate-limit          disable upload rate limiting
--no-periodic-tasks      disable report scanning/pruning
```

**Shader relevance:** None. Crash telemetry only.

**Linux replacement:** The engine already has a Linux crash reporter path (the
`.mdmp` file at `game/bin/resourcecompiler-*.mdmp` is a Windows minidump from a prior crash).
On Linux, `sentry-native` supports the Crashpad protocol natively — no Wine needed.

---

## Summary Table

| Tool | Category | Shader relevance | Wine needed | Linux replacement |
|------|----------|-----------------|-------------|-------------------|
| `contentbuilder.exe` | Asset pipeline | **Yes (high-level)** | ✅ interim | ShaderCompiler ELF |
| `resourcecompiler.exe` | Asset compile | **Yes (direct)** | ✅ interim | ShaderCompiler ELF |
| `dmxconvert.exe` | Data interchange | None | Optional | assimp / open-source |
| `fbx2dmx.exe` | Model import | None | Optional | Blender / assimp |
| `obj2dmx.exe` | Model import | None | Optional | Blender / assimp |
| `vrad2.exe` | Lightmap baking | None | If needed | No equivalent |
| `vrad3.exe` | Lightmap baking | None | If needed | No equivalent |
| `vsopen.exe` | IDE integration | None | ❌ broken | `code`/`rider` script |
| `vswhere.exe` | VS discovery | None | ❌ broken | Not needed on Linux |
| `crashpad_handler.exe` | Crash reporting | None | N/A | sentry-native |

**For shader compilation specifically:** only `resourcecompiler.exe` (direct) and
`contentbuilder.exe` (orchestrated) are relevant. The Python script should target
`resourcecompiler.exe` directly to compile individual `.shader` files via Wine.

# Downloaded Texture Load Failure — g_tRma / g_tRmaB

## Status
**UNSOLVED** — root cause partially understood; correct fix not yet determined.

---

## Symptom

Per-frame log spam when rendering materials with downloaded package textures:

```
[engine/RenderSystem] returning error texture resource for "g_tRmaB" in CTextureManagerVK::GetTextureResource
[engine/RenderSystem] Texture manager doesn't know about texture
    "materials/trimsheets/wood/wood_trim_weathered_rough_tga_c2c187f3.generated.vtex"
    when setting "g_tRmaB" - returning error texture in CTextureManagerVK::GetImageView
```

Observed parameters: `g_tRma`, `g_tRmaB` (roughness/metalness/AO PBR slots).  
Visual result: materials render without correct roughness/metalness.

---

## What Was Ruled Out

### Hypothesis 1 — .vtex symlink alias (WRONG)

Initial theory: the render system looks up `.vtex` (source extension) in the native symlink table,
but `AddSymLink` is only ever called with `.vtex_c` (compiled extension) keys. Fix: register a
`.vtex` alias alongside every `.vtex_c` symlink in `RedirectFileSystem.AddAbsFile`.

**Why it was wrong:** The alias fix was applied and the errors continued unchanged. More importantly,
the diagnostic data showed the failing textures were never passed to `TryMount` at all — the
`.vtex_c` symlink for them was never registered, making the alias irrelevant.

### Hypothesis 2 — Case sensitivity in symlink table (WRONG)

The casemap shim (`libsbox_casemap.c`) intercepts file I/O syscalls but cannot intercept
in-memory hash table lookups in `libfilesystem_stdio.so`. However, `NormalizeFilename(true)`
already lowercases all symlink keys, and the error message paths are lowercase too. Not the issue.

---

## Actual Root Cause (Best Current Understanding)

### The failing textures are not in any mounted package's symlink table

Diagnostic: added `[SymlinkDiag]` logging to `AssetDownloadCache.TryMount` and
`RedirectFileSystem.AddAbsFile`, grepped `sbox.log` to `symlink_diag.txt`.

Results:
- `wood_trim_weathered_color_tga_3659b48a.generated.vtex_c` → TryMount DOWNLOAD, symlink registered, `exists=True`
- `wood_trim_weathered_rough_tga_c2c187f3.generated.vtex_c` → **zero entries** (TryMount never called)
- `tristaniopsis laurina/tristaniopsis laurina_vmat_g_trma_e63e070.generated.vtex_c` → **zero entries**

Physical files on disk from previous sessions:
```
game/download/assets/materials/trimsheets/wood/
    wood_trim_weathered_rough_tga_c2c187f3_generated.f108e16a37ec8df0.vtex_c   ← exists
    wood_trim_weathered_rough_tga_c2c187f3_generated.21b818dfbd19444b.vtex_c   ← exists
```

Not in core content (`/game/core/`, `/game/addons/base/Assets/`). No gamecache entries.

### Two candidate sub-causes (not yet determined which)

**Sub-cause A — Textures come via LargeNetworkFiles (server file push), CRC mismatch with cache**

`LargeNetworkFiles` is the server-file-push mechanism: the server sends specific files to the
client during gameplay. `AddFileToFileSystem` checks if the file is already mounted, then calls
`TryMount`. If `TryMount` fails (cached file has wrong CRC), the file is added to `downloadQueue`.
`RunDownloadQueue` downloads from the server and calls `AddAbsFile`. BUT:

- `RunDownloadQueue` does NOT call `ReloadSymlinkedResidentResources` after completing
- `ReloadSymlinkedResidentResources` only reloads resident (successfully loaded) resources, not
  resources that are already in an error/failed state
- If the texture was requested before the download completed, the error is cached permanently

This explains why the errors occur before the "Install Package (Already Mounted)" log: the
package is already installed but the textures come via a separate LargeNetworkFiles push, and
the download races with the first render frame.

**Sub-cause B — Textures not in any manifest for this session**

The symlink_diag was from a session (01:47:05) that may not have used the same map/content as
the error session (02:14:19). If the package serving these materials did not include the
roughness textures in its manifest, `TryMount` is never called and the textures are simply
unavailable.

### Why the same session's color texture works

The color texture (`wood_trim_weathered_color_tga_3659b48a`) IS registered:
- It appears in the symlink_diag as TryMount DOWNLOAD with `exists=True`
- This means its CRC matched the cached file → mounted before any render frame
- The roughness texture was not cached with a matching CRC (or wasn't in the manifest at all)

---

## Reload Flow (for reference)

```
ServerPackages.InstallAll()
  └─ ForEachTaskAsync( ClientInstallPackage, reloadResources: false )
       └─ DownloadAndMount → Package.Download
            ├─ TryAddToDownloadQueue → TryMount (for cached files)
            └─ ForEachTaskAsync( DownloadFileAsync )
                 └─ TryMount (after each file downloaded)
  └─ ReloadSymlinkedResidentResources()   ← called once, after all packages done

LargeNetworkFiles.RunDownloadQueue()
  └─ AddAbsFile (after each server-pushed file downloaded)
  └─ *** NO ReloadSymlinkedResidentResources call ***
```

`ReloadSymlinkedResidentResources` (nativeFunctions[1569]) reloads resources that are currently
**resident** (successfully loaded) and have registered symlinks. It does NOT retry resources
that are in an error/failed state.

---

## Candidate Fixes (not yet implemented)

**Fix A — Call ReloadSymlinkedResidentResources after LargeNetworkFiles.RunDownloadQueue**

```csharp
// LargeNetworkFiles.RunDownloadQueue — at the end, before return
NativeEngine.g_pResourceSystem.ReloadSymlinkedResidentResources();
```

Addresses Sub-cause A. Does not help if the textures are missing from manifests entirely (Sub-cause B).
Does not help if `ReloadSymlinkedResidentResources` skips error-state resources (unconfirmed).

**Fix B — Determine whether the texture is expected from LargeNetworkFiles or a package manifest**

Enable `debug_network_files` convar (`ConVarFlags.Protected`) in a test session and check whether
these specific textures appear in the network file queue. This would distinguish Sub-cause A from B.

**Fix C — Pre-cache stale textures using existing cached versions**

The cached files `f108e16a37ec8df0` and `21b818dfbd19444b` are on disk. If the server's manifest
now references a different CRC, the old cache files are useless. A fix could mount the best-CRC
matching cached file when an exact-CRC match fails (fuzzy fallback). Risky — could load stale data.

---

## Diagnostic Artifacts

- `symlink_diag.txt` (sbox-public root) — grepped [SymlinkDiag] log output from session 01:47:05
  - Contains all TryMount DOWNLOAD/MISS/CORE entries for vtex files during that session
  - 6283 entries total, all `.vtex_c`, zero `.vtex`

## Next Steps

1. Enable `debug_network_files` in-game to see if roughness textures appear in the LargeNetworkFiles queue
2. If yes: try Fix A (reload after RunDownloadQueue) and confirm textures render correctly
3. If no: the textures are simply not in any manifest → server-side content issue or missing package dependency

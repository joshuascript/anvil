# Error Texture Failures — Two Separate Issues

## Status
**Issue 1 — PROPOSED FIX (Option A not yet applied)**
**Issue 2 — SEPARATE DOC** — see `anvil/llmcontext/downloaded_texture_load_failure.md`

---

## Issue 1 — Particle Sprite Renderer: ResourceHandleToData (anonymous texture)

### Symptom

Per-frame log spam:
```
[engine/Engine] texturebase.cpp(3526):ResourceHandleToData(  ) failed! Falling back to error texture!
```

Stack:
```
at Sandbox.EngineLoop.Print
at Managed.SandboxEngine.Exports.Sandbox_EngineLoop_Print
at Sandbox.Texture.get_Size()                                   ← Texture.cs:130
at Sandbox.IBatchedParticleSpriteRenderer.ProcessParticlesDirectly  ← IBatchedParticleSpriteRenderer.cs:93
at Sandbox.SceneSpriteSystem.<UpdateParticleSprites>b__14_0
at System.Threading.Tasks.Parallel.<>c__DisplayClass19_0        ← Parallel.For
```

### Empty name → anonymous texture

The error prints `ResourceHandleToData(  )` — **empty name in the parens**. Named disk-loaded
textures always have a path. Empty name means the handle belongs to an **anonymous texture**
(created via `FindOrCreateTexture2` with `anonymous=true`).

### Call chain

`IBatchedParticleSpriteRenderer.ProcessParticlesDirectly` (line 93):
```csharp
var texture = RenderTexture ?? Texture.White;
...
var aspect = texture.Size.x / texture.Size.y;   // ← line 93, triggers failure
```

`Texture.get_Size()` → `Texture.Desc` property → `g_pRenderDevice.GetOnDiskTextureDesc(native)`:
```csharp
internal CTextureDesc Desc {
    get {
        if ( gotdesc ) return _desc;
        gotdesc = true;
        _desc = g_pRenderDevice.GetOnDiskTextureDesc( native );  // ← calls ResourceHandleToData
        return _desc;
    }
}
```

`GetOnDiskTextureDesc` calls `ResourceHandleToData()` in `librendersystemvulkan.so`. When the
handle is invalid the native code falls back gracefully but fires `SeriousWarning`, which routes
through `EngineLoop.Print` — generating a managed log entry every frame.

`texture.SequenceData` (line 78) and `texture.Index` (line 80) also call into native with the
same handle before line 93 and may have the same failure mode.

### Where does the anonymous texture come from?

`ParticleSpriteRenderer.RenderTexture`:
```csharp
return _anim.Frames[currentFrameIndex]?.Texture;
```

`Sprite.Animation.Frame` default:
```csharp
public Texture Texture { get; set; } = Texture.Transparent;
```

`Texture.Transparent` (and `White`, `Black`, `Invalid`) are anonymous, created at static init:
```csharp
// Texture.Static.cs
public static Texture Transparent { get; internal set; } =
    Create( 1, 1 ).WithData( new byte[4] { 128, 128, 128, 0 } ).Finish();
```

**Leading theory:** If a sprite frame's `Texture` is never set (stays as `Texture.Transparent`
default), AND `Texture.Transparent` has an invalid native handle on Linux (e.g., static
initializer ran before the render device was ready), the particle renderer hits this failure
every frame with an empty name.

**Alternative theory:** The sprite's frame texture WAS set from disk, but the loaded `Texture`
object has an invalid handle (eviction or load failure). However, disk-loaded textures always
have a path — the empty name rules this out unless the texture was registered without a path.

### Race condition: CopyFrom() vs Parallel.For (strongest theory)

`Texture.CopyFrom()` (called by `TryReload` on hotload, or by `ReplacementAsync`) briefly sets
`native = IntPtr.Zero` between the old handle teardown and the new handle assignment:

```csharp
internal void CopyFrom( Texture texture )
{
    if ( !native.IsNull )
    {
        var n = native;
        native = IntPtr.Zero;            // ← briefly zero here
        NativeResourceCache.Remove( ... );
        MainThread.Queue( () => n.DestroyStrongHandle() );
    }
    native = texture.native.CopyStrongHandle();  // ← restored here
    ...
}
```

The particle renderer runs in `Parallel.For` on thread pool threads (`SceneSpriteSystem`).
If a thread pool thread reads `Frame.Texture.native` (via `Desc`/`get_Size`) while the main
thread is in the zero window:

1. Thread pool: `texture.Desc` → `GetOnDiskTextureDesc(native)` — `native` is `IntPtr.Zero`
2. Native: `ResourceHandleToData(null CTextureBase*)` → failure, logs empty name, falls back
3. `SeriousWarning` fires → `EngineLoop.Print` → per-frame log spam

This explains the empty name: a null `CTextureBase*` has no registered resource path.

`Texture.Destroy()` has the same pattern — sets `native = IntPtr.Zero` then defers
`DestroyStrongHandle` to the main thread. If a `Texture` is explicitly disposed while the
particle renderer still holds a reference (e.g. the sprite is destroyed mid-frame), the same
race occurs.

**`NativeResourceCache` details:**
- `MemoryCache` with 5-second sliding expiration
- `WeakTable` (`ConcurrentDictionary<long, WeakReference>`) as fallback
- `NativeResourceCache.Clear()` is called on game reset — removes cache entries but does NOT
  call `Destroy()` on textures. Frame textures held by strong references survive.

### No validity guard in ProcessParticlesDirectly

- No `texture.IsError` check before `texture.Size`, `texture.SequenceData`, or `texture.Index`
- No `texture.MarkUsed()` call
- `RenderTexture ?? Texture.White` guards C# null but not native handle invalidity

### Native location

Binary: `game/bin/linuxsteamrt64/librendersystemvulkan.so`

Error string: `0x601868` → `"texturebase.cpp(3526):ResourceHandleToData( %s ) failed!..."`

Code site:
```
10218d:  lea    0x4ff6d4(%rip),%rdi   # loads string 0x601868
102194:  mov    %rax,%rsi
102197:  xor    %eax,%eax
102199:  call   SeriousWarning        # ← triggers EngineLoop.Print per frame
1021a5:  call   CBufferString::SetUnusable
1021ba:  jmp    1020fe               # exit with fallback
```

8 instances of this error string in the binary (texturebase.cpp lines 3486, 3526, 3546, 3566,
3587, 3610, 3623, 3636). Only 3526 appears in the particle stack trace.

### CTextureDesc layout (confirmed not the issue)

`engine/Sandbox.Engine/Core/Interop/NativeEngine/CTextureDesc.cs` has no `StructLayout`
attribute but the 28-byte managed layout matches native (verified via `nativeInit` structSizes
check at startup — boot succeeds). `ITexture` is just `IntPtr self`, no packing concern.

### Proposed fix (not yet applied — waiting on Issue 2)

**Option A — managed guard (treats symptom, not race):**
```csharp
// IBatchedParticleSpriteRenderer.ProcessParticlesDirectly, before line 78
var texture = RenderTexture ?? Texture.White;
if ( texture is null || texture.IsError ) texture = Texture.White;
```
Resolves SequenceData (line 78), Index (line 80), and Size (line 93) to a known-good texture.
Does not prevent the race — just avoids hitting native with a null handle.
NOTE: `IsError` itself calls `native.IsStrongHandleValid()` which also dereferences `native`.
If `native` is `IntPtr.Zero`, `IsStrongHandleValid()` throws NRE before `IsError` returns.
Must check `native.IsNull` first — `Texture.IsError` already does this:
`public bool IsError => native.IsNull || !native.IsStrongHandleValid() || native.IsError();`
So Option A is safe: `native.IsNull` short-circuits before any native call.

**Option B — fix the race in CopyFrom:**
Read `native` once into a local before any null-check + native call in `Texture.Desc`:
```csharp
internal CTextureDesc Desc {
    get {
        if ( gotdesc ) return _desc;
        var n = native;           // snapshot before any check
        if ( n.IsNull ) return default;
        gotdesc = true;
        _desc = g_pRenderDevice.GetOnDiskTextureDesc( n );
        return _desc;
    }
}
```
This doesn't fully solve the race (the Parallel.For could still observe stale cached `gotdesc`),
but prevents the null-pointer native call. A proper fix would require a memory barrier or
snapshotting `native` before the Parallel loop starts.

**Option C — native patch (suppress warning only):**
NOP the 5-byte `call SeriousWarning` at `0x102199` in `librendersystemvulkan.so`.
Pattern (verify unique before use):
```
48 8d 3d d4 f6 4f 00  # lea rdi, [rip+0x4ff6d4]
48 89 c6              # mov rsi, rax
31 c0                 # xor eax, eax
e8 52 32 fb ff        # call SeriousWarning  ← NOP these 5 bytes
```
Silences log spam. Does not fix the null-pointer dereference or the race.

---

## Issue 2 — Material System: Named Texture Not Found (missing .vtex symlink alias)

### Symptom

```
[engine/RenderSystem] returning error texture resource for "g_tRmaB" in CTextureManagerVK::GetTextureResource
[engine/RenderSystem] Texture manager doesn't know about texture
    "materials/trimsheets/wood/wood_trim_weathered_rough_tga_c2c187f3.generated.vtex"
    when setting "g_tRmaB" - returning error texture in CTextureManagerVK::GetImageView
```

`g_tRmaB` is the roughness/metallic/AO-B PBR material parameter. Visual result: wood trim
materials render without correct roughness/metalness.

### Root cause: .vtex symlink key never registered — only .vtex_c

`AssetDownloadCache.TryMount` is called with paths from the package manifest. Manifest paths
are always the compiled extension (e.g., `...generated.vtex_c`). `AddAbsFile` registers the
key in the native filesystem symlink table verbatim.

`CTextureManagerVK::GetImageView` looks up textures using the **source** path (`.vtex`,
no `_c` suffix). The lookup is an in-memory hash table query in `libfilesystem_stdio.so` —
not a file I/O call. `libsbox_casemap.c` cannot intercept it.

Diagnostic confirmation (via `[SymlinkDiag]` logging in `TryMount`/`AddAbsFile`):
- Every `.vtex_c` symlink is registered correctly (`exists=True`)
- **Zero** `.vtex` (source path) symlinks are ever registered
- The lookup key that `CTextureManagerVK` uses (`.vtex`) is never in the table → miss every time

### Applied Fix

In `RedirectFileSystem.AddAbsFile` — when registering a `.vtex_c` symlink, also register the
`.vtex` (source) key pointing to the same physical file:

```csharp
NativeEngine.FullFileSystem.AddSymLink( localPath[1..], "GAME", absoluteTargetFile );

// The render system looks up source paths (.vtex) but the manifest only provides compiled
// paths (.vtex_c). Register both keys so native lookups succeed.
if ( localPath.EndsWith( ".vtex_c", StringComparison.OrdinalIgnoreCase ) )
{
    var sourcePath = localPath[..^2]; // strip _c → .vtex
    NativeEngine.FullFileSystem.AddSymLink( sourcePath[1..], "GAME", absoluteTargetFile );
}
```

**Verification:** Awaiting user confirmation that wood trim materials render with correct
roughness/metalness and the `CTextureManagerVK` error is gone.

---

## Relationship Between Issues

These are **independent** failures that share the error-texture-fallback symptom:

| | Issue 1 | Issue 2 |
|---|---|---|
| Texture name in log | empty (anonymous) | full path |
| Subsystem | Particle renderer → `texturebase.cpp` | Material system → `CTextureManagerVK` |
| Layer | Managed + native render system | Native render system only |
| Likely cause | Invalid handle on anonymous texture | Package content path resolution failure |
| Visual impact | Log spam, wrong particle aspect ratio | Missing roughness on wood materials |

---

## Recommended Fix

**Issue 1 — Option A** in `IBatchedParticleSpriteRenderer.ProcessParticlesDirectly`
(`engine/Sandbox.Engine/Scene/Components/Particles/Renderers/IBatchedParticleSpriteRenderer.cs`):

```csharp
var texture = RenderTexture ?? Texture.White;
if ( texture.IsError ) texture = Texture.White;   // add this line
```

`Texture.IsError` short-circuits at `native.IsNull` before any native call, so it is safe to call
from a thread pool thread during the race window. If the handle is null or invalid, all three
downstream native calls (`SequenceData`, `Index`, `Size`) are avoided by using `Texture.White`
instead.

Not yet applied — reverted pending confirmation.

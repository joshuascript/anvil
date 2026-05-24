# prejit_native_loading — JIT, PreJIT, and native binary loading in s&box

## Overview

s&box uses .NET 10 with a bidirectional managed↔native bridge to libengine2.so.
`PreJITAsync` pre-compiles all IL on background threads to avoid gameplay hitches.
When it fires before native initialization completes, TP Workers execute engine code
paths that race with the main thread's hash table population — the root cause of the
ligmoidprime crashes.

---

## Native Binary Loading

### DllImportResolver (`engine/Sandbox.Engine/Platform/DLLImportResolver.cs`)

Registered in `Bootstrap.PreInit()` via `DLLImportResolver.SetupResolvers()`. Hooks
`NativeLibrary.SetDllImportResolver()` on all loaded assemblies and on future ones via
`AppDomain.CurrentDomain.AssemblyLoad`. Resolution maps library names to
platform-specific paths:

```
Windows:  name.dll
Linux:    lib{name}.so
macOS:    lib{name}.dylib

Full path: Path.Combine(NetCore.NativeDllPath, platformName)
```

Special case: `steam_api64` on non-Windows maps to `libsteam_api` (name only, no
extension — the OS adds `.so`). This is why the `steam_api64 DllNotFoundException`
appears in Linux logs; the fallback lookup eventually resolves it, so it is non-fatal.

### libengine2 load (`engine/Sandbox.Engine/Interop.Engine.cs`, auto-generated)

```csharp
string libName = GetNativeLibraryName("engine2")   // → "libengine2.so" on Linux
NativeLibrary.TryLoad(Path.Combine(NetCore.NativeDllPath, libName), out var nativeDll)
IntPtr nativeInitPtr = NativeLibrary.GetExport(nativeDll, "igen_engine")
```

`igen_engine` accepts:
- 83 managed callbacks (C# delegate function pointers passed to native)
- 2858 native function pointers (C++ exports that managed code calls)
- A struct-size array for marshalling validation

Orchestrated by `NetCore.InitializeInterop(gameFolder)`, called from the launcher
before any managed engine code runs.

### libmeshsystem.so / libanimationsystem.so

These are not referenced in generated interop files — they are sub-modules loaded
by libengine2.so itself at native startup via its own `dlopen`. The `failed to dlopen`
messages in the log are from the native engine reporting missing libs; they surface in
managed logs via the engine's logging bridge. Their absence on a given machine
(missing system libs or stripped retail build) is non-fatal if the engine falls
back gracefully.

---

## JIT and PreJIT

### How JIT works here

The .NET CLR JIT-compiles IL to native x86-64 on first call. Methods that call into
libengine2.so via P/Invoke are JIT-compiled on demand — the first time a managed
method is invoked, the JIT emits native code and records native→native call sites for
all P/Invoke targets already resolved.

### PreJITAsync (`engine/Sandbox.Reflection/Utility.cs`, lines 225–309)

```csharp
public static Task PreJITAsync(Assembly assembly)
{
    return Task.Run(() =>
    {
        foreach (var type in assembly.GetTypes())
        {
            foreach (var method in type.GetMethods(...))
            {
                RuntimeHelpers.PrepareMethod(method.MethodHandle, ...);
            }
        }
    }, cancellationToken);
}
```

`RuntimeHelpers.PrepareMethod` forces the JIT to compile a method immediately on
the calling thread. If the method body contains any call to a P/Invoke or engine
interop function, the JIT emits a call site and may execute a small stub that hits
the native library.

Key behaviour: it does **not** just emit code — for some interop patterns,
`PrepareMethod` will execute the method's type initializer or static constructor
if not already run, which can invoke real engine code.

### Where PreJITAsync is called

| Call site | Timing |
|-----------|--------|
| `GameInstanceDll.Create()` (line 923–925) | During `Bootstrap.PreInit()` |
| `PackageLoader` on each loaded package assembly | During package loading (can overlap with engine init) |

`Bootstrap.PreInit()` runs **before** `Bootstrap.Init()`. Engine subsystem
initialization (rendering, physics, networking, hash table population) happens
in `Bootstrap.Init()`. This means PreJITAsync TP Workers are live and compiling
engine code paths **while the main thread is still running native initialization**.

---

## Initialization Order (the race window)

```
Main thread                          .NET TP Workers (PreJITAsync)
─────────────────────────────────────────────────────────────────
NetCore.InitializeInterop()
  └─ igen_engine sets up 2858 fn ptrs

Bootstrap.PreInit()
  ├─ DLLImportResolver.SetupResolvers()
  ├─ Application.Initialize()
  ├─ Run static constructors (Sandbox.System, Sandbox.Engine)
  └─ GameInstanceDll.Create()
       └─ PreJITAsync(gameInstanceAssembly) ← spawns TP Workers
            │
            ├─ PrepareMethod(SomeEngineMethod)
            │   └─ JIT stub executes → calls libengine2.so
            │       └─ hash table lookup  ← RACE starts here

Bootstrap.Init()                              (still running PrepareMethod)
  ├─ Subsystem init
  │   ├─ native hash tables being populated
  │   └─ incremental rehash triggered by new insertions
  │
  └─ (hash table stable only after Init completes)
```

The race: a TP Worker's JIT stub hits a hash table lookup while the main thread is
in the middle of Init, inserting new entries and triggering rehashes. The native
lookup reads a capacity snapshot then reloads the array pointer; a resize between
those two loads produces a stale slot address.

---

## Why the native bounds-check patch was insufficient

The trampoline inserted two capacity guards (`cmp edx,[rbx+0x1c]` /
`cmp edx,[rbx+0x24]`). In the second crash session:

- Both guards passed — `edx` was within at least one stored capacity.
- But `rax` (the bucket array pointer at `[rbx]`) had already been replaced by
  the concurrent resize *after* the capacity check ran.
- `rax + edx*8` computed a slot past the end of the newly-allocated array.
- `mov rbx,[r13]` faulted inside our trampoline at `0x7ff7ccbc2013`; different
  hash key (`0x64dc8d0b` vs original `0x672244bb`).

The race window is between the capacity read and the array-pointer reload, not
just between the capacity read and the slot dereference. A native patch would need
to snapshot both `rax` and the capacity atomically, or use a retry loop — neither
is practical without locking.

## Fix applied (2026-05-22)

`engine/Sandbox.Reflection/Utility.cs` — `PreJITAsync` gates on `_engineReady`
(volatile bool). When false, the assembly is enqueued and `Task.CompletedTask`
is returned. `SignalEngineReady()` sets the flag and drains the queue.

`engine/Sandbox.Engine/Core/Bootstrap.cs` — `ReflectionUtility.SignalEngineReady()`
called after `IGameInstanceDll.Current.Initialize()` returns in `Bootstrap.Init()`.

**Confirmed fixed** — all PreJIT errors stopped after the change.

## Alternate solutions considered

| Approach | Feasibility | Notes |
|----------|-------------|-------|
| Move call sites to `Initialize()` | Done (partially — gate covers both sites) | Only fixes `GameInstanceDll`; `PackageLoader` still needed separate handling |
| Gate in `ReflectionUtility` (current fix) | Done | Handles all call sites; no native access needed |
| Native barrier — engine signals managed when ready | Blocked (private source) | Most precise; eliminates approximation of "ready" |
| Fix hash table to be thread-safe natively | Blocked (private source) | Best long-term fix — see below |
| ReadyToRun / NativeAOT | Impractical | Conflicts with hotloading and dynamic reflection |
| Fixed delay | Fragile | Hardware-dependent; strictly worse than the gate |

### Best long-term fix

Fixing the native hash table to be thread-safe is the correct solution regardless
of PreJIT. The race is an engine bug independent of any managed feature — any
future work that runs managed code on background threads during startup will hit
it again. The gate protects against PreJIT specifically; a different TP Worker
doing something else could expose the same race.

The gate is the right fix given that native sources are private. The ideal
outcome is an upstream bug report to Facepunch with enough detail to reproduce
and fix it natively. The analysis here covers everything needed:

- Crash PC and faulting instruction (`libengine2.so+0x15bc0c0`, `mov rbx,[r13]`)
- The two-load race: capacity snapshot at `[rbx+0x1c]` / `[rbx+0x24]`, array
  pointer reload at `[rbx]` — resize between the two loads produces a stale slot
- Consistent hash key `0x672244bb` identifying the specific lookup
- Trigger: `PreJITAsync` on `.NET TP Worker` racing main-thread hash table init
- Managed log timeline showing the 400ms race window after steam auth ticket

## Tradeoff: reduced PreJIT head start

Previously `PreJITAsync` fired during `PreInit` and ran in parallel with the
entire `Init` phase, giving it the maximum overlap with the loading screen.
Now it fires at the end of `Init`, so the head start is shorter.

If a method is called before PreJIT has compiled it, the CLR JIT-compiles it
on-demand on the calling thread — exactly the hitch PreJIT was designed to
prevent. This is a real tradeoff, but acceptable in practice:

- The menu renders after `Init` and provides additional warm-up time before
  the user can enter a game.
- The three assemblies queued in `GameInstanceDll.Create()` are core engine
  code. Methods in those assemblies that would cause hitches tend to be called
  early enough that normal execution paths have already JIT-compiled them.
- `PackageLoader.AddAssembly()` calls during runtime and hotloads are
  unaffected — `_engineReady` is true by then and they fire immediately.
- The users most likely to notice a shorter PreJIT window (very slow hardware,
  fast game entry) are the same users who were crashing before the fix. Net
  outcome is still better for them.

---

## Key files

| File | Role |
|------|------|
| `engine/Sandbox.Reflection/Utility.cs:225` | `PreJITAsync` — spawns TP Worker JIT pass |
| `engine/Sandbox.Engine/Platform/DLLImportResolver.cs` | P/Invoke name resolution, `steam_api64` mapping |
| `engine/Sandbox.Engine/Core/Interop/NetCore.cs` | Orchestrates `NativeInterop.Initialize()` |
| `engine/Sandbox.Engine/Interop.Engine.cs` | Auto-generated; loads libengine2.so, wires 83+2858 fn ptrs |
| `engine/Sandbox.Engine/Core/Bootstrap.cs` | `PreInit()` / `Init()` — subsystem init order |
| `engine/Sandbox.GameInstance/GameInstanceDll.cs:923` | Calls `PreJITAsync` during `PreInit` — too early |
| `engine/Sandbox.Engine/Services/Packages/PackageLoader.cs` | Calls `PreJITAsync` per package — also early |

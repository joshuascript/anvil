# Steam HTML Callback Struct Fix (Linux)

## Problem

On Linux, `SteamAPI_RunCallbacks()` fires HTML surface callbacks whose struct data is
marshalled via `Marshal.PtrToStructure`. The HTML callback structs in
`engine/Sandbox.Engine/Platform/Steam/Generated/SteamCallbacks.cs` originally declared
`const char*` fields as managed `string`:

```csharp
[StructLayout(LayoutKind.Sequential, Pack = Platform.StructPlatformPackSize)] // Pack = 8
internal struct HTML_ChangedTitle_t : ICallbackData
{
    internal uint UnBrowserHandle;
    internal string PchTitle; // const char *
}
```

### Why this crashes — original failure mode

`Platform.StructPlatformPackSize = 8`. With `Pack = 8`, the CLR marshaller places
`string` (an 8-byte pointer) at offset 8 after 4 bytes of padding following `UnBrowserHandle`.
But Steam's Linux IPC delivers `const char*` fields packed at offset 4 with no padding
(native Pack = 4).

The marshaller reads 8 bytes from offset 8 (4 bytes of the pointer + 4 bytes of the next
field) as a pointer, passes it to `String.Ctor(SByte*)`, which calls
`SpanHelpers.IndexOfNullByte` → SIGSEGV.

### Why the first fix (uint) was still wrong

An intermediate fix changed `string` → `uint Pch*_Ptr` + computed property, with `Pack = 4`.
With `Pack = 4`, a `uint` at offset 4 reads **only the low 32 bits** of the native 64-bit
`const char*` pointer. The native struct uses Pack = 4 alignment but still stores full
64-bit pointers:

```
Native layout (Pack = 4, 64-bit pointer):
  offset 0: uint32_t unBrowserHandle    (4 bytes)
  offset 4: const char* pchTitle        (8 bytes, 4-byte aligned — no padding gap)
```

So `uint PchTitle_Ptr` at offset 4 captured `0x876b3690` — the low 32 bits of the actual
pointer, e.g. `0x00007fff????876b3690`. Zero-extended to 64-bit this is an unmapped address →
`PtrToStringAnsi` crashes in `IndexOfNullByte`.

## Correct Fix

For each affected struct:

1. **`Pack = 4`** — matches the native 4-byte alignment used by Steam's Linux IPC.
   Do NOT change `StructPlatformPackSize` globally; that would break structs with `uint64` fields.

2. **`internal ulong PchFoo_Ptr`** — reads the full 8-byte pointer at the Pack=4 aligned
   offset. `ulong` with `Pack = 4` is placed at the next 4-byte boundary, same as native.

3. **`(IntPtr)(long)PchFoo_Ptr`** — converts `ulong` → `long` (same bits) → `IntPtr`
   for passing to `Marshal.PtrToStringAnsi`.

```csharp
// Final correct form
[StructLayout(LayoutKind.Sequential, Pack = 4)]
internal struct HTML_ChangedTitle_t : ICallbackData
{
    internal uint UnBrowserHandle;
    internal ulong PchTitle_Ptr; // const char * — full 64-bit pointer, 4-byte aligned
    internal string PchTitle => System.Runtime.InteropServices.Marshal.PtrToStringAnsi(
        (IntPtr)(long)PchTitle_Ptr );
    ...
}
```

### Struct size with correct fix

For `HTML_ChangedTitle_t` (single string field):
- `uint` (4) + `ulong` (8) = 12 bytes at Pack=4

For `HTML_StartRequest_t` (three string fields + bool):
- `uint` (4) + `ulong`×3 (24) + `bool` (1) + 3 padding = 32 bytes at Pack=4

## Affected Structs

All 13 HTML callback structs in `SteamCallbacks.cs`:

| Struct | String fields |
|--------|--------------|
| `HTML_StartRequest_t` | `PchURL`, `PchTarget`, `PchPostData` |
| `HTML_URLChanged_t` | `PchURL`, `PchPostData`, `PchPageTitle` |
| `HTML_FinishedRequest_t` | `PchURL`, `PchPageTitle` |
| `HTML_OpenLinkInNewTab_t` | `PchURL` |
| `HTML_ChangedTitle_t` | `PchTitle` |
| `HTML_LinkAtPosition_t` | `PchURL` |
| `HTML_JSAlert_t` | `PchMessage` |
| `HTML_JSConfirm_t` | `PchMessage` |
| `HTML_FileOpenDialog_t` | `PchTitle`, `PchInitialFile` |
| `HTML_NewWindow_t` | `PchURL` |
| `HTML_StatusText_t` | `PchMsg` |
| `HTML_ShowToolTip_t` | `PchMsg` |
| `HTML_UpdateToolTip_t` | `PchMsg` |

## DO NOT change Platform.StructPlatformPackSize globally

The issue is field-type-specific (Pack=4 alignment + 64-bit IPC pointers for string fields),
not a global pack size issue. Changing `StructPlatformPackSize` would break structs with
`uint64` fields that rely on 8-byte alignment.

## Verification

- Session `20260519_215348` (before any fix): 7 SIGSEGVs, fatal `AccessViolationException`
  in `CSTRMarshaler` / `HTML_ChangedTitle_t`
- Session `20260519_221443` (uint fix): `CSTRMarshaler` crash gone, but `IndexOfNullByte`
  persists at `rdi=0x876b3690` — the low 32 bits of a truncated pointer
- Session `20260519_230729` (ulong fix): HTML callback crashes absent; engine proceeds to
  unrelated `libanimationsystem.so` crash ~70 seconds later

## File Changed

`engine/Sandbox.Engine/Platform/Steam/Generated/SteamCallbacks.cs`

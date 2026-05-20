# SDL & Platform Window Registration — Linux Binary Scan

Scanned: `game/bin/linuxsteamrt64/`  
Date: 2026-05-19

Signatures for `Plat_*` functions are reverse-engineered from x86-64 SysV ABI register usage (rdi/rsi/rdx/rcx/r8/r9, xmm0 for first float arg). SDL3 signatures match the upstream SDL3 public API.

---

## SDL Symbol Density by Binary

| Binary | SDL Symbols |
|--------|-------------|
| `libengine2.so` | 3267 |
| `libtier0.so` | 3270 |
| `librendersystemvulkan.so` | 3264 |
| `libsteam_api.so` | 1 (unrelated) |
| `librendersystemempty.so` | 1 (unrelated) |

`librendersystemvulkan.so` only re-exports SDL stubs — no unique platform symbols.  
Focus targets: **`libtier0.so`** and **`libengine2.so`**.

---

## Architecture Summary

- **`libtier0.so`** owns the live SDL3 instance. It exports the full SDL3 public API as stubs that dispatch into the real implementation inside `libengine2.so`. It also owns the entire `Plat_*` platform window management API.
- **`libengine2.so`** contains the real SDL3 implementation (functions suffixed `_REAL`), all display backend code (X11, Wayland, KMSDRM, OFFSCREEN), and imports `Plat_FindOrCreateWrappedPlatWindow` / `Plat_OsSpecificHandleToPlatWindow` from libtier0 as `U` (undefined — linked at runtime).

---

## Currently Exposed in Managed Code

### DisplaySurface — P/Invoke via libtier0

Split across two partial class files in `Sandbox.Engine/Platform/Display/`:

**`DisplaySurface.cs`** — window discovery, core fields, `FindGameWindow()`
```csharp
IntPtr*  SDL_GetWindows( out int count )
uint     SDL_GetWindowProperties( IntPtr window )
IntPtr   SDL_GetPointerProperty( uint props, string name, IntPtr defaultValue )
long     SDL_GetNumberProperty( uint props, string name, long defaultValue )
void     SDL_free( IntPtr mem )
```

**`DisplaySurface.Input.cs`** — input methods: `SetMouseGrab()`, `SetTextInput()`, `WarpToCenter()`
```csharp
bool     SDL_SetWindowRelativeMouseMode( IntPtr window, bool enabled )
bool     SDL_SetWindowMouseGrab( IntPtr window, bool grabbed )
bool     SDL_SetWindowMouseRect( IntPtr window, ref SDL_Rect rect )
bool     SDL_SetWindowMouseRect( IntPtr window, IntPtr rect )          // null overload
void     SDL_GetWindowSizeInPixels( IntPtr window, out int w, out int h )
void     SDL_GetWindowSize( IntPtr window, out int w, out int h )
void     SDL_WarpMouseInWindow( IntPtr window, float x, float y )
bool     SDL_StartTextInput( IntPtr window )
bool     SDL_StopTextInput( IntPtr window )
```

`LinuxDisplay.cs` also lives in `Sandbox.Engine/Platform/Display/`.

### Interop.Engine.cs — vtable slots (nativeFunctions[])

```csharp
void   Plat_ScreenToWindowCoords( IntPtr hwnd, ref int x, ref int y )   // [1625]
void   Plat_WindowToScreenCoords( IntPtr hwnd, ref int x, ref int y )   // [1626]
void   Plat_MessageBox( IntPtr title, IntPtr message )                  // [1627]
bool   Plat_GetDesktopResolution( int nMonitorIndex, ref int pWidth, ref int pHeight, ref uint pRefreshRate )  // [1628]
int    Plat_GetDefaultMonitorIndex()                                    // [1629]
bool   Plat_SafeRemoveFile( IntPtr file )                               // [1630]
void   Plat_SetModuleFilename( IntPtr filename )                        // [1631]
void   Plat_SetCurrentDirectory( IntPtr filename )                      // [1632]
ulong  Plat_GetCurrentFrame()                                           // [1633]
void   Plat_SetCurrentFrame( ulong nFrame )                             // [1634]
void   Plat_ChangeCurrentFrame( long nDelta )                           // [1635]
bool   Plat_IsRunningOnCustomerMachine()                                // [1636]
bool   Plat_HasClipboardText()                                          // [1637]
void   Plat_SetClipboardText( IntPtr text )                             // [1638]
IntPtr Plat_GetClipboardText()                                          // [1639]
void   Plat_ClearClipboardText()                                        // [1640]
void   Plat_SetNoAssert()                                               // [1644]
void   Plat_ExitProcess( int exitCode )                                 // [1645]
```

---

## Unexposed — Platform Window Registration (libtier0.so)

Signatures derived from x86-64 SysV register saves in function prologues. `PlatWindow_t*` is an opaque handle (`IntPtr` in C#). Confidence noted per function.

### Handle Conversion — most critical for Linux input registration

```c
// 0x14bb80 — wraps an OS handle in a PlatWindow_t; creates one if none exists.
// libengine2.so imports this as U (linked at runtime).
// rdi=osHandle saved, rdx→r13, rcx→r12; calls Plat_OsSpecificHandleToPlatWindow(rdi) first.
// Args 3+4 are likely type/display-context params (e.g. PlatWindowType_t enum, display conn ptr).
// Confidence: HIGH for arg1; MEDIUM for args 2-4.
PlatWindow_t* Plat_FindOrCreateWrappedPlatWindow(
    void*  osHandle,       // wl_surface* on Wayland, XID on X11
    int    ePlatWindowType,
    void*  pDisplayConn,
    void*  pReserved
);

// 0x14bb00 — lookup only, no allocation; returns null if not found.
// Single rdi tested before map lookup.
// Confidence: HIGH.
PlatWindow_t* Plat_OsSpecificHandleToPlatWindow( void* osHandle );

// 0x14b710 — reverse: PlatWindow_t* → OS handle.
// Single rdi (PlatWindow_t*); goes through SDL main-thread guard.
// Confidence: HIGH.
void* Plat_WindowToOsSpecificHandle( PlatWindow_t* pWindow );
```

### Window Lifecycle

```c
// 0x14b840 — creates a PlatWindow_t. Saves rsi (title ptr), edx (flags), ecx, r8d, r9d,
// reads 7th arg from stack at 0x10(%rbp). Pattern identical to CreateAppWindow in Interop.Engine.cs.
// Calls TEST_BLOCK_REQUIRES_SDL_MAIN_THREAD_CONDITIONS — must be called from SDL main thread.
// Confidence: HIGH.
PlatWindow_t* Plat_CreateWindow(
    const char* pTitle,
    int         nPlatWindowFlags,
    int         x,
    int         y,
    int         w,
    int         h,
    int         nRefreshRateHz
);

// 0x14b780 — single rdi; immediately calls Plat_WindowToOsSpecificHandle(rdi).
// Confidence: HIGH.
void Plat_DestroyWindow( PlatWindow_t* pWindow );

// 0x14bf10 — stub (immediate ret on Linux); code path uses rsi/rdx for coords.
// Confidence: MEDIUM.
PlatWindow_t* Plat_GetShellWindow();
```

### Visibility & Focus

```c
// 0x14a280 — rdi=ptr, rsi=nShowCmd (tested: 0=hide, 2=minimize, else show).
// Routes to SDL_ShowWindow or SDL_MinimizeWindow based on esi.
// Confidence: HIGH.
void Plat_ShowWindow( PlatWindow_t* pWindow, int nShowCmd );

// 0x14a2e0 — single rdi; calls SDL_GetWindowFlags, checks SDL_WINDOW_SHOWN.
// Confidence: HIGH.
bool Plat_IsWindowVisible( PlatWindow_t* pWindow );

// 0x14a540 — single rdi; raises window to front.
// Confidence: HIGH.
void Plat_SetForegroundWindow( PlatWindow_t* pWindow );

// 0x14a560 — single rdi; calls SDL_GetWindowFlags.
// Confidence: HIGH.
void Plat_SetActiveWindow( PlatWindow_t* pWindow );

// 0x14a580 — single rdi; calls SDL_GetWindowFlags, checks SDL_WINDOW_INPUT_FOCUS.
// Confidence: HIGH.
bool Plat_IsWindowFocused( PlatWindow_t* pWindow );

// 0x14a5b0
bool Plat_IsScreenKeyboardShown( PlatWindow_t* pWindow );

// 0x14b6b0 — rdi=ptr, sil=bool (test %sil,%sil confirms second arg is byte/bool).
// Confidence: HIGH.
void Plat_MinimizeWindow( PlatWindow_t* pWindow, bool bMinimize );

// 0x14b6e0 — single rdi; calls SDL_GetWindowFlags, checks SDL_WINDOW_MINIMIZED.
// Confidence: HIGH.
bool Plat_IsWindowMinimized( PlatWindow_t* pWindow );

// 0x14b4e0 — single rdi; calls SDL_FlashWindow(rdi, SDL_FLASH_BRIEFLY=1).
// Confidence: HIGH.
void Plat_FlashWindow( PlatWindow_t* pWindow );

// 0x14b510 — rdi=ptr, rsi=string path. Calls fork() internally (spawns notify-send or similar).
// Confidence: MEDIUM (separate function starts immediately after Plat_FlashWindow).
void Plat_DesktopNotify( PlatWindow_t* pWindow, const char* pMessage );
```

### Window Title, Icon & Decoration

```c
// 0x14a310 — rdi→r12 (ptr), rsi saved (const char*); calls SDL_SetWindowTitle(r12, rsi).
// Goes through SDL main-thread guard.
// Confidence: HIGH.
void Plat_SetWindowTitle( PlatWindow_t* pWindow, const char* pTitle );

// 0x14a380 — wide-char variant (wchar_t*).
// Confidence: HIGH.
void Plat_SetWindowTitleW( PlatWindow_t* pWindow, const wchar_t* pTitle );

// 0x14a3e0 — rdi→r14 (ptr), rsi→rbx (path string); calls SDL_IOFromFile(rbx, "rb") to load icon.
// Confidence: HIGH.
void Plat_SetWindowIcon( PlatWindow_t* pWindow, const char* pIconPath );

// 0x14a7e0 — sil (bool), rdi (ptr). Sets SDL_SetWindowBordered.
// Confidence: HIGH.
void Plat_SetWindowBorder( PlatWindow_t* pWindow, bool bBorder );
```

### Geometry

```c
// 0x14a5d0 — rdi=ptr, rsi=x (saved to stack), rdx=y (saved to stack).
// Calls SDL_SetWindowPosition; goes through SDL main-thread guard.
// Confidence: HIGH.
void Plat_SetWindowPos( PlatWindow_t* pWindow, int x, int y );

// 0x14a650 — rdi=ptr; esi/edx/rcx/r8 used. Probable signature based on usage pattern.
// Confidence: MEDIUM.
void Plat_SetWindowSize( PlatWindow_t* pWindow, int w, int h );

// 0x14a670 — rdi→rbx (ptr), rcx→r12, r8→r13; calls SDL_GetWindowPosition then SDL_GetWindowSize.
// Four out-ptrs for x, y, w, h.
// Confidence: HIGH.
void Plat_GetWindowBounds( PlatWindow_t* pWindow, int* pX, int* pY, int* pW, int* pH );

// 0x14b0e0 — rdi=ptr, rsi=int*, rdx=int*. Early-return zeroes *rsi and *rdx on null window.
// Confidence: HIGH.
void Plat_GetWindowClientSize( PlatWindow_t* pWindow, int* pWidth, int* pHeight );

// 0x14b100 — rdi=ptr, rsi→rbx, rcx→rbx, r8→r14 (out-ptrs); calls SDL_GetWindowPosition.
// Confidence: HIGH.
void Plat_GetWindowClientBounds( PlatWindow_t* pWindow, int* pX, int* pY, int* pW, int* pH );

// 0x14bf20 — already exposed via Interop.Engine.cs vtable slot.
void Plat_WindowToScreenCoords( PlatWindow_t* pWindow, int* pX, int* pY );

// 0x14bf80 — already exposed via Interop.Engine.cs vtable slot.
void Plat_ScreenToWindowCoords( PlatWindow_t* pWindow, int* pX, int* pY );
```

### Fullscreen

```c
// 0x14a6c0 — rdi=ptr, rsi=flags (bit 0x40 and 0x80 tested).
// Calls SDL_SetWindowFullscreenMode and SDL_SetWindowFullscreen internally.
// Confidence: HIGH.
void Plat_SetWindowFullscreenState( PlatWindow_t* pWindow, int nFlags );

// 0x14a7b0 — single bool arg in dil; sets global hint string, no window handle.
// Confidence: HIGH.
void Plat_SetFullscreenMinimizeOnFocusLoss( bool bEnable );
```

### Comprehensive Reconfigure

```c
// 0x14aad0 — rdi→rbx (ptr), rsi (int, -0x80), rdx (int, -0x78), xmm0 (float!, -0x74),
// rcx→r12 (int), r8→r13 (int), r9→r14 (int). Likely a 7th stack arg too.
// xmm0 = first float arg per SysV ABI (position-independent of integer args).
// This is the "full state apply": position + size + scale + flags in one call.
// Confidence: HIGH for arg count/types; MEDIUM for exact semantic ordering.
void Plat_ReconfigureWindow(
    PlatWindow_t* pWindow,
    int           x,
    int           y,
    float         fContentsScale,
    int           w,
    int           h,
    int           nFlags
    // possibly an 8th stack arg
);
```

### Display & DPI

```c
// 0x14b160 — single rdi (ptr), returns float in xmm0.
// Early-return returns 1.0f constant on null window.
// Confidence: HIGH.
float Plat_GetWindowContentsScale( PlatWindow_t* pWindow );

// 0x14b230 — rdi=ptr, rsi→r14, rdx→r13, rcx→r12, r8→rbx (four out-ptrs), pxor xmm0.
// Four inset values (top, bottom, left, right).
// Confidence: HIGH.
void Plat_GetWindowSafeAreaInsets(
    PlatWindow_t* pWindow,
    int* pTop, int* pBottom, int* pLeft, int* pRight
);

// 0x14a820 — single rdi (ptr); calls SDL_GetDisplayForWindow then SDL_GetDisplays, returns int.
// Confidence: HIGH.
int Plat_GetWindowMonitorIndex( PlatWindow_t* pWindow );

// 0x14bfe0 — no args; checks command-line flags; returns bool (1.0f branch if true).
// Confidence: HIGH.
bool Plat_IsHighDPI();

// 0x14c070 — no args; calls Plat_IsHighDPI(), returns cached float DPI value.
// Confidence: HIGH.
float Plat_GetDPI();

// 0x14c090 — no args; clears cached DPI TLS value so next call to Plat_GetDPI recomputes.
// Confidence: HIGH.
void Plat_RefreshDPI();
```

### Monitor Enumeration

```c
// 0x14afc0 — no args; calls SDL_GetDisplays(&count), returns count.
// Confidence: HIGH.
int Plat_GetMonitorCount();

// 0x14a7f0 — no args; compares current default monitor against cached value.
// Confidence: HIGH.
bool Plat_HasDefaultMonitorIndexChanged();

// 0x14a810 — already exposed via Interop.Engine.cs vtable slot [1629].
int Plat_GetDefaultMonitorIndex();

// 0x14b020 — edi = monitor index; validates against Plat_GetMonitorCount() before setting.
// Confidence: HIGH.
void Plat_SetDefaultMonitorIndex( int nMonitorIndex );

// 0x14af40 — edi = monitor index; iterates SDL display list; returns const char*.
// Confidence: HIGH.
const char* Plat_GetMonitorName( int nMonitorIndex );

// 0x14b060 — rdi→r13 (const char*); iterates SDL_GetDisplayName + strcmp; returns int index or -1.
// Confidence: HIGH.
int Plat_GetMonitorIndexFromName( const char* pName );

// 0x14ad60 — edi saved to stack (monitor index), rsi→r13, rdx→r12 (out-ptrs), plus refresh.
// Same pattern as existing Plat_GetDesktopResolution.
// Confidence: HIGH.
bool Plat_GetMonitorResolution(
    int nMonitorIndex, int* pWidth, int* pHeight, uint* pRefreshRate
);

// 0x14ae30 — edi=monitor index (saved to -0x54), rsi→r15, rdx→r14, rcx→r13, r8→r12 (out-ptrs).
// Confidence: HIGH.
void Plat_GetMonitorBounds(
    int nMonitorIndex, int* pX, int* pY, int* pW, int* pH
);

// 0x14b180 — edi=monitor index; iterates SDL display list; returns float scale factor.
// Confidence: HIGH.
float Plat_GetMonitorContentsScale( int nMonitorIndex );

// 0x14a970 — edi=monitor index (saved to -0x54), rsi→r15, rdx→r14, rcx→r13, r8→r12.
// Same layout as Plat_GetMonitorBounds. Already partially exposed as GetDesktopResolution.
// Confidence: HIGH.
void Plat_GetDesktopBounds(
    int nMonitorIndex, int* pX, int* pY, int* pW, int* pH
);
```

### Alert

```c
// 0x14b610 — plain C linkage: rsi→rdi, rdx→rsi before UTF-32 conversion.
// Two wchar_t* args, no window handle.
void Plat_AlertUser( const wchar_t* pTitle, const wchar_t* pMessage );

// 0x14b620 — C++ mangled: _Z14Plat_AlertUserP14PlatWindow_t__PKwS2_
// Demangled: Plat_AlertUser(PlatWindow_t__*, wchar_t const*, wchar_t const*)
void Plat_AlertUser( PlatWindow_t* pWindow, const wchar_t* pTitle, const wchar_t* pMessage );
```

---

## Unexposed — SDL Functions (libtier0.so)

SDL3 public API signatures. Types map to C# as: `SDL_Window* → IntPtr`, `bool → [return] bool`, `uint → uint`, `float → float`, `int* → out int / ref int`.

### Window Identity & State

```c
uint           SDL_GetWindowID( SDL_Window* window )
SDL_Window*    SDL_GetWindowFromID( uint id )
SDL_WindowFlags SDL_GetWindowFlags( SDL_Window* window )
const char*    SDL_GetWindowTitle( SDL_Window* window )
bool           SDL_SetWindowTitle( SDL_Window* window, const char* title )
bool           SDL_GetWindowPosition( SDL_Window* window, int* x, int* y )
float          SDL_GetWindowDisplayScale( SDL_Window* window )
float          SDL_GetWindowPixelDensity( SDL_Window* window )
SDL_PixelFormat SDL_GetWindowPixelFormat( SDL_Window* window )
bool           SDL_GetWindowSafeArea( SDL_Window* window, SDL_Rect* rect )
bool           SDL_SyncWindow( SDL_Window* window )
SDL_Window*    SDL_GetWindowFromEvent( SDL_Event* event )
```

### Window Operations

```c
bool SDL_ShowWindow( SDL_Window* window )
bool SDL_HideWindow( SDL_Window* window )
bool SDL_RaiseWindow( SDL_Window* window )
bool SDL_MaximizeWindow( SDL_Window* window )
bool SDL_MinimizeWindow( SDL_Window* window )
bool SDL_RestoreWindow( SDL_Window* window )
bool SDL_FlashWindow( SDL_Window* window, SDL_FlashOperation operation )
     // SDL_FLASH_CANCEL=0, SDL_FLASH_BRIEFLY=1, SDL_FLASH_UNTIL_FOCUSED=2

bool SDL_SetWindowPosition( SDL_Window* window, int x, int y )
bool SDL_SetWindowSize( SDL_Window* window, int w, int h )
bool SDL_GetWindowMinimumSize( SDL_Window* window, int* w, int* h )
bool SDL_SetWindowMinimumSize( SDL_Window* window, int min_w, int min_h )
bool SDL_GetWindowMaximumSize( SDL_Window* window, int* w, int* h )
bool SDL_SetWindowMaximumSize( SDL_Window* window, int max_w, int max_h )

bool SDL_SetWindowBordered( SDL_Window* window, bool bordered )
bool SDL_SetWindowResizable( SDL_Window* window, bool resizable )
bool SDL_SetWindowAlwaysOnTop( SDL_Window* window, bool on_top )
bool SDL_SetWindowFocusable( SDL_Window* window, bool focusable )

bool SDL_GetWindowKeyboardGrab( SDL_Window* window )
bool SDL_SetWindowKeyboardGrab( SDL_Window* window, bool grabbed )

float SDL_GetWindowOpacity( SDL_Window* window )
bool  SDL_SetWindowOpacity( SDL_Window* window, float opacity )

SDL_Window* SDL_GetWindowParent( SDL_Window* window )
bool        SDL_SetWindowParent( SDL_Window* window, SDL_Window* parent )
bool        SDL_SetWindowModal( SDL_Window* window, bool modal )

bool SDL_SetWindowHitTest( SDL_Window* window, SDL_HitTest callback, void* callback_data )
bool SDL_SetWindowShape( SDL_Window* window, SDL_Surface* shape )

bool SDL_SetWindowFullscreen( SDL_Window* window, bool fullscreen )
const SDL_DisplayMode* SDL_GetWindowFullscreenMode( SDL_Window* window )
bool SDL_SetWindowFullscreenMode( SDL_Window* window, const SDL_DisplayMode* mode )

// Linux taskbar integration (e.g. Unity/KDE progress indicator)
bool              SDL_SetWindowProgressState( SDL_Window* window, SDL_ProgressState state )
bool              SDL_SetWindowProgressValue( SDL_Window* window, double value )
SDL_ProgressState SDL_GetWindowProgressState( SDL_Window* window )
double            SDL_GetWindowProgressValue( SDL_Window* window )
```

### Window Lifecycle

```c
SDL_Window* SDL_CreateWindow( const char* title, int w, int h, SDL_WindowFlags flags )
SDL_Window* SDL_CreateWindowWithProperties( SDL_PropertiesID props )
SDL_Window* SDL_CreatePopupWindow( SDL_Window* parent, int offset_x, int offset_y,
                                   int w, int h, SDL_WindowFlags flags )
void        SDL_DestroyWindow( SDL_Window* window )
```

### Mouse & Input

```c
SDL_Window*          SDL_GetMouseFocus()
SDL_Window*          SDL_GetKeyboardFocus()    // in libengine2 internal; libtier0 stub

SDL_MouseButtonFlags SDL_GetMouseState( float* x, float* y )
SDL_MouseButtonFlags SDL_GetGlobalMouseState( float* x, float* y )
SDL_MouseButtonFlags SDL_GetRelativeMouseState( float* x, float* y )

bool SDL_CaptureMouse( bool enabled )
bool SDL_WarpMouseGlobal( float x, float y )
bool SDL_HasMouse()
const char* SDL_GetMouseNameForID( SDL_MouseID instance_id )
```

### Events

```c
bool SDL_PollEvent( SDL_Event* event )
bool SDL_PushEvent( SDL_Event* event )
bool SDL_AddEventWatch( SDL_EventFilter filter, void* userdata )
bool SDL_HasEvents( SDL_EventType minType, SDL_EventType maxType )
void SDL_SetEventEnabled( SDL_EventType type, bool enabled )
```

### Application

```c
const char* SDL_GetPlatform()                     // returns "Linux" at runtime
bool        SDL_Init( SDL_InitFlags flags )
void        SDL_Quit()
bool        SDL_InitSubSystem( SDL_InitFlags flags )
void        SDL_QuitSubSystem( SDL_InitFlags flags )

// Windows-only; present on Linux as no-ops
bool SDL_RegisterApp( const char* name, Uint32 style, void* hInst )
void SDL_UnregisterApp()
```

---

## SDL Thread Guard (libtier0.so)

```
_ZN29_BlockRequiresSDLMainThread_tC1Ev      @ 0x14c0b0  (ctor — enters guard)
_ZN29_BlockRequiresSDLMainThread_tD1Ev      @ 0x14c0d0  (dtor — exits guard)
TEST_BLOCK_REQUIRES_SDL_MAIN_THREAD_CONDITIONS @ 0x14c120 (assertion check)
```

Functions that call `TEST_BLOCK_REQUIRES_SDL_MAIN_THREAD_CONDITIONS` in their prologue:
`Plat_CreateWindow`, `Plat_SetWindowTitle`, `Plat_SetWindowIcon`, `Plat_SetWindowPos`,
`Plat_WindowToOsSpecificHandle`.

`LinuxDisplay.RegisterInputWindow()` is called from `InputRouter.Frame()` on the game thread. If that thread is not SDL's main thread, any of the above will assert.

---

## libengine2.so — Display Backend Exports

### X11
```c
SDL_Window*  X11_CreateWindow(...)
void         X11_DestroyWindow(...)
bool         X11_ShowWindow(...)   / X11_HideWindow(...)  / X11_RaiseWindow(...)
bool         X11_MaximizeWindow(...) / X11_MinimizeWindow(...) / X11_RestoreWindow(...)
void         X11_SetWindowTitle(...) / X11_GetWindowTitle(...)
bool         X11_SetWindowPosition(...) / X11_SetWindowSize(...)
bool         X11_SetWindowMinMax(...) / X11_SetWindowAspectRatio(...)
bool         X11_SetWindowBordered(...) / X11_SetWindowResizable(...)
bool         X11_SetWindowFullscreen(...) / X11_SetWindowFocusable(...)
bool         X11_SetWindowMouseGrab(...) / X11_SetWindowKeyboardGrab(...)
bool         X11_SetWindowOpacity(...) / X11_SetWindowParent(...) / X11_SetWindowModal(...)
bool         X11_SetWindowAlwaysOnTop(...) / X11_SetWindowHitTest(...)
bool         X11_GetWindowBordersSize(...) / char* X11_GetWindowICCProfile(...)
void         X11_UpdateWindowPosition(...)
bool         X11_FlashWindow(...) / X11_SyncWindow(...)
void         X11_ShowWindowSystemMenu(...)
void         X11_UpdateWindowShape(...) / X11_UpdateWindowFramebuffer(...)
void         X11_FindWindow(...)
void         X11_InitMouse(...) / X11_QuitMouse(...)
void         X11_WarpMouseXTest(...)
void         X11_Xinput2SelectMouseAndKeyboard(...)
bool         X11_Xinput2GrabTouch(...) / X11_Xinput2UngrabTouch(...)
```

### Wayland
```c
SDL_Window*  Wayland_CreateWindow(...)
void         Wayland_DestroyWindow(...)
bool         Wayland_ShowWindow(...) / Wayland_HideWindow(...) / Wayland_RaiseWindow(...)
bool         Wayland_MaximizeWindow(...) / Wayland_MinimizeWindow(...) / Wayland_RestoreWindow(...)
void         Wayland_SetWindowTitle(...) / Wayland_SetWindowPosition(...) / Wayland_SetWindowSize(...)
bool         Wayland_SetWindowMinimumSize(...) / Wayland_SetWindowMaximumSize(...)
bool         Wayland_SetWindowBordered(...) / Wayland_SetWindowResizable(...)
bool         Wayland_SetWindowFullscreen(...) / Wayland_SetWindowFocusable(...)
bool         Wayland_SetWindowMouseGrab(...) / Wayland_SetWindowKeyboardGrab(...)
bool         Wayland_SetWindowMouseRect(...)
bool         Wayland_SetWindowOpacity(...) / Wayland_SetWindowParent(...) / Wayland_SetWindowModal(...)
bool         Wayland_SetWindowHitTest(...) / Wayland_SetWindowIcon(...)
bool         Wayland_FlashWindow(...) / Wayland_SyncWindow(...)
void         Wayland_ShowWindowSystemMenu(...)
bool         Wayland_GetWindowSizeInPixels(...) / float Wayland_GetWindowContentScale(...)
SDL_DisplayID Wayland_GetDisplayForWindow(...)
char*        Wayland_GetWindowICCProfile(...)
void         Wayland_GetColorInfoForWindow(...)
void         Wayland_AddWindowDataToExternalList(...) / Wayland_RemoveWindowDataFromExternalList(...)
void*        Wayland_GetWindowDataForOwnedSurface(...)
void         Wayland_RemoveOutputFromWindow(...)
void         Wayland_DisplayRemoveWindowReferencesFromSeats(...)
void         Wayland_SeatWarpMouse(...)
void         Wayland_InitMouse(...) / Wayland_FiniMouse(...)
```

### KMSDRM
```c
SDL_Window*  KMSDRM_CreateWindow(...)
void         KMSDRM_DestroyWindow(...)
void         KMSDRM_ShowWindow(...) / KMSDRM_HideWindow(...) / KMSDRM_RaiseWindow(...)
void         KMSDRM_MaximizeWindow(...) / KMSDRM_MinimizeWindow(...) / KMSDRM_RestoreWindow(...)
void         KMSDRM_SetWindowTitle(...) / KMSDRM_SetWindowPosition(...)
bool         KMSDRM_SetWindowSize(...) / KMSDRM_SetWindowFullscreen(...)
void         KMSDRM_InitMouse(...) / KMSDRM_QuitMouse(...)
```

### OFFSCREEN
```c
SDL_Window*  OFFSCREEN_CreateWindow(...)
void         OFFSCREEN_DestroyWindow(...)
void         OFFSCREEN_SetWindowSize(...)
```

---

## The Core Gap — Linux Window Registration

### Current managed flow (`LinuxDisplay.RegisterInputWindow`)

Called once from `Bootstrap.cs` after the display surface is ready (not from `InputRouter.Frame()`).

```
DisplaySurface.FindGameWindow()
  ├─ SDL_GetWindows()              walk SDL window list
  ├─ SDL_GetWindowProperties()     get props bag for each window
  ├─ SDL_GetPointerProperty()      extract wl_surface* (Wayland)
  ├─ SDL_GetNumberProperty()       extract XID (X11/XWayland)
  └─ caches SDL_Window* in s_window; returns raw OS handle

InputSystem.RegisterWindowWithSDL( osHandle )   passes raw OS handle
InputSystem.SetEditorMainWindow( osHandle )
InputSystem.OnEditorGameFocusChange( osHandle, true )
```

### The missing bridge

`InputSystem.RegisterWindowWithSDL` receives a raw OS handle (`wl_surface*` or X11 `XID`). The native engine input system likely works with `PlatWindow_t*`. The function that bridges this — **`Plat_FindOrCreateWrappedPlatWindow`** — exists in libtier0.so at `0x14bb80`, is actively imported by libengine2.so, but has **no managed P/Invoke binding**.

The related pair:
- `Plat_OsSpecificHandleToPlatWindow` (`0x14bb00`) — lookup without allocating
- `Plat_WindowToOsSpecificHandle` (`0x14b710`) — reverse: `PlatWindow_t*` → OS handle

These three are the unexposed managed bindings most directly implicated in Linux platform window registration.

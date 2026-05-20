"""
capture_steam_callback.py — Intercept Steam callback dispatch and dump raw struct bytes.

Source from GDB:  source /path/to/capture_steam_callback.py

Breaks on Steamworks_Dispatch_OnClientCallback, and for each callback type listed
in TARGET_TYPES, prints:
  - callback type ID and name
  - native dataSize parameter (the true native struct size Steam sends)
  - hex + ASCII dump of the raw bytes at the data pointer
  - resolved pointer-width fields (4-byte and 8-byte) at each aligned offset

Calling convention (x86-64 System V):
  RDI = int    type
  RSI = IntPtr data  (pointer to raw callback struct bytes)
  RDX = int    dataSize
  RCX = int    isServer

Usage:
  source capture_steam_callback.py
  # then run the engine; output goes to stdout and SBOX_CAPTURE_DIR (default: session log dir)

Configure TARGET_TYPES to restrict which callback types are captured.
Empty set = capture all.
"""

try:
    import gdb
except ImportError:
    raise SystemExit("Load inside GDB: source capture_steam_callback.py")

import os
import datetime

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Callback type IDs to capture. Empty = capture all.
TARGET_TYPES = {
    4503,  # HTML_StartRequest
    4505,  # HTML_URLChanged
    4508,  # HTML_ChangedTitle
    4509,  # HTML_SearchResults (no strings, sanity check)
    4516,  # HTML_LinkAtPosition
    4519,  # HTML_JSAlert
    4520,  # HTML_JSConfirm
    4521,  # HTML_FileOpenDialog
    4522,  # HTML_NewWindow
    4523,  # HTML_SetCursor
    4524,  # HTML_StatusText
    4525,  # HTML_ShowToolTip
    4526,  # HTML_UpdateToolTip
}

# Max captures before the breakpoint is disabled (avoid log flood)
MAX_CAPTURES = 40

# ---------------------------------------------------------------------------
# Type name table
# ---------------------------------------------------------------------------

_TYPE_NAMES = {
    4503: "HTML_StartRequest_t",
    4504: "HTML_CloseBrowser_t",
    4505: "HTML_URLChanged_t",
    4506: "HTML_FinishedRequest_t",
    4507: "HTML_OpenLinkInNewTab_t",
    4508: "HTML_ChangedTitle_t",
    4509: "HTML_SearchResults_t",
    4510: "HTML_CanGoBackAndForward_t",
    4511: "HTML_HorizontalScroll_t",
    4512: "HTML_VerticalScroll_t",
    4516: "HTML_LinkAtPosition_t",
    4519: "HTML_JSAlert_t",
    4520: "HTML_JSConfirm_t",
    4521: "HTML_FileOpenDialog_t",
    4522: "HTML_NewWindow_t",
    4523: "HTML_SetCursor_t",
    4524: "HTML_StatusText_t",
    4525: "HTML_ShowToolTip_t",
    4526: "HTML_UpdateToolTip_t",
    4527: "HTML_HideToolTip_t",
}

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

_out_path = None

def _out_dir():
    base = os.environ.get("SBOX_TRACE_DIR")
    if not base:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        base = os.path.join(script_dir, "..", "..", "logs")
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    d = os.path.join(base, f"callbacks_{ts}")
    os.makedirs(d, exist_ok=True)
    return d

def _log(msg, f=None):
    gdb.write(msg + "\n")
    if f:
        f.write(msg + "\n")

# ---------------------------------------------------------------------------
# Hex dump
# ---------------------------------------------------------------------------

def _hexdump(data: bytes, base_addr: int) -> str:
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i+16]
        hex_part  = " ".join(f"{b:02x}" for b in chunk)
        asc_part  = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"  {base_addr+i:#010x}  {hex_part:<47}  {asc_part}")
    return "\n".join(lines)

def _pointer_table(data: bytes, base_addr: int) -> str:
    """Show 4-byte and 8-byte reads at every 4-byte aligned offset."""
    lines = ["  offset  4-byte (uint32)    8-byte (uint64)"]
    for off in range(0, len(data), 4):
        u32 = int.from_bytes(data[off:off+4], "little") if off+4 <= len(data) else None
        u64 = int.from_bytes(data[off:off+8], "little") if off+8 <= len(data) else None
        u32s = f"{u32:#010x}" if u32 is not None else "    --    "
        u64s = f"{u64:#018x}" if u64 is not None else "       --         "
        lines.append(f"  [{off:3d}]    {u32s}    {u64s}")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Breakpoint
# ---------------------------------------------------------------------------

class _SteamCallbackCatcher(gdb.Breakpoint):

    def __init__(self, sym, out_dir):
        super().__init__(sym, gdb.BP_BREAKPOINT, internal=False)
        self.silent    = True
        self._count    = 0
        self._out_dir  = out_dir
        self._logfile  = open(os.path.join(out_dir, "captures.txt"), "w", buffering=1)
        gdb.write(f"[capture] breakpoint set on '{sym}'\n")
        gdb.write(f"[capture] output → {out_dir}/captures.txt\n")

    def stop(self):
        try:
            frame = gdb.selected_frame()
            rdi = int(frame.read_register("rdi")) & 0xFFFFFFFF  # type (int32)
            rsi = int(frame.read_register("rsi"))               # data ptr
            rdx = int(frame.read_register("rdx")) & 0xFFFFFFFF  # dataSize (int32)
        except Exception as e:
            gdb.write(f"[capture] register read failed: {e}\n")
            return False

        cb_type = rdi
        if TARGET_TYPES and cb_type not in TARGET_TYPES:
            return False  # not a type we care about — don't stop

        self._count += 1
        name = _TYPE_NAMES.get(cb_type, f"type_{cb_type}")
        ts   = datetime.datetime.now().isoformat()
        hdr  = f"\n=== {name} ({cb_type}) @ {ts}  dataPtr={rsi:#018x}  dataSize={rdx} ==="

        _log(hdr, self._logfile)

        # Read raw bytes
        try:
            inf  = gdb.selected_inferior()
            data = bytes(inf.read_memory(rsi, rdx))
            _log(_hexdump(data, rsi), self._logfile)
            _log("\nPointer-width reads at each 4-byte offset:", self._logfile)
            _log(_pointer_table(data, rsi), self._logfile)
        except Exception as e:
            _log(f"  (memory read failed: {e})", self._logfile)

        if self._count >= MAX_CAPTURES:
            _log(f"\n[capture] MAX_CAPTURES={MAX_CAPTURES} reached — disabling breakpoint", self._logfile)
            self._logfile.close()
            self.enabled = False

        return False  # never stop execution

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

if not hasattr(gdb, "_steam_callback_catcher"):
    _dir = _out_dir()
    try:
        gdb._steam_callback_catcher = _SteamCallbackCatcher(
            "Steamworks_Dispatch_OnClientCallback", _dir
        )
    except Exception as e:
        gdb.write(f"[capture] could not set breakpoint: {e}\n")
        gdb.write("[capture] the export symbol may not be visible until managed code loads.\n")
        gdb.write("[capture] try: source capture_steam_callback.py  after the engine starts.\n")
else:
    gdb.write("[capture] already installed — skipping re-source\n")

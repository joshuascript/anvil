"""
probe_wrapper_layout.py - Inspect the ISteamHTMLSurface wrapper object layout.

Waits for libengine2.so to load, then installs a breakpoint at the known
wrong-vtable function (offset 0x34d160). On each hit it dumps:
  - RDI/RSI/RDX/RCX  (self, browser_handle, url, postdata)
  - 8 x 8-byte fields of the wrapper object at RDI

Goal: identify which field of the wrapper holds the raw ISteamHTMLSurface
pointer (the one whose vtable[6] IS LoadURL). Stops after MAX_HITS.
"""

try:
    import gdb
except ImportError:
    raise SystemExit("Load inside GDB: source probe_wrapper_layout.py")

MAX_HITS    = 2
FUNC_OFFSET = 0x34d160   # crashing function in libengine2.so

_hit_count = 0


def _lib_base(lib_name: str) -> int | None:
    """Return the ELF load base (first mapping) for the named library.

    The first PT_LOAD segment (R--) maps at load_base+0.  Using the r-xp
    mapping would return load_base+p_vaddr_of_text, which is wrong for
    computing addresses from ELF VMAs.
    """
    pid = gdb.selected_inferior().pid
    try:
        with open(f"/proc/{pid}/maps") as f:
            for line in f:
                if lib_name in line:
                    return int(line.split("-")[0], 16)
    except OSError:
        pass
    return None


class _WrapperProbe(gdb.Breakpoint):
    def stop(self) -> bool:
        global _hit_count
        _hit_count += 1

        rdi = int(gdb.parse_and_eval("$rdi"))
        rsi = int(gdb.parse_and_eval("$rsi"))
        rdx = int(gdb.parse_and_eval("$rdx"))
        rcx = int(gdb.parse_and_eval("$rcx"))

        # Try to read the URL string
        url = "<unreadable>"
        if rdx:
            try:
                url = gdb.parse_and_eval(f"(char*){rdx}").string()[:120]
            except Exception:
                url = f"0x{rdx:x}"

        # Determine load_base so we can compute ELF VMAs
        load_base = _lib_base("libengine2") or 0

        gdb.write(f"\n=== WRAPPER LAYOUT HIT #{_hit_count} ===\n")
        gdb.write(f"  load_base           = 0x{load_base:016x}\n")
        gdb.write(f"  RDI  wrapper self   = 0x{rdi:016x}\n")
        gdb.write(f"  RSI  browser_handle = {rsi}  (0x{rsi:x})\n")
        gdb.write(f"  RDX  url ptr        = 0x{rdx:016x}  -> {url!r}\n")
        gdb.write(f"  RCX  postdata ptr   = 0x{rcx:016x}\n")
        gdb.write(f"\n  Wrapper object fields at RDI:\n")

        vtable_ptr = None
        for i in range(8):
            off = i * 8
            try:
                val = int(gdb.parse_and_eval(f"*(unsigned long long*)({rdi + off})"))
                elf_vma = val - load_base if load_base else 0
                gdb.write(f"    [+0x{off:02x}]  0x{val:016x}  (ELF VMA 0x{elf_vma:x})\n")
                if i == 0:
                    vtable_ptr = val
            except Exception as e:
                gdb.write(f"    [+0x{off:02x}]  <error: {e}>\n")

        # Dump vtable slots [0..7] so we can see slot 6 and its neighbours
        if vtable_ptr:
            vtable_elf = vtable_ptr - load_base if load_base else vtable_ptr
            gdb.write(f"\n  Vtable at 0x{vtable_ptr:016x}  (ELF VMA 0x{vtable_elf:x}):\n")
            for i in range(8):
                off = i * 8
                try:
                    fn = int(gdb.parse_and_eval(f"*(unsigned long long*)({vtable_ptr + off})"))
                    fn_elf = fn - load_base if load_base else fn
                    marker = "  <-- SLOT 6 (wrong fn)" if i == 6 else ""
                    gdb.write(f"    vtable[{i}]  0x{fn:016x}  (ELF VMA 0x{fn_elf:x}){marker}\n")
                except Exception as e:
                    gdb.write(f"    vtable[{i}]  <error: {e}>\n")

        gdb.write("\n")

        if _hit_count >= MAX_HITS:
            gdb.write(f"[probe] Reached {MAX_HITS} hits — disabling breakpoint.\n")
            self.enabled = False

        return False  # don't stop; let execution continue


def _on_new_objfile(event):
    if not hasattr(event, "new_objfile"):
        return
    fname = event.new_objfile.filename or ""
    if "libengine2" not in fname:
        return
    base = _lib_base("libengine2")
    if base is None:
        gdb.write("[probe] libengine2.so loaded but base address not found in /proc/maps\n")
        return
    addr = base + FUNC_OFFSET
    gdb.write(f"[probe] libengine2.so base=0x{base:x}  installing breakpoint at 0x{addr:x}\n")
    _WrapperProbe(f"*0x{addr:x}")


gdb.events.new_objfile.connect(_on_new_objfile)
gdb.write(f"[probe] Waiting for libengine2.so — will break at offset 0x{FUNC_OFFSET:x}\n")

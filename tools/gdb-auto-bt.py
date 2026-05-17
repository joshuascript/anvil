"""
gdb-auto-bt.py — GDB Python init script for automated SIGSEGV capture.

Source from GDB:  source /path/to/gdb-auto-bt.py

On every SIGSEGV:
  1. Determines whether the PC falls inside a named shared library or in an
     anonymous mmap region (JIT'd .NET code).
  2. JIT-range crashes: the .NET CLR uses SIGSEGV internally to implement
     NullReferenceException.  We capture the bt for reference but forward the
     signal back to the process so the CLR's own handler can convert it into a
     managed exception.  Without forwarding, the CLR never sees the signal and
     the same instruction faults repeatedly.
  3. Named-library crashes: a real native crash.  Capture bt and continue
     without forwarding the signal (the default) so execution resumes past the
     faulting instruction.
  4. Writes to gdb/crash_NNN.txt (numbered, relative to this script's directory).
"""
try:
    import gdb
except ImportError:
    raise SystemExit("This script must be loaded inside GDB: source gdb-auto-bt.py")

import os
import datetime

_crash_count = 0
_session_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
_out_dir = os.path.join(
    os.environ.get("SBOX_TRACE_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "_debug", "trace"),
    _session_ts
)
os.makedirs(_out_dir, exist_ok=True)


def _pc_in_named_library(pc: int) -> bool:
    """Return True if pc falls inside a loaded shared library (not JIT/anonymous).

    gdb.solib_name() covers dlopen'd libraries (e.g. librendersystemvulkan.so
    loaded by the CLR at runtime) that gdb.objfiles() misses.
    """
    try:
        name = gdb.solib_name(pc)
        if name:
            return True
    except Exception:
        pass
    try:
        for objfile in gdb.objfiles():
            for section in objfile.sections():
                if section.start <= pc < section.end:
                    return True
    except Exception:
        pass
    return False


def _on_stop(event):
    global _crash_count

    if not isinstance(event, gdb.SignalEvent) or event.stop_signal != "SIGSEGV":
        return

    _crash_count += 1
    n = _crash_count

    try:
        pc = gdb.selected_frame().pc()
        pc_str = f"0x{pc:016x}"
    except Exception:
        pc = 0
        pc_str = "unknown"

    in_library = _pc_in_named_library(pc)
    crash_kind = "native" if in_library else "jit/clr"

    path = os.path.join(_out_dir, f"crash_{n:03d}.txt")

    sections = [
        (f"=== SIGSEGV #{n}  {datetime.datetime.now().isoformat()}  PC={pc_str}  kind={crash_kind} ===\n\n", None),
        ("--- thread apply all bt ---\n", "thread apply all bt"),
        ("--- info registers ---\n",      "info registers"),
        ("--- x/16i $pc-24 ---\n",        "x/16i $pc-24"),
    ]

    with open(path, "w") as f:
        for header, cmd in sections:
            f.write(header)
            if cmd:
                try:
                    f.write(gdb.execute(cmd, to_string=True))
                except Exception as exc:
                    f.write(f"(error running '{cmd}': {exc})\n")
            f.write("\n")

    gdb.write(f"[auto-bt] crash #{n} ({crash_kind}) saved → {path}\n")

    if in_library:
        # Real native crash — resume without forwarding the signal.
        gdb.post_event(lambda: gdb.execute("continue"))
    else:
        # CLR-internal signal (NullReferenceException etc.) — forward so the
        # CLR's own signal handler can convert it to a managed exception.
        gdb.post_event(lambda: gdb.execute("signal SIGSEGV"))


gdb.events.stop.connect(_on_stop)
gdb.write("[auto-bt] handler installed — SIGSEGV backtraces → ./gdb/crash_NNN.txt\n")

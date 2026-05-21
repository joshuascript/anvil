"""
gdb-auto-bt.py — GDB Python init script for automated crash capture.

Source from GDB:  source /path/to/gdb-auto-bt.py

Handles SIGSEGV and SIGABRT:

  SIGSEGV:
    1. Determines whether the PC falls inside a named shared library or in an
       anonymous mmap region (JIT'd .NET code).
    2. JIT-range crashes: the .NET CLR uses SIGSEGV internally to implement
       NullReferenceException.  We capture the bt for reference but forward the
       signal back to the process so the CLR's own handler can convert it into a
       managed exception.  Without forwarding, the CLR never sees the signal and
       the same instruction faults repeatedly.
    3. Named-library crashes: a real native crash.  Capture bt and continue
       without forwarding the signal so execution resumes past the faulting
       instruction.

  SIGABRT:
    Triggered by abort() — e.g. free(): invalid pointer, assert failures.
    Captures bt then forwards SIGABRT so the process terminates normally.

  Writes crash_NNN.txt to SBOX_TRACE_DIR/<session>/.
"""
try:
    import gdb
except ImportError:
    raise SystemExit("This script must be loaded inside GDB: source gdb-auto-bt.py")

import os
import datetime

# Guard against being sourced more than once in a single GDB session.
# All mutable session state lives in gdb._auto_bt so it survives re-sources.
if hasattr(gdb, '_auto_bt'):
    gdb.write("[auto-bt] already installed — skipping re-source\n")
else:
    _session_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    _anvil_logs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "logs")
    _base = os.environ.get("SBOX_TRACE_DIR") or _anvil_logs
    _out_dir = os.path.join(_base, _session_ts)
    try:
        os.makedirs(_out_dir, exist_ok=True)
    except OSError:
        _out_dir = os.path.join(os.path.expanduser("~/.cache/sbox-gdb"), _session_ts)
        os.makedirs(_out_dir, exist_ok=True)

    gdb._auto_bt = {"crash_count": 0, "out_dir": _out_dir}

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
        if not isinstance(event, gdb.SignalEvent):
            return
        sig = event.stop_signal
        if sig not in ("SIGSEGV", "SIGABRT"):
            return

        gdb._auto_bt["crash_count"] += 1
        n = gdb._auto_bt["crash_count"]

        try:
            pc = gdb.selected_frame().pc()
            pc_str = f"0x{pc:016x}"
        except Exception:
            pc = 0
            pc_str = "unknown"

        if sig == "SIGABRT":
            crash_kind = "abort"
        elif _pc_in_named_library(pc):
            crash_kind = "native"
        else:
            crash_kind = "jit/clr"

        path = os.path.join(gdb._auto_bt["out_dir"], f"crash_{n:03d}.txt")

        sections = [
            (f"=== {sig} #{n}  {datetime.datetime.now().isoformat()}  PC={pc_str}  kind={crash_kind} ===\n\n", None),
            ("--- thread apply all bt ---\n", "thread apply all bt"),
            ("--- info registers ---\n",      "info registers"),
            ("--- x/16i $pc-24 ---\n",        "x/16i $pc-24"),
            ("--- info proc mappings ---\n",  "info proc mappings"),
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

        if sig == "SIGABRT":
            # Process is aborting — capture is done, forward the signal and let it die.
            gdb.post_event(lambda: gdb.execute("signal SIGABRT"))
        elif crash_kind == "native":
            gdb.post_event(lambda: gdb.execute("continue"))
        else:
            gdb.post_event(lambda: gdb.execute("signal SIGSEGV"))

    gdb.events.stop.connect(_on_stop)
    gdb.write(f"[auto-bt] handler installed — SIGSEGV backtraces → {_out_dir}\n")

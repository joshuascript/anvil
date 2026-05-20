"""
gdb-finalizeload-bt.py — captures backtraces whenever FinalizeLoadRequest()'s
depth-ordering assertion would fire.

Source from GDB:  source /path/to/gdb-finalizeload-bt.py

The script dynamically scans libengine2.so's executable segment for the
byte sequence that precedes the assertion jge, sets a silent breakpoint there,
and on each hit records:

  - pLoadingResource->ExtRefDepth  ($eax at the jge)
  - this->ExtRefDepth              (*($rbx + 0x6c))
  - full thread backtrace
  - register state

Output goes to <SBOX_TRACE_DIR>/finalizeload_<timestamp>/captures.txt.

This lets us determine whether the assertion exclusively fires on file-not-found
error paths or also on valid dependency ordering.
"""
try:
    import gdb
except ImportError:
    raise SystemExit("This script must be loaded inside GDB: source gdb-finalizeload-bt.py")

import os
import datetime

MAX_CAPTURES = 40

# Guard against re-source.
if hasattr(gdb, '_finalizeload_bt'):
    gdb.write("[finalizeload-bt] already installed — skipping re-source\n")
else:
    gdb._finalizeload_bt = {"count": 0, "log": None, "bp": None}

    # mov 0x6c(%rax),%eax ; cmp %eax,0x6c(%rbx) ; jge
    _PATTERN = bytes([0x8b, 0x40, 0x6c, 0x39, 0x43, 0x6c, 0x0f, 0x8d])
    _JGE_IN_PATTERN = 6  # 0f 8d sits at offset 6 within _PATTERN

    def _open_log():
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        anvil_logs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "logs")
        base = os.environ.get("SBOX_TRACE_DIR") or anvil_logs
        out_dir = os.path.join(base, f"finalizeload_{ts}")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "captures.txt")
        gdb._finalizeload_bt["log"] = open(path, "w", buffering=1)
        gdb.write(f"[finalizeload-bt] writing to {path}\n")

    def _find_engine2_exec_range():
        """Return (start, size) of libengine2.so's executable mapping, or None."""
        try:
            mappings = gdb.execute("info proc mappings", to_string=True)
        except Exception:
            return None
        start = end = None
        for line in mappings.splitlines():
            if "libengine2.so" not in line:
                continue
            parts = line.split()
            # permissions column (e.g. "r-xp") is second-to-last when filename is present
            perms = parts[-2] if len(parts) >= 2 else ""
            if "x" not in perms:
                continue
            try:
                s = int(parts[0], 16)
                e = int(parts[1], 16)
                if start is None or s < start:
                    start = s
                if end is None or e > end:
                    end = e
            except ValueError:
                pass
        if start is None:
            return None
        return start, end - start

    def _scan_for_jge():
        """Scan libengine2.so in inferior memory and return the jge address, or None."""
        result = _find_engine2_exec_range()
        if result is None:
            gdb.write("[finalizeload-bt] could not find libengine2.so executable mapping\n")
            return None

        base, size = result
        inf = gdb.inferiors()[0]
        chunk = 0x10000

        for off in range(0, size - len(_PATTERN), chunk):
            read_size = min(chunk + len(_PATTERN), size - off)
            try:
                data = bytes(inf.read_memory(base + off, read_size))
            except gdb.MemoryError:
                continue
            pos = data.find(_PATTERN)
            if pos != -1:
                return base + off + pos + _JGE_IN_PATTERN

        return None

    class _FinalizeLoadBP(gdb.Breakpoint):
        def stop(self):
            state = gdb._finalizeload_bt
            if state["count"] >= MAX_CAPTURES:
                return False

            state["count"] += 1
            n = state["count"]
            ts = datetime.datetime.now().isoformat()

            # At the jge:
            #   $eax = pLoadingResource->ExtRefDepth
            #   $rbx = this (current resource)
            #   *(int*)($rbx + 0x6c) = this->ExtRefDepth
            try:
                dep_depth = int(gdb.parse_and_eval("$eax"))
                cur_depth = int(gdb.parse_and_eval("*(int*)($rbx + 0x6c)"))
                depth_line = (
                    f"pLoadingResource->ExtRefDepth = {dep_depth}\n"
                    f"this->ExtRefDepth             = {cur_depth}\n"
                    f"assertion (dep > cur):         {'PASS — unexpected hit' if dep_depth > cur_depth else 'FAIL — expected'}\n"
                )
            except Exception as exc:
                depth_line = f"(could not read depth fields: {exc})\n"

            sections = [
                (f"=== FinalizeLoadRequest assert #{n}  {ts} ===\n\n{depth_line}\n", None),
                ("--- thread apply all bt ---\n", "thread apply all bt"),
                ("--- info registers ---\n",      "info registers"),
            ]

            log = state["log"]
            for header, cmd in sections:
                log.write(header)
                if cmd:
                    try:
                        log.write(gdb.execute(cmd, to_string=True))
                    except Exception as exc:
                        log.write(f"(error running '{cmd}': {exc})\n")
                log.write("\n")

            log.write("---\n\n")
            gdb.write(f"[finalizeload-bt] capture #{n} — dep_depth={dep_depth if 'dep_depth' in dir() else '?'}  cur_depth={cur_depth if 'cur_depth' in dir() else '?'}\n")

            if n >= MAX_CAPTURES:
                gdb.write("[finalizeload-bt] MAX_CAPTURES reached — breakpoint removed\n")
                self.delete()

            return False  # never stop; keep running

    def _install(objfile_name="(already loaded)"):
        if gdb._finalizeload_bt["bp"] is not None:
            return

        jge_addr = _scan_for_jge()
        if jge_addr is None:
            gdb.write("[finalizeload-bt] pattern not found in libengine2.so\n")
            return

        gdb.write(f"[finalizeload-bt] jge found at 0x{jge_addr:x} ({objfile_name})\n")
        _open_log()
        bp = _FinalizeLoadBP(f"*0x{jge_addr:x}")
        bp.silent = True
        gdb._finalizeload_bt["bp"] = bp

    def _on_new_objfile(event):
        if not hasattr(event, 'new_objfile') or event.new_objfile is None:
            return
        if "libengine2.so" not in (event.new_objfile.filename or ""):
            return
        _install(event.new_objfile.filename)

    gdb.events.new_objfile.connect(_on_new_objfile)

    # Also install immediately if libengine2.so is already loaded.
    for _of in gdb.objfiles():
        if "libengine2.so" in (_of.filename or ""):
            _install(_of.filename)
            break

    gdb.write("[finalizeload-bt] loaded — will install breakpoint when libengine2.so loads\n")

#!/usr/bin/env python3
"""
parse_crashes.py — Aggregates GDB crash logs into a single LLM-readable markdown file.

Usage:
    python3 parse_crashes.py <session_dir> [output.md]

If output.md is omitted, writes to <session_dir>/analysis.md.
"""

import argparse
import os
import re
import sys
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Frame:
    number: int
    address: str
    symbol: Optional[str]   # None when GDB reports ??
    library: Optional[str]

@dataclass
class Thread:
    gdb_id: int
    tid: str
    lwp: int
    name: str
    frames: list
    collapsed: Optional[str]  # raw "[0-4, 7, 9]" annotation if present

@dataclass
class CrashFile:
    filename: str
    signal: str
    crash_n: int
    timestamp: str
    pc: str
    kind: str
    threads: list
    registers: Optional[str]
    disasm: Optional[str]
    mappings: Optional[str]

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

RE_CRASH_HEADER = re.compile(
    r'=== (SIG\w+) #(\d+)\s+(\S+)\s+PC=(\S+)\s+kind=(\S+) ==='
)
RE_THREAD_HEADER = re.compile(
    r'^Thread (\d+) \(Thread (0x[0-9a-f]+) \(LWP (\d+)\) "([^"]+)"\):'
)
RE_FRAME = re.compile(r'^#(\d+)\s+(0x[0-9a-f]+) in (.*)')
RE_FRAME_FROM = re.compile(r'^(.*?)\s+from\s+(.+)$')
RE_COLLAPSED = re.compile(r'^\[[\d,\s\-]+\]$')


def _parse_frame(line: str) -> Optional[Frame]:
    m = RE_FRAME.match(line.strip())
    if not m:
        return None
    num, addr, rest = int(m.group(1)), m.group(2), m.group(3).strip()
    fm = RE_FRAME_FROM.match(rest)
    if fm:
        sym_part = fm.group(1).strip()
        lib = os.path.basename(fm.group(2).strip())
    else:
        sym_part, lib = rest, None
    symbol = None if sym_part.startswith('??') else sym_part
    return Frame(num, addr, symbol, lib)


def parse_file(path: str) -> Optional[CrashFile]:
    with open(path) as f:
        lines = f.readlines()

    filename = os.path.basename(path)
    signal = crash_n = timestamp = pc = kind = None
    threads: list = []
    current_thread: Optional[Thread] = None
    section = None
    reg_lines: list = []
    disasm_lines: list = []
    mapping_lines: list = []

    for raw in lines:
        line = raw.rstrip()

        m = RE_CRASH_HEADER.match(line)
        if m:
            signal, crash_n, timestamp, pc, kind = m.group(1), int(m.group(2)), m.group(3), m.group(4), m.group(5)
            continue

        if '--- thread apply all bt ---' in line:
            section = 'bt'
            continue
        if '--- info registers ---' in line:
            if current_thread:
                threads.append(current_thread)
                current_thread = None
            section = 'registers'
            continue
        if '--- x/16i $pc-24 ---' in line:
            section = 'disasm'
            continue
        if '--- info proc mappings ---' in line:
            section = 'mappings'
            continue

        if section == 'bt':
            m = RE_THREAD_HEADER.match(line)
            if m:
                if current_thread:
                    threads.append(current_thread)
                current_thread = Thread(
                    gdb_id=int(m.group(1)), tid=m.group(2), lwp=int(m.group(3)),
                    name=m.group(4), frames=[], collapsed=None,
                )
                continue
            if current_thread:
                frame = _parse_frame(line)
                if frame:
                    current_thread.frames.append(frame)
                elif RE_COLLAPSED.match(line.strip()):
                    current_thread.collapsed = line.strip()

        elif section == 'registers' and line:
            reg_lines.append(line)

        elif section == 'disasm' and line:
            disasm_lines.append(line)

        elif section == 'mappings' and line:
            mapping_lines.append(line)

    if current_thread:
        threads.append(current_thread)

    if crash_n is None:
        return None

    return CrashFile(
        filename=filename, signal=signal, crash_n=crash_n, timestamp=timestamp,
        pc=pc, kind=kind, threads=threads,
        registers='\n'.join(reg_lines) or None,
        disasm='\n'.join(disasm_lines) or None,
        mappings='\n'.join(mapping_lines) or None,
    )

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def collect_unique_calls(crash_files: list) -> OrderedDict:
    """
    Returns an OrderedDict keyed by "symbol (library)", ordered by first
    appearance across all files. Value holds count and first-seen metadata.
    """
    seen: OrderedDict = OrderedDict()
    for cf in crash_files:
        for thread in cf.threads:
            for frame in thread.frames:
                if not frame.symbol:
                    continue
                key = f"{frame.symbol} ({frame.library or 'unknown'})"
                if key not in seen:
                    seen[key] = {
                        'symbol': frame.symbol,
                        'library': frame.library,
                        'count': 0,
                        'first_file': cf.filename,
                        'first_thread': thread.name,
                        'first_frame': frame.number,
                    }
                seen[key]['count'] += 1
    return seen

# ---------------------------------------------------------------------------
# Mappings helpers
# ---------------------------------------------------------------------------

RE_MAPPING = re.compile(
    r'^\s*(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+0x[0-9a-fA-F]+\s+0x[0-9a-fA-F]+\s+\S+\s*(.*?)\s*$'
)


def _parse_mappings(raw: str) -> list:
    result = []
    for line in raw.splitlines():
        m = RE_MAPPING.match(line)
        if m:
            result.append((int(m.group(1), 16), int(m.group(2), 16), m.group(3).strip() or None))
    return result


def _lookup_pc(pc_str: str, mappings_raw: str) -> Optional[str]:
    try:
        pc = int(pc_str, 16)
    except ValueError:
        return None
    for start, end, name in _parse_mappings(mappings_raw):
        if start <= pc < end:
            return name or '[anonymous]'
    return None


# ---------------------------------------------------------------------------
# Thread grouping helpers
# ---------------------------------------------------------------------------

def _thread_fingerprint(thread: Thread) -> tuple:
    """Hashable key based on the exact frame addresses and symbols."""
    return tuple((f.address, f.symbol, f.library) for f in thread.frames)


def _compress_ids(ids: list) -> str:
    """Convert [1, 3, 4, 5, 6, 7, 9] → '1, 3-7, 9'."""
    ids = sorted(set(ids))
    if not ids:
        return ''
    ranges = []
    start = end = ids[0]
    for n in ids[1:]:
        if n == end + 1:
            end = n
        else:
            ranges.append(f"{start}-{end}" if end > start else str(start))
            start = end = n
    ranges.append(f"{start}-{end}" if end > start else str(start))
    return ', '.join(ranges)


def _is_all_unresolved(thread: Thread) -> bool:
    """True when every frame has neither a symbol nor a library attribution."""
    return all(f.symbol is None and f.library is None for f in thread.frames)


def _find_faulting_thread(cf: 'CrashFile') -> Optional[Thread]:
    """Find the thread whose frame #0 address matches the crash PC."""
    for thread in cf.threads:
        if thread.frames and thread.frames[0].address == cf.pc:
            return thread
    return None


def _variant_label(idx: int) -> str:
    """Spreadsheet-style label: 0->A, 25->Z, 26->AA, 27->AB, ..."""
    label = ''
    n = idx
    while True:
        label = chr(ord('A') + n % 26) + label
        n = n // 26 - 1
        if n < 0:
            break
    return label


def _build_global_groups(crash_files: list, show_unresolved: bool = False) -> list:
    """
    Pool threads from all crash files and group by (name, fingerprint).
    Returns list of (name, variant_label, rep_thread, total_count, all_lwps)
    in order of first appearance across all files.
    variant_label is 'A'/'B'/... when the same name has multiple distinct stacks,
    otherwise None. Uses LWP (OS thread ID) as the stable cross-file identifier.
    """
    seen_keys: list = []
    groups: dict = {}

    for cf in crash_files:
        for thread in cf.threads:
            if not show_unresolved and _is_all_unresolved(thread):
                continue
            key = (thread.name, _thread_fingerprint(thread))
            if key not in groups:
                seen_keys.append(key)
                groups[key] = {'rep': thread, 'lwps': []}
            groups[key]['lwps'].append(thread.lwp)

    name_variant_count: dict = defaultdict(int)
    for name, _ in seen_keys:
        name_variant_count[name] += 1

    name_next_idx: dict = defaultdict(int)
    result = []
    for key in seen_keys:
        name, _ = key
        g = groups[key]
        if name_variant_count[name] > 1:
            idx = name_next_idx[name]
            name_next_idx[name] += 1
            variant: Optional[str] = _variant_label(idx)
        else:
            variant = None
        result.append((name, variant, g['rep'], len(g['lwps']), g['lwps']))

    return result


# ---------------------------------------------------------------------------
# Markdown generation
# ---------------------------------------------------------------------------

def _frame_md(frame: Frame) -> str:
    sym = f"**{frame.symbol}**" if frame.symbol else '`??`'
    lib = f" - `{frame.library}`" if frame.library else ''
    return f"  - `#{frame.number}` `{frame.address}` -> {sym}{lib}"


def generate_markdown(crash_files: list, unique_calls: OrderedDict, session_dir: str, show_unresolved: bool = False) -> str:
    out = []

    out += [
        "# Crash Session Analysis",
        "",
        f"**Session:** `{session_dir}`",
        f"**Files:** {', '.join(f'`{cf.filename}`' for cf in crash_files)}",
        f"**Total crashes:** {len(crash_files)}",
        f"**Unique named calls:** {len(unique_calls)}",
        "",
        "---",
        "",
    ]

    # Unique calls table
    out += [
        "## Unique Named Calls - order of first appearance",
        "",
        "| # | Symbol | Library | Occurrences | First seen |",
        "|---|--------|---------|-------------|------------|",
    ]
    for i, (_, info) in enumerate(unique_calls.items(), 1):
        lib = f"`{info['library']}`" if info['library'] else '-'
        first = (
            f"`{info['first_file']}` / "
            f"thread `{info['first_thread']}` / "
            f"frame `#{info['first_frame']}`"
        )
        out.append(f"| {i} | `{info['symbol']}` | {lib} | {info['count']} | {first} |")

    out += ["", "---", ""]

    # Build global groups once so crash events can cross-reference them
    global_groups = _build_global_groups(crash_files, show_unresolved)

    # Fingerprint -> display label lookup for cross-referencing
    fp_to_label: dict = {}
    for name, variant, rep, count, lwps in global_groups:
        label = f"`{name}`" + (f" (variant {variant})" if variant else "")
        fp_to_label[_thread_fingerprint(rep)] = label

    # Crash events — one per file, ordered by capture time
    out += ["## Crash Events", ""]

    for cf in crash_files:
        faulting = _find_faulting_thread(cf)
        out.append(
            f"### {cf.signal} #{cf.crash_n} - {cf.timestamp} @ `{cf.pc}` ({cf.kind})"
        )
        out.append("")
        if cf.mappings:
            lib = _lookup_pc(cf.pc, cf.mappings)
            if lib:
                out.append(f"**PC library:** `{os.path.basename(lib)}`  (`{lib}`)")
                out.append("")
        if faulting:
            chain_label = fp_to_label.get(_thread_fingerprint(faulting), "unknown")
            out.append(
                f"**Faulting thread:** `{faulting.name}` (LWP {faulting.lwp})"
            )
            out.append(
                f"**Call chain:** see Thread Call Chains -> {chain_label}"
            )
            out.append("")
        else:
            out.append("*(faulting thread not identified)*")
            out.append("")
        if cf.registers:
            out += ["#### Registers", "", "```", cf.registers, "```", ""]

    out += ["---", ""]

    # Global thread call chains (all files pooled, deduplicated by fingerprint)
    out += ["## Thread Call Chains", ""]

    for name, variant, rep, count, lwps in global_groups:
        label = f"`{name}`" + (f" (variant {variant})" if variant else "")
        if count == 1:
            header = f"### {label} - LWP {lwps[0]}"
        else:
            header = f"### {label} - **{count} instances**, LWPs: [{_compress_ids(lwps)}]"
        out.append(header)
        for frame in rep.frames:
            out.append(_frame_md(frame))
        if rep.collapsed:
            out.append(f"  - *(pattern also applies to threads {rep.collapsed})*")
        out.append("")

    # Disassembly appendix — deduplicated if identical across all files
    disasms = [cf.disasm for cf in crash_files if cf.disasm]
    if disasms:
        out += ["---", "", "## Appendix - Disassembly", ""]
        if len(set(disasms)) == 1:
            out += [
                "### PC - 24 (same across all crash events)", "",
                "```asm", disasms[0], "```", "",
            ]
        else:
            for cf in crash_files:
                if cf.disasm:
                    out.append(f"### {cf.signal} #{cf.crash_n} - PC - 24")
                    out += ["", "```asm", cf.disasm, "```", ""]

    # Mappings appendix — deduplicated if identical across all files
    all_mappings = [cf.mappings for cf in crash_files if cf.mappings]
    if all_mappings:
        out += ["---", "", "## Appendix - Process Mappings", ""]
        if len(set(all_mappings)) == 1:
            out += [
                "### Address map (same across all crash events)", "",
                "```", all_mappings[0], "```", "",
            ]
        else:
            for cf in crash_files:
                if cf.mappings:
                    out.append(f"### {cf.signal} #{cf.crash_n} - Address map")
                    out += ["", "```", cf.mappings, "```", ""]

    return '\n'.join(out)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Aggregate GDB crash logs into a single markdown file.")
    parser.add_argument("session_dir", help="Directory containing crash .txt files")
    parser.add_argument("output", nargs="?", help="Output markdown path (default: <session_dir>/analysis.md)")
    parser.add_argument("--show-unresolved", action="store_true",
                        help="Include threads where every frame has no symbol and no library attribution")
    args = parser.parse_args()

    session_dir = os.path.abspath(args.session_dir)
    output_path = args.output or os.path.join(session_dir, "analysis.md")

    crash_re = re.compile(r'^crash_(\d+)\.txt$')
    txt_files = [f for f in os.listdir(session_dir) if f.endswith('.txt')]
    numbered = sorted(
        [f for f in txt_files if crash_re.match(f)],
        key=lambda f: int(crash_re.match(f).group(1)),
    )
    others = sorted(f for f in txt_files if not crash_re.match(f))
    # non-crash files first (reference/sample data), then crash_NNN in order
    all_names = others + numbered

    crash_files = []
    for fname in all_names:
        cf = parse_file(os.path.join(session_dir, fname))
        if cf:
            crash_files.append(cf)
        else:
            print(f"[warn] skipped {fname} (no crash header)", file=sys.stderr)

    if not crash_files:
        print("No parseable crash files found.", file=sys.stderr)
        sys.exit(1)

    unique_calls = collect_unique_calls(crash_files)
    md = generate_markdown(unique_calls=unique_calls, crash_files=crash_files, session_dir=session_dir, show_unresolved=args.show_unresolved)

    with open(output_path, 'w') as f:
        f.write(md)

    print(f"Written → {output_path}")
    print(f"  {len(crash_files)} file(s), {len(unique_calls)} unique named calls")


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
find_funcs.py - Find function start addresses in an x86-64 ELF binary.

Scans for endbr64 (F3 0F 1E FA) as a function-entry marker.
Optionally resolves each address against the nearest exported symbol.

Usage:
    python3 find_funcs.py <binary> [--start 0xOFFSET] [--end 0xOFFSET]
                                   [--near 0xOFFSET]   # show N funcs around a target
                                   [--window 10]        # how many either side (default 5)
                                   [--resolve]          # match against exported symbols

Output: hex file offsets, one per line.
"""

import argparse
import struct
import sys


ENDBR64 = b'\xf3\x0f\x1e\xfa'


def find_endbr64(data: bytes, start: int, end: int) -> list[int]:
    positions = []
    pos = start
    while pos < end:
        idx = data.find(ENDBR64, pos, end)
        if idx == -1:
            break
        positions.append(idx)
        pos = idx + 1
    return positions


def load_exports(data: bytes) -> list[tuple[int, str]]:
    """
    Parse ELF dynamic symbol table and return list of (file_offset, name)
    for all defined function symbols, sorted by file_offset.
    """
    # Parse ELF header
    if data[:4] != b'\x7fELF':
        return []

    e_shoff     = struct.unpack_from('<Q', data, 0x28)[0]
    e_shentsize = struct.unpack_from('<H', data, 0x3A)[0]
    e_shnum     = struct.unpack_from('<H', data, 0x3C)[0]
    e_shstrndx  = struct.unpack_from('<H', data, 0x3E)[0]

    # Get section name string table offset
    shstr_offset = struct.unpack_from('<Q', data, e_shoff + e_shstrndx * e_shentsize + 0x18)[0]

    def section(idx):
        b = e_shoff + idx * e_shentsize
        name_off = struct.unpack_from('<I', data, b)[0]
        sh_type   = struct.unpack_from('<I', data, b + 4)[0]
        sh_offset = struct.unpack_from('<Q', data, b + 24)[0]
        sh_size   = struct.unpack_from('<Q', data, b + 32)[0]
        sh_entsize= struct.unpack_from('<Q', data, b + 56)[0]
        sh_link   = struct.unpack_from('<I', data, b + 40)[0]
        name = data[shstr_offset + name_off:].split(b'\x00')[0].decode('utf-8', errors='replace')
        return name, sh_type, sh_offset, sh_size, sh_entsize, sh_link

    dynsym_off = dynsym_size = dynsym_ent = 0
    dynstr_off = 0

    for i in range(e_shnum):
        name, sh_type, sh_offset, sh_size, sh_entsize, sh_link = section(i)
        if sh_type == 11:  # SHT_DYNSYM
            dynsym_off, dynsym_size, dynsym_ent = sh_offset, sh_size, sh_entsize
            # linked string table
            _, _, dynstr_off, _, _, _ = section(sh_link)
        if sh_type == 3 and name == '.dynstr':
            dynstr_off = sh_offset

    if not dynsym_off:
        return []

    # Find PT_LOAD segments to compute file offset from virtual address
    e_phoff     = struct.unpack_from('<Q', data, 0x20)[0]
    e_phentsize = struct.unpack_from('<H', data, 0x36)[0]
    e_phnum     = struct.unpack_from('<H', data, 0x38)[0]

    loads = []
    for i in range(e_phnum):
        b = e_phoff + i * e_phentsize
        p_type   = struct.unpack_from('<I', data, b)[0]
        p_offset = struct.unpack_from('<Q', data, b + 8)[0]
        p_vaddr  = struct.unpack_from('<Q', data, b + 16)[0]
        p_filesz = struct.unpack_from('<Q', data, b + 32)[0]
        if p_type == 1:  # PT_LOAD
            loads.append((p_vaddr, p_offset, p_filesz))

    def vaddr_to_file(va):
        for vaddr, foff, fsz in loads:
            if vaddr <= va < vaddr + fsz:
                return foff + (va - vaddr)
        return None

    results = []
    n = dynsym_size // dynsym_ent
    for i in range(n):
        b = dynsym_off + i * dynsym_ent
        st_name  = struct.unpack_from('<I', data, b)[0]
        st_info  = data[b + 4]
        st_value = struct.unpack_from('<Q', data, b + 8)[0]
        st_size  = struct.unpack_from('<Q', data, b + 24)[0]
        st_type  = st_info & 0xF   # STT_FUNC = 2
        st_bind  = st_info >> 4

        if st_type == 2 and st_value != 0:  # defined function
            name = data[dynstr_off + st_name:].split(b'\x00')[0].decode('utf-8', errors='replace')
            foff = vaddr_to_file(st_value)
            if foff is not None:
                results.append((foff, name))

    return sorted(results)


def nearest_symbol(file_off: int, exports: list[tuple[int, str]]) -> str:
    if not exports:
        return ""
    lo, hi = 0, len(exports) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if exports[mid][0] <= file_off:
            lo = mid
        else:
            hi = mid - 1
    sym_off, sym_name = exports[lo]
    delta = file_off - sym_off
    if delta == 0:
        return sym_name
    return f"{sym_name} + 0x{delta:x}"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("binary", help="Path to ELF binary")
    p.add_argument("--start",  type=lambda x: int(x, 0), default=0)
    p.add_argument("--end",    type=lambda x: int(x, 0), default=None)
    p.add_argument("--near",   type=lambda x: int(x, 0), default=None,
                   help="Show functions near this file offset")
    p.add_argument("--window", type=int, default=5,
                   help="How many functions either side of --near (default 5)")
    p.add_argument("--resolve", action="store_true",
                   help="Resolve addresses to nearest exported symbol")
    return p.parse_args()


def main():
    args = parse_args()

    with open(args.binary, 'rb') as f:
        data = f.read()

    end = args.end if args.end is not None else len(data)
    funcs = find_endbr64(data, args.start, end)

    exports = []
    if args.resolve:
        exports = load_exports(data)

    if args.near is not None:
        # Find the index of the function closest to args.near
        target = args.near
        closest = min(range(len(funcs)), key=lambda i: abs(funcs[i] - target))
        lo = max(0, closest - args.window)
        hi = min(len(funcs) - 1, closest + args.window)
        subset = funcs[lo:hi + 1]
        print(f"# Functions near 0x{target:x}  ({lo}..{hi} of {len(funcs)} total)")
        for off in subset:
            marker = " ← target" if off == target else ""
            sym = f"  {nearest_symbol(off, exports)}" if exports else ""
            print(f"  0x{off:x}{sym}{marker}")
    else:
        print(f"# {len(funcs)} functions found between 0x{args.start:x} and 0x{end:x}")
        for off in funcs:
            sym = f"\t{nearest_symbol(off, exports)}" if exports else ""
            print(f"0x{off:x}{sym}")


if __name__ == "__main__":
    main()

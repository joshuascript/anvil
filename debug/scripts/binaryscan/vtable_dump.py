#!/usr/bin/env python3
"""
vtable_dump.py - Dump C++ vtable contents from a PIE ELF binary.

Uses R_X86_64_RELATIVE relocations from .rela.dyn to reconstruct vtable
slot → function file offset mappings without needing a running process.

In a PIE ELF, each vtable slot is filled at load time via a relocation:
    R_X86_64_RELATIVE at <slot_vaddr>  addend=<target_vaddr>
The addend is the target function's virtual address (= file offset at base=0).

Usage:
    python3 vtable_dump.py <binary> <vtable_offset>
                           [--count N]     # stop after N slots (default: until gap)
                           [--resolve]     # annotate with nearest exported symbol
    python3 vtable_dump.py <binary> --list [--min-slots N]  # list all vtables found
"""

import argparse
import struct
import sys


R_X86_64_RELATIVE = 8


def parse_loads(data: bytes) -> list[tuple[int, int, int]]:
    """Return list of (vaddr, file_offset, filesz) for all PT_LOAD segments."""
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
        if p_type == 1:
            loads.append((p_vaddr, p_offset, p_filesz))
    return loads


def make_converters(loads):
    def vaddr_to_file(va):
        for vaddr, foff, fsz in loads:
            if vaddr <= va < vaddr + fsz:
                return foff + (va - vaddr)
        return None

    def file_to_vaddr(fo):
        for vaddr, foff, fsz in loads:
            if foff <= fo < foff + fsz:
                return vaddr + (fo - foff)
        return None

    return vaddr_to_file, file_to_vaddr


def load_rela_dyn(data: bytes, vaddr_to_file) -> dict[int, int]:
    """
    Parse .rela.dyn and return {slot_file_offset: target_file_offset}
    for all R_X86_64_RELATIVE entries.
    """
    if data[:4] != b'\x7fELF':
        sys.exit("Not an ELF file")

    e_shoff     = struct.unpack_from('<Q', data, 0x28)[0]
    e_shentsize = struct.unpack_from('<H', data, 0x3A)[0]
    e_shnum     = struct.unpack_from('<H', data, 0x3C)[0]
    e_shstrndx  = struct.unpack_from('<H', data, 0x3E)[0]

    shstr_offset = struct.unpack_from('<Q', data, e_shoff + e_shstrndx * e_shentsize + 0x18)[0]

    relocs = {}
    for i in range(e_shnum):
        b = e_shoff + i * e_shentsize
        name_off  = struct.unpack_from('<I', data, b)[0]
        sh_type   = struct.unpack_from('<I', data, b + 4)[0]
        sh_offset = struct.unpack_from('<Q', data, b + 24)[0]
        sh_size   = struct.unpack_from('<Q', data, b + 32)[0]
        sh_entsize= struct.unpack_from('<Q', data, b + 56)[0]
        name = data[shstr_offset + name_off:].split(b'\x00')[0].decode('utf-8', errors='replace')

        # SHT_RELA = 4; accept .rela.dyn and any other RELA section
        if sh_type != 4 or sh_entsize == 0:
            continue

        n = sh_size // sh_entsize
        for j in range(n):
            eb = sh_offset + j * sh_entsize
            r_offset = struct.unpack_from('<Q', data, eb)[0]
            r_info   = struct.unpack_from('<Q', data, eb + 8)[0]
            r_addend = struct.unpack_from('<q', data, eb + 16)[0]  # signed
            r_type   = r_info & 0xFFFFFFFF

            if r_type == R_X86_64_RELATIVE:
                slot_foff   = vaddr_to_file(r_offset)
                target_foff = vaddr_to_file(r_addend) if r_addend >= 0 else None
                if slot_foff is not None and target_foff is not None:
                    relocs[slot_foff] = target_foff

    return relocs


def load_exports(data: bytes, vaddr_to_file) -> list[tuple[int, str]]:
    """Return sorted list of (file_offset, name) for all defined function symbols."""
    e_shoff     = struct.unpack_from('<Q', data, 0x28)[0]
    e_shentsize = struct.unpack_from('<H', data, 0x3A)[0]
    e_shnum     = struct.unpack_from('<H', data, 0x3C)[0]
    e_shstrndx  = struct.unpack_from('<H', data, 0x3E)[0]
    shstr_offset = struct.unpack_from('<Q', data, e_shoff + e_shstrndx * e_shentsize + 0x18)[0]

    dynsym_off = dynsym_size = dynsym_ent = dynstr_off = 0
    for i in range(e_shnum):
        b = e_shoff + i * e_shentsize
        name_off  = struct.unpack_from('<I', data, b)[0]
        sh_type   = struct.unpack_from('<I', data, b + 4)[0]
        sh_offset = struct.unpack_from('<Q', data, b + 24)[0]
        sh_size   = struct.unpack_from('<Q', data, b + 32)[0]
        sh_entsize= struct.unpack_from('<Q', data, b + 56)[0]
        sh_link   = struct.unpack_from('<I', data, b + 40)[0]
        name = data[shstr_offset + name_off:].split(b'\x00')[0].decode('utf-8', errors='replace')
        if sh_type == 11:  # SHT_DYNSYM
            dynsym_off, dynsym_size, dynsym_ent = sh_offset, sh_size, sh_entsize
            _, _, dynstr_off, _, _, _ = _section(data, e_shoff, e_shentsize, sh_link, shstr_offset)
        if sh_type == 3 and name == '.dynstr':
            dynstr_off = sh_offset

    if not dynsym_off:
        return []

    results = []
    n = dynsym_size // dynsym_ent
    for i in range(n):
        b = dynsym_off + i * dynsym_ent
        st_name  = struct.unpack_from('<I', data, b)[0]
        st_info  = data[b + 4]
        st_value = struct.unpack_from('<Q', data, b + 8)[0]
        st_type  = st_info & 0xF
        if st_type == 2 and st_value != 0:
            name = data[dynstr_off + st_name:].split(b'\x00')[0].decode('utf-8', errors='replace')
            foff = vaddr_to_file(st_value)
            if foff is not None:
                results.append((foff, name))
    return sorted(results)


def _section(data, e_shoff, e_shentsize, idx, shstr_offset):
    b = e_shoff + idx * e_shentsize
    name_off  = struct.unpack_from('<I', data, b)[0]
    sh_type   = struct.unpack_from('<I', data, b + 4)[0]
    sh_offset = struct.unpack_from('<Q', data, b + 24)[0]
    sh_size   = struct.unpack_from('<Q', data, b + 32)[0]
    sh_entsize= struct.unpack_from('<Q', data, b + 56)[0]
    sh_link   = struct.unpack_from('<I', data, b + 40)[0]
    name = data[shstr_offset + name_off:].split(b'\x00')[0].decode('utf-8', errors='replace')
    return name, sh_type, sh_offset, sh_size, sh_entsize, sh_link


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
    return sym_name if delta == 0 else f"{sym_name}+0x{delta:x}"


def dump_vtable(relocs: dict, start_foff: int, count: int | None,
                exports: list, file_to_vaddr) -> list[tuple[int, int, int]]:
    """
    Walk consecutive 8-byte relocation entries starting at start_foff.
    Returns list of (slot_index, slot_foff, target_foff).
    """
    results = []
    slot = 0
    pos = start_foff
    while True:
        if count is not None and slot >= count:
            break
        if pos not in relocs:
            break
        results.append((slot, pos, relocs[pos]))
        slot += 1
        pos += 8
    return results


def find_all_vtables(relocs: dict, min_slots: int) -> list[tuple[int, int]]:
    """
    Find all clusters of consecutive relocations with at least min_slots entries.
    Returns list of (start_foff, slot_count).
    """
    if not relocs:
        return []
    sorted_offsets = sorted(relocs)
    clusters = []
    i = 0
    while i < len(sorted_offsets):
        start = sorted_offsets[i]
        count = 1
        while i + count < len(sorted_offsets) and sorted_offsets[i + count] == start + count * 8:
            count += 1
        if count >= min_slots:
            clusters.append((start, count))
        i += count
    return clusters


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("binary", help="Path to ELF binary")
    p.add_argument("vtable_offset", nargs="?", type=lambda x: int(x, 0),
                   help="File offset of the vtable to dump")
    p.add_argument("--count",  type=int, default=None,
                   help="Max slots to dump (default: until first gap)")
    p.add_argument("--resolve", action="store_true",
                   help="Annotate each slot with nearest exported symbol")
    p.add_argument("--list", action="store_true",
                   help="List all vtable-like relocation clusters in the binary")
    p.add_argument("--min-slots", type=int, default=3,
                   help="Minimum slots for --list (default: 3)")
    return p.parse_args()


def main():
    args = parse_args()

    if not args.list and args.vtable_offset is None:
        sys.exit("Provide a vtable_offset or use --list")

    with open(args.binary, 'rb') as f:
        data = f.read()

    loads = parse_loads(data)
    vaddr_to_file, file_to_vaddr = make_converters(loads)
    relocs = load_rela_dyn(data, vaddr_to_file)

    exports = []
    if args.resolve:
        exports = load_exports(data, vaddr_to_file)

    if args.list:
        clusters = find_all_vtables(relocs, args.min_slots)
        print(f"# {len(clusters)} vtable-like clusters (>= {args.min_slots} consecutive slots)")
        print(f"# {'start_offset':<16}  slots")
        for start, count in clusters:
            sym = f"  {nearest_symbol(start, exports)}" if exports else ""
            print(f"  0x{start:x}{' '*(14-len(f'{start:x}'))}  {count}{sym}")
        return

    slots = dump_vtable(relocs, args.vtable_offset, args.count, exports, file_to_vaddr)
    if not slots:
        print(f"# No R_X86_64_RELATIVE relocation found at 0x{args.vtable_offset:x}")
        print(f"# (vtable may be in a non-PIE segment, or offset is wrong)")
        return

    print(f"# vtable at 0x{args.vtable_offset:x}  ({len(slots)} slots)")
    print(f"# {'slot':<6}  {'slot_offset':<14}  {'target_offset':<14}  symbol")
    for slot, slot_foff, target_foff in slots:
        sym = f"  {nearest_symbol(target_foff, exports)}" if exports else ""
        print(f"  {slot:<6}  0x{slot_foff:x}{' '*(12-len(f'{slot_foff:x}'))}  0x{target_foff:x}{sym}")


if __name__ == "__main__":
    main()

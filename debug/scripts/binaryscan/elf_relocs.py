#!/usr/bin/env python3
"""
elf_relocs.py - Query ELF relocations by address, range, or type.

Parses all RELA sections (.rela.dyn, .rela.plt, etc.) and outputs matching
entries. The addend for R_X86_64_RELATIVE is the target file offset (at base=0),
making this the right tool for reconstructing vtables, pointer arrays, and
any data structure filled with function or data pointers.

Relocation types (x86-64 common):
    1  = R_X86_64_64         absolute 64-bit address
    6  = R_X86_64_GLOB_DAT   GOT slot for symbol
    7  = R_X86_64_JUMP_SLOT  PLT slot for symbol
    8  = R_X86_64_RELATIVE   base + addend (no symbol; used for vtables)
    37 = R_X86_64_IRELATIVE  indirect (ifunc)

Usage:
    # Show all relocations at a specific offset
    python3 elf_relocs.py <binary> --at 0xOFFSET

    # Show all relocations in a file offset range
    python3 elf_relocs.py <binary> --range 0xSTART-0xEND

    # Show all relocations of a given type
    python3 elf_relocs.py <binary> --type 8

    # Find all relocations whose addend points to a target offset
    python3 elf_relocs.py <binary> --target 0xOFFSET

    # Dump everything (large output)
    python3 elf_relocs.py <binary> --all [--resolve]
"""

import argparse
import struct
import sys


_TYPE_NAMES = {
    0:  "NONE",
    1:  "R_64",
    2:  "R_PC32",
    4:  "R_PLT32",
    5:  "R_COPY",
    6:  "R_GLOB_DAT",
    7:  "R_JUMP_SLOT",
    8:  "R_RELATIVE",
    9:  "R_GOTPCREL",
    10: "R_32",
    37: "R_IRELATIVE",
    41: "R_SIZE32",
    42: "R_SIZE64",
}


def parse_loads(data: bytes) -> list[tuple[int, int, int]]:
    e_phoff     = struct.unpack_from('<Q', data, 0x20)[0]
    e_phentsize = struct.unpack_from('<H', data, 0x36)[0]
    e_phnum     = struct.unpack_from('<H', data, 0x38)[0]
    loads = []
    for i in range(e_phnum):
        b = e_phoff + i * e_phentsize
        if struct.unpack_from('<I', data, b)[0] == 1:
            loads.append((
                struct.unpack_from('<Q', data, b + 16)[0],
                struct.unpack_from('<Q', data, b + 8)[0],
                struct.unpack_from('<Q', data, b + 32)[0],
            ))
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


def load_dynsym(data: bytes, vaddr_to_file) -> dict[int, str]:
    """Return {sym_index: name} for dynamic symbols."""
    e_shoff     = struct.unpack_from('<Q', data, 0x28)[0]
    e_shentsize = struct.unpack_from('<H', data, 0x3A)[0]
    e_shnum     = struct.unpack_from('<H', data, 0x3C)[0]
    e_shstrndx  = struct.unpack_from('<H', data, 0x3E)[0]
    shstr_off   = struct.unpack_from('<Q', data, e_shoff + e_shstrndx * e_shentsize + 0x18)[0]

    dynsym_off = dynsym_size = dynsym_ent = dynstr_off = 0
    for i in range(e_shnum):
        b = e_shoff + i * e_shentsize
        name_off  = struct.unpack_from('<I', data, b)[0]
        sh_type   = struct.unpack_from('<I', data, b + 4)[0]
        sh_offset = struct.unpack_from('<Q', data, b + 24)[0]
        sh_size   = struct.unpack_from('<Q', data, b + 32)[0]
        sh_entsize= struct.unpack_from('<Q', data, b + 56)[0]
        sh_link   = struct.unpack_from('<I', data, b + 40)[0]
        name = data[shstr_off + name_off:].split(b'\x00')[0].decode('utf-8', errors='replace')
        if sh_type == 11:
            dynsym_off, dynsym_size, dynsym_ent = sh_offset, sh_size, sh_entsize
            lb = e_shoff + sh_link * e_shentsize
            dynstr_off = struct.unpack_from('<Q', data, lb + 24)[0]
        if sh_type == 3 and name == '.dynstr':
            dynstr_off = sh_offset

    result = {}
    if dynsym_off and dynsym_ent:
        for i in range(dynsym_size // dynsym_ent):
            b = dynsym_off + i * dynsym_ent
            st_name = struct.unpack_from('<I', data, b)[0]
            sym = data[dynstr_off + st_name:].split(b'\x00')[0].decode('utf-8', errors='replace')
            result[i] = sym
    return result


def load_relocs(data: bytes, vaddr_to_file, dynsym: dict) -> list[dict]:
    """Parse all RELA sections and return a list of relocation dicts."""
    e_shoff     = struct.unpack_from('<Q', data, 0x28)[0]
    e_shentsize = struct.unpack_from('<H', data, 0x3A)[0]
    e_shnum     = struct.unpack_from('<H', data, 0x3C)[0]
    e_shstrndx  = struct.unpack_from('<H', data, 0x3E)[0]
    shstr_off   = struct.unpack_from('<Q', data, e_shoff + e_shstrndx * e_shentsize + 0x18)[0]

    relocs = []
    for i in range(e_shnum):
        b = e_shoff + i * e_shentsize
        name_off  = struct.unpack_from('<I', data, b)[0]
        sh_type   = struct.unpack_from('<I', data, b + 4)[0]
        sh_offset = struct.unpack_from('<Q', data, b + 24)[0]
        sh_size   = struct.unpack_from('<Q', data, b + 32)[0]
        sh_entsize= struct.unpack_from('<Q', data, b + 56)[0]
        section_name = data[shstr_off + name_off:].split(b'\x00')[0].decode('utf-8', errors='replace')

        if sh_type != 4 or sh_entsize == 0:  # SHT_RELA = 4
            continue

        n = sh_size // sh_entsize
        for j in range(n):
            eb = sh_offset + j * sh_entsize
            r_offset = struct.unpack_from('<Q', data, eb)[0]
            r_info   = struct.unpack_from('<Q', data, eb + 8)[0]
            r_addend = struct.unpack_from('<q', data, eb + 16)[0]
            r_type   = r_info & 0xFFFFFFFF
            r_sym    = r_info >> 32

            slot_foff   = vaddr_to_file(r_offset)
            # For R_X86_64_RELATIVE the addend is the target vaddr (base=0)
            target_foff = vaddr_to_file(r_addend) if r_addend > 0 else None

            relocs.append({
                "section":     section_name,
                "r_offset":    r_offset,
                "slot_foff":   slot_foff,
                "r_type":      r_type,
                "r_sym":       r_sym,
                "sym_name":    dynsym.get(r_sym, ""),
                "r_addend":    r_addend,
                "target_foff": target_foff,
            })

    return relocs


def format_row(r: dict, resolve: bool) -> str:
    type_name = _TYPE_NAMES.get(r["r_type"], f"type_{r['r_type']}")
    slot   = f"0x{r['slot_foff']:x}" if r["slot_foff"] is not None else f"vaddr:0x{r['r_offset']:x}"
    target = f"0x{r['target_foff']:x}" if r["target_foff"] is not None else f"0x{r['r_addend']:x}"
    sym    = f"  {r['sym_name']}" if resolve and r["sym_name"] else ""
    return f"  {slot:<16}  {type_name:<14}  addend={target}{sym}"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("binary", help="Path to ELF binary")

    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--at",     type=lambda x: int(x, 0), metavar="OFFSET",
                      help="Show relocation(s) at this exact file offset")
    mode.add_argument("--range",  metavar="START-END",
                      help="Show relocations in file offset range, e.g. 0x357240-0x3574c0")
    mode.add_argument("--type",   type=int, metavar="N",
                      help="Show all relocations of this type (e.g. 8 for R_RELATIVE)")
    mode.add_argument("--target", type=lambda x: int(x, 0), metavar="OFFSET",
                      help="Find all relocations whose addend resolves to this file offset")
    mode.add_argument("--all",    action="store_true",
                      help="Dump all relocations")

    p.add_argument("--resolve", action="store_true", help="Show symbol name for non-RELATIVE entries")
    p.add_argument("--section", help="Filter by section name (e.g. .rela.dyn)")
    return p.parse_args()


def main():
    args = parse_args()

    with open(args.binary, 'rb') as f:
        data = f.read()

    if data[:4] != b'\x7fELF':
        sys.exit("Not an ELF file")

    loads = parse_loads(data)
    vaddr_to_file, _ = make_converters(loads)
    dynsym = load_dynsym(data, vaddr_to_file)
    relocs = load_relocs(data, vaddr_to_file, dynsym)

    if args.section:
        relocs = [r for r in relocs if r["section"] == args.section]

    if args.at is not None:
        results = [r for r in relocs if r["slot_foff"] == args.at]
        print(f"# Relocations at 0x{args.at:x}  ({len(results)} found)")
    elif args.range is not None:
        parts = args.range.split('-')
        lo = int(parts[0], 0)
        hi = int(parts[1], 0)
        results = [r for r in relocs if r["slot_foff"] is not None and lo <= r["slot_foff"] <= hi]
        print(f"# Relocations in 0x{lo:x}–0x{hi:x}  ({len(results)} found)")
    elif args.type is not None:
        results = [r for r in relocs if r["r_type"] == args.type]
        type_name = _TYPE_NAMES.get(args.type, f"type_{args.type}")
        print(f"# {type_name} relocations  ({len(results)} found)")
    elif args.target is not None:
        results = [r for r in relocs if r["target_foff"] == args.target]
        print(f"# Relocations targeting 0x{args.target:x}  ({len(results)} found)")
    else:
        results = relocs
        print(f"# All relocations  ({len(results)} total)")

    print(f"# {'slot_offset':<16}  {'type':<14}  addend")
    for r in results:
        print(format_row(r, args.resolve))


if __name__ == "__main__":
    main()

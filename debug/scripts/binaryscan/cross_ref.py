#!/usr/bin/env python3
"""
cross_ref.py - Find all references to a file offset in an x86-64 ELF binary.

Scans for RIP-relative instructions that resolve to the target address:
  - CALL rel32         (E8 <disp32>)
  - JMP  rel32         (E9 <disp32>)
  - LEA  reg, [rip+X]  (REX 8D /r <disp32>)
  - MOV  reg, [rip+X]  (REX 8B /r <disp32>)  — load from memory
  - MOV  [rip+X], reg  (REX 89 /r <disp32>)  — store to memory
  - MOVQ xmm, [rip+X]  (F3 0F 7E 05 <disp32>)

Usage:
    python3 cross_ref.py <binary> <target_offset>
                         [--type call|jmp|lea|mov|all]  # default: all
                         [--start 0xOFFSET] [--end 0xOFFSET]
                         [--resolve]                    # annotate with nearest symbol

Output: one reference per line — file offset of the instruction + type + symbol annotation.
"""

import argparse
import struct
import sys


def parse_loads(data: bytes) -> list[tuple[int, int, int]]:
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


def load_exports(data: bytes, vaddr_to_file) -> list[tuple[int, str]]:
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

    if not dynsym_off:
        return []

    results = []
    for i in range(dynsym_size // dynsym_ent):
        b = dynsym_off + i * dynsym_ent
        st_name  = struct.unpack_from('<I', data, b)[0]
        st_info  = data[b + 4]
        st_value = struct.unpack_from('<Q', data, b + 8)[0]
        if (st_info & 0xF) == 2 and st_value != 0:
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
    return sym_name if delta == 0 else f"{sym_name}+0x{delta:x}"


# Each pattern: (name, prefix_bytes, disp_offset_from_prefix, instr_len)
# disp_offset_from_prefix = byte index within prefix where the disp32 starts
# instr_len = total instruction length (prefix + disp32)
_PATTERNS = [
    # CALL rel32: E8 <disp32>
    ("call", b'\xe8', 1, 5),
    # JMP rel32: E9 <disp32>
    ("jmp",  b'\xe9', 1, 5),
    # LEA reg64, [rip+disp32]: 48 8D ?? <disp32>  (REX.W + 8D + ModRM where mod=00,rm=101)
    # We match the first two bytes and check ModRM separately
    ("lea",  b'\x48\x8d', 2, 7),
    ("lea",  b'\x4c\x8d', 2, 7),  # REX.WR (xmm8-15 dst)
    # MOV reg64, [rip+disp32]: 48 8B ?? <disp32>
    ("mov",  b'\x48\x8b', 2, 7),
    ("mov",  b'\x4c\x8b', 2, 7),
    # MOV [rip+disp32], reg64: 48 89 ?? <disp32>
    ("mov",  b'\x48\x89', 2, 7),
    ("mov",  b'\x4c\x89', 2, 7),
    # MOVQ xmm0, [rip+disp32]: F3 0F 7E 05 <disp32>
    ("mov",  b'\xf3\x0f\x7e\x05', 4, 8),
    # MOV rax, [rip+disp32]: 48 8B 05 <disp32>  (common specific form)
    # Already covered by the generic 48 8B above
]

# For 3-byte prefixes (48 8D/8B/89 + ModRM), the ModRM for [rip+disp32] has mod=00, rm=101
_RIP_MODRM_MASK = 0b11000111  # mod and rm bits
_RIP_MODRM_VAL  = 0b00000101  # mod=00, rm=101


def scan(data: bytes, target_foff: int, start: int, end: int,
         file_to_vaddr, type_filter: set) -> list[tuple[int, str, int]]:
    """
    Returns list of (instr_file_offset, ref_type, target_file_offset).
    target_file_offset will always equal target_foff for direct refs;
    included for future wildcard use.
    """
    target_vaddr = file_to_vaddr(target_foff)
    if target_vaddr is None:
        return []

    results = []
    i = start

    while i < end:
        for ref_type, prefix, disp_off, instr_len in _PATTERNS:
            if ref_type not in type_filter:
                continue
            plen = len(prefix)
            if i + instr_len > end:
                continue
            if data[i:i+plen] != prefix:
                continue

            # For 2-byte prefixes (REX + opcode), validate ModRM byte
            if plen == 2:
                modrm = data[i + 2]
                if (modrm & _RIP_MODRM_MASK) != _RIP_MODRM_VAL:
                    continue

            disp = struct.unpack_from('<i', data, i + disp_off)[0]
            # RIP = address of next instruction = file offset of (i + instr_len), as vaddr
            next_foff = i + instr_len
            # Convert to vaddr for RIP calculation
            from itertools import chain
            rip_vaddr = file_to_vaddr(next_foff)
            if rip_vaddr is None:
                continue
            resolved_vaddr = (rip_vaddr + disp) & 0xFFFFFFFFFFFFFFFF
            if resolved_vaddr == target_vaddr:
                results.append((i, ref_type, target_foff))
            break  # only try first matching pattern at this position

        i += 1

    return results


def scan_optimized(data: bytes, target_foff: int, start: int, end: int,
                   file_to_vaddr, vaddr_to_file, type_filter: set) -> list[tuple[int, str, int]]:
    """
    Faster version: for each instruction length/pattern, compute what disp32 value
    would resolve to target, then search for that exact byte sequence.
    """
    target_vaddr = file_to_vaddr(target_foff)
    if target_vaddr is None:
        return []

    hits = {}  # foff -> ref_type (deduplicated)

    for ref_type, prefix, disp_off, instr_len in _PATTERNS:
        if ref_type not in type_filter:
            continue
        plen = len(prefix)
        # For each possible instruction position i, rip = i + instr_len
        # We need: target_vaddr = rip_vaddr + disp
        # disp = target_vaddr - rip_vaddr
        # rip_vaddr = file_to_vaddr(i + instr_len)
        # Since file_to_vaddr is linear within each segment: rip_vaddr ≈ (i + instr_len) + segment_delta
        # We'll do a direct scan but only check the disp bytes after confirming prefix match.

        pos = start
        while pos < end:
            idx = data.find(prefix, pos, end)
            if idx == -1:
                break
            pos = idx + 1

            if idx + instr_len > end:
                continue

            if plen == 2:
                modrm = data[idx + 2]
                if (modrm & _RIP_MODRM_MASK) != _RIP_MODRM_VAL:
                    continue

            disp = struct.unpack_from('<i', data, idx + disp_off)[0]
            next_foff = idx + instr_len
            rip_vaddr = file_to_vaddr(next_foff)
            if rip_vaddr is None:
                continue
            resolved_vaddr = (rip_vaddr + disp) & 0xFFFFFFFFFFFFFFFF
            if resolved_vaddr == target_vaddr:
                if idx not in hits:
                    hits[idx] = ref_type

    return sorted((foff, rtype, target_foff) for foff, rtype in hits.items())


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("binary", help="Path to ELF binary")
    p.add_argument("target", type=lambda x: int(x, 0), help="File offset to find references to")
    p.add_argument("--type", default="all", choices=["call", "jmp", "lea", "mov", "all"],
                   help="Instruction type filter (default: all)")
    p.add_argument("--start",   type=lambda x: int(x, 0), default=0)
    p.add_argument("--end",     type=lambda x: int(x, 0), default=None)
    p.add_argument("--resolve", action="store_true",
                   help="Annotate each result with nearest exported symbol of the caller")
    return p.parse_args()


def main():
    args = parse_args()

    with open(args.binary, 'rb') as f:
        data = f.read()

    loads = parse_loads(data)
    vaddr_to_file, file_to_vaddr = make_converters(loads)

    type_filter = {"call", "jmp", "lea", "mov"} if args.type == "all" else {args.type}
    end = args.end if args.end is not None else len(data)

    exports = []
    if args.resolve:
        exports = load_exports(data, vaddr_to_file)

    refs = scan_optimized(data, args.target, args.start, end,
                          file_to_vaddr, vaddr_to_file, type_filter)

    print(f"# {len(refs)} reference(s) to 0x{args.target:x}")
    print(f"# {'instr_offset':<16}  type    caller_symbol")
    for instr_foff, ref_type, _ in refs:
        sym = f"  {nearest_symbol(instr_foff, exports)}" if exports else ""
        print(f"  0x{instr_foff:x}{' '*(14-len(f'{instr_foff:x}'))}  {ref_type:<6}{sym}")


if __name__ == "__main__":
    main()

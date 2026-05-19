#!/usr/bin/env python3
"""
pattern_scan.py - Wildcard byte pattern search in an ELF binary.

Pattern syntax:
  - Hex bytes:    FF D0 48 8B 07
  - Wildcards:    FF ?? 48 ?? ??     (single byte wildcard)
  - Nibble wild:  FF D? 4? 8B        (low or high nibble wildcard)
  - Ranges:       [--start] [--end]  (file offset bounds)

Usage:
    python3 pattern_scan.py <binary> <pattern>
                            [--start 0xOFFSET] [--end 0xOFFSET]
                            [--limit N]        # stop after N hits (default: unlimited)
                            [--resolve]        # nearest exported symbol per hit
                            [--context N]      # show N bytes before/after each hit

Examples:
    python3 pattern_scan.py libengine2.so "F3 0F 1E FA 48 8B 07 FF"
    python3 pattern_scan.py libengine2.so "48 8B 07 FF ?? 0?"
    python3 pattern_scan.py libengine2.so "E8 ?? ?? ?? ?? 48 8B 00 C3"
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
        if struct.unpack_from('<I', data, b)[0] == 1:
            loads.append((
                struct.unpack_from('<Q', data, b + 16)[0],
                struct.unpack_from('<Q', data, b + 8)[0],
                struct.unpack_from('<Q', data, b + 32)[0],
            ))
    return loads


def make_vaddr_to_file(loads):
    def vaddr_to_file(va):
        for vaddr, foff, fsz in loads:
            if vaddr <= va < vaddr + fsz:
                return foff + (va - vaddr)
        return None
    return vaddr_to_file


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

    results = []
    if dynsym_off:
        for i in range(dynsym_size // dynsym_ent):
            b = dynsym_off + i * dynsym_ent
            st_name  = struct.unpack_from('<I', data, b)[0]
            st_info  = data[b + 4]
            st_value = struct.unpack_from('<Q', data, b + 8)[0]
            if (st_info & 0xF) == 2 and st_value != 0:
                sym = data[dynstr_off + st_name:].split(b'\x00')[0].decode('utf-8', errors='replace')
                foff = vaddr_to_file(st_value)
                if foff is not None:
                    results.append((foff, sym))
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


def parse_pattern(pattern_str: str) -> list[tuple[int, int]]:
    """
    Parse pattern string into list of (value, mask) byte pairs.
    mask=0xFF means exact match; mask=0x00 means full wildcard;
    mask=0x0F or 0xF0 means nibble wildcard.

    Raises ValueError on bad input.
    """
    tokens = pattern_str.split()
    result = []
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        if tok == '??':
            result.append((0x00, 0x00))
        elif len(tok) == 2:
            hi, lo = tok[0], tok[1]
            hi_wild = hi == '?'
            lo_wild = lo == '?'
            hi_val = 0 if hi_wild else int(hi, 16)
            lo_val = 0 if lo_wild else int(lo, 16)
            val  = (hi_val << 4) | lo_val
            mask = ((0 if hi_wild else 0xF) << 4) | (0 if lo_wild else 0xF)
            result.append((val, mask))
        else:
            raise ValueError(f"Invalid pattern token: {tok!r}  (expected 2 hex chars or ??)")
    if not result:
        raise ValueError("Empty pattern")
    return result


def find_anchor(pattern: list[tuple[int, int]]) -> tuple[int, bytes]:
    """Find the longest exact-byte run to use as a fast pre-filter anchor."""
    best_start = best_len = 0
    cur_start = cur_len = 0
    for i, (val, mask) in enumerate(pattern):
        if mask == 0xFF:
            if cur_len == 0:
                cur_start = i
            cur_len += 1
            if cur_len > best_len:
                best_len, best_start = cur_len, cur_start
        else:
            cur_len = 0
    anchor_bytes = bytes(pattern[best_start + i][0] for i in range(best_len))
    return best_start, anchor_bytes


def scan(data: bytes, pattern: list[tuple[int, int]],
         start: int, end: int, limit: int) -> list[int]:
    plen = len(pattern)
    hits = []
    anchor_offset, anchor_bytes = find_anchor(pattern)

    pos = start
    while pos < end:
        # Fast scan for anchor
        if anchor_bytes:
            idx = data.find(anchor_bytes, pos, end - plen + anchor_offset + len(anchor_bytes))
            if idx == -1:
                break
            candidate = idx - anchor_offset
        else:
            candidate = pos

        if candidate < start or candidate + plen > end:
            pos = candidate + 1
            continue

        # Full pattern match
        matched = all(
            (data[candidate + i] & mask) == (val & mask)
            for i, (val, mask) in enumerate(pattern)
        )

        if matched:
            hits.append(candidate)
            if limit and len(hits) >= limit:
                break

        pos = candidate + 1

    return hits


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("binary",  help="Path to ELF binary")
    p.add_argument("pattern", help="Hex pattern with optional ?? or nibble wildcards")
    p.add_argument("--start",   type=lambda x: int(x, 0), default=0)
    p.add_argument("--end",     type=lambda x: int(x, 0), default=None)
    p.add_argument("--limit",   type=int, default=0, help="Stop after N hits (0 = unlimited)")
    p.add_argument("--resolve", action="store_true", help="Show nearest exported symbol")
    p.add_argument("--context", type=int, default=0, metavar="N",
                   help="Show N bytes of hex context around each hit")
    return p.parse_args()


def main():
    args = parse_args()

    try:
        pattern = parse_pattern(args.pattern)
    except ValueError as e:
        sys.exit(f"Pattern error: {e}")

    with open(args.binary, 'rb') as f:
        data = f.read()

    loads = parse_loads(data)
    vaddr_to_file = make_vaddr_to_file(loads)

    exports = []
    if args.resolve:
        exports = load_exports(data, vaddr_to_file)

    end = args.end if args.end is not None else len(data)
    hits = scan(data, pattern, args.start, end, args.limit)

    plen = len(pattern)
    pattern_str = ' '.join(
        '??' if mask == 0x00
        else f'{val:02x}' if mask == 0xFF
        else f'{val >> 4:x}?' if (mask & 0x0F) == 0
        else f'?{val & 0xF:x}'
        for val, mask in pattern
    )

    limited = args.limit and len(hits) >= args.limit
    print(f"# pattern: {pattern_str}  ({plen} bytes)")
    print(f"# {len(hits)} hit(s){' (limit reached)' if limited else ''}")

    for hit in hits:
        sym = f"  {nearest_symbol(hit, exports)}" if exports else ""
        line = f"0x{hit:x}{sym}"

        if args.context > 0:
            ctx_start = max(0, hit - args.context)
            ctx_end   = min(len(data), hit + plen + args.context)
            before = data[ctx_start:hit].hex(' ') if hit > ctx_start else ""
            match  = data[hit:hit + plen].hex(' ')
            after  = data[hit + plen:ctx_end].hex(' ') if hit + plen < ctx_end else ""
            parts = []
            if before: parts.append(before)
            parts.append(f"[{match}]")
            if after:  parts.append(after)
            line += "  " + ' '.join(parts)

        print(line)


if __name__ == "__main__":
    main()

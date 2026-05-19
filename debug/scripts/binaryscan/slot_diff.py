#!/usr/bin/env python3
"""
slot_diff.py - Compare nativeInit slot tables between two ELF binaries.

Extracts slot→function mappings from both binaries using the SSE2 slot-fill
pattern (same as decode_nativeinit.py), then identifies each function by:
  1. Vtable dispatch offset  — if it's an endbr64/mov rax,[rdi]/jmp *N(%rax) thunk
  2. Exported symbol name    — if the function is in .dynsym
  3. Byte fingerprint        — first 16 bytes hex, as a last resort

Useful for finding how many slots shifted between a Windows and Linux build.

Usage:
    python3 slot_diff.py <binary_a> <binary_b>
                         [--slots N-M]        # slot range (default: all)
                         [--all]              # show matching slots too
                         [--tsv]              # tab-separated output (for piping)
"""

import argparse
import hashlib
import struct
import sys


ENDBR64  = b'\xf3\x0f\x1e\xfa'
MOV_RAX_RDI = b'\x48\x8b\x07'


# ── ELF helpers ──────────────────────────────────────────────────────────────

def parse_loads(data: bytes) -> list[tuple[int, int, int]]:
    e_phoff     = struct.unpack_from('<Q', data, 0x20)[0]
    e_phentsize = struct.unpack_from('<H', data, 0x36)[0]
    e_phnum     = struct.unpack_from('<H', data, 0x38)[0]
    loads = []
    for i in range(e_phnum):
        b = e_phoff + i * e_phentsize
        if struct.unpack_from('<I', data, b)[0] == 1:  # PT_LOAD
            p_offset = struct.unpack_from('<Q', data, b + 8)[0]
            p_vaddr  = struct.unpack_from('<Q', data, b + 16)[0]
            p_filesz = struct.unpack_from('<Q', data, b + 32)[0]
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


def load_exports(data: bytes, vaddr_to_file) -> dict[int, str]:
    """Return {file_offset: symbol_name} for all defined function exports."""
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
        if sh_type == 11:  # SHT_DYNSYM
            dynsym_off, dynsym_size, dynsym_ent = sh_offset, sh_size, sh_entsize
            lb = e_shoff + sh_link * e_shentsize
            dynstr_off = struct.unpack_from('<Q', data, lb + 24)[0]
        if sh_type == 3 and name == '.dynstr':
            dynstr_off = sh_offset

    result = {}
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
                    result[foff] = sym
    return result


# ── Slot extraction (mirrors decode_nativeinit.py) ────────────────────────────

_MOVUPS_MODRM = {"rdi": 0x87, "rsi": 0x86, "rdx": 0x82, "rcx": 0x89}


def extract_slots(data: bytes, base_reg: str = "rdx") -> dict[int, int]:
    modrm_byte = _MOVUPS_MODRM.get(base_reg, 0x82)
    slots = {}
    last_rax = None
    xmm_regs = {}
    xmm0_lo  = None
    i = 0
    end = len(data)

    while i < end:
        if data[i:i+4] == b'\xf3\x0f\x7e\x05' and i + 8 <= end:
            disp = struct.unpack_from('<i', data, i + 4)[0]
            ptr_off = (i + 8 + disp) & 0xFFFFFFFFFFFFFFFF
            if ptr_off + 8 <= len(data):
                val = struct.unpack_from('<q', data, ptr_off)[0]
                xmm0_lo = val & 0xFFFFFFFFFFFFFFFF
            i += 8; continue

        if data[i:i+3] == b'\x48\x8d\x05' and i + 7 <= end:
            disp = struct.unpack_from('<i', data, i + 3)[0]
            last_rax = (i + 7 + disp) & 0xFFFFFFFFFFFFFFFF
            i += 7; continue

        if data[i:i+4] == b'\x66\x48\x0f\x6e' and i + 5 <= end:
            mb = data[i + 4]
            if (mb & 0xC7) == 0xC0:
                xmm_regs[(mb >> 3) & 0x7] = last_rax
            i += 5; continue

        if data[i:i+4] == b'\x66\x4c\x0f\x6e' and i + 5 <= end:
            mb = data[i + 4]
            if (mb & 0xC7) == 0xC0:
                xmm_regs[((mb >> 3) & 0x7) + 8] = last_rax
            i += 5; continue

        if data[i:i+3] == b'\x66\x0f\x6c' and i + 4 <= end:
            mb = data[i + 3]
            if (mb & 0xF8) == 0xC0:
                src_idx = mb & 0x7
                xmm_regs[0] = (xmm_regs.get(src_idx, 0), xmm0_lo)
            i += 4; continue

        if data[i:i+2] == b'\x0f\x11' and i + 7 <= end and data[i + 2] == modrm_byte:
            offset = struct.unpack_from('<I', data, i + 3)[0]
            slot_lo = offset // 8
            packed = xmm_regs.get(0)
            if isinstance(packed, tuple):
                hi_val, lo_val = packed
                if lo_val is not None: slots[slot_lo]     = lo_val
                if hi_val is not None: slots[slot_lo + 1] = hi_val
            elif xmm0_lo is not None:
                slots[slot_lo] = xmm0_lo
            i += 7; continue

        i += 1

    return slots


# ── Function identity ─────────────────────────────────────────────────────────

def decode_thunk_vtable_slot(data: bytes, foff: int) -> int | None:
    """Return vtable byte offset if foff is a vtable dispatch thunk, else None."""
    if foff + 32 > len(data):
        return None
    pos = foff
    if data[pos:pos+4] == ENDBR64:
        pos += 4
    if data[pos:pos+3] != MOV_RAX_RDI:
        return None
    pos += 3

    if data[pos] != 0xFF:
        return None
    pos += 1
    modrm = data[pos]; pos += 1
    mod = (modrm >> 6) & 0x3
    reg = (modrm >> 3) & 0x7
    rm  =  modrm       & 0x7
    if rm != 0 or reg not in (2, 4):
        return None

    if mod == 0:
        return 0
    elif mod == 1:
        return struct.unpack_from('<b', data, pos)[0] & 0xFFFFFFFFFFFFFFFF
    elif mod == 2:
        return struct.unpack_from('<I', data, pos)[0]
    return None


def func_identity(data: bytes, foff: int, exports: dict[int, str]) -> str:
    """Return a stable string identity for the function at foff."""
    # 1. Vtable thunk
    vt = decode_thunk_vtable_slot(data, foff)
    if vt is not None:
        return f"vtable[{vt // 8}]"
    # 2. Exported symbol
    if foff in exports:
        return exports[foff]
    # 3. Byte fingerprint
    chunk = data[foff:foff + 16]
    return "bytes:" + chunk.hex()


# ── Diff logic ────────────────────────────────────────────────────────────────

def diff_slots(slots_a: dict, slots_b: dict,
               data_a: bytes, data_b: bytes,
               exports_a: dict, exports_b: dict,
               slot_range=None) -> list[dict]:
    all_slots = sorted(set(slots_a) | set(slots_b))
    if slot_range:
        all_slots = [s for s in all_slots if slot_range[0] <= s <= slot_range[1]]

    rows = []
    for slot in all_slots:
        fa = slots_a.get(slot)
        fb = slots_b.get(slot)
        id_a = func_identity(data_a, fa, exports_a) if fa is not None else "(absent)"
        id_b = func_identity(data_b, fb, exports_b) if fb is not None else "(absent)"
        rows.append({
            "slot": slot,
            "foff_a": fa,
            "foff_b": fb,
            "id_a": id_a,
            "id_b": id_b,
            "match": id_a == id_b,
        })
    return rows


def detect_shift(rows: list[dict]) -> str | None:
    """If most diffs look like a constant vtable slot shift, report it."""
    diffs = [r for r in rows if not r["match"]
             and r["id_a"].startswith("vtable[")
             and r["id_b"].startswith("vtable[")]
    if len(diffs) < 3:
        return None
    deltas = []
    for r in diffs:
        try:
            va = int(r["id_a"][7:-1])
            vb = int(r["id_b"][7:-1])
            deltas.append(vb - va)
        except ValueError:
            pass
    if not deltas:
        return None
    most_common = max(set(deltas), key=deltas.count)
    frac = deltas.count(most_common) / len(deltas)
    if frac >= 0.75:
        sign = "+" if most_common >= 0 else ""
        return f"Dominant vtable slot shift: {sign}{most_common}  ({deltas.count(most_common)}/{len(deltas)} thunk diffs)"
    return None


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("binary_a", help="First ELF binary (e.g. Windows build)")
    p.add_argument("binary_b", help="Second ELF binary (e.g. Linux build)")
    p.add_argument("--slots", help="Slot range, e.g. 2315-2340")
    p.add_argument("--all",   action="store_true", help="Show matching slots too")
    p.add_argument("--tsv",   action="store_true", help="Tab-separated output")
    return p.parse_args()


def main():
    args = parse_args()

    with open(args.binary_a, 'rb') as f: data_a = f.read()
    with open(args.binary_b, 'rb') as f: data_b = f.read()

    loads_a = parse_loads(data_a); v2f_a, f2v_a = make_converters(loads_a)
    loads_b = parse_loads(data_b); v2f_b, f2v_b = make_converters(loads_b)

    exports_a = load_exports(data_a, v2f_a)
    exports_b = load_exports(data_b, v2f_b)

    slots_a = extract_slots(data_a)
    slots_b = extract_slots(data_b)

    slot_range = None
    if args.slots:
        lo, hi = (int(x) for x in args.slots.split('-'))
        slot_range = (lo, hi)

    rows = diff_slots(slots_a, slots_b, data_a, data_b, exports_a, exports_b, slot_range)

    diffs   = [r for r in rows if not r["match"]]
    matches = [r for r in rows if r["match"]]

    shift_msg = detect_shift(rows)
    if shift_msg:
        print(f"# {shift_msg}")

    print(f"# {len(diffs)} differing slot(s), {len(matches)} matching slot(s) in range")

    display = rows if args.all else diffs
    if not display:
        print("# (no differences)")
        return

    if args.tsv:
        print("slot\tfoff_a\tfoff_b\tid_a\tid_b\tstatus")
        for r in display:
            fa = f"0x{r['foff_a']:x}" if r['foff_a'] is not None else "-"
            fb = f"0x{r['foff_b']:x}" if r['foff_b'] is not None else "-"
            status = "MATCH" if r["match"] else "DIFF"
            print(f"{r['slot']}\t{fa}\t{fb}\t{r['id_a']}\t{r['id_b']}\t{status}")
    else:
        W = max((len(r["id_a"]) for r in display), default=12)
        header = f"  {'slot':<8}  {'identity_a':<{W}}  identity_b"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for r in display:
            fa = f"0x{r['foff_a']:x}" if r['foff_a'] is not None else "-"
            fb = f"0x{r['foff_b']:x}" if r['foff_b'] is not None else "-"
            marker = "    ✓" if r["match"] else ""
            print(f"  {r['slot']:<8}  {r['id_a']:<{W}}  {r['id_b']}{marker}")


if __name__ == "__main__":
    main()

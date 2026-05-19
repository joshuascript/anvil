#!/usr/bin/env python3
"""
decode_nativeinit.py - Decode SSE2-optimized slot table fills in a binary.

The compiler optimizes dense function-pointer table fills using:
    MOVQ xmm0, [rip+X]       ; load func_a (from .rodata relocation)
    LEA  rax,  [rip+Y]       ; load func_b address directly
    MOVQ xmmN, rax
    PUNPCKLQDQ xmm0, xmmN    ; pack: xmm0[63:0]=func_a, xmm0[127:64]=func_b
    MOVUPS [rdx+off], xmm0   ; store two slots at once

Usage:
    python3 decode_nativeinit.py <binary> [--start 0xOFFSET] [--end 0xOFFSET]
                                          [--slots 2315-2340]
                                          [--base-reg rdx]

Output: tab-separated  slot  func_offset  for each assigned slot.
"""

import argparse
import struct
import sys


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("binary", help="Path to ELF binary")
    p.add_argument("--start", type=lambda x: int(x, 0), default=0, help="Start file offset to scan (default: 0)")
    p.add_argument("--end",   type=lambda x: int(x, 0), default=None, help="End file offset to scan (default: EOF)")
    p.add_argument("--slots", help="Slot range to filter output, e.g. 2315-2340")
    p.add_argument("--base-reg", default="rdx", choices=["rdi","rsi","rdx","rcx","r8","r9"],
                   help="Register used as array base pointer (default: rdx)")
    return p.parse_args()


# ModRM byte for [base_reg + disp32], opcode 0F 11 xx
# Only supports rdx (0x82), rdi (0x87), rsi (0x86), rcx (0x89), r8 (REX+0x80), r9 (REX+0x81)
_MOVUPS_MODRM = {
    "rdi": 0x87,
    "rsi": 0x86,
    "rdx": 0x82,
    "rcx": 0x89,
}


def decode(data: bytes, start: int, end: int, base_reg: str) -> dict[int, int]:
    """
    Scan [start, end) for SSE2 slot-fill patterns.
    Returns {slot_index: func_file_offset}.
    """
    modrm = _MOVUPS_MODRM.get(base_reg)
    if modrm is None:
        sys.exit(f"Unsupported base register: {base_reg}")

    slots = {}
    last_rax = None   # func offset loaded via LEA into rax
    xmm_regs = {}     # xmmN -> value (only track what's been set)
    xmm0_lo = None    # low 64 bits of xmm0 (set by MOVQ xmm0, [rip+X])

    i = start
    while i < end:
        # ---- MOVQ xmm0, [rip+disp32]:  F3 0F 7E 05 <disp32>
        if data[i:i+4] == b'\xf3\x0f\x7e\x05' and i + 8 <= end:
            disp = struct.unpack_from('<i', data, i + 4)[0]
            ptr_off = (i + 8 + disp) & 0xFFFFFFFFFFFFFFFF
            if ptr_off + 8 <= len(data):
                val = struct.unpack_from('<q', data, ptr_off)[0]
                xmm0_lo = val & 0xFFFFFFFFFFFFFFFF
            i += 8
            continue

        # ---- LEA rax, [rip+disp32]:  48 8D 05 <disp32>
        if data[i:i+3] == b'\x48\x8d\x05' and i + 7 <= end:
            disp = struct.unpack_from('<i', data, i + 3)[0]
            last_rax = (i + 7 + disp) & 0xFFFFFFFFFFFFFFFF
            i += 7
            continue

        # ---- MOVQ xmmN, rax:  66 48 0F 6E <modrm>
        #      modrm = 11 rrr 000  where rrr = xmm reg index (0-7)
        if data[i:i+4] == b'\x66\x48\x0f\x6e' and i + 5 <= end:
            modrm_byte = data[i + 4]
            if (modrm_byte & 0xC7) == 0xC0:  # mod=11, rm=0 (rax)
                xmm_idx = (modrm_byte >> 3) & 0x7
                xmm_regs[xmm_idx] = last_rax
            i += 5
            continue

        # ---- MOVQ xmmN, rax (REX.R variant for xmm8-15):  66 4C 0F 6E <modrm>
        if data[i:i+4] == b'\x66\x4c\x0f\x6e' and i + 5 <= end:
            modrm_byte = data[i + 4]
            if (modrm_byte & 0xC7) == 0xC0:
                xmm_idx = ((modrm_byte >> 3) & 0x7) + 8
                xmm_regs[xmm_idx] = last_rax
            i += 5
            continue

        # ---- PUNPCKLQDQ xmm0, xmmN:  66 0F 6C <modrm>
        #      modrm = 11 000 rrr  where rrr = source xmm index
        if data[i:i+3] == b'\x66\x0f\x6c' and i + 4 <= end:
            modrm_byte = data[i + 3]
            if (modrm_byte & 0xF8) == 0xC0:  # mod=11, dst_reg=0
                src_idx = modrm_byte & 0x7
                # xmm0[127:64] = xmm_regs[src_idx][63:0]
                xmm_regs[0] = (xmm_regs.get(src_idx, 0), xmm0_lo)  # (hi, lo) tuple
            i += 4
            continue

        # ---- MOVUPS [base_reg+disp32], xmm0:  0F 11 <modrm> <disp32>
        if data[i:i+2] == b'\x0f\x11' and i + 7 <= end and data[i + 2] == modrm:
            offset = struct.unpack_from('<I', data, i + 3)[0]
            slot_lo = offset // 8

            packed = xmm_regs.get(0)
            if isinstance(packed, tuple):
                hi_val, lo_val = packed
                if lo_val is not None:
                    slots[slot_lo] = lo_val
                if hi_val is not None:
                    slots[slot_lo + 1] = hi_val
            elif xmm0_lo is not None:
                slots[slot_lo] = xmm0_lo

            i += 7
            continue

        i += 1

    return slots


def main():
    args = parse_args()

    with open(args.binary, 'rb') as f:
        data = f.read()

    end = args.end if args.end is not None else len(data)
    slots = decode(data, args.start, end, args.base_reg)

    slot_filter = None
    if args.slots:
        lo, hi = (int(x) for x in args.slots.split('-'))
        slot_filter = range(lo, hi + 1)

    print(f"# slot\tfunc_offset")
    for slot in sorted(slots):
        if slot_filter and slot not in slot_filter:
            continue
        print(f"{slot}\t0x{slots[slot]:x}")


if __name__ == "__main__":
    main()

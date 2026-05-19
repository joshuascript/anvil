#!/usr/bin/env python3
"""
decode_thunks.py - Extract vtable dispatch offsets from tiny C++ thunks.

Recognizes two common thunk patterns:

  Pattern A (jmp):
    endbr64
    mov rax, [rdi]       ; load vtable from this/self
    jmp *N(%rax)         ; jump to vtable[N/8]

  Pattern B (call + ret):
    endbr64
    mov rax, [rdi]
    call *N(%rax)
    ret

Usage:
    python3 decode_thunks.py <binary> <rva1> [rva2 ...]
    python3 decode_thunks.py <binary> --stdin        # read RVAs from stdin

Output: tab-separated  rva  vtable_byte_offset  vtable_slot  for each thunk.
Prints "?" if pattern is not recognized.
"""

import argparse
import struct
import sys


ENDBR64 = b'\xf3\x0f\x1e\xfa'


def decode_thunk(data: bytes, rva: int) -> int | None:
    """
    Parse thunk at file offset `rva`.
    Returns the vtable byte offset (N from *N(%rax)), or None if unrecognized.
    """
    if rva + 32 > len(data):
        return None

    pos = rva

    # Optional: endbr64 prefix
    if data[pos:pos+4] == ENDBR64:
        pos += 4

    # mov rax, [rdi]  →  48 8B 07
    if data[pos:pos+3] != b'\x48\x8b\x07':
        return None
    pos += 3

    # jmp *disp(%rax)  or  call *disp(%rax)
    # Short form (disp8):  FF 60 <disp8>  or  FF D0  (no disp, [rax])
    # Long form (disp32):  FF A0 <disp32> or  FF 90 <disp32> (call)
    op = data[pos]
    if op not in (0xFF,):
        return None
    pos += 1

    modrm = data[pos]
    pos += 1

    mod = (modrm >> 6) & 0x3
    reg = (modrm >> 3) & 0x7   # 4=jmp, 2=call
    rm  =  modrm       & 0x7   # must be 0 (rax)

    if rm != 0 or reg not in (2, 4):
        return None

    if mod == 0:
        # [rax] — vtable offset 0
        return 0
    elif mod == 1:
        # [rax + disp8]
        disp = struct.unpack_from('<b', data, pos)[0]
        return disp & 0xFFFFFFFFFFFFFFFF
    elif mod == 2:
        # [rax + disp32]
        disp = struct.unpack_from('<I', data, pos)[0]
        return disp
    else:
        return None


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("binary", help="Path to ELF binary")
    p.add_argument("rvas", nargs="*", type=lambda x: int(x, 0), help="File-offset RVAs to decode")
    p.add_argument("--stdin", action="store_true", help="Read RVAs (hex, one per line) from stdin")
    return p.parse_args()


def main():
    args = parse_args()

    with open(args.binary, 'rb') as f:
        data = f.read()

    rvas = list(args.rvas)
    if args.stdin:
        for line in sys.stdin:
            line = line.strip()
            if line:
                try:
                    rvas.append(int(line, 0))
                except ValueError:
                    pass

    if not rvas:
        sys.exit("No RVAs provided. Pass them as arguments or use --stdin.")

    print("# rva\t\tvtable_off\tvtable_slot")
    for rva in rvas:
        off = decode_thunk(data, rva)
        if off is None:
            print(f"0x{rva:x}\t?\t?")
        else:
            print(f"0x{rva:x}\t0x{off:x}\t{off // 8}")


if __name__ == "__main__":
    main()

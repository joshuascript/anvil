#!/usr/bin/env python3
"""
Dry-run validation of libsbox_htmlcb_patch against the live binary.
Mirrors the C patch logic: scan executable PT_LOAD for the 8-byte pattern,
verify the crash instruction bytes, report whether the patch would apply.
Exit 0 = all checks pass.
"""

import struct, sys
from pathlib import Path

BINARY = str(Path(__file__).resolve().parents[4] / 'game/bin/linuxsteamrt64/libengine2.so')
PT_LOAD, PF_X, PF_R = 1, 1, 4

PATTERN       = bytes([0x74, 0x03, 0x48, 0x8b, 0x36, 0x4c, 0x8d, 0x2d])
CRASH_OFFSET  = 2   # offset of 48 8b 36 within PATTERN
CRASH_BYTES   = bytes([0x48, 0x8b, 0x36])   # mov (%rsi),%rsi  — must match before patching
PATCH_BYTES   = bytes([0x48, 0x31, 0xf6])   # xor rsi,rsi      — replacement

with open(BINARY, 'rb') as f:
    data = bytearray(f.read())

# ── parse PT_LOAD segments ────────────────────────────────────────────

def parse_segs(data):
    e_phoff     = struct.unpack_from('<Q', data, 32)[0]
    e_phentsize = struct.unpack_from('<H', data, 54)[0]
    e_phnum     = struct.unpack_from('<H', data, 56)[0]
    segs = []
    for i in range(e_phnum):
        o = e_phoff + i * e_phentsize
        p_type, p_flags = struct.unpack_from('<II', data, o)
        p_offset = struct.unpack_from('<Q', data, o + 8)[0]
        p_vaddr  = struct.unpack_from('<Q', data, o + 16)[0]
        p_filesz = struct.unpack_from('<Q', data, o + 32)[0]
        if p_type == PT_LOAD and (p_flags & PF_R) and p_filesz:
            segs.append({'vaddr': p_vaddr, 'offset': p_offset,
                         'filesz': p_filesz, 'exec': bool(p_flags & PF_X)})
    return segs

segs = parse_segs(data)

# ── scan for pattern in executable segments ───────────────────────────

def find_pattern():
    hits = []
    for s in segs:
        if not s['exec']: continue
        seg = data[s['offset']:s['offset'] + s['filesz']]
        i = 0
        while True:
            pos = seg.find(PATTERN, i)
            if pos == -1: break
            file_off = s['offset'] + pos
            vma      = s['vaddr']  + pos
            hits.append({'fo': file_off, 'vma': vma})
            i = pos + 1
    return hits

# ── run checks ────────────────────────────────────────────────────────

ok = True
print(f"Binary: {BINARY}\n")
print(f"Pattern: {PATTERN.hex(' ')}")
print(f"  = je +3 / mov (%rsi),%rsi / lea r13,[rip+...]\n")

hits = find_pattern()
print(f"Pattern hits: {len(hits)}")
for h in hits:
    print(f"  file 0x{h['fo']:x}  VMA 0x{h['vma']:x}")

if len(hits) == 0:
    print("\nFAIL  pattern not found in executable segment")
    sys.exit(1)

if len(hits) > 1:
    print(f"\nFAIL  pattern is not unique ({len(hits)} hits) — patch may target wrong location")
    ok = False

# ── verify crash instruction bytes ────────────────────────────────────

h = hits[0]
crash_fo = h['fo'] + CRASH_OFFSET
actual = bytes(data[crash_fo:crash_fo + 3])

print(f"\nCrash instruction at file 0x{crash_fo:x}  VMA 0x{h['vma'] + CRASH_OFFSET:x}:")
print(f"  actual bytes : {actual.hex(' ')}")
print(f"  expected     : {CRASH_BYTES.hex(' ')}  (mov (%rsi),%rsi)")
print(f"  would become : {PATCH_BYTES.hex(' ')}  (xor rsi,rsi)")

if actual == PATCH_BYTES:
    print("\n  NOTE: crash instruction already patched to xor rsi,rsi")
    print(f"\nPASS  all checks  (already patched)")
    sys.exit(0)

if actual != CRASH_BYTES:
    print(f"\nFAIL  unexpected bytes — binary version mismatch? patch would skip")
    sys.exit(1)

# ── show surrounding disassembly context ─────────────────────────────

print(f"\n  Surrounding bytes (pattern):")
pattern_fo = h['fo']
for off in range(len(PATTERN)):
    b = data[pattern_fo + off]
    marker = " ← crash insn (would patch)" if off == CRASH_OFFSET else \
             " ← crash insn+1" if off == CRASH_OFFSET + 1 else \
             " ← crash insn+2" if off == CRASH_OFFSET + 2 else ""
    print(f"    +{off}: 0x{b:02x}{marker}")

print(f"\nPASS  all checks")
sys.exit(0)

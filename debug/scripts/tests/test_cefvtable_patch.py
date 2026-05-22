#!/usr/bin/env python3
"""
Dry-run validation of libsbox_cefvtable_patch against the live binary.
Mirrors the C patch logic: scan executable PT_LOAD for the masked pattern,
verify the patch site bytes, report whether the patch would apply.
Exit 0 = all checks pass.
"""

import struct, sys
from pathlib import Path

BINARY = str(Path(__file__).resolve().parents[4] / 'game/bin/linuxsteamrt64/libengine2.so')
PT_LOAD, PF_X, PF_R = 1, 1, 4

F_CHROME_SENTINEL = 0x454d4f5248435f46

PATCH_OFFSET = 3   # test rax,rax + je within pattern
CRASH_OFFSET = 8   # mov rsi,[rax] within pattern

# (byte, must_match) — False = wildcard (je displacement bytes)
PATTERN_MASK = [
    (0x48, True),  (0x8b, True),  (0x06, True),          # mov rax,[rsi]
    (0x48, True),  (0x85, True),  (0xc0, True),          # test rax,rax   <- patch site
    (0x74, True),  (0x00, False),                          # je ?? wildcard
    (0x48, True),  (0x8b, True),  (0x30, True),          # mov rsi,[rax]  <- crash
    (0x48, True),  (0x85, True),  (0xf6, True),          # test rsi,rsi
    (0x74, True),  (0x00, False),                          # je ?? wildcard
    (0x48, True),  (0x8b, True),  (0x06, True),          # mov rax,[rsi]
    (0x48, True),  (0x89, True),  (0x7d, True), (0xe8, True),  # mov [rbp-0x18],rdi
    (0xff, True),  (0x90, True),                           # call [rax+
    (0x18, True),  (0x01, True),  (0x00, True), (0x00, True),  # +0x118]
]

with open(BINARY, 'rb') as f:
    data = bytearray(f.read())

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

def find_pattern():
    hits = []
    plen = len(PATTERN_MASK)
    for s in segs:
        if not s['exec']: continue
        base = s['offset']
        seg  = data[base:base + s['filesz']]
        for i in range(len(seg) - plen):
            if all(not chk or seg[i+j] == byte for j, (byte, chk) in enumerate(PATTERN_MASK)):
                hits.append({'fo': base + i, 'vma': s['vaddr'] + i})
    return hits

print(f"Binary: {BINARY}\n")
print("Pattern: mov rax,[rsi] / test rax,rax / je / mov rsi,[rax] / ... / call [rax+0x118]")

hits = find_pattern()
print(f"\nPattern hits: {len(hits)}")
for h in hits:
    print(f"  file 0x{h['fo']:x}  VMA 0x{h['vma']:x}")

ok = True

if len(hits) == 0:
    print("\nFAIL  pattern not found in executable segment")
    sys.exit(1)

if len(hits) > 1:
    print(f"\nFAIL  pattern not unique ({len(hits)} hits) — patch may target wrong location")
    ok = False

h = hits[0]
patch_fo  = h['fo']  + PATCH_OFFSET
crash_fo  = h['fo']  + CRASH_OFFSET
patch_vma = h['vma'] + PATCH_OFFSET
crash_vma = h['vma'] + CRASH_OFFSET

patch_bytes = bytes(data[patch_fo:patch_fo + 5])
crash_bytes = bytes(data[crash_fo:crash_fo + 3])

print(f"\nPatch site at file 0x{patch_fo:x}  VMA 0x{patch_vma:x}:")
print(f"  actual   : {patch_bytes.hex(' ')}")
print(f"  expected : 48 85 c0 74 ??  (test rax,rax / je)")

print(f"\nCrash insn at file 0x{crash_fo:x}  VMA 0x{crash_vma:x}:")
print(f"  actual   : {crash_bytes.hex(' ')}")
print(f"  expected : 48 8b 30  (mov rsi,[rax])")

if patch_bytes[0] == 0xe9:
    print("\n  NOTE: patch site already contains jmp rel32 — patch already applied")
    print(f"\nPASS  all checks  (already patched)")
    sys.exit(0)

if patch_bytes[:3] != bytes([0x48, 0x85, 0xc0]) or patch_bytes[3] != 0x74:
    print(f"\nFAIL  unexpected bytes at patch site — binary version mismatch?")
    ok = False

if crash_bytes != bytes([0x48, 0x8b, 0x30]):
    print(f"\nFAIL  unexpected crash insn bytes — binary version mismatch?")
    ok = False

if ok:
    je_disp = data[patch_fo + 4]
    je_disp_signed = je_disp if je_disp < 128 else je_disp - 256
    safe_exit_vma = crash_vma + je_disp_signed
    print(f"\n  je displacement  : {je_disp_signed:+d}  (safe_exit at VMA 0x{safe_exit_vma:x})")
    print(f"  F_CHROME sentinel: 0x{F_CHROME_SENTINEL:016x}")
    print(f"\nPASS  all checks  (would install trampoline at patch site)")

sys.exit(0 if ok else 1)

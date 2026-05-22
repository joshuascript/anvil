#!/usr/bin/env python3
"""
Dry-run validation of libsbox_hashtable_patch against the live binary.
Mirrors the C patch logic: scan executable PT_LOAD for the masked pattern,
verify the patch site bytes, report whether the patch would apply.
Exit 0 = all checks pass.
"""

import struct, sys
from pathlib import Path

BINARY = str(Path(__file__).resolve().parents[4] / 'game/bin/linuxsteamrt64/libengine2.so')
PT_LOAD, PF_X, PF_R = 1, 1, 4

PATCH_OFFSET  = 21   # lea r13,[rax+rdx*8] within pattern
RESUME_OFFSET = 29   # test rbx,rbx within pattern

# (byte, must_match) — False = wildcard (je displacement byte)
PATTERN_MASK = [
    (0x8b, True),  (0x4b, True),  (0x1c, True),          # mov ecx,[rbx+0x1c]
    (0x4c, True),  (0x89, True),  (0xe0, True),           # mov rax,r12
    (0x31, True),  (0xd2, True),                           # xor edx,edx
    (0x48, True),  (0xf7, True),  (0xf1, True),           # div rcx
    (0x48, True),  (0x8b, True),  (0x03, True),           # mov rax,[rbx]
    (0x4c, True),  (0x8b, True),  (0x7b, True), (0x08, True), # mov r15,[rbx+8]
    (0x48, True),  (0x63, True),  (0xd2, True),           # movslq edx,edx
    (0x4c, True),  (0x8d, True),  (0x2c, True), (0xd0, True), # lea r13,[rax+rdx*8]
    (0x49, True),  (0x8b, True),  (0x5d, True), (0x00, True), # mov rbx,[r13]
    (0x48, True),  (0x85, True),  (0xdb, True),           # test rbx,rbx
    (0x74, True),  (0x00, False),                          # je ?? wildcard
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
print("Pattern: mov ecx,[rbx+0x1c] / div rcx / mov rax,[rbx] / lea r13 / mov rbx,[r13] / test / je")

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
resume_fo = h['fo']  + RESUME_OFFSET
patch_vma = h['vma'] + PATCH_OFFSET
resume_vma = h['vma'] + RESUME_OFFSET

patch_bytes  = bytes(data[patch_fo:patch_fo + 5])
resume_bytes = bytes(data[resume_fo:resume_fo + 3])

plen = len(PATTERN_MASK)
je_disp_byte = data[h['fo'] + plen - 1]
je_disp_signed = je_disp_byte if je_disp_byte < 128 else je_disp_byte - 256
safe_exit_vma = h['vma'] + plen + je_disp_signed

print(f"\nPatch site at file 0x{patch_fo:x}  VMA 0x{patch_vma:x}:")
print(f"  actual   : {patch_bytes.hex(' ')}")
print(f"  expected : 4c 8d 2c d0 49  (lea r13,[rax+rdx*8] + first byte of mov rbx,[r13])")

print(f"\nResume insn at file 0x{resume_fo:x}  VMA 0x{resume_vma:x}:")
print(f"  actual   : {resume_bytes.hex(' ')}")
print(f"  expected : 48 85 db  (test rbx,rbx)")

if patch_bytes[0] == 0xe9:
    print("\n  NOTE: patch site already contains jmp rel32 — patch already applied")
    print(f"\nPASS  all checks  (already patched)")
    sys.exit(0)

if patch_bytes[:4] != bytes([0x4c, 0x8d, 0x2c, 0xd0]) or patch_bytes[4] != 0x49:
    print(f"\nFAIL  unexpected bytes at patch site — binary version mismatch?")
    ok = False

if resume_bytes != bytes([0x48, 0x85, 0xdb]):
    print(f"\nFAIL  unexpected resume insn bytes — binary version mismatch?")
    ok = False

if ok:
    print(f"\n  je displacement  : {je_disp_signed:+d}  (safe_exit at VMA 0x{safe_exit_vma:x})")
    print(f"  bounds guards    : edx < [rbx+0x1c]  OR  edx < [rbx+0x24]")
    print(f"\nPASS  all checks  (would install trampoline at patch site)")

sys.exit(0 if ok else 1)

#!/usr/bin/env python3
"""
Dry-run validation of libsbox_finalizeload_patch against the live binary.
Mirrors the C patch logic: scan executable PT_LOAD for the 8-byte pattern,
verify the jge bytes, report whether the patch would apply.
Exit 0 = all checks pass.
"""

import struct, sys

BINARY = '/home/joshua/Documents/GitHub/joshuascript/sbox-public/game/bin/linuxsteamrt64/libengine2.so'
PT_LOAD, PF_X, PF_R = 1, 1, 4

# mov 0x6c(%rax),%eax ; cmp %eax,0x6c(%rbx) ; jge
PATTERN     = bytes([0x8b, 0x40, 0x6c, 0x39, 0x43, 0x6c, 0x0f, 0x8d])
JGE_OFFSET  = 6   # 0f 8d starts at pattern+6
JGE_BYTES   = bytes([0x0f, 0x8d])   # far jge — must match before patching
PATCH_BYTES = bytes([0x90] * 6)     # 6x NOP — replacement (covers jge + 4-byte disp)

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
print(f"  = mov 0x6c(%rax),%eax / cmp %eax,0x6c(%rbx) / jge\n")

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

# ── verify jge bytes ──────────────────────────────────────────────────

h = hits[0]
jge_fo  = h['fo'] + JGE_OFFSET
jge_vma = h['vma'] + JGE_OFFSET
actual2 = bytes(data[jge_fo:jge_fo + 2])
actual6 = bytes(data[jge_fo:jge_fo + 6])

print(f"\njge instruction at file 0x{jge_fo:x}  VMA 0x{jge_vma:x}:")
print(f"  actual bytes (6) : {actual6.hex(' ')}")
print(f"  expected opcode  : {JGE_BYTES.hex(' ')}  (far jge)")
print(f"  would become     : {PATCH_BYTES.hex(' ')}  (6x NOP)")

if actual6 == PATCH_BYTES:
    print("\n  NOTE: jge already patched to 6x NOP")
    print(f"\nPASS  all checks  (already patched)")
    sys.exit(0)

if actual2 != JGE_BYTES:
    print(f"\nFAIL  unexpected opcode {actual2.hex(' ')} — binary version mismatch? patch would skip")
    sys.exit(1)

# ── show displacement ─────────────────────────────────────────────────

disp = struct.unpack_from('<i', data, jge_fo + 2)[0]
target_vma = jge_vma + 6 + disp
print(f"\n  jge displacement : {disp:#x}  (jumps to VMA 0x{target_vma:x} — assertion handler)")

print(f"\nPASS  all checks")
sys.exit(0)

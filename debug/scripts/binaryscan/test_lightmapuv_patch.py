#!/usr/bin/env python3
"""
Dry-run validation of libsbox_lightmapuv_patch against the live binary.
Mirrors the C patch logic: data-anchored table find, then bounds scan.
Exit 0 = all checks pass.
"""

import struct, sys

BINARY = '/home/joshua/Documents/GitHub/joshuascript/sbox-public/game/bin/linuxsteamrt64/librendersystemvulkan.so'
PT_LOAD, PF_X, PF_R = 1, 1, 4
PASS, FAIL = True, False

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

def vma2fo(vma, size=1):
    for s in segs:
        if s['vaddr'] <= vma < s['vaddr'] + s['filesz']:
            if vma + size <= s['vaddr'] + s['filesz']:
                return s['offset'] + (vma - s['vaddr'])
    return None

def read_str(vma):
    fo = vma2fo(vma)
    if fo is None: return None
    end = data.index(b'\x00', fo)
    return data[fo:end].decode('ascii', errors='replace')

# ── find_table: data-anchored ─────────────────────────────────────────

def find_table():
    for s in segs:
        if s['exec']: continue
        seg = memoryview(data)[s['offset']:s['offset']+s['filesz']]
        align = (16 - (s['vaddr'] % 16)) % 16
        for i in range(align, len(seg) - 48, 16):
            p0, f0_8, f0_12 = struct.unpack_from('<QII', seg, i)
            p1, f1_8, f1_12 = struct.unpack_from('<QII', seg, i + 16)
            p2, f2_8, f2_12 = struct.unpack_from('<QII', seg, i + 32)
            if (f0_8, f0_12) != (0, 0): continue
            if (f1_8, f1_12) != (1, 0): continue
            if (f2_8, f2_12) != (2, 0): continue
            if read_str(p0) != 'position':    continue
            if read_str(p1) != 'blendweight':  continue
            if read_str(p2) != 'blendindices': continue
            return s['vaddr'] + i
    return None

# ── find_bounds: RIP-relative LEA → cmp $0x10 ────────────────────────

def find_bounds(table_vma):
    seen = set()
    bounds = []
    for s in segs:
        if not s['exec']: continue
        seg = data[s['offset']:s['offset']+s['filesz']]
        sv  = s['vaddr']
        for i in range(len(seg) - 8):
            if not (0x48 <= seg[i] <= 0x4f): continue
            if seg[i+1] != 0x8d:             continue
            if (seg[i+2] & 0xc7) != 0x05:   continue
            disp = struct.unpack_from('<i', seg, i+3)[0]
            if sv + i + 7 + disp != table_vma: continue

            lo, hi = max(0, i-512), min(len(seg)-5, i+512)
            for j in range(lo, hi):
                bvma = bfo = None
                if seg[j] == 0x83 and (seg[j+1] & 0xf8) == 0xf8 and seg[j+2] == 0x10:
                    bvma = sv + j + 2;  bfo = s['offset'] + j + 2;  after = seg[j+3:j+5]
                elif seg[j] == 0x41 and seg[j+1] == 0x83 and (seg[j+2] & 0xf8) == 0xf8 and seg[j+3] == 0x10:
                    bvma = sv + j + 3;  bfo = s['offset'] + j + 3;  after = seg[j+4:j+6]
                if bvma is None or bvma in seen: continue
                cjmp = after[0] in (0x74, 0x75) or (after[0] == 0x0f and after[1] in (0x84, 0x85))
                if not cjmp: continue
                seen.add(bvma)
                bounds.append({'vma': bvma, 'fo': bfo, 'byte': data[bfo]})
    return bounds

# ── run checks ────────────────────────────────────────────────────────

ok = PASS
print(f"Binary: {BINARY}\n")

# 1. Table find
table_vma = find_table()
if not table_vma:
    print("FAIL  find_table: not found")
    sys.exit(1)
print(f"PASS  find_table: VMA 0x{table_vma:x}")

# 2. Print all 16 existing entries
fo_tbl = vma2fo(table_vma, 16*16)
print("\n      Existing table entries:")
for i in range(16):
    p, f8, f12 = struct.unpack_from('<QII', data, fo_tbl + i*16)
    name = read_str(p) or '?'
    print(f"        [{i:2d}] {name!r:24s} f8=0x{f8:04x} f12=0x{f12:04x}")

# 3. Slot 16 pre-patch state
fo16 = vma2fo(table_vma + 256, 16)
p16, f8_16, f12_16 = struct.unpack_from('<QII', data, fo16)
slot16_name = read_str(p16) if p16 else None
print(f"\n      Slot 16 (pre-patch): ptr=0x{p16:016x} f8=0x{f8_16:04x} f12=0x{f12_16:04x}"
      + (f"  → {slot16_name!r}" if slot16_name else ""))

# 4. Bounds
bounds = find_bounds(table_vma)
if not bounds:
    print("\nFAIL  find_bounds: no loop bounds found near table references")
    ok = FAIL
else:
    print(f"\nPASS  find_bounds: {len(bounds)} bound(s):")
    for b in bounds:
        val = b['byte']
        status = "PASS" if val == 0x10 else "FAIL"
        if val != 0x10: ok = FAIL
        print(f"        {status}  VMA 0x{b['vma']:x}  file 0x{b['fo']:x}  current=0x{val:02x}"
              + ("  (would patch → 0x11)" if val == 0x10 else "  (unexpected value)"))

# 5. Final
print(f"\n{'PASS' if ok else 'FAIL'}  all checks")
sys.exit(0 if ok else 1)

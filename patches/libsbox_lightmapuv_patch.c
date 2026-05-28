/*
 * libsbox_lightmapuv_patch.c  (v4 — adds lightmapuv + vertexpaintblendparams)
 *
 * Adds "lightmapuv" (slot 16) and "vertexpaintblendparams" (slot 17) as recognised
 * vertex semantics in librendersystemvulkan.so, preventing crashes and assertions
 * from SemanticNameToUsage() when rendering lightmapped or vertex-painted geometry.
 *
 * Root cause
 * ----------
 * SemanticNameToUsage() in vulkan/inputlayoutvulkan.cpp contains a 16-entry
 * lookup table of known vertex semantic names.  "LightmapUV" is absent from
 * the table.  When a lightmapped mesh is rendered the function asserts
 * "Unknown semantic name 'LightmapUV'", returns a garbage usage value, and
 * the NVIDIA driver crashes on the invalid VkVertexInputAttributeDescription.
 *
 * Strategy — data-anchored, not code-anchored
 * -------------------------------------------
 * Previous versions scanned .text for specific loop instruction encodings.
 * Those patterns break whenever the compiler changes register allocation.
 *
 * This version anchors on the TABLE CONTENT, which is source-level string
 * literal data and stays stable across recompilations:
 *
 *   Entry 0: ptr → "position",     field8=0, field12=0
 *   Entry 1: ptr → "blendweight",  field8=1, field12=0
 *   Entry 2: ptr → "blendindices", field8=2, field12=0
 *
 * Three matching consecutive entries uniquely identify the table regardless
 * of where it lands after ASLR.
 *
 * Once the table is found:
 *   1. Entries 16 and 17 are written unconditionally at table_base + 256/272.
 *   2. The .text segment is scanned for RIP-relative LEAs that reference
 *      table_base.  Within ±512 bytes of each LEA, any `cmp $0x10, %reg`
 *      followed by a conditional jump is patched 0x10 → 0x12.
 *
 * Entry 16 is written even if no bounds are found (graceful degradation —
 * a subsequent `find_bounds` run can patch the byte separately).
 *
 * Table entry layout (16 bytes):
 *   [0..7]  : 8-byte pointer to semantic name string (R_X86_64_RELATIVE)
 *   [8..11] : uint32 field8  — usage class (0x0005 = TEXCOORD, same as "texcoord")
 *   [12..15]: uint32 field12 — index modifier (0x0000 = none)
 *
 * Observed at runtime (librendersystemvulkan.so, 2026-05-20)
 * ----------------------------------------------------------
 * Table base offset:  0x737560 (VMA; file offset 0x736560 due to segment delta)
 * Bound A offset:     0x128d39 (function with %ebx counter)
 * Bound B offset:     0x1292fb (function with %r13d counter)
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <elf.h>
#include <link.h>
#include <sys/mman.h>
#include <unistd.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <pthread.h>

static const char lightmapuv_str[]             = "lightmapuv";
#define LIGHTMAPUV_FIELD8   0x0005u   /* TEXCOORD usage class — matches "texcoord" entry */
#define LIGHTMAPUV_FIELD12  0x0000u

static const char vertexpaintblendparams_str[] = "vertexpaintblendparams";
#define VERTEXPAINTBLENDPARAMS_FIELD8   0x0005u   /* TEXCOORD — same class as vertexpainttintcolor */
#define VERTEXPAINTBLENDPARAMS_FIELD12  0x0000u

static pthread_once_t patch_once = PTHREAD_ONCE_INIT;

/* ── segment collection ───────────────────────────────────────── */

#define MAX_SEGS 8

struct lib_info {
    uintptr_t base;
    uintptr_t range_lo, range_hi;
    struct { uintptr_t start; size_t size; int exec; } segs[MAX_SEGS];
    int nseg, found;
};

static int collect_segs(struct dl_phdr_info *info, size_t sz, void *data)
{
    (void)sz;
    if (!info->dlpi_name || !strstr(info->dlpi_name, "librendersystemvulkan.so"))
        return 0;

    struct lib_info *li = data;
    li->base     = info->dlpi_addr;
    li->range_lo = UINTPTR_MAX;
    li->range_hi = 0;
    li->found    = 1;

    for (int i = 0; i < info->dlpi_phnum && li->nseg < MAX_SEGS; i++) {
        const ElfW(Phdr) *ph = &info->dlpi_phdr[i];
        if (ph->p_type != PT_LOAD || !(ph->p_flags & PF_R) || !ph->p_filesz)
            continue;
        uintptr_t start = info->dlpi_addr + ph->p_vaddr;
        size_t    size  = ph->p_filesz;
        li->segs[li->nseg].start = start;
        li->segs[li->nseg].size  = size;
        li->segs[li->nseg].exec  = !!(ph->p_flags & PF_X);
        li->nseg++;
        if (start < li->range_lo)        li->range_lo = start;
        if (start + size > li->range_hi) li->range_hi = start + size;
    }
    return 1;
}

/* ── helpers ──────────────────────────────────────────────────── */

static int ptr_valid(struct lib_info *li, uintptr_t p, size_t len)
{
    return p >= li->range_lo && p + len <= li->range_hi;
}

static int str_at(struct lib_info *li, uintptr_t ptr, const char *s)
{
    size_t len = strlen(s);
    return ptr_valid(li, ptr, len + 1) &&
           memcmp((const void *)ptr, s, len + 1) == 0;
}

static void mprotect_page(void *addr, int prot)
{
    long  pgsz = getpagesize();
    void *page = (void *)((uintptr_t)addr & ~(uintptr_t)(pgsz - 1));
    if (mprotect(page, (size_t)pgsz, prot) != 0)
        perror("[lightmapuv_patch] mprotect");
}

/* ── table finder (data-anchored) ─────────────────────────────── */

static uintptr_t find_table(struct lib_info *li)
{
    for (int s = 0; s < li->nseg; s++) {
        if (li->segs[s].exec) continue;   /* table is in data, not .text */

        uint8_t *mem = (uint8_t *)li->segs[s].start;
        size_t   sz  = li->segs[s].size;

        /* walk 16-byte-aligned positions */
        size_t off = (16 - ((uintptr_t)mem % 16)) % 16;
        for (size_t i = off; i + 48 <= sz; i += 16) {
            uint64_t p0, p1, p2;
            uint32_t f0_8, f0_12, f1_8, f1_12, f2_8, f2_12;
            memcpy(&p0,    mem+i,    8); memcpy(&f0_8,  mem+i+8,  4); memcpy(&f0_12, mem+i+12, 4);
            memcpy(&p1,    mem+i+16, 8); memcpy(&f1_8,  mem+i+24, 4); memcpy(&f1_12, mem+i+28, 4);
            memcpy(&p2,    mem+i+32, 8); memcpy(&f2_8,  mem+i+40, 4); memcpy(&f2_12, mem+i+44, 4);

            if (f0_8 != 0 || f0_12 != 0 ||
                f1_8 != 1 || f1_12 != 0 ||
                f2_8 != 2 || f2_12 != 0) continue;

            if (!str_at(li, p0, "position"))     continue;
            if (!str_at(li, p1, "blendweight"))   continue;
            if (!str_at(li, p2, "blendindices"))  continue;

            return (uintptr_t)(mem + i);
        }
    }
    return 0;
}

/* ── bound patcher (code scan near table references) ─────────── */

/*
 * Scan .text for any RIP-relative LEA that resolves to table_base.
 * Within ±512 bytes of each LEA, find `cmp $0x10, %<reg>` followed by
 * a conditional jump and patch the immediate 0x10 → 0x12 (covers slots 0-17).
 *
 * cmp $0x10 encodings:
 *   83 [F8-FF] 10      — 32-bit register (eax/ecx/edx/ebx/esp/ebp/esi/edi)
 *   41 83 [F8-FF] 10   — r8d–r15d (REX.B extends ModRM.rm)
 */
static int patch_bounds(struct lib_info *li, uintptr_t table_base)
{
    int patched = 0;

    for (int s = 0; s < li->nseg; s++) {
        if (!li->segs[s].exec) continue;

        uint8_t *text  = (uint8_t *)li->segs[s].start;
        size_t   limit = li->segs[s].size;

        for (size_t i = 0; i + 8 <= limit; i++) {
            /* RIP-relative LEA: REX(0x48-0x4f) 0x8d ModRM(mod=00,rm=101) disp32 */
            if (text[i] < 0x48 || text[i] > 0x4f) continue;
            if (text[i+1] != 0x8d)                 continue;
            if ((text[i+2] & 0xc7) != 0x05)        continue;  /* mod=00, rm=101 */

            int32_t disp;
            memcpy(&disp, text + i + 3, 4);
            uintptr_t resolved = (uintptr_t)(text + i + 7) + (uintptr_t)(intptr_t)disp;
            if (resolved != table_base) continue;

            /* Scan ±512 bytes for cmp $0x10, %reg + conditional jump */
            ssize_t lo = (ssize_t)i - 512, hi = (ssize_t)i + 512;
            if (lo < 0) lo = 0;
            if ((size_t)hi > limit - 4) hi = (ssize_t)(limit - 4);

            for (ssize_t j = lo; j < hi; j++) {
                uint8_t *p = text + j, *bound = NULL, *after = NULL;

                if (p[0] == 0x83 && (p[1] & 0xf8) == 0xf8 && p[2] == 0x10) {
                    bound = p + 2; after = p + 3;
                } else if (p[0] == 0x41 && p[1] == 0x83 &&
                           (p[2] & 0xf8) == 0xf8 && p[3] == 0x10) {
                    bound = p + 3; after = p + 4;
                }

                if (!bound || *bound != 0x10) continue;

                /* Must be followed by je or jne — confirm it's the assert branch */
                int cjmp = (*after == 0x74 || *after == 0x75) ||
                           (*after == 0x0f && (*(after+1) == 0x84 || *(after+1) == 0x85));
                if (!cjmp) continue;

                mprotect_page(bound, PROT_READ | PROT_WRITE | PROT_EXEC);
                *bound = 0x12;  /* cover slots 0-17: lightmapuv (16) + vertexpaintblendparams (17) */
                mprotect_page(bound, PROT_READ | PROT_EXEC);

                fprintf(stderr,
                        "[lightmapuv_patch] bound patched at offset 0x%lx (0x10→0x12)\n",
                        (uintptr_t)bound - li->base);
                patched++;
            }
        }
    }
    return patched;
}

/* ── main patch ───────────────────────────────────────────────── */

static void do_patch(void)
{
    struct lib_info li = { 0 };
    dl_iterate_phdr(collect_segs, &li);

    if (!li.found) {
        fprintf(stderr, "[lightmapuv_patch] librendersystemvulkan.so not found\n");
        return;
    }

    /* 1. Locate the semantic table by content */
    uintptr_t table_base = find_table(&li);
    if (!table_base) {
        fprintf(stderr, "[lightmapuv_patch] semantic table not found — patch not installed\n");
        return;
    }
    fprintf(stderr, "[lightmapuv_patch] table at offset 0x%lx\n", table_base - li.base);

    /* 2. Write entry 16 at table_base + 256 */
    uint8_t  *entry  = (uint8_t *)(table_base + 16 * 16);
    uintptr_t p1     = (uintptr_t)entry        & ~(uintptr_t)(getpagesize()-1);
    uintptr_t p2     = ((uintptr_t)entry + 15) & ~(uintptr_t)(getpagesize()-1);

    mprotect_page(entry,      PROT_READ | PROT_WRITE);
    if (p2 != p1) mprotect_page(entry + 15, PROT_READ | PROT_WRITE);

    uintptr_t str_va = (uintptr_t)lightmapuv_str;
    uint32_t  f8 = LIGHTMAPUV_FIELD8, f12 = LIGHTMAPUV_FIELD12;
    memcpy(entry,      &str_va, 8);
    memcpy(entry + 8,  &f8,     4);
    memcpy(entry + 12, &f12,    4);

    mprotect_page(entry,      PROT_READ);
    if (p2 != p1) mprotect_page(entry + 15, PROT_READ);

    fprintf(stderr,
            "[lightmapuv_patch] entry 16 written at offset 0x%lx "
            "→ \"%s\" (field8=0x%04x field12=0x%04x)\n",
            (uintptr_t)entry - li.base,
            lightmapuv_str, LIGHTMAPUV_FIELD8, LIGHTMAPUV_FIELD12);

    /* 2b. Write entry 17 — vertexpaintblendparams */
    uint8_t  *entry17 = (uint8_t *)(table_base + 17 * 16);
    uintptr_t p1b     = (uintptr_t)entry17        & ~(uintptr_t)(getpagesize()-1);
    uintptr_t p2b     = ((uintptr_t)entry17 + 15) & ~(uintptr_t)(getpagesize()-1);

    mprotect_page(entry17,      PROT_READ | PROT_WRITE);
    if (p2b != p1b) mprotect_page(entry17 + 15, PROT_READ | PROT_WRITE);

    uintptr_t str_va17 = (uintptr_t)vertexpaintblendparams_str;
    uint32_t  f8b = VERTEXPAINTBLENDPARAMS_FIELD8, f12b = VERTEXPAINTBLENDPARAMS_FIELD12;
    memcpy(entry17,      &str_va17, 8);
    memcpy(entry17 + 8,  &f8b,      4);
    memcpy(entry17 + 12, &f12b,     4);

    mprotect_page(entry17,      PROT_READ);
    if (p2b != p1b) mprotect_page(entry17 + 15, PROT_READ);

    fprintf(stderr,
            "[lightmapuv_patch] entry 17 written at offset 0x%lx "
            "→ \"%s\" (field8=0x%04x field12=0x%04x)\n",
            (uintptr_t)entry17 - li.base,
            vertexpaintblendparams_str, VERTEXPAINTBLENDPARAMS_FIELD8, VERTEXPAINTBLENDPARAMS_FIELD12);

    /* 3. Patch loop bounds near table references */
    int n = patch_bounds(&li, table_base);
    if (n == 0)
        fprintf(stderr,
                "[lightmapuv_patch] WARNING: entries written but no loop bounds found — "
                "lightmapuv/vertexpaintblendparams will not be reached until bounds are patched manually\n");
    else
        fprintf(stderr, "[lightmapuv_patch] installed — entries 16+17 written, %d loop bound(s) patched\n", n);
}

void *dlopen(const char *filename, int flags)
{
    static void *(*real_dlopen)(const char *, int) = NULL;
    if (!real_dlopen)
        real_dlopen = dlsym(RTLD_NEXT, "dlopen");

    void *handle = real_dlopen(filename, flags);

    if (handle && filename && strstr(filename, "librendersystemvulkan.so"))
        pthread_once(&patch_once, do_patch);

    return handle;
}

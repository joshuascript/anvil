/*
 * libsbox_lightmapuv_patch.c
 *
 * Adds "lightmapuv" as a recognised vertex semantic in librendersystemvulkan.so,
 * preventing a crash inside libnvidia-glcore.so when rendering lightmapped geometry.
 *
 * Root cause
 * ----------
 * SemanticNameToUsage() in vulkan/inputlayoutvulkan.cpp contains a 16-entry
 * lookup table of known vertex semantic names.  "LightmapUV" is used by Source 2
 * lightmapped meshes but is absent from the Vulkan renderer's table.  When a
 * mesh with a LightmapUV attribute is encountered the function fires:
 *
 *   Assertion Failed in function `SemanticNameToUsage()`:
 *   Unknown semantic name 'LightmapUV'
 *
 * The assertion is non-fatal (logs and continues), but the function returns an
 * uninitialised/garbage usage value.  That value populates a
 * VkVertexInputAttributeDescription with an out-of-range location, which the
 * NVIDIA driver then crashes on inside libnvidia-glcore.so.
 *
 * Table layout (16 entries × 16 bytes, VA/file-offset 0x737560)
 * --------------------------------------------------------------
 * Each entry:
 *   [0..7]  : 8-byte pointer to semantic name string (R_X86_64_RELATIVE reloc)
 *   [8..11] : uint32 usage field (field8)
 *   [12..15]: uint32 index modifier (field12)
 *
 * The loop iterates r13 = 0..15 and asserts when r13 reaches 16 (0x10):
 *   0x1292f8: 41 83 fd 10   cmp r13d, 0x10
 *   0x1292fc: 0f 84 ...     je  <assertion>
 *
 * Fix
 * ---
 * Two changes applied at runtime after librendersystemvulkan.so is loaded:
 *
 * 1. Patch the loop bound: change `cmp r13d, 0x10` → `cmp r13d, 0x11`
 *    (single byte change at offset 0x1292fb: 0x10 → 0x11)
 *
 * 2. Write a valid entry at table slot 16 (VA 0x737660):
 *    - pointer → our static "lightmapuv" string
 *    - field8  = 0x322  (same as "texcoord" — maps to TEXCOORD usage class)
 *    - field12 = 0x14   (same as "texcoord" — index base offset)
 *
 * The table page uses R_X86_64_RELATIVE relocations so it lives in a
 * writable segment (.data.rel.ro, made writable by the dynamic linker before
 * being re-protected).  We mprotect it writable, write the entry, and restore.
 *
 * Offset history (librendersystemvulkan.so)
 * -----------------------------------------
 * Date       | CMP offset  | Table offset | Notes
 * 2026-05-20 | 0x1292fb    | 0x737660     | Initial
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <link.h>
#include <sys/mman.h>
#include <unistd.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <pthread.h>

/* Offset of the byte '0x10' inside `cmp r13d, 0x10` (41 83 fd [10]) */
#define CMP_BOUND_OFFSET    0x1292fbUL

/* VA/file-offset of entry slot 16 in the semantic lookup table */
#define TABLE_ENTRY16_OFFSET 0x737660UL

/* Usage values matching the "texcoord" entry (maps LightmapUV → TEXCOORD class) */
#define LIGHTMAPUV_FIELD8   0x322u
#define LIGHTMAPUV_FIELD12  0x14u

static const char lightmapuv_str[] = "lightmapuv";

static pthread_once_t patch_once = PTHREAD_ONCE_INIT;

struct find_result { uintptr_t base; int found; };

static int find_vulkan(struct dl_phdr_info *info, size_t size, void *data)
{
    (void)size;
    if (info->dlpi_name && strstr(info->dlpi_name, "librendersystemvulkan.so")) {
        ((struct find_result *)data)->base  = (uintptr_t)info->dlpi_addr;
        ((struct find_result *)data)->found = 1;
        return 1;
    }
    return 0;
}

static void mprotect_page(void *addr, int prot)
{
    long  pgsz = getpagesize();
    void *page = (void *)((uintptr_t)addr & ~(uintptr_t)(pgsz - 1));
    if (mprotect(page, (size_t)pgsz, prot) != 0)
        perror("[lightmapuv_patch] mprotect failed");
}

static void do_patch(void)
{
    struct find_result res = { 0, 0 };
    dl_iterate_phdr(find_vulkan, &res);

    if (!res.found) {
        fprintf(stderr, "[lightmapuv_patch] librendersystemvulkan.so not found — patch not installed\n");
        return;
    }

    uintptr_t base = res.base;

    /* ------------------------------------------------------------------ *
     * Patch 1: extend loop bound 0x10 → 0x11                             *
     * ------------------------------------------------------------------ */
    uint8_t *cmp_byte = (uint8_t *)(base + CMP_BOUND_OFFSET);

    if (*cmp_byte != 0x10) {
        fprintf(stderr,
                "[lightmapuv_patch] unexpected byte 0x%02x at 0x%lx+0x%lx "
                "— patch skipped (binary version mismatch?)\n",
                *cmp_byte, base, CMP_BOUND_OFFSET);
        return;
    }

    mprotect_page(cmp_byte, PROT_READ | PROT_WRITE | PROT_EXEC);
    *cmp_byte = 0x11;
    mprotect_page(cmp_byte, PROT_READ | PROT_EXEC);

    /* ------------------------------------------------------------------ *
     * Patch 2: write entry 16 into the semantic lookup table              *
     * ------------------------------------------------------------------ */
    uint8_t *entry = (uint8_t *)(base + TABLE_ENTRY16_OFFSET);

    mprotect_page(entry, PROT_READ | PROT_WRITE);

    /* string pointer (8 bytes) */
    uintptr_t str_va = (uintptr_t)lightmapuv_str;
    memcpy(entry,     &str_va,              8);

    /* field8 (uint32) */
    uint32_t f8 = LIGHTMAPUV_FIELD8;
    memcpy(entry + 8, &f8, 4);

    /* field12 (uint32) */
    uint32_t f12 = LIGHTMAPUV_FIELD12;
    memcpy(entry + 12, &f12, 4);

    mprotect_page(entry, PROT_READ);

    fprintf(stderr,
            "[lightmapuv_patch] installed — base=0x%lx  "
            "cmp_byte=0x%lx (0x10→0x11)  entry16=0x%lx → \"%s\"\n",
            base,
            (uintptr_t)cmp_byte,
            (uintptr_t)entry,
            lightmapuv_str);
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

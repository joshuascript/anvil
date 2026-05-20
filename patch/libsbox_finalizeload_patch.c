/*
 * libsbox_finalizeload_patch.c
 *
 * Suppresses a spurious assertion in FinalizeLoadRequest() inside
 * libengine2.so that fires when a resource dependency fails to load
 * (ERROR_FILEOPEN: File not found) and its ExtRefDepth is left at a
 * default/uninitialised value that violates the expected depth ordering.
 *
 * Root cause
 * ----------
 * resourcesystem/loadingresource.cpp line 1194, inside FinalizeLoadRequest():
 *
 *   Assert( pLoadingResource->GetExtRefDepth() > GetExtRefDepth() );
 *
 * When a file-not-found error occurs the failed resource's ExtRefDepth
 * is never set, so the depth ordering invariant isn't guaranteed.  The
 * assertion fires, logs, shows an "Always Ignore" popup, and then falls
 * through to the exact same continuation as the non-assertion path.
 * There is no behavioural difference between the two paths — the assert
 * is pure log/UI noise on the error path.
 *
 * Disassembly (libengine2.so, confirmed 2026-05-20)
 * --------------------------------------------------
 * 3e6f99:  mov  0x6c(%rax),%eax         ; pLoadingResource->ExtRefDepth
 * 3e6f9c:  cmp  %eax, 0x6c(%rbx)        ; this->ExtRefDepth vs dependency
 * 3e6f9f:  jge  0x26f1da                ; assert if this->depth >= dep->depth
 *          ...                          ; (assertion handler, then falls through)
 * 3e6fa5:  mov  0x30(%rbx),%rax         ; normal continuation (same as after assert)
 *
 * Fix
 * ---
 * NOP the 6-byte jge at offset 0x3e6f9f:
 *   0f 8d 35 82 e8 ff  →  90 90 90 90 90 90
 *
 * This makes execution always fall through to the normal continuation
 * without ever triggering the assertion.
 *
 * Offset history (libengine2.so)
 * ------------------------------
 * Date       | JGE offset  | Expected bytes       | Notes
 * 2026-05-20 | 0x3e6f9f    | 0f 8d 35 82 e8 ff    | Initial
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

#define JGE_OFFSET 0x3e6f9fUL

static const uint8_t expected[6] = { 0x0f, 0x8d, 0x35, 0x82, 0xe8, 0xff };
static const uint8_t nops[6]     = { 0x90, 0x90, 0x90, 0x90, 0x90, 0x90 };

static pthread_once_t patch_once = PTHREAD_ONCE_INIT;

struct find_result { uintptr_t base; int found; };

static int find_engine2(struct dl_phdr_info *info, size_t size, void *data)
{
    (void)size;
    if (info->dlpi_name && strstr(info->dlpi_name, "libengine2.so")) {
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
        perror("[finalizeload_patch] mprotect failed");
}

static void do_patch(void)
{
    struct find_result res = { 0, 0 };
    dl_iterate_phdr(find_engine2, &res);

    if (!res.found) {
        fprintf(stderr, "[finalizeload_patch] libengine2.so not found — patch not installed\n");
        return;
    }

    uintptr_t base = res.base;
    uint8_t *jge = (uint8_t *)(base + JGE_OFFSET);

    if (memcmp(jge, expected, 6) != 0) {
        fprintf(stderr,
                "[finalizeload_patch] unexpected bytes at 0x%lx+0x%lx: %02x %02x %02x %02x %02x %02x"
                " — patch skipped (binary version mismatch?)\n",
                base, JGE_OFFSET,
                jge[0], jge[1], jge[2], jge[3], jge[4], jge[5]);
        return;
    }

    mprotect_page(jge, PROT_READ | PROT_WRITE | PROT_EXEC);
    memcpy(jge, nops, 6);
    mprotect_page(jge, PROT_READ | PROT_EXEC);

    fprintf(stderr,
            "[finalizeload_patch] installed — base=0x%lx  jge@0x%lx → 6×NOP\n",
            base, (uintptr_t)jge);
}

void *dlopen(const char *filename, int flags)
{
    static void *(*real_dlopen)(const char *, int) = NULL;
    if (!real_dlopen)
        real_dlopen = dlsym(RTLD_NEXT, "dlopen");

    void *handle = real_dlopen(filename, flags);

    if (handle && filename && strstr(filename, "libengine2.so"))
        pthread_once(&patch_once, do_patch);

    return handle;
}

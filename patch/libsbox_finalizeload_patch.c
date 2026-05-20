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
 * Pattern scan
 * ------------
 * Rather than a hardcoded offset, the patch scans the executable PT_LOAD
 * segment of libengine2.so for the unique sequence:
 *
 *   8b 40 6c        mov  0x6c(%rax),%eax   ; pLoadingResource->ExtRefDepth
 *   39 43 6c        cmp  %eax,0x6c(%rbx)   ; this->ExtRefDepth
 *   0f 8d ?? ?? ?? ??  jge  <assert>       ; fires if depth ordering violated
 *            ...                           ; (assertion handler, falls through)
 *
 * The jge (6 bytes starting at pattern+6) is replaced with 6 NOPs, making
 * execution always fall through to the same continuation as the non-assert
 * path.  The found offset is printed to stderr for reference.
 *
 * Offset history (libengine2.so) — for reference only, not used at runtime
 * -------------------------------------------------------------------------
 * Date       | JGE offset  | jge bytes            | Notes
 * 2026-05-20 | 0x3e6f9f    | 0f 8d 35 82 e8 ff    | Initial
 * 2026-05-20 | 0x3e6b1f    | 0f 8d b5 86 e8 ff    | After engine update (-0x480)
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

/* mov 0x6c(%rax),%eax ; cmp %eax,0x6c(%rbx) ; jge */
static const uint8_t pattern[]  = { 0x8b, 0x40, 0x6c, 0x39, 0x43, 0x6c, 0x0f, 0x8d };
static const uint8_t nops[6]    = { 0x90, 0x90, 0x90, 0x90, 0x90, 0x90 };

static pthread_once_t patch_once = PTHREAD_ONCE_INIT;

struct find_result {
    uintptr_t text_start;
    size_t    text_size;
    int       found;
};

static int find_engine2(struct dl_phdr_info *info, size_t size, void *data)
{
    (void)size;
    if (!info->dlpi_name || !strstr(info->dlpi_name, "libengine2.so"))
        return 0;

    struct find_result *res = data;
    for (int i = 0; i < info->dlpi_phnum; i++) {
        const ElfW(Phdr) *ph = &info->dlpi_phdr[i];
        if (ph->p_type == PT_LOAD && (ph->p_flags & PF_X)) {
            res->text_start = (uintptr_t)info->dlpi_addr + ph->p_vaddr;
            res->text_size  = ph->p_memsz;
            res->found      = 1;
            return 1;
        }
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
    struct find_result res = { 0, 0, 0 };
    dl_iterate_phdr(find_engine2, &res);

    if (!res.found) {
        fprintf(stderr, "[finalizeload_patch] libengine2.so executable segment not found\n");
        return;
    }

    uint8_t *text  = (uint8_t *)res.text_start;
    size_t   limit = res.text_size - sizeof(pattern) - 6;
    uint8_t *jge   = NULL;

    for (size_t i = 0; i < limit; i++) {
        if (memcmp(text + i, pattern, sizeof(pattern)) == 0) {
            jge = text + i + sizeof(pattern);
            break;
        }
    }

    if (!jge) {
        fprintf(stderr, "[finalizeload_patch] pattern not found — patch not installed\n");
        return;
    }

    /* Sanity check: should be a far jge (0f 8d) */
    if (jge[0] != 0x0f || jge[1] != 0x8d) {
        fprintf(stderr,
                "[finalizeload_patch] unexpected opcode at pattern+6: %02x %02x — skipped\n",
                jge[0], jge[1]);
        return;
    }

    mprotect_page(jge, PROT_READ | PROT_WRITE | PROT_EXEC);
    memcpy(jge, nops, 6);
    mprotect_page(jge, PROT_READ | PROT_EXEC);

    fprintf(stderr,
            "[finalizeload_patch] installed — jge@0x%lx (offset 0x%lx) → 6×NOP\n",
            (uintptr_t)jge, (uintptr_t)(jge - res.text_start));
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

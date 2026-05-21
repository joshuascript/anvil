/*
 * libsbox_htmlcb_patch.c
 *
 * Patches the ISteamHTMLSurface callback in libengine2.so to prevent a crash
 * caused by an ABI mismatch in the callback's second argument (RSI).
 *
 * Root cause
 * ----------
 * A function in libengine2.so is registered as a callback that steamclient.so
 * invokes during HTML surface operations.  The callback expects RSI to be a
 * nullable pointer to navigation data.  On Linux, steamclient.so passes the
 * browser handle integer (e.g. 8) in RSI instead, causing a SIGSEGV:
 *
 *   test   %rsi,%rsi          ; non-zero — null branch not taken
 *   je     <skip>             ; skipped because RSI == 8, not 0
 *   mov    (%rsi),%rsi        ; CRASH — dereferences integer 8 as pointer
 *
 * Fix
 * ---
 * Replace the 3-byte `mov (%rsi),%rsi` (48 8b 36) with `xor rsi,rsi` (48 31 f6).
 * This zeroes RSI at that point, equivalent to the null branch the function
 * already handles correctly.
 *
 * Dynamic scan
 * ------------
 * The patch locates the crash instruction at runtime by scanning the executable
 * PT_LOAD segment of libengine2.so for the 8-byte pattern:
 *
 *   74 03           je +3          (conditional skip over the dereference)
 *   48 8b 36        mov (%rsi),%rsi  ← crash instruction at pattern+2
 *   4c 8d 2d        lea r13,[rip+…] (ISteamHTMLSurface global load — follows immediately)
 *
 * The `74 03` displacement is fixed because it skips exactly 3 bytes (the length
 * of `48 8b 36`).  The `4c 8d 2d` (lea r13 RIP-relative) is characteristic of
 * this specific callback.  The combination is unique in libengine2.so.
 *
 * Why dlopen hook, not constructor
 * --------------------------------
 * sbox dlopen's libengine2.so at runtime — it is not a direct ELF dependency.
 * A plain __attribute__((constructor)) fires before libengine2.so is loaded, so
 * dl_iterate_phdr finds nothing.  The dlopen interpose fires at exactly the right
 * time.
 *
 * Offset history (libengine2.so) — for reference only, not used at runtime
 * -------------------------------------------------------------------------
 * Date       | Pattern addr | Crash insn  | Notes
 * 2026-05-18 | 0x34d181     | 0x34d186    | Original
 * 2026-05-19 | 0x34d1a1     | 0x34d1a6    | After update (+0x20, endbr64 + stack canary added)
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

/*
 * 74 03       je +3           (skip 3 bytes = length of mov (%rsi),%rsi)
 * 48 8b 36    mov (%rsi),%rsi ← crash instruction at CRASH_INSN_OFFSET
 * 4c 8d 2d    lea r13,[rip+…] (ISteamHTMLSurface global — immediately follows)
 */
static const uint8_t pattern[] = { 0x74, 0x03, 0x48, 0x8b, 0x36, 0x4c, 0x8d, 0x2d };
#define CRASH_INSN_OFFSET  2   /* offset of 48 8b 36 within pattern */

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

static void do_patch(void)
{
    struct find_result res = { 0, 0, 0 };
    dl_iterate_phdr(find_engine2, &res);

    if (!res.found) {
        fprintf(stderr, "[htmlcb_patch] libengine2.so not found — patch not installed\n");
        return;
    }

    uint8_t *text  = (uint8_t *)res.text_start;
    size_t   limit = res.text_size - sizeof(pattern);
    uint8_t *match = NULL;

    for (size_t i = 0; i < limit; i++) {
        if (memcmp(text + i, pattern, sizeof(pattern)) == 0) {
            match = text + i;
            break;
        }
    }

    if (!match) {
        fprintf(stderr, "[htmlcb_patch] pattern not found — patch not installed\n");
        return;
    }

    uint8_t *insn = match + CRASH_INSN_OFFSET;

    /* Sanity check: should be mov (%rsi),%rsi */
    if (insn[0] != 0x48 || insn[1] != 0x8b || insn[2] != 0x36) {
        fprintf(stderr,
                "[htmlcb_patch] unexpected bytes at pattern+%d: %02x %02x %02x "
                "— binary version mismatch?\n",
                CRASH_INSN_OFFSET, insn[0], insn[1], insn[2]);
        return;
    }

    long  pgsz = getpagesize();
    void *page = (void *)((uintptr_t)insn & ~(uintptr_t)(pgsz - 1));

    if (mprotect(page, (size_t)pgsz, PROT_READ | PROT_WRITE | PROT_EXEC) != 0) {
        perror("[htmlcb_patch] mprotect(RWX) failed");
        return;
    }

    insn[0] = 0x48; insn[1] = 0x31; insn[2] = 0xf6;  /* xor rsi,rsi */

    if (mprotect(page, (size_t)pgsz, PROT_READ | PROT_EXEC) != 0)
        perror("[htmlcb_patch] mprotect(RX) restore failed");

    fprintf(stderr,
            "[htmlcb_patch] installed — pattern@0x%lx  insn@0x%lx  "
            "(mov rsi,[rsi] → xor rsi,rsi)\n",
            (uintptr_t)match,
            (uintptr_t)insn);
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

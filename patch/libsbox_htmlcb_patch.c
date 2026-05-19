/*
 * libsbox_htmlcb_patch.c
 *
 * Patches the ISteamHTMLSurface callback at ELF offset 0x34d160 in
 * libengine2.so to prevent a crash caused by an ABI mismatch in the
 * callback's second argument (RSI).
 *
 * Root cause
 * ----------
 * The function at 0x34d160 is registered by libengine2.so as a callback
 * that steamclient.so invokes during HTML surface operations.  The callback
 * expects RSI to be a pointer to a URL/navigation data struct (nullable).
 * On Linux, steamclient.so passes the browser handle integer (e.g. 8) in
 * RSI instead, causing a SIGSEGV when the function dereferences it:
 *
 *   34d171:  test   %rsi,%rsi     ; non-zero — not taken as null
 *   34d184:  je     34d189        ; skipped because RSI == 8, not 0
 *   34d186:  mov    (%rsi),%rsi   ; CRASH — dereferences integer 8 as ptr
 *
 * Fix
 * ---
 * Replace the 3-byte MOV at 0x34d186 with XOR RSI,RSI (also 3 bytes):
 *   48 8b 36  →  48 31 f6
 *
 * This zeroes RSI at that point, which is equivalent to the null branch
 * the function already handles correctly (the je at 0x34d184 would have
 * taken the same skip path if RSI had been null from the start).  The
 * rest of the function then proceeds with RSI=0 (no-op / empty data).
 *
 * Why dlopen hook, not constructor
 * --------------------------------
 * sbox is a launcher that dlopen's libengine2.so at runtime — it is NOT
 * a direct ELF dependency.  A plain __attribute__((constructor)) fires at
 * LD_PRELOAD load time, before libengine2.so exists in the process, so
 * dl_iterate_phdr finds nothing.  Instead we interpose dlopen: when the
 * call for "libengine2.so" returns we immediately apply the patch.
 *
 * Load via LD_PRELOAD before sbox:
 *   export LD_PRELOAD=".../libsbox_htmlcb_patch.so"
 *
 * Build (handled by anvil/patch_engine.sh):
 *   gcc -shared -fPIC -O2 -o libsbox_htmlcb_patch.so \
 *       anvil/shims/libsbox_htmlcb_patch.c -ldl
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

/*
 * ELF offset of the crashing instruction within libengine2.so.
 * Instruction: 48 8b 36  (mov rsi, [rsi])  — 3 bytes
 * Replacement: 48 31 f6  (xor rsi, rsi)    — 3 bytes
 */
#define CRASH_INSN_OFFSET  0x34d186UL
#define PATCH_BYTES        "\x48\x31\xf6"   /* xor rsi, rsi */
#define PATCH_LEN          3

static pthread_once_t  patch_once = PTHREAD_ONCE_INIT;

/* dl_iterate_phdr callback — locates libengine2.so and captures its load base. */
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

static void do_patch(void)
{
    struct find_result res = { 0, 0 };
    dl_iterate_phdr(find_engine2, &res);

    if (!res.found) {
        fprintf(stderr, "[htmlcb_patch] libengine2.so not found — patch not installed\n");
        return;
    }

    uintptr_t base  = res.base;
    uint8_t  *insn  = (uint8_t *)(base + CRASH_INSN_OFFSET);

    long   pgsz = getpagesize();
    void  *page = (void *)((uintptr_t)insn & ~(uintptr_t)(pgsz - 1));

    /* Verify we're patching the expected bytes before overwriting. */
    if (memcmp(insn, "\x48\x8b\x36", PATCH_LEN) != 0) {
        fprintf(stderr,
                "[htmlcb_patch] unexpected bytes at 0x%lx+0x%lx — patch skipped "
                "(binary version mismatch?)\n",
                base, CRASH_INSN_OFFSET);
        return;
    }

    if (mprotect(page, (size_t)pgsz, PROT_READ | PROT_WRITE | PROT_EXEC) != 0) {
        perror("[htmlcb_patch] mprotect(RWX) failed — patch not installed");
        return;
    }

    memcpy(insn, PATCH_BYTES, PATCH_LEN);

    if (mprotect(page, (size_t)pgsz, PROT_READ | PROT_EXEC) != 0)
        perror("[htmlcb_patch] mprotect(RX) restore failed");

    fprintf(stderr,
            "[htmlcb_patch] installed — base=0x%lx  patched=0x%lx  "
            "(mov rsi,[rsi] → xor rsi,rsi)\n",
            base, (uintptr_t)insn);
}

/*
 * Interpose dlopen.  When libengine2.so is loaded, apply the patch once.
 * pthread_once guards against concurrent dlopen calls racing to patch.
 */
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

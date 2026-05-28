/*
 * libsbox_cefvtable_patch.c
 *
 * Patches a vtable dispatch in libengine2.so that crashes when a CEF browser
 * object is accessed after teardown (vtable ptr overwritten with F_CHROME sentinel).
 *
 * Root cause
 * ----------
 * The Sandbox gamemode triggers a lobby timeout ("Didn't enter lobby in a
 * reasonable time!"), which tears down a CEF browser object.  Concurrent UGC/
 * workshop callbacks still hold a raw pointer to that object and try to call a
 * virtual method through it.  CEF writes the type sentinel F_CHROME
 * (0x454d4f5248435f46) where the vtable pointer was.  The engine checks for
 * null but not for this sentinel, causing a SIGSEGV on the main thread:
 *
 *   mov    (%rsi),%rax      ; loads F_CHROME from torn-down object
 *   test   %rax,%rax        ; non-zero — null branch skipped
 *   je     <safe_exit>
 *   mov    (%rax),%rsi      ; CRASH — dereferences 0x454d4f5248435f46
 *   ...
 *   call   *0x118(%rax)     ; intended vtable call (never reached)
 *
 * Fix
 * ---
 * Trampoline the 5-byte `test rax,rax / je` sequence.  The trampoline adds a
 * sentinel check after the null check, jumping to the existing safe-exit path
 * if the F_CHROME value is detected.
 *
 * Dynamic scan
 * ------------
 * Scans executable PT_LOAD segments of libengine2.so for (wildcards at je
 * displacement bytes):
 *
 *   48 8b 06                 mov rax,[rsi]
 *   48 85 c0                 test rax,rax       <- 5-byte patch site
 *   74 ??                    je <safe_exit>
 *   48 8b 30                 mov rsi,[rax]      <- crash instruction
 *   48 85 f6                 test rsi,rsi
 *   74 ??                    je <safe_exit>
 *   48 8b 06                 mov rax,[rsi]
 *   48 89 7d e8              mov [rbp-0x18],rdi
 *   ff 90 18 01 00 00        call [rax+0x118]   <- vtable slot anchor
 *
 * Confirmed unique (1 hit) in libengine2.so at file offset 0x34df30.
 *
 * Offset history (libengine2.so) — for reference only, not used at runtime
 * -------------------------------------------------------------------------
 * Date       | Pattern addr | Patch site  | Notes
 * 2026-05-21 | 0x34df30     | 0x34df33    | Initial
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

#define F_CHROME_SENTINEL  UINT64_C(0x454d4f5248435f46)

#define PATCH_OFFSET  3   /* test rax,rax + je — 5 bytes replaced by jmp rel32 */
#define CRASH_OFFSET  8   /* mov rsi,[rax] — execution resumes here if clean    */
#define PATTERN_LEN  29

static pthread_once_t patch_once = PTHREAD_ONCE_INIT;
static uint8_t *g_trampoline = NULL;

struct seg_info {
    uintptr_t start;
    size_t    size;
    int       found;
};

static int find_engine2(struct dl_phdr_info *info, size_t sz, void *data)
{
    (void)sz;
    if (!info->dlpi_name || !strstr(info->dlpi_name, "libengine2.so"))
        return 0;
    struct seg_info *s = data;
    for (int i = 0; i < info->dlpi_phnum; i++) {
        const ElfW(Phdr) *ph = &info->dlpi_phdr[i];
        if (ph->p_type == PT_LOAD && (ph->p_flags & PF_X)) {
            s->start = (uintptr_t)info->dlpi_addr + ph->p_vaddr;
            s->size  = ph->p_memsz;
            s->found = 1;
            return 1;
        }
    }
    return 0;
}

static void do_patch(void)
{
    struct seg_info seg = {0, 0, 0};
    dl_iterate_phdr(find_engine2, &seg);
    if (!seg.found) {
        fprintf(stderr, "[cefvtable_patch] libengine2.so not found — skipping\n");
        return;
    }

    uint8_t *text  = (uint8_t *)seg.start;
    size_t   limit = seg.size > PATTERN_LEN ? seg.size - PATTERN_LEN : 0;
    uint8_t *match = NULL;

    for (size_t i = 0; i < limit; i++) {
        uint8_t *p = text + i;
        if (p[0]==0x48 && p[1]==0x8b && p[2]==0x06 &&                /* mov rax,[rsi]        */
            p[3]==0x48 && p[4]==0x85 && p[5]==0xc0 &&                /* test rax,rax         */
            p[6]==0x74 &&                                              /* je (?? wildcard)     */
            p[8]==0x48 && p[9]==0x8b && p[10]==0x30 &&               /* mov rsi,[rax] ←crash */
            p[11]==0x48 && p[12]==0x85 && p[13]==0xf6 &&             /* test rsi,rsi         */
            p[14]==0x74 &&                                             /* je (?? wildcard)     */
            p[16]==0x48 && p[17]==0x8b && p[18]==0x06 &&             /* mov rax,[rsi]        */
            p[19]==0x48 && p[20]==0x89 && p[21]==0x7d && p[22]==0xe8 && /* mov [rbp-0x18],rdi */
            p[23]==0xff && p[24]==0x90 &&                              /* call [rax+           */
            p[25]==0x18 && p[26]==0x01 && p[27]==0x00 && p[28]==0x00) /* 0x118]              */
        {
            match = p;
            break;
        }
    }

    if (!match) {
        fprintf(stderr,
            "[cefvtable_patch] pattern not found — binary mismatch? patch not installed\n");
        return;
    }

    uint8_t *patch_site = match + PATCH_OFFSET;
    uint8_t *crash_insn = match + CRASH_OFFSET;

    /* Sanity: expect 48 85 c0 74 ?? at patch site */
    if (patch_site[0] != 0x48 || patch_site[1] != 0x85 ||
        patch_site[2] != 0xc0 || patch_site[3] != 0x74) {
        fprintf(stderr,
            "[cefvtable_patch] unexpected bytes at patch site: %02x %02x %02x %02x"
            " — binary mismatch? patch not installed\n",
            patch_site[0], patch_site[1], patch_site[2], patch_site[3]);
        return;
    }

    /*
     * Safe-exit address: derived from the je displacement at patch_site[3..4].
     * The je is 2 bytes at patch_site[3]; next instruction = patch_site+5 = crash_insn.
     * Target = crash_insn + (int8_t)patch_site[4].
     */
    uint8_t *safe_exit = crash_insn + (int8_t)patch_site[4];

    /*
     * Allocate trampoline within jmp rel32 range (±2 GB) of patch_site.
     * Scan outward from the patch site in both directions using MAP_FIXED_NOREPLACE
     * so we only land in genuinely free pages without disturbing existing mappings.
     */
    {
        long pgsz = getpagesize();
        uintptr_t base = (uintptr_t)patch_site & ~(uintptr_t)(pgsz - 1);
        g_trampoline = NULL;
        for (uintptr_t step = (uintptr_t)pgsz; step < (1UL << 31); step <<= 1) {
            for (int dir = -1; dir <= 1; dir += 2) {
                uintptr_t addr = base + (uintptr_t)((intptr_t)step * dir);
                if (addr == 0) continue;
                void *p = mmap((void *)addr, 4096,
                               PROT_READ | PROT_WRITE | PROT_EXEC,
                               MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE,
                               -1, 0);
                if (p == MAP_FAILED) continue;
                intptr_t reach = (intptr_t)p - (intptr_t)(patch_site + 5);
                if (reach >= -(1L << 31) && reach <= (1L << 31) - 1) {
                    g_trampoline = (uint8_t *)p;
                    break;
                }
                munmap(p, 4096);
            }
            if (g_trampoline) break;
        }
    }

    if (!g_trampoline) {
        fprintf(stderr,
            "[cefvtable_patch] could not allocate trampoline within jmp rel32 range"
            " — patch not installed\n");
        return;
    }

    /*
     * Build trampoline:
     *   48 85 c0              test rax,rax
     *   0f 84 ?? ?? ?? ??     je rel32 -> safe_exit
     *   49 bb ?? x8           movabs r11, F_CHROME_SENTINEL
     *   4c 39 d8              cmp rax,r11
     *   0f 84 ?? ?? ?? ??     je rel32 -> safe_exit
     *   e9 ?? ?? ?? ??        jmp rel32 -> crash_insn  (clean path)
     */
    uint8_t *t = g_trampoline;

#define EMIT(b)     (*t++ = (uint8_t)(b))
#define EMIT32(v)   do { int32_t _v = (int32_t)(intptr_t)(v); memcpy(t, &_v, 4); t += 4; } while(0)
#define EMIT64(v)   do { uint64_t _v = (uint64_t)(v); memcpy(t, &_v, 8); t += 8; } while(0)
#define REL32(tgt)  ((intptr_t)(tgt) - (intptr_t)(t + 4))

    EMIT(0x48); EMIT(0x85); EMIT(0xc0);               /* test rax,rax            */
    EMIT(0x0f); EMIT(0x84); EMIT32(REL32(safe_exit)); /* je rel32 -> safe_exit   */
    EMIT(0x49); EMIT(0xbb); EMIT64(F_CHROME_SENTINEL); /* movabs r11, sentinel   */
    EMIT(0x4c); EMIT(0x39); EMIT(0xd8);               /* cmp rax,r11             */
    EMIT(0x0f); EMIT(0x84); EMIT32(REL32(safe_exit)); /* je rel32 -> safe_exit   */
    EMIT(0xe9); EMIT32(REL32(crash_insn));             /* jmp rel32 -> crash_insn */

#undef EMIT
#undef EMIT32
#undef EMIT64
#undef REL32

    /* Write jmp rel32 over the 5-byte patch site */
    long  pgsz = getpagesize();
    void *page = (void *)((uintptr_t)patch_site & ~(uintptr_t)(pgsz - 1));

    if (mprotect(page, (size_t)pgsz * 2, PROT_READ | PROT_WRITE | PROT_EXEC) != 0) {
        perror("[cefvtable_patch] mprotect RWX failed");
        munmap(g_trampoline, 4096);
        g_trampoline = NULL;
        return;
    }

    patch_site[0] = 0xe9;
    int32_t jmp_disp = (int32_t)((intptr_t)g_trampoline - (intptr_t)(patch_site + 5));
    memcpy(patch_site + 1, &jmp_disp, 4);

    if (mprotect(page, (size_t)pgsz * 2, PROT_READ | PROT_EXEC) != 0)
        perror("[cefvtable_patch] mprotect RX restore failed");

    fprintf(stderr,
        "[cefvtable_patch] installed —"
        " pattern@%p  patch_site@%p  trampoline@%p  safe_exit@%p\n"
        "[cefvtable_patch] guards: rax==null -> skip, rax==F_CHROME(0x%016lx) -> skip\n",
        (void *)match, (void *)patch_site,
        (void *)g_trampoline, (void *)safe_exit,
        (unsigned long)F_CHROME_SENTINEL);
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

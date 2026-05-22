/*
 * libsbox_hashtable_patch.c
 *
 * Patches a hash table bucket lookup in libengine2.so that crashes when a
 * concurrent resize leaves the computed bucket slot pointer out of bounds.
 *
 * Root cause
 * ----------
 * A .NET TP Worker thread performs a hash table lookup while the main thread
 * concurrently resizes the table.  The lookup computes a bucket index using
 * the capacity held in rcx at div time, but the bucket array pointer is
 * reloaded from [rbx] after the resize.  If the new array is smaller (or the
 * old one was freed and unmapped), the computed slot is past the end:
 *
 *   div    %rcx               ; rdx = hash % capacity  (rcx = [rbx+0x1c] or [rbx+0x24])
 *   mov    (%rbx),%rax        ; reload bucket array (may now be a different allocation)
 *   mov    0x8(%rbx),%r15     ; reload [rbx+8]
 *   movslq %edx,%rdx          ; sign-extend index
 *   lea    (%rax,%rdx,8),%r13  ; slot = new_array_base + old_index * 8
 *   mov    0x0(%r13),%rbx     ; CRASH — slot is unmapped
 *
 * The crash is on a .NET TP Worker thread.  r12 = 0x672244bb (the hash key)
 * is identical across all observed crashes, suggesting the same lookup
 * consistently hits the race window during UGC/workshop content loading.
 *
 * Fix
 * ---
 * Trampoline the 5-byte patch site (4 bytes of `lea r13` + first byte of
 * `mov rbx,[r13]`).  The trampoline performs two bounds checks before the
 * slot dereference:
 *
 *   cmp    edx, [rbx+0x1c]   ; is index within old/second capacity?
 *   jb     .proceed
 *   cmp    edx, [rbx+0x24]   ; is index within current/first capacity?
 *   jb     .proceed
 *   jmp    safe_exit          ; out of bounds for both — empty bucket path
 *  .proceed:
 *   lea    r13, [rax+rdx*8]  ; original instruction (reconstructed)
 *   mov    rbx, [r13]        ; original crash instruction (reconstructed)
 *   jmp    continue           ; resume at test rbx,rbx
 *
 * Dynamic scan
 * ------------
 * Scans executable PT_LOAD of libengine2.so for (wildcard at je displacement):
 *
 *   8b 4b 1c                 mov ecx,[rbx+0x1c]   <- old/second capacity
 *   4c 89 e0                 mov rax,r12
 *   31 d2                    xor edx,edx
 *   48 f7 f1                 div rcx
 *   48 8b 03                 mov rax,[rbx]
 *   4c 8b 7b 08              mov r15,[rbx+0x8]
 *   48 63 d2                 movslq edx,edx
 *   4c 8d 2c d0              lea r13,[rax+rdx*8]  <- 5-byte patch site
 *   49 8b 5d 00              mov rbx,[r13]        <- crash instruction
 *   48 85 db                 test rbx,rbx
 *   74 ??                    je <safe_exit>        <- wildcard
 *
 * Confirmed unique (1 hit) in libengine2.so at file offset 0x15bc0a7.
 *
 * Offset history (libengine2.so) — for reference only, not used at runtime
 * -------------------------------------------------------------------------
 * Date       | Pattern addr | Patch site  | Notes
 * 2026-05-21 | 0x15bc0a7    | 0x15bc0bc   | Initial
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
 * Offsets within the pattern:
 *   PATCH_OFFSET   — first byte of lea r13,[rax+rdx*8] (5 bytes replaced by jmp rel32)
 *   RESUME_OFFSET  — first byte of test rbx,rbx (trampoline jumps here after patch)
 *   PATTERN_LEN    — total pattern length (je displacement is at [PATTERN_LEN-1])
 */
#define PATCH_OFFSET   21
#define RESUME_OFFSET  29
#define PATTERN_LEN    34

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
        fprintf(stderr, "[hashtable_patch] libengine2.so not found — skipping\n");
        return;
    }

    uint8_t *text  = (uint8_t *)seg.start;
    size_t   limit = seg.size > PATTERN_LEN ? seg.size - PATTERN_LEN : 0;
    uint8_t *match = NULL;

    for (size_t i = 0; i < limit; i++) {
        uint8_t *p = text + i;
        if (p[0]==0x8b && p[1]==0x4b && p[2]==0x1c &&       /* mov ecx,[rbx+0x1c]    */
            p[3]==0x4c && p[4]==0x89 && p[5]==0xe0 &&        /* mov rax,r12            */
            p[6]==0x31 && p[7]==0xd2 &&                       /* xor edx,edx            */
            p[8]==0x48 && p[9]==0xf7 && p[10]==0xf1 &&       /* div rcx                */
            p[11]==0x48 && p[12]==0x8b && p[13]==0x03 &&     /* mov rax,[rbx]          */
            p[14]==0x4c && p[15]==0x8b && p[16]==0x7b && p[17]==0x08 && /* mov r15,[rbx+8] */
            p[18]==0x48 && p[19]==0x63 && p[20]==0xd2 &&     /* movslq edx,edx         */
            p[21]==0x4c && p[22]==0x8d && p[23]==0x2c && p[24]==0xd0 && /* lea r13,[rax+rdx*8] */
            p[25]==0x49 && p[26]==0x8b && p[27]==0x5d && p[28]==0x00 && /* mov rbx,[r13]  */
            p[29]==0x48 && p[30]==0x85 && p[31]==0xdb &&     /* test rbx,rbx           */
            p[32]==0x74)                                       /* je ?? (wildcard)       */
        {
            match = p;
            break;
        }
    }

    if (!match) {
        fprintf(stderr,
            "[hashtable_patch] pattern not found — binary mismatch? patch not installed\n");
        return;
    }

    uint8_t *patch_site  = match + PATCH_OFFSET;
    uint8_t *resume_insn = match + RESUME_OFFSET;

    /* Sanity: expect 4c 8d 2c d0 49 at patch site */
    if (patch_site[0] != 0x4c || patch_site[1] != 0x8d ||
        patch_site[2] != 0x2c || patch_site[3] != 0xd0 || patch_site[4] != 0x49) {
        fprintf(stderr,
            "[hashtable_patch] unexpected bytes at patch site: %02x %02x %02x %02x %02x"
            " — binary mismatch? patch not installed\n",
            patch_site[0], patch_site[1], patch_site[2], patch_site[3], patch_site[4]);
        return;
    }

    /*
     * Safe-exit address: derived from the je displacement at match[32..33].
     * je is 2 bytes; next instruction = match + PATTERN_LEN.
     * Target = (match + PATTERN_LEN) + (int8_t)match[PATTERN_LEN - 1].
     */
    uint8_t *safe_exit = (match + PATTERN_LEN) + (int8_t)match[PATTERN_LEN - 1];

    /*
     * Allocate trampoline within jmp rel32 range (±2 GB) of patch_site using
     * MAP_FIXED_NOREPLACE, scanning outward in both directions at exponentially
     * increasing offsets until a free page is found within reach.
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
            "[hashtable_patch] could not allocate trampoline within jmp rel32 range"
            " — patch not installed\n");
        return;
    }

    /*
     * Build trampoline (28 bytes):
     *
     *  [+0]  3b 53 1c           cmp    edx, [rbx+0x1c]
     *  [+3]  72 0a              jb     +10  -> .proceed
     *  [+5]  3b 53 24           cmp    edx, [rbx+0x24]
     *  [+8]  72 05              jb     +5   -> .proceed
     *  [+10] e9 ?? ?? ?? ??     jmp    rel32 -> safe_exit
     *  [+15] 4c 8d 2c d0        lea    r13, [rax+rdx*8]   (original)
     *  [+19] 49 8b 5d 00        mov    rbx, [r13]          (original)
     *  [+23] e9 ?? ?? ?? ??     jmp    rel32 -> resume_insn
     */
    uint8_t *t = g_trampoline;

#define EMIT(b)     (*t++ = (uint8_t)(b))
#define EMIT32(v)   do { int32_t _v = (int32_t)(intptr_t)(v); memcpy(t, &_v, 4); t += 4; } while(0)
#define REL32(tgt)  ((intptr_t)(tgt) - (intptr_t)(t + 4))

    EMIT(0x3b); EMIT(0x53); EMIT(0x1c);         /* cmp edx,[rbx+0x1c]          */
    EMIT(0x72); EMIT(0x0a);                       /* jb +10 -> .proceed          */
    EMIT(0x3b); EMIT(0x53); EMIT(0x24);         /* cmp edx,[rbx+0x24]          */
    EMIT(0x72); EMIT(0x05);                       /* jb +5  -> .proceed          */
    EMIT(0xe9); EMIT32(REL32(safe_exit));         /* jmp rel32 -> safe_exit      */
    /* .proceed: */
    EMIT(0x4c); EMIT(0x8d); EMIT(0x2c); EMIT(0xd0); /* lea r13,[rax+rdx*8]     */
    EMIT(0x49); EMIT(0x8b); EMIT(0x5d); EMIT(0x00); /* mov rbx,[r13]            */
    EMIT(0xe9); EMIT32(REL32(resume_insn));       /* jmp rel32 -> resume_insn    */

#undef EMIT
#undef EMIT32
#undef REL32

    /* Write jmp rel32 over the 5-byte patch site */
    long  pgsz = getpagesize();
    void *page = (void *)((uintptr_t)patch_site & ~(uintptr_t)(pgsz - 1));

    if (mprotect(page, (size_t)pgsz * 2, PROT_READ | PROT_WRITE | PROT_EXEC) != 0) {
        perror("[hashtable_patch] mprotect RWX failed");
        munmap(g_trampoline, 4096);
        g_trampoline = NULL;
        return;
    }

    patch_site[0] = 0xe9;
    int32_t jmp_disp = (int32_t)((intptr_t)g_trampoline - (intptr_t)(patch_site + 5));
    memcpy(patch_site + 1, &jmp_disp, 4);

    if (mprotect(page, (size_t)pgsz * 2, PROT_READ | PROT_EXEC) != 0)
        perror("[hashtable_patch] mprotect RX restore failed");

    fprintf(stderr,
        "[hashtable_patch] installed —"
        " pattern@%p  patch_site@%p  trampoline@%p\n"
        "[hashtable_patch] safe_exit@%p  resume@%p\n"
        "[hashtable_patch] guards: edx<[rbx+0x1c] or edx<[rbx+0x24] to proceed;"
        " else -> safe_exit\n",
        (void *)match, (void *)patch_site,
        (void *)g_trampoline, (void *)safe_exit, (void *)resume_insn);
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

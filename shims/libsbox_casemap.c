/*
 * libsbox_casemap.c
 *
 * LD_PRELOAD shim: resolves wrong-cased file paths for s&box on Linux.
 *
 * The engine was written for Windows (case-insensitive NTFS). On Linux,
 * paths like "addons/base/assets" fail when the real path is "addons/base/Assets".
 * This shim intercepts the three syscall wrappers that carry game-content paths
 * (fstatat, openat, inotify_add_watch) and corrects casing before the kernel sees them.
 *
 * Algorithm — segment map cache:
 *   1. CWD anchor — only paths under the game directory are processed.
 *   2. Fast path  — stat() the path as given; if it exists, pass through immediately.
 *   3. Neg cache  — if the lowercase path is a confirmed miss, pass through immediately.
 *   4. Segment walk — split the relative path into components. For each component,
 *      scan its parent directory once and cache a lowercase→real mapping. Resolve
 *      each segment case-insensitively (FirstOrDefault match).
 *   5. On walk failure, add the lowercase path to the neg cache.
 *
 * Build (from game/bin/linuxsteamrt64/):
 *   gcc -shared -fPIC -O2 -o libsbox_casemap.so shims/libsbox_casemap.c -ldl
 */

#define _GNU_SOURCE
#include <ctype.h>
#include <dirent.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <limits.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/inotify.h>
#include <sys/stat.h>
#include <unistd.h>

/* ── open-addressing hash map ───────────────────────────────────── */

static uint64_t fnv1a(const char *s) {
    uint64_t h = 14695981039346656037ULL;
    for (; *s; s++) { h ^= (uint8_t)*s; h *= 1099511628211ULL; }
    return h;
}

typedef struct { char *k; void *v; } Slot;
typedef struct { Slot *b; size_t cap, size; } Map;

static void map_set(Map *m, const char *k, void *v);

static void map_init(Map *m, size_t cap) {
    m->b   = calloc(cap, sizeof *m->b);
    m->cap = cap;
    m->size = 0;
}

static void map_grow(Map *m) {
    Map n; map_init(&n, m->cap * 2);
    for (size_t i = 0; i < m->cap; i++)
        if (m->b[i].k) map_set(&n, m->b[i].k, m->b[i].v);
    free(m->b); *m = n;
}

static void map_set(Map *m, const char *k, void *v) {
    if (m->size * 4 >= m->cap * 3) map_grow(m);
    size_t i = fnv1a(k) % m->cap;
    while (m->b[i].k && strcmp(m->b[i].k, k) != 0) i = (i + 1) % m->cap;
    if (!m->b[i].k) { m->b[i].k = strdup(k); m->size++; }
    m->b[i].v = v;
}

static void *map_get(const Map *m, const char *k) {
    size_t i = fnv1a(k) % m->cap;
    while (m->b[i].k) {
        if (strcmp(m->b[i].k, k) == 0) return m->b[i].v;
        i = (i + 1) % m->cap;
    }
    return NULL;
}

static int  map_has(const Map *m, const char *k) { return map_get(m, k) != NULL; }
static void set_add(Map *m, const char *k)        { map_set(m, k, (void *)1); }

/* ── globals ────────────────────────────────────────────────────── */

static char   cwd[PATH_MAX];
static size_t cwd_len;

static Map dir_map;              /* dir_path → Map* { lowercase_child → real_child } */
static Map neg;                  /* set of confirmed-missing lowercase paths */

static int    (*real_openat)           (int, const char *, int, ...) = NULL;
static int    (*real_fstatat)          (int, const char *, struct stat *, int) = NULL;
static int    (*real_inotify_add_watch)(int, const char *, uint32_t) = NULL;

/* C-level wrappers used by libtier0/libengine2 (not caught by openat intercept) */
static int    (*real_open)    (const char *, int, ...) = NULL;
static int    (*real_open64)  (const char *, int, ...) = NULL;
static FILE * (*real_fopen)   (const char *, const char *) = NULL;
static FILE * (*real_fopen64) (const char *, const char *) = NULL;
static FILE * (*real_freopen64)(const char *, const char *, FILE *) = NULL;
static int    (*real_stat)    (const char *, struct stat *) = NULL;
static int    (*real_stat64)  (const char *, struct stat64 *) = NULL;
static int    (*real_lstat64) (const char *, struct stat64 *) = NULL;
static int    (*real_access)  (const char *, int) = NULL;

static __thread int active = 0;  /* re-entrancy guard — prevents recursion from opendir() */

/* ── init ───────────────────────────────────────────────────────── */

__attribute__((constructor))
static void casemap_init(void) {
    real_openat            = dlsym(RTLD_NEXT, "openat");
    real_fstatat           = dlsym(RTLD_NEXT, "fstatat");
    real_inotify_add_watch = dlsym(RTLD_NEXT, "inotify_add_watch");

    real_open      = dlsym(RTLD_NEXT, "open");
    real_open64    = dlsym(RTLD_NEXT, "open64");
    real_fopen     = dlsym(RTLD_NEXT, "fopen");
    real_fopen64   = dlsym(RTLD_NEXT, "fopen64");
    real_freopen64 = dlsym(RTLD_NEXT, "freopen64");
    real_stat      = dlsym(RTLD_NEXT, "stat");
    real_stat64    = dlsym(RTLD_NEXT, "stat64");
    real_lstat64   = dlsym(RTLD_NEXT, "lstat64");
    real_access    = dlsym(RTLD_NEXT, "access");

    /* Anchor CWD to the directory containing the game executable.
     * /proc/self/exe is more reliable than getcwd() in an LD_PRELOAD context. */
    char exe[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", exe, sizeof exe - 1);
    if (n > 0) {
        exe[n] = '\0';
        char *slash = strrchr(exe, '/');
        if (slash) { *slash = '\0'; snprintf(cwd, sizeof cwd, "%s", exe); }
    }
    if (!cwd[0] && !getcwd(cwd, sizeof cwd)) cwd[0] = '\0';
    cwd_len = strlen(cwd);

    map_init(&dir_map, 256);
    map_init(&neg, 65536);
}

/* ── directory scanner ──────────────────────────────────────────── */

/* Scan dir_path once. Returns a Map* of { lowercase_entry → real_entry }. */
static Map *scan_dir(const char *dir_path) {
    Map *m = calloc(1, sizeof *m);
    map_init(m, 32);
    DIR *d = opendir(dir_path);
    if (!d) return m;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        /* skip . and .. */
        if (e->d_name[0] == '.' &&
            (e->d_name[1] == '\0' || (e->d_name[1] == '.' && e->d_name[2] == '\0')))
            continue;
        char lo[NAME_MAX + 1];
        size_t len = strlen(e->d_name);
        for (size_t i = 0; i <= len; i++)
            lo[i] = (char)tolower((unsigned char)e->d_name[i]);
        map_set(m, lo, strdup(e->d_name));
    }
    closedir(d);
    return m;
}

/* ── core resolver ──────────────────────────────────────────────── */

/*
 * Attempt to resolve path to its real on-disk equivalent.
 * buf must be PATH_MAX bytes.
 * Returns: path  — already correct (fast path), no copy needed
 *          buf   — resolved to a different casing
 *          NULL  — confirmed miss or not under CWD
 */
static const char *resolve(const char *path, char *buf) {
    if (!cwd_len) return NULL;  /* called before constructor — maps not initialised */
    if (!path || strncmp(path, cwd, cwd_len) != 0) return NULL;

    const char *rel = path + cwd_len;
    if (*rel == '/') rel++;
    if (!*rel) return NULL;

    /* Build lowercase version of the full path for cache keying. */
    char lo[PATH_MAX];
    size_t plen = strlen(path);
    for (size_t i = 0; i <= plen; i++) lo[i] = (char)tolower((unsigned char)path[i]);

    /* 1. Negative cache check. */
    if (map_has(&neg, lo)) return NULL;

    /* 2. Fast path — stat() the path exactly as given. */
    struct stat st;
    if (stat(path, &st) == 0) return path;

    /* 3. Segment walk from CWD. */
    char rel_copy[PATH_MAX];
    strncpy(rel_copy, rel, sizeof rel_copy - 1);
    rel_copy[sizeof rel_copy - 1] = '\0';
    snprintf(buf, PATH_MAX, "%s", cwd);

    int ok = 1;
    char *save = NULL;
    char *seg  = strtok_r(rel_copy, "/", &save);
    while (seg) {
        /* Lowercase the segment for lookup. */
        char seg_lo[NAME_MAX + 1];
        size_t slen = strlen(seg);
        for (size_t i = 0; i <= slen; i++)
            seg_lo[i] = (char)tolower((unsigned char)seg[i]);

        /* Fetch (or populate) the cache entry for the current directory. */
        Map *entries = (Map *)map_get(&dir_map, buf);
        if (!entries) {
            entries = scan_dir(buf);
            map_set(&dir_map, buf, entries);
        }

        const char *real = (const char *)map_get(entries, seg_lo);

        if (!real) { ok = 0; break; }

        size_t blen = strlen(buf);
        buf[blen] = '/';
        strncpy(buf + blen + 1, real, PATH_MAX - blen - 2);
        buf[PATH_MAX - 1] = '\0';

        seg = strtok_r(NULL, "/", &save);
    }

    if (ok) return buf;

    /* Walk failed — confirmed miss. Cache it to avoid re-walking. */
    set_add(&neg, lo);
    return NULL;
}

/* ── hooks ──────────────────────────────────────────────────────── */

int openat(int dirfd, const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap; va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }
    if (!active) {
        active = 1;
        char buf[PATH_MAX];
        const char *rp = resolve(path, buf);
        active = 0;
        if (rp && rp != path)
            return real_openat(dirfd, rp, flags, mode);
    }
    return real_openat(dirfd, path, flags, mode);
}

int fstatat(int dirfd, const char *path, struct stat *st, int flags) {
    if (!active) {
        active = 1;
        char buf[PATH_MAX];
        const char *rp = resolve(path, buf);
        active = 0;
        if (rp && rp != path)
            return real_fstatat(dirfd, rp, st, flags);
    }
    return real_fstatat(dirfd, path, st, flags);
}

int inotify_add_watch(int fd, const char *path, uint32_t mask) {
    if (!active) {
        active = 1;
        char buf[PATH_MAX];
        const char *rp = resolve(path, buf);
        active = 0;
        if (rp && rp != path)
            return real_inotify_add_watch(fd, rp, mask);
    }
    return real_inotify_add_watch(fd, path, mask);
}

/* ── C-level wrappers (libtier0 / libengine2) ───────────────────── */

#define LAZY(ptr, sym) do { if (!(ptr)) (ptr) = dlsym(RTLD_NEXT, (sym)); } while(0)
#define TRY_RESOLVE(path, buf) \
    char buf[PATH_MAX]; \
    if (!active) { active = 1; const char *_rp = resolve(path, buf); active = 0; if (_rp && _rp != (path)) (path) = _rp; }

int open(const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) { va_list ap; va_start(ap, flags); mode = va_arg(ap, mode_t); va_end(ap); }
    LAZY(real_open, "open");
    TRY_RESOLVE(path, _buf0);
    return real_open(path, flags, mode);
}

int open64(const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) { va_list ap; va_start(ap, flags); mode = va_arg(ap, mode_t); va_end(ap); }
    LAZY(real_open64, "open64");
    TRY_RESOLVE(path, _buf1);
    return real_open64(path, flags, mode);
}

FILE *fopen(const char *path, const char *mode) {
    LAZY(real_fopen, "fopen");
    TRY_RESOLVE(path, _buf2);
    return real_fopen(path, mode);
}

FILE *fopen64(const char *path, const char *mode) {
    LAZY(real_fopen64, "fopen64");
    TRY_RESOLVE(path, _buf3);
    return real_fopen64(path, mode);
}

FILE *freopen64(const char *path, const char *mode, FILE *stream) {
    LAZY(real_freopen64, "freopen64");
    TRY_RESOLVE(path, _buf4);
    return real_freopen64(path, mode, stream);
}

int stat(const char *path, struct stat *st) {
    LAZY(real_stat, "stat");
    TRY_RESOLVE(path, _buf5);
    return real_stat(path, st);
}

int stat64(const char *path, struct stat64 *st) {
    LAZY(real_stat64, "stat64");
    TRY_RESOLVE(path, _buf6);
    return real_stat64(path, st);
}

int lstat64(const char *path, struct stat64 *st) {
    LAZY(real_lstat64, "lstat64");
    TRY_RESOLVE(path, _buf7);
    return real_lstat64(path, st);
}

int access(const char *path, int mode) {
    LAZY(real_access, "access");
    TRY_RESOLVE(path, _buf8);
    return real_access(path, mode);
}

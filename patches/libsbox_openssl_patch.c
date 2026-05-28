/*
 * libsbox_openssl_patch.c
 *
 * LD_PRELOAD shim: pre-loads and initializes libcrypto.so.3 and libssl.so.3
 * before .NET's TLS stack starts, preventing silent HTTPS failures during the
 * preload screen on systems where the unversioned libssl.so.3 symlink is not
 * in the default search path.
 *
 * Why this is needed
 * ------------------
 * libengine2.so has OpenSSL 3.0.13 statically compiled in and is unaffected.
 * sbox uses the system .NET 10 runtime (DOTNET_ROOT not set). .NET 10's
 * libSystem.Security.Cryptography.Native.OpenSsl.so dlopen's libcrypto.so.3
 * and libssl.so.3 at runtime with no OpenSSL 1.x fallback. Every HttpClient
 * call during the preload screen (Api.cs, Http.cs, HttpImageLoader.cs) makes
 * HTTPS requests; if .NET's dlopen fails silently, all of those fail and the
 * game crashes or hangs on the preload screen.
 *
 * Affected systems: any distro where the unversioned libssl.so.3 symlink is
 * absent or not on the default search path (some minimal installs, containers,
 * or distros that split the symlink into a -dev package).
 *
 * No binary offsets — no pattern scan or test script needed.
 *
 * Credits: @keybangz, @vincetheanimator
 *
 * Build:
 *   bash anvil/launch/patch_engine.sh   (compiles all patches to patches/bin/)
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>

__attribute__((constructor))
static void sbox_openssl_init(void)
{
    void *libcrypto = dlopen("libcrypto.so.3", RTLD_NOW | RTLD_GLOBAL | RTLD_NOLOAD);
    if (!libcrypto)
        libcrypto = dlopen("libcrypto.so.3", RTLD_NOW | RTLD_GLOBAL);
    if (!libcrypto) {
        fprintf(stderr,
            "[openssl_patch] WARNING: libcrypto.so.3 not found — "
            ".NET TLS (HttpClient) will fail during preload screen. "
            "Install the OpenSSL 3 runtime package for your distro "
            "(e.g. libssl3 on Debian/Ubuntu, openssl-libs on Fedora).\n");
        return;
    }

    typedef int (*openssl_init_fn)(uint64_t opts, const void *settings);
    openssl_init_fn init_fn = (openssl_init_fn)dlsym(libcrypto, "OPENSSL_init_crypto");
    if (init_fn)
        /* ADD_ALL_CIPHERS | ADD_ALL_DIGESTS | LOAD_CRYPTO_STRINGS | LOAD_CONFIG */
        init_fn(0x4 | 0x8 | 0x2 | 0x40, NULL);

    typedef void *(*provider_load_fn)(void *ctx, const char *name);
    provider_load_fn load_fn = (provider_load_fn)dlsym(libcrypto, "OSSL_PROVIDER_load");
    if (load_fn) {
        load_fn(NULL, "default");
        load_fn(NULL, "legacy"); /* optional — absent on many systems, failure is silent */
    }

    fprintf(stderr, "[openssl_patch] installed — libcrypto.so.3 initialized\n");

    void *libssl = dlopen("libssl.so.3", RTLD_NOW | RTLD_GLOBAL | RTLD_NOLOAD);
    if (!libssl)
        libssl = dlopen("libssl.so.3", RTLD_NOW | RTLD_GLOBAL);
    if (!libssl) {
        fprintf(stderr,
            "[openssl_patch] WARNING: libssl.so.3 not found — "
            "HTTPS connections will fail.\n");
        return;
    }

    typedef int (*ssl_init_fn)(uint64_t opts, const void *settings);
    ssl_init_fn ssl_init = (ssl_init_fn)dlsym(libssl, "OPENSSL_init_ssl");
    if (ssl_init)
        ssl_init(0, NULL);

    fprintf(stderr, "[openssl_patch] installed — libssl.so.3 initialized\n");
}

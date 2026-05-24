# libsbox_openssl_patch — OpenSSL 3 preload for .NET TLS

## Status
**Applied** — not yet confirmed by user.

---

## Problem

Some Linux systems crash or hang on the preload screen. Only a subset of users
are affected; most systems (including the developer's) work fine.

---

## Root Cause

sbox uses the **system .NET 10 runtime** (`DOTNET_ROOT` not set — falls back to
`/usr/lib/dotnet` or `/usr/share/dotnet`). .NET 10's native TLS interop library:

```
libSystem.Security.Cryptography.Native.OpenSsl.so
```

dlopen's `libcrypto.so.3` and `libssl.so.3` at runtime. It has **no OpenSSL 1.x
fallback** at this version. Every HTTPS call from managed code during the preload
screen goes through this path:

- `Sandbox.Engine/Utility/Web/Http.cs` — `SocketsHttpHandler` / `HttpClient`
- `Sandbox.Engine/Services/Api/Api.cs` — backend API client
- `Sandbox.Engine/Resources/Textures/Loader/HttpImageLoader.cs` — texture HTTP

If `libcrypto.so.3` / `libssl.so.3` are not findable by dlopen, all of those
calls fail silently and the preload screen crashes or never progresses.

**`libengine2.so` is not involved** — it has OpenSSL 3.0.13 statically compiled
in (confirmed via `nm -D`: `OPENSSL_init_crypto`, `OPENSSL_init_ssl`,
`OSSL_PROVIDER_load` all exported as `T` symbols). It never calls out to the
system libcrypto.

### Affected systems

Any distro where the unversioned `libssl.so.3` / `libcrypto.so.3` symlink is
absent or not on the default dlopen search path:

- Distros that split the symlink into a `-dev` package (e.g. `libssl-dev`)
- Minimal installs or containers missing the OpenSSL 3 runtime package
- Unusual `LD_LIBRARY_PATH` configurations that shadow the system path

---

## Fix

`patches/libsbox_openssl_patch.c` — `__attribute__((constructor))` that runs
before .NET loads:

1. `dlopen("libcrypto.so.3", RTLD_NOW | RTLD_GLOBAL)` — loads into process with
   global visibility so .NET's subsequent dlopen finds it already present
2. Calls `OPENSSL_init_crypto` with all ciphers, digests, strings, and config
3. Calls `OSSL_PROVIDER_load` for `default` and `legacy` providers
4. `dlopen("libssl.so.3", RTLD_NOW | RTLD_GLOBAL)` and calls `OPENSSL_init_ssl`

Prints a clear warning to stderr if either library is not found, so the user
knows the dependency is missing rather than seeing a cryptic crash.

**Limitation:** if `libcrypto.so.3` does not exist on the system at all, the
patch skips gracefully but cannot fix the underlying missing package. The warning
message names the package to install (`libssl3` on Debian/Ubuntu,
`openssl-libs` on Fedora).

---

## Verification

Not yet confirmed. Ask the user: "Did that fix the preload screen crash?"

#!/usr/bin/env python3
"""
preload_inotify.py

Temporarily raises fs.inotify.max_user_watches for the duration of an s&box
session, then restores the original value on exit.

Usage (called by launch scripts):
    python3 preload_inotify.py <game_command> [args...]

Why this is needed:
    libengine2.so and libtier0.so use inotify to watch the game content
    directories for file changes. When the inotify watch limit is exhausted
    (Debian default: 8192), the engine falls back to polling via readdir().
    That polling loop runs on the main thread while .NET thread pool workers
    concurrently read the engine's internal file-name hash table, producing a
    race condition that causes a SIGSEGV inside libengine2.so.
"""

import atexit
import os
import signal
import subprocess
import sys

INOTIFY_PATH = "/proc/sys/fs/inotify/max_user_watches"
TARGET        = 524288
SYSCTL_KEY    = "fs.inotify.max_user_watches"


def _read_current() -> int:
    with open(INOTIFY_PATH) as f:
        return int(f.read().strip())


def _set(value: int) -> bool:
    result = subprocess.run(
        ["sudo", "sysctl", "-w", f"{SYSCTL_KEY}={value}"],
        capture_output=True,
    )
    return result.returncode == 0


def _restore(original: int) -> None:
    if not _set(original):
        print(
            f"\n[preload_inotify] WARNING: could not restore {SYSCTL_KEY} to "
            f"{original}. Run manually:\n"
            f"  sudo sysctl -w {SYSCTL_KEY}={original}",
            file=sys.stderr,
        )
    else:
        print(
            f"[preload_inotify] Restored {SYSCTL_KEY} to {original}.",
            file=sys.stderr,
        )


def setup() -> None:
    current = _read_current()

    if current >= TARGET:
        print(
            f"[preload_inotify] {SYSCTL_KEY} is already {current} (>= {TARGET}), "
            f"no change needed.",
            file=sys.stderr,
        )
    else:
        print(
            "\n"
            "  *** s&box inotify watch limit warning ***\n"
            "\n"
            f"  Current {SYSCTL_KEY} = {current}\n"
            f"  Required for stable operation      >= {TARGET}\n"
            "\n"
            "  Why this matters:\n"
            "    s&box watches game content directories for live file changes\n"
            "    using the Linux inotify API. When the watch limit is exhausted,\n"
            "    the engine falls back to polling directories with readdir().\n"
            "    That polling loop races with internal engine hash table lookups\n"
            "    on .NET thread pool threads, causing a SIGSEGV in libengine2.so.\n"
            "\n"
            f"  This script will temporarily raise the limit to {TARGET} using sudo\n"
            f"  by writing to {INOTIFY_PATH}.\n"
            f"  Script: {os.path.abspath(__file__)}\n"
            "  The original value is restored when the game exits. No permanent system changes.\n",
            file=sys.stderr,
        )

        try:
            answer = input("  Raise the limit for this session? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""

        if answer not in ("y", "yes"):
            print(
                "[preload_inotify] Skipped. The game may crash during this session.",
                file=sys.stderr,
            )
            import time; time.sleep(1)
        else:
            if _set(TARGET):
                print(
                    f"[preload_inotify] Raised {SYSCTL_KEY} to {TARGET}.",
                    file=sys.stderr,
                )
                atexit.register(_restore, current)

                def _sig_handler(signum, _frame):
                    _restore(current)
                    signal.signal(signum, signal.SIG_DFL)
                    os.kill(os.getpid(), signum)

                for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                    signal.signal(sig, _sig_handler)
            else:
                print(
                    "[preload_inotify] sudo sysctl failed — check sudo permissions.\n"
                    "  The game may crash during this session.",
                    file=sys.stderr,
                )

    print(file=sys.stderr)


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <command> [args...]", file=sys.stderr)
        sys.exit(1)

    setup()

    proc = subprocess.run(sys.argv[1:])
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()

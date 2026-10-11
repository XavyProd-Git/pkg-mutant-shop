"""Stamp the real version into every artifact that carries one, before anything is bundled.

The UI carried a hardcoded APP_VERSION used until /api/health answers, so every load
flashed whatever number was last typed there (1.1.1) before snapping to the true one.
Both artifacts bundle web/ directly, so stamping the file once covers the EXE and the ELF.

Single source of truth: companion/server.py VERSION. Stamped into:
    web/index.html                 var APP_VERSION="..."
    ps5-app/onconsole/server.c     #define SHOP_VERSION "..."
    ps5-app/homebrew.js            "version": "..."      (the launcher manifest sat at 0.5.0)

REFUSES if any target is missing or carries no stamp point. An earlier version silently skipped
server.c when it could not be read, and "stamped" then meant only the UI - which is how the
console reported 3.39.0 while the companion said 3.40.0.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "companion", "server.py")
SHOP_C = os.path.join(ROOT, "ps5-app", "onconsole", "server.c")
# The PS4 payload carries its own SHOP_VERSION and is stamped from the same source. Left out, it
# drifted immediately - it was written at 3.62.0 while everything else said 3.61.0, which is the
# exact failure this file exists to prevent.
PS4_C = os.path.join(ROOT, "ps4-app", "onconsole", "server_ps4.c")
INDEX = os.path.join(ROOT, "web", "index.html")
HOMEBREW_JS = os.path.join(ROOT, "ps5-app", "homebrew.js")
# THE FRONT PAGE IS AN ARTIFACT TOO. Its badges were written by hand, so they said 3.89.1 and PS5
# 12.70 while the app was 3.92.1 on a 13.60 console - the first thing anyone sees, and three
# releases out of date. Stamped from the same source as everything else now.
README = os.path.join(ROOT, "README.md")

# (path, pattern with the version as group 2 between group 1 and group 3, human name)
TARGETS = (
    (SHOP_C, rb'(#define\s+SHOP_VERSION\s+")([^"]+)(")', "ps5-app/onconsole/server.c SHOP_VERSION"),
    (PS4_C, rb'(#define\s+SHOP_VERSION\s+")([^"]+)(")', "ps4-app/onconsole/server_ps4.c SHOP_VERSION"),
    (INDEX, rb'(var APP_VERSION=")([^"]*)(";)', "web/index.html APP_VERSION"),
    (HOMEBREW_JS, rb'("version"\s*:\s*")([^"]*)(")', "ps5-app/homebrew.js version"),
    (README, rb'(badge/version-)([^-]+)(-e8c547)', "README.md version badge"),
)

# The supported PS5 firmware, stamped from companion/server.py PS5_FW_SUPPORTED. Separate from the
# version because it moves on its own clock - a console firmware, not a release of ours.
FW_TARGETS = (
    (README, rb'(badge/PS5-)([0-9.]+)(-2a6fdb)', "README.md PS5 firmware badge"),
    # THE NUMBER, WHATEVER IS WRITTEN AROUND IT. This matched the cell exactly - "| PlayStation 5 |
    # 13.60 |" - so rewording it to "up to **13.60**" left no stamp point and stamp_version refused,
    # which ABORTS both console builds. The prose in that cell is free to change; the version in it
    # is what is being stamped.
    (README, rb'(\| PlayStation 5 \|[^|0-9]*)([0-9]+\.[0-9]+)([^|]*\|)',
     "README.md supported-firmware table"),
)


def read_version():
    with open(SERVER, encoding="utf-8") as f:
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
    if not m:
        raise SystemExit("could not find VERSION in companion/server.py")
    return m.group(1)


def read_ps5_fw():
    with open(SERVER, encoding="utf-8") as f:
        m = re.search(r'^PS5_FW_SUPPORTED\s*=\s*"([^"]+)"', f.read(), re.M)
    if not m:
        raise SystemExit("could not find PS5_FW_SUPPORTED in companion/server.py")
    return m.group(1)


def elf_version():
    """Kept for callers that only want to read it (ready_check does its own)."""
    try:
        with open(SHOP_C, "rb") as f:
            m = re.search(rb'#define\s+SHOP_VERSION\s+"([^"]+)"', f.read())
        return m.group(1).decode() if m else None
    except OSError:
        return None


def stamp(path, pattern, label, ver):
    """STAMP it, do not warn about it. The artifacts must ship the same number, and a warning
    in the middle of a long build is not a mechanism - it scrolls past."""
    try:
        raw = open(path, "rb").read()
    except OSError as e:
        raise SystemExit("stamp_version: %s is missing (%s) - refusing to stamp a partial set"
                         % (label, e))
    m = re.search(pattern, raw)
    if not m:
        raise SystemExit("stamp_version: no stamp point for %s - refusing to stamp a partial set"
                         % label)
    old = m.group(2).decode("utf-8", "replace")
    if old == ver:
        print("%s already at %s" % (label, ver))
        return
    new_raw, k = re.subn(pattern, lambda mm: mm.group(1) + ver.encode() + mm.group(3), raw, count=1)
    if k != 1:
        raise SystemExit("stamp_version: could not rewrite %s" % label)
    open(path, "wb").write(new_raw)
    print("stamped %s -> %s (was %s)" % (label, ver, old))


def main():
    ver = read_version()
    fw = read_ps5_fw()
    # Check every target exists BEFORE writing any, so a missing one never leaves the others
    # half-stamped.
    for path, pattern, label in TARGETS + FW_TARGETS:
        if not os.path.isfile(path):
            raise SystemExit("stamp_version: %s is missing - refusing to stamp a partial set" % label)
    for path, pattern, label in TARGETS:
        stamp(path, pattern, label, ver)
    for path, pattern, label in FW_TARGETS:
        stamp(path, pattern, label, fw)
    return 0


if __name__ == "__main__":
    sys.exit(main())

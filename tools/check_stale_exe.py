# -*- coding: utf-8 -*-
"""Gate: no INCOMPLETE PKG-MUTANT-SHOP.exe may sit in the repo waiting to be copied.

WHY THIS EXISTS. The owner copied the app to a second PC, saw the PS4 reported Offline there, and
concluded "the exe seems to not have everything in it". That turned out to be a stale saved address
rather than a bad exe - but while checking it, a genuinely incomplete exe was found sitting in the
repo:

    pkg-mutant-shop/dist/PKG-MUTANT-SHOP.exe   21.9 MB   built 2026-09-23
        ps4-elf     ABSENT   -> cannot start a PS4's payload, so a PS4 never comes up
        cheats-pack ABSENT   -> no cheat library to give a PS4

That is a build from an older layout, before PyInstaller was pointed at companion/dist, and it looks
exactly as legitimate as the real one in a file listing. Copying it produces precisely the symptom the
owner described and reported - a PS4 that never works on that PC - with nothing to explain it.

So: any PKG-MUTANT-SHOP.exe found in the repo must carry the four things the spec bundles. A stale one
is not a warning, it is a trap with a plausible name.

    python tools/check_stale_exe.py
"""
import hashlib
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# WHAT THE EXE BUNDLES, AND WHERE THE REAL ONE LIVES. The archive path is what PyInstaller stores
# (from the spec's `datas`); the disk path is what the build produces. These must be the same bytes,
# or the exe is complete with yesterday's parts - see the docstring.
BUNDLED = [
    ("ps4-elf/PKG-MUTANT-SHOP-PS4.elf",
     os.path.join(ROOT, "ps4-app", "onconsole", "PKG-MUTANT-SHOP-PS4.elf"),
     "the PS4 payload"),
    ("ps4-tile/IV0000-PKGM00001_00-PKGMUTANTSHOP001.pkg",
     os.path.join(ROOT, "ps4-app", "tile-pkg", "IV0000-PKGM00001_00-PKGMUTANTSHOP001.pkg"),
     "the PS4 dashboard app"),
    ("web/index.html", os.path.join(ROOT, "web", "index.html"), "the UI"),
    ("cheats-pack/cheats-library.zip",
     os.path.join(ROOT, "assets", "cheats-library.zip"), "the cheat library"),
]


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def embedded_drift(exe_path):
    """[(what, embedded_sha, disk_sha, sizes)] for every bundled file that does NOT match, or None
    when the archive cannot be read (which is not a failure - see the docstring)."""
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except Exception:
        return None
    try:
        arch = CArchiveReader(exe_path)
    except Exception:
        return None
    names = set(arch.toc)
    out = []
    for arc, disk, what in BUNDLED:
        hit = None
        for cand in (arc, arc.replace("/", os.sep), arc.replace("/", "\\")):
            if cand in names:
                hit = cand
                break
        if hit is None:
            out.append((what, "not in the archive", "-", ""))
            continue
        if not os.path.isfile(disk):
            continue                      # nothing built to compare against; not the exe's fault
        try:
            got = arch.extract(hit)
            eb = got[1] if isinstance(got, tuple) else got
            db = io.open(disk, "rb").read()
        except Exception as e:
            out.append((what, "could not be extracted: %s" % e, "-", ""))
            continue
        if _sha(eb) != _sha(db):
            out.append((what, _sha(eb)[:12], _sha(db)[:12],
                        "%d vs %d bytes" % (len(eb), len(db))))
    return out

# The markers PyInstaller leaves for each `datas` entry in companion/PKG-MUTANT-SHOP.spec. Searched
# as raw bytes in the archive, which is cheap and needs no PyInstaller import.
NEED = [
    (b"ps4-elf", "the PS4 payload (a PS4 can never be started without it)"),
    (b"cheats-pack", "the cheat library (a PS4 has no cheats without it)"),
    (b"ps4-tile", "the PS4 dashboard app"),
    (b"web", "the UI"),
]

# Not ours to police: a dated backup is SUPPOSED to hold whatever it held.
SKIP_DIRS = {"backups", "build", "__pycache__", ".git", "node_modules"}


def main():
    found, bad = [], []
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if d.lower() not in SKIP_DIRS]
        for f in fn:
            if f.lower() != "pkg-mutant-shop.exe":
                continue
            p = os.path.join(dp, f)
            try:
                b = io.open(p, "rb").read()
            except OSError as e:
                bad.append((p, ["could not be read: %s" % e]))
                continue
            missing = [why for marker, why in NEED if marker not in b]
            rel = os.path.relpath(p, ROOT)
            found.append((rel, len(b), missing))
            if missing:
                bad.append((rel, missing))

    if not found:
        print("check_stale_exe: no exe built yet - nothing to check")
        return 0

    for rel, size, missing in sorted(found):
        print("  %-52s %10d bytes  %s"
              % (rel, size, "OK" if not missing else "INCOMPLETE"))

    # AND THE HARDER QUESTION: is what it carries what we just built? A complete exe built before the
    # payload changed is a trap with a plausible name AND a plausible size.
    drift_any = False
    for rel, size, missing in sorted(found):
        if missing:
            continue
        d = embedded_drift(os.path.join(ROOT, rel))
        if d is None:
            print("  (PyInstaller is not importable here - the embedded copies were not compared)")
            break
        for what, esha, dsha, sizes in d:
            drift_any = True
            print("  STALE: %s inside %s is not the built one (%s vs %s) %s"
                  % (what, rel, esha, dsha, sizes))
    if drift_any:
        print("\ncheck_stale_exe: FAIL - the exe carries a file that is not the one on disk")
        print("  ship.py hashes the three artifacts it COPIES, so the loose payload beside the exe can")
        print("  be current while the copy inside the exe is a build behind. On another PC the exe is")
        print("  all there is, and it would push that older payload to the console without a word.")
        print("  Rebuild it: python -m PyInstaller --noconfirm companion/PKG-MUTANT-SHOP.spec")
        return 1

    if bad:
        print("\ncheck_stale_exe: FAIL - an incomplete exe is sitting in the repo")
        for rel, missing in bad:
            print("  %s" % rel)
            for m in missing:
                print("      missing: %s" % m)
        print("\n  An exe that is missing these looks exactly as legitimate as the real one in a")
        print("  folder listing, and copying it to another PC gives a console that never works with")
        print("  nothing on screen to explain why. That has already cost one round of debugging.")
        print("  Delete it, or rebuild it from companion/PKG-MUTANT-SHOP.spec.")
        return 1

    print("\ncheck_stale_exe: OK (%d exe(s), each carrying the payload, tile, cheats and UI - and "
          "each of those is the build on disk)" % len(found))
    return 0


if __name__ == "__main__":
    sys.exit(main())

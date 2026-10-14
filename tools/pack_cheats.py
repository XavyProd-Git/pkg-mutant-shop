# -*- coding: utf-8 -*-
"""Pack the cheat library into one file, so the exe can carry it to a PC that has no repo.

THE GAP THIS CLOSES. A PS5 is self-sufficient: the ELF embeds all 7022 cheat files and writes them
to the console at boot. A PS4 is not - its payload is injected into a shared system daemon whose
heap it does not own, and carrying 43 MB of library into that process is not a trade worth making,
so a PS4 gets its library from the PC over the file API. Which was fine here, where the companion
runs from the repo and assets/cheats is right there. On any other PC the frozen exe bundled only
web/, CHEATS_DIR did not exist, and the sync had nothing to push - so a PS4 set up from the shipped
exe had no cheats at all and no way to get any.

WHY ONE ARCHIVE AND NOT 6275 LOOSE FILES. PyInstaller's one-file build unpacks every bundled data
file into %TEMP% on EVERY launch. Six thousand small files is a visible startup cost and a lot of
disk churn for something that is read rarely and changes almost never - the same argument that got
numpy excluded from this build. One archive is one file to unpack, and the companion expands it
beside its own config exactly once, keyed on the archive's identity so a later release replaces it.

Deflate on purpose: these are JSON and XML, and they compress to roughly a fifth.

    python tools/pack_cheats.py            # build it
    python tools/pack_cheats.py --check    # is it present and current?
"""
import hashlib
import io
import os
import sys
import zipfile
from zlib import crc32 as _crc32

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "assets", "cheats")
OUT = os.path.join(ROOT, "assets", "cheats-library.zip")

# Only what the on-console engine reads. The xml/xml_orbis/xml_prospero folders are the
# pre-migration layout that neither payload ever opens - the same four names companion/server.py
# syncs and ps5-app's bundle generator ships.
SUBS = ("json", "mc4", "shn", "patches")


def entries():
    """(archive name, full path) for every file that belongs in the library, sorted.

    Sorted so the archive is reproducible: an unsorted os.walk gives a different byte stream on a
    different machine, and then every build 'changes' the library and every PC re-extracts it."""
    out = []
    for sub in SUBS:
        d = os.path.join(SRC, sub)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            # A <name>.mc4.xml is the DECRYPTED TWIN of a .mc4 the console decrypts for itself -
            # 706 of them sit beside the .mc4 files. The engine never opens one, and the PC's own
            # sync has excluded them from what it pushes for exactly that reason (see
            # _cheat_local_files in companion/server.py). Carrying them in the exe would be ~3 MB
            # of an archive that exists to be small.
            if name.lower().endswith(".mc4.xml") or name.startswith("."):
                continue
            p = os.path.join(d, name)
            if os.path.isfile(p):
                out.append(("%s/%s" % (sub, name), p))
    return out


def main():
    files = entries()
    if not files:
        print("pack_cheats: no library at %s - nothing to pack" % SRC)
        return 0

    if "--check" in sys.argv:
        if not os.path.exists(OUT):
            print("pack_cheats: %s is MISSING - the exe would ship with no cheat library"
                  % os.path.relpath(OUT, ROOT))
            return 1
        # NAMES ARE NOT CONTENTS. This compared the set of names, so a cheat file edited in place -
        # which is exactly what porting one does - left the archive "current" while the exe went on
        # shipping the old bytes to every console that asks the PC for cheats. Silently, with a green
        # gate. Size and CRC are in the zip's own directory, so this costs no decompression.
        with zipfile.ZipFile(OUT) as z:
            have = {i.filename: (i.file_size, i.CRC) for i in z.infolist()}
        want = {}
        for n, p in files:
            try:
                b = io.open(p, "rb").read()
            except OSError:
                continue
            want[n] = (len(b), _crc32(b) & 0xFFFFFFFF)
        missing = set(want) - set(have)
        extra = set(have) - set(want)
        changed = sorted(n for n in (set(want) & set(have)) if want[n] != have[n])
        if missing or extra or changed:
            print("pack_cheats: the archive is stale - %d missing, %d no longer in the library, "
                  "%d CHANGED since it was packed" % (len(missing), len(extra), len(changed)))
            for n in changed[:6]:
                print("    changed: %s" % n)
            if len(changed) > 6:
                print("    ...and %d more" % (len(changed) - 6))
            return 1
        print("pack_cheats: %s is current (%d files, %.1f MB, contents verified)"
              % (os.path.relpath(OUT, ROOT), len(have), os.path.getsize(OUT) / 1048576.0))
        return 0

    tmp = OUT + ".part"          # never leave a half-written archive where the build will find one
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, path in files:
            # A fixed timestamp, for the same reason the list is sorted: the archive must not
            # change just because the files were checked out again.
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with io.open(path, "rb") as f:
                z.writestr(info, f.read())
    if os.path.exists(OUT):
        os.remove(OUT)
    os.rename(tmp, OUT)

    raw = sum(os.path.getsize(p) for _, p in files)
    sha = hashlib.sha256(io.open(OUT, "rb").read()).hexdigest()[:12]
    print("pack_cheats: %s  %d files  %.1f MB -> %.1f MB  id %s"
          % (os.path.relpath(OUT, ROOT), len(files), raw / 1048576.0,
             os.path.getsize(OUT) / 1048576.0, sha))
    return 0


if __name__ == "__main__":
    sys.exit(main())

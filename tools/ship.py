# -*- coding: utf-8 -*-
"""Copy the three built artifacts into the shipping folder, and PROVE each one landed.

WHY THIS EXISTS. The exe was copied over by hand and the copy silently did nothing: the companion
was still running from that exact file, Windows refuses to replace an image that is mapped into a
live process, and the shell said so in a line that scrolled past. The folder then held a 3.64.0 PS5
ELF, a 3.64.0 PS4 ELF, and a 3.63.0 exe with a timestamp two hours older than its neighbours -
which is indistinguishable, from the outside, from a release that was simply built wrong.

So this does not trust the copy. It hashes the source, copies, hashes the destination, and refuses
to report success unless they match. A file held open by a running process is named as exactly that,
with the PIDs holding it, instead of becoming a wrong number in a folder.

    python tools/ship.py              # copy and verify
    python tools/ship.py --check      # verify only, copy nothing
"""
import hashlib
import io
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEST = os.path.join(os.path.expanduser("~"), "Desktop", "PKG MUTANT SHOP")

# (built path, name in the shipping folder, what it is)
ARTIFACTS = (
    (os.path.join(ROOT, "companion", "dist", "PKG-MUTANT-SHOP.exe"),
     "PKG-MUTANT-SHOP.exe", "the Windows companion"),
    (os.path.join(ROOT, "ps5-app", "onconsole", "PKG-MUTANT-SHOP.elf"),
     "PKG-MUTANT-SHOP.elf", "the PS5 payload"),
    (os.path.join(ROOT, "ps4-app", "onconsole", "PKG-MUTANT-SHOP-PS4.elf"),
     "PKG-MUTANT-SHOP-PS4.elf", "the PS4 payload"),
)


def sha(path):
    h = hashlib.sha256()
    with io.open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def holders(path):
    """PIDs of processes running FROM this file, so a refusal can say who to close.

    Windows only, and best-effort: this is here to make a failure legible, never to gate on.
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | Where-Object { $_.Path -eq '%s' } | "
             "Select-Object -ExpandProperty Id" % path.replace("'", "''")],
            capture_output=True, text=True, timeout=30)
        return [l.strip() for l in out.stdout.splitlines() if l.strip().isdigit()]
    except Exception:
        return []


def main():
    check_only = "--check" in sys.argv
    if not os.path.isdir(DEST):
        print("the shipping folder is not there: %s" % DEST)
        return 1

    bad = 0
    for src, name, what in ARTIFACTS:
        dst = os.path.join(DEST, name)
        if not os.path.exists(src):
            print("[MISSING] %-26s %s was never built (%s)" % (name, what, src))
            bad += 1
            continue
        s = sha(src)

        # ALREADY THERE IS NOT A COPY. Without this, an artifact that is correct but happens to be
        # RUNNING - which the companion exe usually is - is reported LOCKED on a release where it
        # did not change at all, and a real failure would be lost among the noise.
        if os.path.exists(dst) and sha(dst) == s:
            print("[ok     ] %-26s %10d bytes  build %s  (%s, already current)"
                  % (name, os.path.getsize(dst), s[:8], what))
            continue

        if not check_only:
            try:
                shutil.copy2(src, dst)
            except OSError as e:
                pids = holders(dst)
                print("[LOCKED ] %-26s could not be replaced: %s" % (name, e.strerror or e))
                if pids:
                    print("           it is running right now as PID %s - close it and run this again"
                          % ", ".join(pids))
                else:
                    print("           nothing obvious is holding it; check what has the file open")
                bad += 1
                continue

        if not os.path.exists(dst):
            print("[ABSENT ] %-26s is not in the shipping folder at all" % name)
            bad += 1
            continue

        d = sha(dst)
        if d != s:
            pids = holders(dst)
            print("[STALE  ] %-26s the copy in the shipping folder is NOT the build" % name)
            print("           built    %s" % s)
            print("           shipped  %s" % d)
            if pids:
                print("           it is running as PID %s, which is why a copy over it does nothing"
                      % ", ".join(pids))
            bad += 1
            continue

        print("[ok     ] %-26s %10d bytes  build %s  (%s)"
              % (name, os.path.getsize(dst), d[:8], what))

    print()
    if bad:
        print("%d of %d artifact(s) are NOT shipped. Nothing here is a release yet."
              % (bad, len(ARTIFACTS)))
        return 1
    print("all %d artifacts in %s match what was built." % (len(ARTIFACTS), DEST))
    # The exe's build string is the first 8 of its sha256, which is exactly what the app reports as
    # `build` in /api/health and prints in Settings > About. So a second PC can be compared against
    # this line without trusting a version number that does not change when the code does.
    print("the exe's build string is what Settings > About shows on any PC running it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

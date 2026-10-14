# -*- coding: utf-8 -*-
"""Compile the real cheat engine on this PC and make it prove what it does.

The engine's byte logic is one file that both consoles share (ps5-app/onconsole/server.c, copied to
ps4-app/onconsole/cheat_core.h by tools/ps4_sync_cheat_core.py), and it reaches memory only through
mem_read/mem_write. That is a seam a test can fill: tools/test_cheat_core.c gives it a fake game in
ordinary process memory and asserts the things that have actually gone wrong here -

  * a mod applied half-way (the Dark Souls II crash: a code cave written without its jump, or worse)
  * a refusal that blames the game's version when the real cause was something else
  * `section` read with strtol against a quoted value, i.e. a check that could never fire

    python tools/test_cheat_core.py            # compile and run
    python tools/test_cheat_core.py --check    # same, exit 1 on any failure (what the gates call)

No console is involved and nothing is installed. If gcc is not on PATH this reports that and exits
0 under --check: a missing compiler is not a failing engine, and a build gate that turns a missing
tool into a red light gets switched off.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(HERE, "test_cheat_core.c")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.parse_args()          # --check exists so the build gates can call it; the run is the same

    cc = shutil.which("gcc") or shutil.which("clang") or shutil.which("cc")
    if not cc:
        print("test_cheat_core: no C compiler on PATH - skipped")
        return 0
    if not os.path.isfile(SRC):
        print("test_cheat_core: %s is missing" % SRC)
        return 1

    out = os.path.join(tempfile.gettempdir(), "pms_test_cheat_core.exe")
    # -w: the engine is written for a PS4/PS5 SDK, and this is not the build that judges its
    # warnings. -O0 keeps the fake game's memory exactly where the asserts look for it.
    cmd = [cc, "-std=gnu11", "-O0", "-w", SRC, "-o", out]
    r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
    if r.returncode != 0:
        print("test_cheat_core: the engine did not compile")
        print((r.stderr or r.stdout)[-4000:])
        return 1

    # the repo root, so the test can read the real cheat file it checks
    r = subprocess.run([out, ROOT], cwd=tempfile.gettempdir(), capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.stderr.strip():
        sys.stdout.write(r.stderr)
    return 1 if r.returncode else 0


if __name__ == "__main__":
    sys.exit(main())

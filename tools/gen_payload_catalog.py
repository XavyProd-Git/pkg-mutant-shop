# -*- coding: utf-8 -*-
"""Turn the owner's payloads/homebrews folder into one catalogue the app can serve.

    python tools/gen_payload_catalog.py                 # rebuild assets/payloads/catalog.json
    python tools/gen_payload_catalog.py --check         # fail if it is out of date, change nothing
    python tools/gen_payload_catalog.py --src "D:/..."  # a different source folder

WHY A GENERATED FILE AND NOT A LIVE SCAN. The panel has to work on a console with every PC switched
off, so the list of what exists cannot be produced by walking a folder on somebody's PC. The walk
happens here, once, at build time; the result is a few kilobytes of JSON that both the exe and both
ELFs carry, and the live folder is then only needed to hand over the actual bytes.

WHAT IS READ OUT OF THE FILES, AND WHAT IS NOT. Everything a file can prove about itself is read
from the file: the platform from the folder it sits in, its size, its sha256, and for a package its
title, title id, content id and version straight out of param.sfo (or param.json for a PS5 app
folder). What a file cannot prove - which port means it is running, whether starting it unattended
is safe, which GitHub project it came from - is NOT guessed here. That lives in
assets/payloads/curated.json, written by hand with a reason each, and anything missing from it gets
a neutral state rather than an invented one.

THE SHAPE OF THE SOURCE FOLDER IS THE PLATFORM RULE.

    Payloads/<PS4|PS5>/<group>/<file>.elf
    Homebrews/<PS4|PS5>/<group>/<file>.pkg
    Homebrews/<PS4|PS5>/<group>/<TITLEID>/sce_sys/param.json      (a folder-shaped app)

so a PS4 tab shows PS4 things because the owner put them in the PS4 folder, not because a filename
was parsed for the letters "ps4".
"""
import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "companion"))

DEFAULT_SRC = os.path.join("C:" + os.sep, "Mutant Payloads & HomeBrews")
# THE OUTPUT GOES INTO web/assets ON PURPOSE. gen_web_bundle.py's allow-list is index.html,
# config.js and assets/<name> with no further slash, so a file written there is embedded in
# BOTH console ELFs and carried by the exe (the spec already ships all of web/) with no new
# bundle header, no new generator call in either build script, and no new staleness gate.
# One output in one place: a second canonical copy is how a stale blob ships unnoticed.
OUT = os.path.join(ROOT, "web", "assets", "payloads-catalog.json")
CURATED = os.path.join(ROOT, "assets", "payloads", "curated.json")

# OUR OWN ARTIFACTS, BY NAME. The owner keeps a copy of the shop in that folder, which is useful to
# them and must never become an .incbin: an artifact carrying a copy of itself is the recursion
# ps4-app/build-all-wsl.sh exists to prevent. These are catalogued and marked, never embedded.
OURS = ("PKG-MUTANT-SHOP.elf", "PKG-MUTANT-SHOP-PS4.elf")

PLATFORMS = ("PS4", "PS5")


# THE SCAN LIVES IN companion/payloads.py, which the app itself carries. This tool is the build-time
# caller of it; the running app is the other. Two implementations of one folder walk is how the
# catalogue and the live view would drift apart.
from payloads import build          # noqa: E402  (companion/ is put on sys.path above)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.environ.get("PMS_PAYLOAD_SRC", DEFAULT_SRC))
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    with io.open(CURATED, encoding="utf-8") as f:
        curated = json.load(f)
    cat = build(a.src, curated)
    text = json.dumps(cat, indent=1, sort_keys=True, ensure_ascii=False) + "\n"

    if a.check:
        if not os.path.exists(OUT):
            print("gen_payload_catalog: FAIL - %s does not exist" % os.path.relpath(OUT, ROOT))
            return 1
        with io.open(OUT, encoding="utf-8") as f:
            have = f.read()
        if have != text:
            print("gen_payload_catalog: FAIL - the catalogue is not what the folder says")
            print("  rebuild it: python tools/gen_payload_catalog.py")
            return 1
        print("gen_payload_catalog: current (%d items)" % len(cat["items"]))
        return 0

    d = os.path.dirname(OUT)
    if not os.path.isdir(d):
        os.makedirs(d)
    with io.open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print("gen_payload_catalog: wrote %s" % os.path.relpath(OUT, ROOT))
    for k in sorted(cat["counts"]):
        print("   %-16s %d" % (k, cat["counts"][k]))
    missing = [i["id"] for i in cat["items"] if not i.get("curated")]
    if missing:
        print("   no curated entry (neutral state, no upstream): %s" % ", ".join(sorted(set(missing))))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Copy the owner's payload ELFs into the two places the builds .incbin them from.

    python tools/sync_payload_bins.py            # copy what differs, report what changed
    python tools/sync_payload_bins.py --check    # fail if anything differs, copy nothing

WHY A COPY AND NOT A PATH. Every .incbin in payload_bundle.h is resolved RELATIVE to
ps5-app/onconsole (that is why both build scripts cd there first), and a build must not depend on a
folder that exists only on the owner's PC - the ELF has to be buildable on a machine that has never
seen "C:/Mutant Payloads & HomeBrews". So the bytes live in the repo beside the other embedded
blobs, and this tool is the one place that says where they came from.

WHAT IT REFUSES TO COPY. Our own two artifacts are in that folder. An artifact that embeds a copy
of itself is the recursion ps4-app/build-all-wsl.sh exists to prevent, and it grows without bound.
"""
import argparse
import hashlib
import io
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_SRC = os.path.join("C:" + os.sep, "Mutant Payloads & HomeBrews")

OURS = ("PKG-MUTANT-SHOP.elf", "PKG-MUTANT-SHOP-PS4.elf")

DEST = {
    "PS5": os.path.join(ROOT, "ps5-app", "onconsole", "payloads"),
    "PS4": os.path.join(ROOT, "ps4-app", "onconsole", "payloads"),
}


def sha(p):
    h = hashlib.sha256()
    with io.open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def wanted(src):
    """(platform, name-in-repo, absolute source path) for every third-party payload.

    THE NAME IN THE REPO IS STABLE AND THE OWNER'S IS NOT. Their copy is
    "webkit-autoloader-installer_v0.5.0.elf" today and "…_v0.5.1.elf" after the update button is
    pressed - and every `.incbin` in payload_bundle.h is a literal path, so a versioned filename
    there means the next upstream release breaks the build. Measured exactly that way: the update
    landed and `sync_payload_bins --check` went red because the file it names no longer exists.

    So the repo copy is named after the catalogue id - the identity that does not move, because it
    comes from a marker inside the binary rather than from the label on it. The console matches a
    request by stem, so the page can still ask for it by the owner's name.
    """
    import json
    cat = {}
    try:
        with io.open(os.path.join(ROOT, "web", "assets", "payloads-catalog.json"),
                     encoding="utf-8") as f:
            for it in (json.load(f).get("items") or []):
                if it.get("kind") == "payload" and it.get("file"):
                    cat[(it["platform"], it["file"])] = it["id"]
    except Exception:
        cat = {}
    out = []
    for plat in ("PS4", "PS5"):
        base = os.path.join(src, "Payloads", plat)
        if not os.path.isdir(base):
            continue
        for dp, _dn, fn in os.walk(base):
            for f in sorted(fn):
                if not f.lower().endswith(".elf") or f in OURS:
                    continue
                ident = cat.get((plat, f)) or os.path.splitext(f)[0]
                out.append((plat, ident + ".elf", os.path.join(dp, f)))
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.environ.get("PMS_PAYLOAD_SRC", DEFAULT_SRC))
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    if not os.path.isdir(a.src):
        # NOT A FAILURE ON A MACHINE THAT NEVER HAD THE FOLDER. The repo carries the bytes; this
        # tool only refreshes them. A build on another PC must still work.
        print("sync_payload_bins: source folder not here (%s) - nothing to do" % a.src)
        return 0

    items = wanted(a.src)
    if not items:
        print("sync_payload_bins: no payload ELFs under %s" % a.src)
        return 1

    stale, copied = [], []
    for plat, name, src in items:
        d = DEST[plat]
        if not os.path.isdir(d):
            os.makedirs(d)
        dst = os.path.join(d, name)
        same = os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src) \
            and sha(dst) == sha(src)
        if same:
            continue
        if a.check:
            stale.append("%s/%s" % (plat, name))
        else:
            shutil.copy2(src, dst)
            copied.append("%s/%s <- %s (%d bytes)"
                          % (plat, name, os.path.basename(src), os.path.getsize(dst)))

    if a.check:
        if stale:
            print("sync_payload_bins: FAIL - the repo's copies are not the owner's:")
            for s in stale:
                print("   %s" % s)
            print("  refresh them: python tools/sync_payload_bins.py")
            return 1
        print("sync_payload_bins: current (%d payload(s))" % len(items))
        return 0

    for c in copied:
        print("   copied %s" % c)
    print("sync_payload_bins: %d payload(s), %d refreshed" % (len(items), len(copied)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Keep the seed store in step with the owner's folder.

    python tools/sync_seed.py            # copy what differs, report what changed
    python tools/sync_seed.py --check    # fail if anything differs, copy nothing

WHAT A SEED IS. One catalogue entry has no download address anywhere - no project page, no release,
no mirror - so there is nothing for a PC to fetch it from. The catalogue marks such an entry
`bundled: seed/<PLAT>/<id>.pkg`, and the app looks for that file in a `seed` folder BESIDE THE EXE
(companion/payloads.py seed_roots). Found, the tile offers Install and the file is copied into the
owner's own folder first; not found, the tile says the file is not here, which is the truth.

WHERE IT LIVES, AND WHERE IT DOES NOT. assets/payloads/seed/ is the staging copy and it is
gitignored: these are somebody else's packages, and THIRD-PARTY-NOTICES.md says in so many words
that no homebrew package is redistributed by this project. tools/ship.py carries the folder to the
Desktop beside the exe, which is the owner's machine and the owner's copy. It is not added to the
release zip and it is not uploaded - putting a package on GitHub is a decision about somebody
else's work, and it is the owner's to make, not a build script's.

The name in the store is the catalogue id, for the reason tools/sync_payload_bins.py gives: the
owner's filename changes when the file is replaced and the id does not.
"""
import hashlib
import io
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_SRC = os.path.join("C:" + os.sep, "Mutant Payloads & HomeBrews")
STORE = os.path.join(ROOT, "assets", "payloads", "seed")
CATALOG = os.path.join(ROOT, "web", "assets", "payloads-catalog.json")


def sha(p):
    h = hashlib.sha256()
    with io.open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def wanted():
    """[(store-relative path, owner-folder-relative path, recorded sha256, title)]"""
    try:
        with io.open(CATALOG, encoding="utf-8") as f:
            items = json.load(f).get("items") or []
    except (OSError, ValueError):
        return []
    out = []
    for it in items:
        rel = str(it.get("bundled") or "").replace("\\", "/")
        if not rel.startswith("seed/") or ".." in rel.split("/"):
            continue
        out.append((rel[len("seed/"):], str(it.get("path") or ""), str(it.get("sha256") or ""),
                    str(it.get("title") or it.get("id") or "")))
    return sorted(set(out))


def stored():
    """Every file in the store, as store-relative forward-slash paths."""
    out = []
    for dp, _dn, fn in os.walk(STORE):
        for f in fn:
            out.append(os.path.relpath(os.path.join(dp, f), STORE).replace("\\", "/"))
    return sorted(out)


def main():
    check = "--check" in sys.argv
    src_root = DEFAULT_SRC
    for a in sys.argv[1:]:
        if a.startswith("--src="):
            src_root = a.split("=", 1)[1]
    want = wanted()
    bad = changed = 0
    for rel, path, digest, title in want:
        dst = os.path.join(STORE, rel.replace("/", os.sep))
        src = os.path.join(src_root, path.replace("/", os.sep)) if path else ""
        if not (src and os.path.isfile(src)):
            # NOT AN ERROR ON A MACHINE THAT NEVER HAD THE FOLDER: the build must work there, and
            # an app with no seed simply does not offer the item. It is one where the store holds
            # a copy the catalogue no longer describes.
            if os.path.isfile(dst) and digest and sha(dst) != digest:
                print("[STALE  ] %-28s the stored copy is not the file the catalogue records"
                      % rel)
                bad += 1
            else:
                print("[absent ] %-28s %s is not in %s - nothing to carry" % (rel, title, src_root))
            continue
        s = sha(src)
        if digest and s != digest:
            print("[DRIFT  ] %-28s the owner's file is not the one the catalogue records - "
                  "run tools/gen_payload_catalog.py first" % rel)
            bad += 1
            continue
        if os.path.isfile(dst) and sha(dst) == s:
            print("[ok     ] %-28s %10d bytes  %s" % (rel, os.path.getsize(dst), s[:12]))
            continue
        if check:
            print("[DIFFERS] %-28s the store does not hold the owner's copy" % rel)
            bad += 1
            continue
        folder = os.path.dirname(dst)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        shutil.copy2(src, dst + ".part")
        os.replace(dst + ".part", dst)
        changed += 1
        print("[copied ] %-28s %10d bytes  %s" % (rel, os.path.getsize(dst), s[:12]))
    # A FILE NOBODY ASKS FOR ANY MORE IS NOT CARRIED FOR EVER. An entry that gained a real download
    # address stops being `bundled`, and its 37 MB would otherwise ride along in every copy.
    keep = set(rel for rel, _p, _d, _t in want)
    for rel in stored():
        if rel in keep or rel.endswith(".part"):
            continue
        if check:
            print("[EXTRA  ] %-28s is in the store and no catalogue entry names it" % rel)
            bad += 1
        else:
            os.remove(os.path.join(STORE, rel.replace("/", os.sep)))
            changed += 1
            print("[removed] %-28s no catalogue entry names it any more" % rel)
    if bad:
        print("\nsync_seed: FAIL - %d file(s) out of step. Run it without --check." % bad)
        return 1
    print("sync_seed: OK (%d carried file(s)%s)" % (len(want), ", %d changed" % changed
                                                   if changed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Generate cheat_bundle.h + cheats.pack — embeds the whole cheat/patch library into the ELF.

Why this is NOT shaped like gen_web_bundle.py: that one emits every file as octal C string
literals, which is fine for 881 KB of UI but would turn 30.7 MB of cheats into roughly 120 MB of C
source and a compile measured in minutes. Instead we concatenate every file into ONE blob
(cheats.pack), pull it in with a single .incbin, and keep a small table of (path, offset, length).
The compiler never sees the payload — the assembler drops it straight into .rodata.

The ELF extracts these to /data/pkg-mutant-shop/cheats on boot, skipping anything already on disk,
so the first boot writes the library and every later boot costs one stat() per file.

One deliberate write into the SOURCE tree: any patch XML the legacy xml* folders hold that
patches/ does not is copied into patches/ (preserve_legacy_patches) before the pack is built - a
one-time migration, so a run that copies something is expected and later runs copy nothing.

Usage: gen_cheat_bundle.py <cheats_dir> <out_header> <out_pack>
"""
import os
import shutil
import sys

here = os.path.dirname(os.path.abspath(__file__))
cheats_dir = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                             else os.path.join(here, "..", "..", "assets", "cheats"))
out_h = sys.argv[2] if len(sys.argv) > 2 else os.path.join(here, "cheat_bundle.h")
out_pack = sys.argv[3] if len(sys.argv) > 3 else os.path.join(here, "cheats.pack")

# Only the folders the on-console engine actually reads (server.c: CHEAT_JSON_DIR / SHN / MC4 /
# CHEAT_PATCH_DIR). Anything else in the tree (a .git checkout, CI files, licence text) is not
# shipped — it would be dead weight in console RAM.
#
# xml/, xml_orbis/ and xml_prospero/ used to be shipped too. They are the pre-migration patch
# layout: the console never opens them (only CHEAT_PATCH_DIR is read; the ELF merely mkdir()s
# the names), and measured against patches/ every one of their 746 patch files is byte-identical
# to the copy there - the only extras are one build.txt each. Likewise the 706 '<name>.mc4.xml'
# twins in mc4/: decrypted copies of .mc4 files the engine decrypts itself, and a name it can
# never pick (cheat_pick_file wants the .mc4 extension). Together ~3.5 MB of console RAM for
# files nothing looked at, and bogus "01.00.mc4" versions in the panel. Before the legacy folders
# are left out, anything in them that patches/ does NOT already hold is copied there
# (preserve_legacy_patches), so dropping them from the bundle loses nothing.
SUBDIRS = ("json", "mc4", "shn", "patches")
LEGACY_PATCH_DIRS = ("xml", "xml_orbis", "xml_prospero")
SKIP_NAMES = {".gitattributes", ".gitignore", "LICENSE"}


def shippable(n):
    return not (n in SKIP_NAMES or n.startswith(".") or n.lower().endswith(".mc4.xml"))


def preserve_legacy_patches():
    """Copy into patches/ whatever the legacy xml* folders hold that patches/ does not.

    Patch XML only: the legacy folders also each carry a build.txt, and copying that shipped a
    non-patch file into /data/pkg-mutant-shop/cheats/patches on every console for nothing.
    A name already present with identical bytes needs nothing; a name present with DIFFERENT
    bytes is kept beside it as <stem>_<folder><ext> rather than overwriting the shipped patch.
    Returns how many files were copied (measured on the current tree: 0 - every patch XML is
    already in patches/ byte for byte)."""
    pdir = os.path.join(cheats_dir, "patches")
    if not os.path.isdir(pdir):
        os.makedirs(pdir)
    have = set(os.listdir(pdir))
    copied = 0
    for sub in LEGACY_PATCH_DIRS:
        d = os.path.join(cheats_dir, sub)
        if not os.path.isdir(d):
            continue
        for n in sorted(os.listdir(d)):
            src = os.path.join(d, n)
            if not shippable(n) or not os.path.isfile(src):
                continue
            if not n.lower().endswith(".xml"):
                continue
            dst_name = n
            if n in have:
                with open(src, "rb") as f:
                    data = f.read()
                with open(os.path.join(pdir, n), "rb") as f:
                    if f.read() == data:
                        continue
                stem, ext = os.path.splitext(n)
                dst_name = "%s_%s%s" % (stem, sub, ext)
                if dst_name in have:
                    continue
            shutil.copyfile(src, os.path.join(pdir, dst_name))
            have.add(dst_name)
            copied += 1
    return copied


preserved = preserve_legacy_patches()
if preserved:
    print("preserved %d legacy patch file(s) into patches/" % preserved)

files = []
for sub in SUBDIRS:
    d = os.path.join(cheats_dir, sub)
    if not os.path.isdir(d):
        continue
    for n in sorted(os.listdir(d)):
        if not shippable(n):
            continue
        ap = os.path.join(d, n)
        if os.path.isfile(ap):
            files.append(("%s/%s" % (sub, n), ap))
files.sort()

blob = bytearray()
table = []
for rel, ap in files:
    with open(ap, "rb") as f:
        data = f.read()
    table.append((rel, len(blob), len(data)))
    blob += data

with open(out_pack, "wb") as f:
    f.write(bytes(blob))


def c_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


lines = [
    "/* AUTO-GENERATED by gen_cheat_bundle.py - do not edit.",
    " * %d files, %d bytes, embedded via one .incbin of cheats.pack. */" % (len(table), len(blob)),
    "#ifndef CHEAT_BUNDLE_H",
    "#define CHEAT_BUNDLE_H",
    "",
    "__asm__(\".section .rodata\\n\"",
    "        \".global cb_pack\\n\"",
    "        \".balign 16\\n\"",
    "        \"cb_pack:\\n\"",
    "        \".incbin \\\"cheats.pack\\\"\\n\"",
    "        \".global cb_pack_end\\n\"",
    "        \"cb_pack_end:\\n\"",
    "        \".previous\\n\");",
    "extern const unsigned char cb_pack[];",
    "extern const unsigned char cb_pack_end[];",
    "",
    "typedef struct { const char *path; unsigned int off; unsigned int len; } cheat_file_t;",
    "",
    "static const cheat_file_t CHEAT_FILES[] = {",
]
for rel, off, ln in table:
    lines.append('  {"%s",%d,%d},' % (c_escape(rel), off, ln))
lines.append("};")
lines.append("#define CHEAT_FILES_COUNT %d" % len(table))
lines.append("#define CHEAT_PACK_BYTES  %d" % len(blob))
lines.append("")
lines.append("#endif")

with open(out_h, "w", encoding="utf-8", newline="\n") as f:
    f.write("\n".join(lines) + "\n")

print("cheat_bundle.h: %d file(s), %d bytes packed -> %s" % (len(table), len(blob), out_h))

# -*- coding: utf-8 -*-
"""Gate: the front page and the release notes say what the code says.

WHY THIS EXISTS. The README's badges were written by hand, so they drifted: the front page of a
public repository said **version 3.89.1** and **PS5 12.70** while the app was 3.92.1 and the
console it had just been fixed for was on 13.60. It is the first thing anyone sees, and it was
three releases and three firmwares out of date. The owner noticed before any gate did.

The release notes drifted the other way - into prose written at one person ("3.92.0 learned it and
kept it, but the console list did not pass it on") instead of a changelog anyone can skim - and
their Download table named the OWNER'S console firmware rather than the ceiling the build could
actually run on, which is the one thing a stranger downloading an ELF needs.

So: the badges are STAMPED by tools/stamp_version.py from companion/server.py, and this refuses a
release where any of it has come apart.

    python tools/check_docs.py
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
README = os.path.join(ROOT, "README.md")
SERVER = os.path.join(ROOT, "companion", "server.py")
NOTES_DIR = os.path.join(ROOT, "docs", "release-notes")
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")

# A note has to tell a reader what changed and what to download. Everything else is optional.
SECTION_RE = re.compile(r"^## (Added|Fixed|Changed|Removed|Known at the time|Checked on hardware|"
                        r"For anyone building this)\s*$", re.M)

fails = []
n = 0


def ok(cond, what, detail=""):
    global n
    n += 1
    if not cond:
        fails.append("%s%s" % (what, (" - " + detail) if detail else ""))


def read(p):
    return io.open(p, encoding="utf-8").read()


def main():
    src = read(SERVER)
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', src, re.M)
    ok(bool(m), "companion/server.py declares VERSION")
    ver = m.group(1) if m else ""
    m = re.search(r'^PS5_FW_SUPPORTED\s*=\s*"([^"]+)"', src, re.M)
    ok(bool(m), "companion/server.py declares PS5_FW_SUPPORTED - the firmware ceiling of the build")
    fw = m.group(1) if m else ""

    # ---- THE FRONT PAGE ------------------------------------------------------------------------
    rd = read(README)
    m = re.search(r"badge/version-([^-]+)-", rd)
    ok(bool(m) and m.group(1) == ver,
       "the README version badge matches the build", "%s vs %s" % (m.group(1) if m else "?", ver))
    m = re.search(r"badge/PS5-([0-9.]+)-", rd)
    ok(bool(m) and m.group(1) == fw,
       "the README PS5 firmware badge matches PS5_FW_SUPPORTED",
       "%s vs %s" % (m.group(1) if m else "?", fw))
    # EXACTLY ONE VERSION IN THAT CELL. stamp_version rewrites the first version-looking token it
    # finds there, so a cell reading "1.00 - 13.60" gets silently turned into "13.60 - 13.60" - which
    # is what happened the moment the cell was reworded to show a range. A second number in it is a
    # mangling waiting to happen, so it is refused rather than rewritten.
    cell = re.search(r"\| PlayStation 5 \|([^|]*)\|", rd)
    ok(bool(cell), "the README has a PS5 firmware row")
    if cell:
        nums = re.findall(r"[0-9]+\.[0-9]+", cell.group(1))
        ok(len(nums) == 1,
           "the PS5 firmware cell names exactly one version - stamp_version rewrites the first one "
           "it finds, so a range there gets mangled",
           "found %s" % (nums or "none"))
    m = re.search(r"\| PlayStation 5 \| (?:up to )?\*{0,2}([0-9.]+)\*{0,2} \|", rd)
    ok(bool(m) and m.group(1) == fw,
       "the README supported-firmware table matches too",
       "%s vs %s" % (m.group(1) if m else "?", fw))
    # The badges are only trustworthy because something writes them.
    st = read(os.path.join(HERE, "stamp_version.py"))
    ok("README" in st and "badge/version-" in st,
       "tools/stamp_version.py stamps the README, rather than leaving it to be typed")
    # ...AND BECAUSE IT STILL FINDS THEM. Checking that the values agree is not enough: reword the
    # text around a stamp point and the values still agree while the stamper can no longer place it,
    # and stamp_version EXITS NON-ZERO - which aborts both console builds. That is exactly what a
    # reworded firmware cell did here, with every value correct and check_docs green.
    import subprocess
    r = subprocess.run([sys.executable, os.path.join(HERE, "stamp_version.py")],
                       capture_output=True, text=True)
    ok(r.returncode == 0,
       "tools/stamp_version.py can still find every stamp point",
       (r.stdout + r.stderr).strip().splitlines()[-1] if (r.stdout + r.stderr).strip() else "")

    # ---- THE RELEASE NOTES ---------------------------------------------------------------------
    names = sorted(f for f in os.listdir(NOTES_DIR) if f.startswith("v") and f.endswith(".md"))
    ok(bool(names), "there are release notes at all")
    for f in names:
        body = read(os.path.join(NOTES_DIR, f))
        ok(bool(SECTION_RE.search(body)),
           "%s says what changed, under a heading" % f)
        ok("## Download" in body, "%s tells you what to download" % f)
        ok("PKG-MUTANT-SHOP.exe" in body and "PKG-MUTANT-SHOP.elf" in body
           and "PKG-MUTANT-SHOP-PS4.elf" in body,
           "%s lists all three artifacts" % f)
        # A note must name the firmware the BUILD supports, not whichever console was on the desk.
        ok(re.search(r"PS5 — firmware up to [0-9.]+", body) is not None,
           "%s says which PS5 firmware that build runs on" % f)
        # Prose creeps back in as walls of text; a changelog line is a line.
        for line in body.splitlines():
            if line.startswith("- ") and len(line) > 400:
                fails.append("%s has a bullet over 400 characters - that is prose, not a note" % f)
                break
    # The current version must have notes, or tools/release.py would refuse anyway - but say so
    # here, where it is cheap to fix.
    ok(os.path.exists(os.path.join(NOTES_DIR, "v%s.md" % ver)),
       "the current version has release notes", "v%s.md" % ver)
    ok(("## [%s]" % ver) in read(CHANGELOG),
       "the current version has a CHANGELOG entry", ver)

    # ---- THE ZIP CARRIES MORE THAN THE THREE FILES ----------------------------------------------
    # Somebody who downloads the archive has no repository to read, so it ships a features list and
    # a changelog beside the artifacts. The features list is maintained by hand - it is checked here
    # for being present and non-trivial, because an empty or missing one would be packed in silence.
    # The changelog is assembled from the release notes at pack time, so it cannot disagree with
    # them; what is checked is that release.py still does it.
    feat = os.path.join(ROOT, "docs", "FEATURES.md")
    ok(os.path.isfile(feat), "there is a features list to ship")
    if os.path.isfile(feat):
        _ft = io.open(feat, encoding="utf-8").read()
        ok(len(_ft.splitlines()) > 60,
           "...and it is a real one", "%d lines" % len(_ft.splitlines()))
        ok("## " in _ft, "...with sections")
    _rel = io.open(os.path.join(ROOT, "tools", "release.py"), encoding="utf-8").read()
    for name in ("FEATURES.txt", "CHANGELOG.txt"):
        ok('z.writestr("%s"' % name in _rel, "the zip includes %s" % name)
    ok("def zip_changelog(" in _rel and "NOTES_DIR" in _rel,
       "the shipped changelog is built from the release notes",
       "generating reader-facing text from CHANGELOG.md leaked an internal note once")
    ok("def md_to_text(" in _rel,
       "...and both are converted to plain text, since they ship as .txt")

    if fails:
        print("check_docs: FAIL")
        for f in sorted(set(fails)):
            print("   %s" % f)
        return 1
    print("check_docs: OK (%d checks, %d release note(s), version %s, PS5 up to %s)"
          % (n, len(names), ver, fw))
    return 0


if __name__ == "__main__":
    sys.exit(main())

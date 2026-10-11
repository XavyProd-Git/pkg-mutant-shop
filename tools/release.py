# -*- coding: utf-8 -*-
"""Cut a GitHub release: check everything first, then tag, then upload.

    python tools/release.py 3.86.0            # check, tag, push, publish
    python tools/release.py 3.86.0 --dry-run  # every check, uploads nothing, tags nothing
    python tools/release.py 3.86.0 --draft    # published but not visible yet

WHY THIS IS A TOOL AND NOT A CHECKLIST. A release is the one thing in this repo that other people's
consoles act on: the app reads the release feed and offers the assets by name, so a release with the
wrong version in its binaries, a stale ELF, or a renamed asset is not a cosmetic mistake - it breaks
the update lane on every installed copy, and it breaks it silently.

So this refuses rather than guesses, and each refusal is a thing that has actually gone wrong
somewhere in this repo:

  * THE VERSION MUST ALREADY BE IN THE SOURCE, in all four places. It is not written by this tool.
    A release whose tag says 3.86.0 while the artifacts report 3.85.0 is worse than no release,
    because the app compares the tag against the version INSIDE the file it holds - so the update
    would be offered for ever and taking it would change nothing.
  * THE ARTIFACTS MUST BE NEWER THAN THEIR SOURCES. Building only ps5-app/onconsole/build-wsl.sh and
    forgetting ps4-app/build-all-wsl.sh is a mistake already made here: the home-screen icon carries
    its own copy of the payload, so the icon stayed a version behind while everything reported fine.
  * THE ASSET NAMES ARE A CONTRACT. companion/payloads.py picks a console's file out of the release
    by name - anything containing "ps4" is the PS4's, the neutral name is the PS5's. They are
    uploaded under their build names, never renamed.
  * EVERY GATE RUNS. tools/ready_check.py is the same thing CI would be.

See internal/RELEASING.md for what is worth publishing at all (not every version is).
"""
import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# WHERE THE VERSION IS WRITTEN, AND HOW IT READS IN EACH FILE. Four files, one number; the app's own
# update check reads the fourth one back OUT of the built binaries (payloads.OURS_VER_RE), which is
# why web/index.html is in this list and not just the servers.
VERSION_SITES = [
    ("companion/server.py",            r'^VERSION\s*=\s*"([0-9][0-9.]*)"'),
    ("ps5-app/onconsole/server.c",     r'^#define\s+SHOP_VERSION\s+"([0-9][0-9.]*)"'),
    ("ps4-app/onconsole/server_ps4.c", r'^#define\s+SHOP_VERSION\s+"([0-9][0-9.]*)"'),
    ("web/index.html",                 r'var APP_VERSION="([0-9][0-9.]*)"'),
]

# THE THREE ARTIFACTS, and the newest source each one must be younger than. "we only have 3 real
# artifacts" - the owner, about a fourth file that was not one.
ARTIFACTS = [
    ("companion/dist/PKG-MUTANT-SHOP.exe",      ["companion", "web", "assets"]),
    ("ps5-app/onconsole/PKG-MUTANT-SHOP.elf",   ["ps5-app/onconsole", "web"]),
    ("ps4-app/onconsole/PKG-MUTANT-SHOP-PS4.elf", ["ps4-app/onconsole", "web"]),
]

# Directories whose contents are build OUTPUT, so their timestamps say nothing about whether an
# artifact is stale. Without this every artifact looks older than the tree that contains it.
IGNORE_DIRS = {".git", "__pycache__", "build", "dist", "release", "node_modules", ".cache"}
IGNORE_EXT = {".elf", ".exe", ".pkg", ".h", ".pyc", ".bak", ".part", ".prev"}
# RUNTIME STATE IS NOT SOURCE. The running app rewrites companion/config.json (the saved console
# addresses, the device id) and installed.json whenever anything changes, so with these counted a
# freshly built exe is "older than its sources" within seconds of being started - a staleness check
# that fires while you are looking at the thing it says is stale is a check that gets switched off.
# They are not in the exe, so they cannot make it stale.
IGNORE_FILES = {"config.json", "installed.json", "pms.log", "sources.json", "known-versions.json"}


# THE NOTES ARE UTF-8 AND A WINDOWS CONSOLE IS NOT. `--notes-only` died on the arrow in "3.85.0 ->
# 3.86.0" because cp1252 cannot encode it - a release tool that crashes while printing the release
# notes is a release tool nobody runs. The file it writes for `gh` was always UTF-8; this is only
# about what reaches the terminal.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


ZIP_README = """PKG MUTANT SHOP %(v)s
================================================================

WHAT IS IN HERE
  PKG-MUTANT-SHOP.exe        the Windows companion - run this on a PC
  PKG-MUTANT-SHOP.elf        the PS5 payload - load it from Payload Manager
  PKG-MUTANT-SHOP-PS4.elf    the PS4 payload - load it through GoldHEN
  SHA256SUMS.txt             the checksum of each program above, as published
  FEATURES.txt               everything the app does, by area
  CHANGELOG.txt              what changed in this release and the ones before it

GETTING STARTED
  On a PC      run PKG-MUTANT-SHOP.exe. It makes its own folders, finds the
               consoles on your network, and opens in your browser.
  On the PS5   load PKG-MUTANT-SHOP.elf from Payload Manager, once. After that
               the app sends payloads for you.
  On the PS4   load PKG-MUTANT-SHOP-PS4.elf through GoldHEN, or press the
               home-screen icon once it has been installed.
  On a phone   open http://<your-pc>:8710 - the same app, nothing to install.

CHECKING WHAT YOU DOWNLOADED
  Every file's SHA-256 is in SHA256SUMS.txt and on the release page:
        Get-FileHash .\PKG-MUTANT-SHOP.exe -Algorithm SHA256
  The whole program is source-available under GPL-3.0, so you can build it
  yourself, or run it from source with Python 3.8+:
        python companion/server.py

CHECKSUMS
%(sums)s
Project: https://github.com/XavyProd-Git/pkg-mutant-shop
"""


def say(msg):
    print(msg)


def die(msg):
    print("\n  REFUSED: %s" % msg)
    sys.exit(1)


def run(args, **kw):
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, **kw)


def sha256(path):
    h = hashlib.sha256()
    with io.open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def artifact_version(path):
    """The version a built artifact reports, or "" if it does not report one.

    TWO ARTIFACTS ARE NOT THE SAME KIND OF FILE. Both ELFs embed web/index.html with `.incbin`, so
    the line is sitting in the binary as plain bytes and a search finds it - which is exactly how
    companion/payloads.py reads the version off a downloaded asset. The EXE is a PyInstaller
    archive and its copy of the page is COMPRESSED, so the same search finds nothing and this
    refused to release a perfectly good build. Its bundled files have to be extracted, the way
    tools/check_stale_exe.py already does.
    """
    blob = io.open(path, "rb").read(64 << 20)
    m = re.search(b'var APP_VERSION="([0-9][0-9.]*)"', blob)
    if m:
        return m.group(1).decode("ascii", "replace")
    if not path.lower().endswith(".exe"):
        return ""
    try:
        from PyInstaller.archive.readers import CArchiveReader
        arch = CArchiveReader(path)
    except Exception:
        # No PyInstaller here to read it with. Say so rather than passing an unchecked artifact.
        die("cannot read %s as a PyInstaller archive - install PyInstaller, or the version inside "
            "the exe goes unchecked" % os.path.relpath(path, ROOT))
    for cand in ("web/index.html", os.path.join("web", "index.html"), "web\index.html"):
        if cand in set(arch.toc):
            got = arch.extract(cand)
            page = got[1] if isinstance(got, tuple) else got
            m = re.search(b'var APP_VERSION="([0-9][0-9.]*)"', page)
            return m.group(1).decode("ascii", "replace") if m else ""
    return ""


def newest_source(rels):
    """Newest mtime under these directories, ignoring build output.

    Only SOURCE counts. Including the generated headers (web_bundle.h, cheat_bundle.h) or the
    artifacts themselves would make every artifact permanently "stale", which is the failure mode
    that makes a staleness check get switched off.
    """
    newest, where = 0.0, ""
    for rel in rels:
        base = os.path.join(ROOT, rel.replace("/", os.sep))
        if os.path.isfile(base):
            t = os.path.getmtime(base)
            if t > newest:
                newest, where = t, rel
            continue
        for dp, dn, fn in os.walk(base):
            dn[:] = [d for d in dn if d not in IGNORE_DIRS]
            for f in fn:
                if os.path.splitext(f)[1].lower() in IGNORE_EXT or f in IGNORE_FILES:
                    continue
                p = os.path.join(dp, f)
                try:
                    t = os.path.getmtime(p)
                except OSError:
                    continue
                if t > newest:
                    newest, where = t, os.path.relpath(p, ROOT).replace("\\", "/")
    return newest, where


def check_version(want):
    say("  version is %s everywhere" % want)
    for rel, rx in VERSION_SITES:
        p = os.path.join(ROOT, rel.replace("/", os.sep))
        try:
            s = io.open(p, encoding="utf-8", errors="replace").read()
        except Exception as e:
            die("cannot read %s (%s)" % (rel, e))
        m = re.search(rx, s, re.M)
        if not m:
            die("no version line in %s - the pattern in VERSION_SITES no longer matches" % rel)
        if m.group(1) != want:
            die("%s says %s, not %s. Set the version in the source first; this tool does not "
                "write it, because a tag that disagrees with the binaries breaks the update lane."
                % (rel, m.group(1), want))
        say("     %-40s %s" % (rel, m.group(1)))


def check_artifacts(want):
    say("  the three artifacts are built, current, and report %s" % want)
    out = []
    for rel, srcs in ARTIFACTS:
        p = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.exists(p):
            die("%s is not built" % rel)
        # THE VERSION THE ARTIFACT ITSELF REPORTS, read the same way the app reads it off a
        # downloaded asset. If this disagrees, the build is older than the source it was built from
        # and every other check here would still have passed.
        got = artifact_version(p)
        if not got:
            die("%s does not report a version - it is not a build of this app, or the web bundle "
                "did not go in" % rel)
        if got != want:
            die("%s was built at %s, not %s - rebuild it" % (rel, got, want))
        src_t, src_f = newest_source(srcs)
        art_t = os.path.getmtime(p)
        if art_t < src_t:
            die("%s is older than %s - rebuild it%s"
                % (rel, src_f,
                   " (remember ps4-app/build-all-wsl.sh, not just the payload)"
                   if "PS4" in rel else ""))
        out.append((rel, p, os.path.getsize(p), sha256(p)))
        say("     %-44s %8.1f MB  %s" % (os.path.basename(rel), os.path.getsize(p) / 1048576.0, got))
    return out


def drop_release(tag):
    """Delete an existing release and tag so this version can be cut again.

    WHY THIS EXISTS RATHER THAN "just re-upload the assets". A release's assets and its tag have to
    be the same build: the tag says which source produced them, and that is the only thing anybody
    can check a download against. When the artifacts are rebuilt AFTER tagging - which happened the
    first time this tool was used, because the payload catalogue records a version that changes when
    the artifacts do - replacing the assets alone leaves a release whose files were never built from
    the commit it points at. Re-cutting is honest; clobbering is not.

    This is for a release nobody has taken yet. Once one is out, the next version is the answer.
    """
    say("  removing the existing %s so it can be cut again" % tag)
    r = run(["gh", "release", "delete", tag, "--yes", "--cleanup-tag"])
    if r.returncode != 0 and "release not found" not in (r.stderr or "").lower():
        say("     gh: %s" % (r.stderr.strip() or r.stdout.strip()))
    run(["git", "tag", "-d", tag])
    run(["git", "push", "origin", ":refs/tags/" + tag])
    if run(["git", "tag", "-l", tag]).stdout.strip():
        die("%s is still there after trying to remove it" % tag)


def check_tree(tag, dry):
    st = run(["git", "status", "--porcelain"])
    if st.returncode != 0:
        die("this is not a git checkout")
    if st.stdout.strip() and not dry:
        die("the working tree has uncommitted changes:\n%s"
            % "\n".join("       " + l for l in st.stdout.strip().splitlines()[:12]))
    ex = run(["git", "tag", "-l", tag])
    if ex.stdout.strip():
        die("%s already exists as a tag" % tag)
    say("  working tree is clean and %s is free" % tag)


NOTES_DIR = "docs/release-notes"


def md_to_text(md):
    """Markdown to something worth opening in Notepad.

    The zip's other files are .txt and are read by people who have just downloaded a program, not
    by anybody with a markdown viewer. Headings become plain lines, emphasis and code ticks go, and
    `*` bullets become `-` so they cannot be confused with the emphasis being stripped around them.
    Tables are left exactly as they are: pipes line up in a monospaced font, which is what Notepad
    uses, and rewriting them would make them worse.
    """
    out = []
    for line in md.splitlines():
        st = line.lstrip()
        indent = line[:len(line) - len(st)]
        if st.startswith("#"):
            st = st.lstrip("#").strip()
            if st:
                out.append(st)
                out.append("-" * min(len(st), 64))
            continue
        # The bullet is converted FIRST, so stripping emphasis below cannot eat it.
        if st.startswith("* "):
            st = "- " + st[2:]
        st = st.replace("**", "").replace("`", "")
        # Then single-asterisk emphasis, from the text only - never from the bullet marker.
        if st.startswith("- "):
            st = "- " + st[2:].replace("*", "")
        else:
            st = st.replace("*", "")
        # A table's separator row is pure markdown punctuation and means nothing in plain text.
        if st.startswith("|") and not st.strip("|-: "):
            continue
        # A markdown link reads better as "text (url)" than as brackets and parentheses.
        st = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
                    lambda m: m.group(1) if m.group(2).endswith(".md") else
                    "%s (%s)" % (m.group(1), m.group(2)), st)
        out.append(indent + st)
    return "\n".join(out)


def zip_changelog(want):
    """Every release note, newest first, as one plain-text file for the zip.

    BUILT FROM THE NOTES, NOT FROM CHANGELOG.md, and that is deliberate for the same reason
    release_notes() below is written by hand: CHANGELOG.md is the engineering record, and generating
    reader-facing text out of it once put an internal note - including an account name that has
    nothing to do with this project - in front of everybody who downloaded the first release. These
    notes were each written to be read by somebody deciding whether to download 90 MB onto a
    console, so collecting them is safe by construction.

    Assembled at pack time rather than kept as a file, so it cannot be stale: a release that adds a
    note gets it, and one that does not cannot ship a changelog that disagrees with its own notes.

    The Download table and the trailing links are dropped from each entry - inside a zip you already
    have the files, and a relative markdown link points at nothing.
    """
    ndir = os.path.join(ROOT, NOTES_DIR)
    vers = []
    for fn in os.listdir(ndir):
        if not (fn.startswith("v") and fn.endswith(".md")):
            continue
        v = fn[1:-3]
        try:
            key = tuple(int(x) for x in v.split("."))
        except ValueError:
            continue                      # not a version-shaped name; not ours to publish
        vers.append((key, v, os.path.join(ndir, fn)))
    vers.sort(reverse=True)

    # Dates come from CHANGELOG.md's own headings, which is where they are already maintained.
    dates = {}
    try:
        cl = io.open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8").read()
        for m in re.finditer(r"^## \[([0-9.]+)\] - (\d{4}-\d{2}-\d{2})", cl, re.M):
            dates[m.group(1)] = m.group(2)
    except Exception:
        pass

    out = ["PKG MUTANT SHOP - what changed",
           "=" * 64,
           "",
           "Newest first. This covers %d release%s; the full engineering record is in CHANGELOG.md"
           % (len(vers), "" if len(vers) == 1 else "s"),
           "in the source tree.",
           ""]
    for _key, v, path in vers:
        try:
            body = io.open(path, encoding="utf-8").read()
        except Exception:
            continue
        # Everything from the Download table on is about getting the files, which the reader of a
        # zip has already done.
        cut = body.find("## Download")
        if cut > 0:
            body = body[:cut]
        head = "%s%s" % (v, ("  -  " + dates[v]) if v in dates else "")
        out.append("")
        out.append("-" * 64)
        out.append(head + ("   (this release)" if v == want else ""))
        out.append("-" * 64)
        out.append(md_to_text(body).strip())
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def release_notes(want):
    """The short, user-facing notes for this version.

    THESE ARE NOT THE CHANGELOG. The changelog is the engineering record - long, exact, and written
    for whoever has to understand why something is the way it is. A release note is read by someone
    deciding whether to download 90 MB onto a console, and it should answer that in a few lines:
    what is new, what is fixed, what they have to do. Everything else is one link away.

    So the notes live in docs/release-notes/v<version>.md and are written by hand. Generating them
    from the changelog was tried and is what put an internal note - including the name of a GitHub
    account that has nothing to do with this project - in front of every reader of the first
    release. A file that has to be written on purpose cannot leak a sentence nobody meant to send.
    """
    p = os.path.join(ROOT, NOTES_DIR, "v%s.md" % want)
    if not os.path.isfile(p):
        die("there are no release notes for %s.\n"
            "       Write %s/v%s.md first - a few short, user-facing lines: what is new, what is\n"
            "       fixed, and anything they have to do. The changelog is the long version."
            % (want, NOTES_DIR, want))
    text = io.open(p, encoding="utf-8").read().strip()
    if not text:
        die("%s/v%s.md is empty." % (NOTES_DIR, want))
    return text


def changelog_section(want):
    """This version's own section of CHANGELOG.md. Used only to check that one exists."""
    p = os.path.join(ROOT, "CHANGELOG.md")
    s = io.open(p, encoding="utf-8", errors="replace").read().splitlines()
    start = None
    for i, line in enumerate(s):
        if re.match(r"^#{1,3}\s", line) and want in line:
            start = i
            break
    if start is None:
        die("CHANGELOG.md has no section for %s. Write it before releasing." % want)
    level = len(re.match(r"^(#+)", s[start]).group(1))
    body = []
    for line in s[start + 1:]:
        m = re.match(r"^(#+)\s", line)
        if m and len(m.group(1)) <= level:
            break
        body.append(line)
    text = "\n".join(body).strip()
    if not text:
        die("CHANGELOG.md's section for %s is empty." % want)
    return text


def gates():
    # THE PAGE PEOPLE READ IS PART OF THE RELEASE. The README's badges were typed by hand and went
    # three releases and three firmwares stale on a public repository before anyone noticed - and
    # the notes had drifted into prose with the wrong firmware in the download table. Both are
    # checked here, first, because they are cheap and they are what a stranger sees.
    say("  the front page and the release notes (tools/check_docs.py)")
    r = run([sys.executable, os.path.join(HERE, "check_docs.py")])
    for l in (r.stdout + r.stderr).strip().splitlines()[-4:]:
        say("     %s" % l)
    if r.returncode != 0:
        die("check_docs failed - the README or the notes do not match the build")

    say("  every gate (tools/ready_check.py)")
    r = run([sys.executable, os.path.join(HERE, "ready_check.py")])
    tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
    for l in tail:
        say("     %s" % l)
    if r.returncode != 0:
        die("ready_check failed - fix it, do not release around it")


def write_release_dir(want, arts, commit):
    d = os.path.join(ROOT, "release")
    if not os.path.isdir(d):
        os.makedirs(d)
    man = {"version": want, "commit": commit,
           "assets": [{"name": os.path.basename(rel), "size": sz, "sha256": h}
                      for rel, _p, sz, h in arts]}
    mp = os.path.join(d, "manifest.json")
    io.open(mp, "w", encoding="utf-8", newline="\n").write(
        json.dumps(man, indent=2, sort_keys=True) + "\n")
    sp = os.path.join(d, "SHA256SUMS")
    io.open(sp, "w", encoding="utf-8", newline="\n").write(
        "".join("%s  %s\n" % (h, os.path.basename(rel)) for rel, _p, _sz, h in arts))
    say("  wrote release/manifest.json and release/SHA256SUMS")
    return mp, sp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("version")
    ap.add_argument("--dry-run", action="store_true",
                    help="every check, then print what would be uploaded. Touches nothing.")
    ap.add_argument("--draft", action="store_true", help="create the release as a draft")
    ap.add_argument("--replace", action="store_true",
                    help="delete an existing release and tag of this version first, then cut it "
                         "again. For a release nobody has taken yet - see drop_release().")
    ap.add_argument("--notes-only", action="store_true",
                    help="print the release notes this version would use, and stop")
    a = ap.parse_args()

    want = a.version.lstrip("v")
    if not re.match(r"^\d+\.\d+\.\d+$", want):
        die("%r is not a version like 3.86.0" % a.version)
    tag = "v" + want

    if a.notes_only:
        print(release_notes(want))
        return 0

    say("\nRELEASE %s%s\n" % (tag, "   (dry run - nothing will be changed)" if a.dry_run else ""))
    if a.replace and not a.dry_run:
        drop_release(tag)
    check_tree(tag, a.dry_run)
    check_version(want)
    arts = check_artifacts(want)
    gates()
    changelog_section(want)              # the long record must exist, even though it is not the notes
    notes = release_notes(want)
    say("  release notes: %d lines from %s/v%s.md" % (len(notes.splitlines()), NOTES_DIR, want))
    commit = run(["git", "rev-parse", "HEAD"]).stdout.strip()[:12]
    mp, sp = write_release_dir(want, arts, commit)

    # THE THREE ARTIFACTS, AND A ZIP OF ALL THREE.
    #
    # WHY THE ZIP. It is the one-download option: all three files and their checksums, for somebody
    # setting up a PC and both consoles in one go, and for any browser that would rather hand over
    # an archive than a bare executable.
    #
    # The loose files stay beside it and are not going anywhere: the app's own update lane picks a
    # console's file out of a release BY NAME (companion/payloads.py pick_asset), so those names are
    # a contract. The zip is additional, never a replacement.
    #
    # SHA256SUMS travels inside it so anyone can check that what they extracted is what was
    # published, without having to take anybody's word for it.
    zip_path = os.path.join(ROOT, "release", "PKG-MUTANT-SHOP-%s.zip" % want)
    sums = "\n".join("%s  %s" % (h, os.path.basename(p)) for _rel, p, _sz, h in arts) + "\n"
    try:
        os.remove(zip_path)
    except OSError:
        pass
    import zipfile
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for _rel, p, _sz, _h in arts:
            z.write(p, os.path.basename(p))
        z.writestr("SHA256SUMS.txt", sums)
        z.writestr("READ ME FIRST.txt", ZIP_README % {"v": want, "sums": sums})
        # WHAT IT DOES AND WHAT CHANGED, for somebody who has the zip and not the repository.
        # FEATURES.md is maintained by hand and is checked by tools/check_docs.py; the changelog is
        # assembled from the release notes at this moment, so it can never disagree with them.
        z.writestr("FEATURES.txt", md_to_text(
            io.open(os.path.join(ROOT, "docs", "FEATURES.md"), encoding="utf-8").read()))
        z.writestr("CHANGELOG.txt", zip_changelog(want))
    say("  packed %s (%.1f MB)" % (os.path.basename(zip_path), os.path.getsize(zip_path) / 1e6))

    uploads = [p for _rel, p, _sz, _h in arts] + [zip_path]
    if a.dry_run:
        say("\n  would tag %s at %s and upload:" % (tag, commit))
        for u in uploads:
            say("     %s" % os.path.relpath(u, ROOT).replace("\\", "/"))
        say("\n  nothing was changed.\n")
        return 0

    say("\n  tagging %s" % tag)
    r = run(["git", "tag", "-a", tag, "-m", "PKG MUTANT SHOP " + want])
    if r.returncode != 0:
        die("git tag failed: %s" % (r.stderr.strip() or r.stdout.strip()))
    r = run(["git", "push", "origin", tag])
    if r.returncode != 0:
        run(["git", "tag", "-d", tag])
        die("could not push the tag (it has been removed locally): %s" % r.stderr.strip())

    nf = os.path.join(ROOT, "release", "NOTES.md")
    io.open(nf, "w", encoding="utf-8", newline="\n").write(notes + "\n")
    cmd = ["gh", "release", "create", tag, "--title", "PKG MUTANT SHOP " + want,
           "--notes-file", nf]
    if a.draft:
        cmd.append("--draft")
    cmd.extend(uploads)
    say("  creating the GitHub release and uploading %d files" % len(uploads))
    r = run(cmd)
    if r.returncode != 0:
        die("gh release create failed (the tag is pushed; delete it if you are starting over):\n%s"
            % (r.stderr.strip() or r.stdout.strip()))
    say("\n  %s\n" % (r.stdout.strip() or "released"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

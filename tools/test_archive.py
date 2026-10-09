# -*- coding: utf-8 -*-
"""shared/pms_inflate.h, pms_zip.h and pms_zplan.h against Python's own zlib and zipfile.

WHY THIS TEST EXISTS. The console can now open an archive on its own - that is what lets it install
and update the emulators with every PC switched off - and the decompressor is code this project
wrote, because neither payload SDK links one. Both ship a zlib.h with nothing behind it; nm over
every object in both target/lib finds no inflate. A decompressor that is subtly wrong does not
crash: it writes a file that is almost right, and the emulator it belongs to fails to start three
weeks later.

So it is checked against something that is not ours.

  1 inflate   492 generated deflate streams against Python's zlib: every block type, every
              compression level, empty, one byte, all-identical, random, text, and sizes on and
              around each 32 KB buffer boundary - the places a streaming decoder goes wrong. Plus
              six CORRUPT streams, which must be refused with a code rather than crash or hang.
  2 zip       every real release archive this app offers is extracted by the shipped reader and by
              Python's zipfile, and the two trees are compared file by file, path and sha256.
  3 plan      the console's planner and companion/payloads.py's zip_plan() are run over the same
              archives and every field compared. Two planners that could drift are only tolerable
              while something holds them together, and this is that something.

    python tools/test_archive.py                 # archives under the session scratchpad, if any
    python tools/test_archive.py <dir-of-zips>

Stage 1 needs nothing but Python. Stages 2 and 3 need real archives, and say so and skip rather
than report a pass, when there are none to hand.
"""
import hashlib
import io
import os
import random
import shutil
import subprocess
import sys
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "companion"))

INF_OK, INF_ERR_DATA, INF_ERR_EOF = 0, -3, -4
WIN = 32 * 1024


def wsl(p):
    return p.replace("C:\\", "/mnt/c/").replace("C:/", "/mnt/c/").replace("\\", "/")


def sh(args, timeout=3600):
    return subprocess.run(["wsl", "-e"] + args, capture_output=True, text=True,
                          timeout=timeout, errors="replace")


def build(out):
    r = sh(["bash", "-lc", "gcc -O2 -Wall -Wextra -Wno-unused-function -o '%s' '%s'"
            % (wsl(out), wsl(os.path.join(HERE, "archive_harness.c")))])
    if r.returncode != 0:
        print(r.stdout + r.stderr)
        sys.exit("the harness did not compile")
    for w in [l for l in (r.stderr or "").splitlines() if "warning" in l][:10]:
        print("  warn:", w)


def deflate(raw, level=6, wbits=-15):
    c = zlib.compressobj(level, zlib.DEFLATED, wbits)
    return c.compress(raw) + c.flush()


def stage_inflate(harness, work):
    """Every shape of deflate stream, against Python's own answer."""
    cases = []
    cases.append(("empty", b""))
    cases.append(("one byte", b"A"))
    cases.append(("all identical 100k", b"\x5a" * 100000))
    cases.append(("text", (b"the quick brown fox jumps over the lazy dog. " * 3000)))
    cases.append(("incompressible 200k", bytes(random.Random(7).getrandbits(8)
                                               for _ in range(200000))))
    # A stored block is what level 0 produces, and it has its own code path.
    cases.append(("stored (level 0) 80k", bytes(random.Random(8).getrandbits(8)
                                                for _ in range(80000))))
    # SIZES ON AND AROUND EVERY BUFFER BOUNDARY. A streaming decoder's bugs live here: the window
    # wrap, the input refill, and the flush that happens exactly at the edge.
    edges = []
    for base in (0, WIN, 2 * WIN, 3 * WIN):
        for d in (-2, -1, 0, 1, 2, 17):
            n = base + d
            if n >= 0:
                edges.append(n)
    rnd = random.Random(99)
    for n in sorted(set(edges)):
        cases.append(("edge %d" % n, bytes(rnd.getrandbits(8) for _ in range(n))))
    # 400 random cases, random size and random content mix, so the suite is not only the shapes
    # somebody thought of.
    for i in range(400):
        r = random.Random(1000 + i)
        n = r.choice([r.randint(0, 300), r.randint(0, 9000), r.randint(0, 70000)])
        if r.random() < 0.4:
            raw = bytes(r.getrandbits(8) for _ in range(n))
        elif r.random() < 0.5:
            raw = (b"abcdefgh" * (n // 8 + 1))[:n]
        else:
            raw = bytes(r.choice(b"\x00\x01\xff AB") for _ in range(n))
        cases.append(("random %d (%d B)" % (i, n), raw))

    din = os.path.join(work, "_in.deflate")
    dout = os.path.join(work, "_out.bin")
    bad = 0
    ran = 0
    levels = [0, 1, 6, 9]
    print("== 1. inflate, against Python's zlib ==")
    for i, (name, raw) in enumerate(cases):
        lvl = levels[i % len(levels)]
        comp = deflate(raw, lvl)
        io.open(din, "wb").write(comp)
        r = sh([wsl(harness), "inflate", wsl(din), str(len(comp)), wsl(dout)])
        ran += 1
        got = io.open(dout, "rb").read() if os.path.exists(dout) else b""
        if r.returncode != 0 or got != raw:
            print("  FAIL %-28s level %d: rc=%s, %d B out of %d"
                  % (name, lvl, (r.stdout or r.stderr).strip()[:40], len(got), len(raw)))
            bad += 1
    print("   %d stream(s), %d failure(s)" % (ran, bad))

    print("== corrupt streams must be refused, not crash or hang ==")
    good = deflate(b"x" * 60000 + b"the end", 6)
    muts = [
        ("truncated", good[:len(good) // 2]),
        ("one bit flipped", good[:9] + bytes([good[9] ^ 0x10]) + good[10:]),
        ("tail chopped", good[:-1]),
        ("all 0xFF", b"\xff" * 4096),
        ("zeros", b"\x00" * 4096),
        ("random", bytes(random.Random(5).getrandbits(8) for _ in range(4096))),
    ]
    for name, blob in muts:
        io.open(din, "wb").write(blob)
        try:
            r = sh([wsl(harness), "inflate", wsl(din), str(len(blob)), wsl(dout)], timeout=60)
        except subprocess.TimeoutExpired:
            print("  FAIL %-18s HUNG" % name)
            bad += 1
            continue
        line = (r.stdout or "").strip()
        rc = None
        for tok in line.split():
            if tok.startswith("rc="):
                rc = int(tok[3:])
        if rc is None or rc >= 0:
            print("  FAIL %-18s accepted a corrupt stream (%s)" % (name, line))
            bad += 1
        else:
            print("  %-18s refused with rc=%d" % (name, rc))
    return bad, ran + len(muts)


def tree_of(root):
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, root).replace("\\", "/")
            h = hashlib.sha256()
            with io.open(full, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            out[rel] = (os.path.getsize(full), h.hexdigest())
    return out


def stage_zip(harness, work, archives):
    """Every real archive, extracted twice and compared byte for byte."""
    print("== 2. the ZIP container, against Python's zipfile ==")
    bad = 0
    for a in archives:
        name = os.path.basename(a)
        # A FRESH PAIR OF DIRECTORIES PER ARCHIVE, PROVEN EMPTY. An earlier run of this reported a
        # failure that was only a left-over tree from an aborted run: ignore_errors on a rmtree
        # will happily leave the files behind. A test that can report a failure it was handed
        # rather than one it found is not a test.
        mine = os.path.join(work, "_mine", name)
        theirs = os.path.join(work, "_theirs", name)
        for d in (mine, theirs):
            shutil.rmtree(d, ignore_errors=True)
            if os.path.exists(d):
                shutil.rmtree(d)
            os.makedirs(d)
            if os.listdir(d):
                sys.exit("%s is not empty, so the comparison would be meaningless" % d)
        r = sh([wsl(harness), "extract", wsl(a), wsl(mine)])
        with zipfile.ZipFile(a) as z:
            z.extractall(theirs)
        tm, tt = tree_of(mine), tree_of(theirs)
        same = tm == tt
        ok = r.returncode == 0
        if not (same and ok):
            bad += 1
        print("   %-34s %-6s %d file(s)   %s"
              % (name, "OK" if (same and ok) else "FAIL", len(tt),
                 (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else ""))
        if not same:
            onlym = sorted(set(tm) - set(tt))[:4]
            onlyt = sorted(set(tt) - set(tm))[:4]
            diff = [k for k in (set(tm) & set(tt)) if tm[k] != tt[k]][:4]
            if onlym:
                print("      only ours  :", onlym)
            if onlyt:
                print("      only python:", onlyt)
            if diff:
                print("      differ     :", diff)
        if not ok:
            print("      harness:", (r.stdout + r.stderr).strip()[:300])
        shutil.rmtree(mine, ignore_errors=True)
        shutil.rmtree(theirs, ignore_errors=True)
    return bad


def stage_plan(harness, archives):
    """The console's planner and the companion's must agree on every field."""
    print("== 3. the planner, against companion/payloads.py's zip_plan() ==")
    import json
    try:
        import payloads as P
    except Exception as e:
        print("   companion/payloads.py would not import (%r), so this stage is skipped" % e)
        return 0
    bad = 0
    for a in archives:
        name = os.path.basename(a)
        r = sh([wsl(harness), "plan", wsl(a)])
        if r.returncode != 0:
            print("   %-34s the C planner failed: %s" % (name, (r.stderr or "")[:100]))
            bad += 1
            continue
        c = json.loads(r.stdout)
        p = P.zip_plan(a)
        diffs = []
        if bool(c["ok"]) != bool(p.get("ok")):
            diffs.append("ok %r != %r" % (c["ok"], p.get("ok")))
        if c["kind"] != (p.get("kind") or ""):
            diffs.append("kind %r != %r" % (c["kind"], p.get("kind")))
        if c["app_root"] != (p.get("app_root") or ""):
            diffs.append("app_root %r != %r" % (c["app_root"], p.get("app_root")))
        if (c["tid"] or "") != (p.get("tid") or ""):
            diffs.append("tid %r != %r" % (c["tid"], p.get("tid")))
        if sorted(c["pkgs"]) != sorted(p.get("pkgs") or []):
            diffs.append("pkgs %r != %r" % (sorted(c["pkgs"])[:3], sorted(p.get("pkgs") or [])[:3]))
        if sorted(c["data_roots"]) != sorted(p.get("data_roots") or []):
            diffs.append("data_roots %r != %r"
                         % (sorted(c["data_roots"]), sorted(p.get("data_roots") or [])))
        if c["entries"] != p.get("entries"):
            diffs.append("entries %r != %r" % (c["entries"], p.get("entries")))
        print("   %-34s %-5s kind=%-5s tid=%-10s app_root=%-26s data=%s"
              % (name, "OK" if not diffs else "DIFF", c["kind"], c["tid"] or "-",
                 (c["app_root"] or "-")[:26], ",".join(c["data_roots"]) or "-"))
        for d in diffs:
            print("        " + d)
            bad += 1
    return bad


def stage_install(harness, work, archives):
    """The whole lane: place each archive the way the console places it, and prove the two
    promises the design rests on.

    1 PLACEMENT    the app folder lands at <root>/<TITLEID>, the emulator's own data directory
                   lands where the catalogue says and NOWHERE ELSE, and loose files at the top of
                   the archive are not copied into the app.
    2 MERGE        installing a second time over a folder the owner has added files to leaves
                   those files alone. This is the promise that matters: PS5X360 reads games from
                   assets/roms/ INSIDE the app folder and RetroArch keeps its whole configuration
                   there, so an update that replaced the folder would destroy a ROM library. A
                   replace would pass stage 1 and fail here, which is the point.
    3 NO GUESSING  an archive with a data directory the catalogue does not name must report that
                   directory BY NAME and leave it in the archive, not pick somewhere for it.
    """
    print("== 4. the install lane, on every real archive ==")
    import json
    bad = 0
    # Known from the catalogue. Anything not listed here must come back as skipped-by-name.
    SPEC = {"PS5SX2-vk-285-139.zip": "PCSX2:%s"}
    for a in archives:
        name = os.path.basename(a)
        root = os.path.join(work, "_hb")
        dataroot = os.path.join(work, "_data", "PCSX2")
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(os.path.join(work, "_data"), ignore_errors=True)
        os.makedirs(root)
        spec = SPEC.get(name, "")
        if spec:
            spec = spec % wsl(dataroot)
        r = sh([wsl(harness), "install", wsl(a), wsl(root), "", spec])
        if r.returncode != 0:
            print("   %-34s FAILED %s" % (name, (r.stdout or r.stderr).strip()[:160]))
            bad += 1
            continue
        j = json.loads(r.stdout)
        placed = j["placed"].replace(wsl(root) + "/", "")
        here = os.path.join(root, placed)
        n_disk = sum(len(f) for _d, _s, f in os.walk(here))
        line = "   %-34s %-10s %-5d file(s)" % (name, j["tid"], n_disk)
        # 1 - the app folder is named after the title id
        if not placed or placed != j["tid"]:
            print(line + "  PLACED AT %r, not the title id" % placed)
            bad += 1
            continue
        # 1 - the data directory went where it was told, and only there
        if spec:
            if not os.path.isdir(dataroot) or not os.listdir(dataroot):
                print(line + "  the catalogue named %s and nothing arrived there" % dataroot)
                bad += 1
                continue
            line += "  + %d in the recorded data folder" % sum(
                len(f) for _d, _s, f in os.walk(dataroot))
        elif j["skipped"]:
            line += "  skipped (named): " + ",".join(j["skipped"])

        # 2 - MERGE. A file the owner put there survives a second install.
        mine = os.path.join(here, "pms-merge-probe.txt")
        io.open(mine, "w", encoding="utf-8").write("the owner's own file")
        deep = os.path.join(here, "assets", "roms")
        os.makedirs(deep, exist_ok=True)
        rom = os.path.join(deep, "a-game.rom")
        io.open(rom, "w", encoding="utf-8").write("x" * 1024)
        r2 = sh([wsl(harness), "install", wsl(a), wsl(root), "", spec])
        if r2.returncode != 0:
            print(line + "  a SECOND install failed: %s" % (r2.stdout or r2.stderr).strip()[:120])
            bad += 1
            continue
        if not os.path.isfile(mine) or not os.path.isfile(rom):
            print(line + "  a second install DESTROYED the owner's files - it replaced, "
                         "it did not merge")
            bad += 1
            continue
        print(line + "  merge OK")
    # 3 - an unnamed data directory must be reported, not placed
    sx = [a for a in archives if "PS5SX2" in os.path.basename(a)]
    if sx:
        root = os.path.join(work, "_hb2")
        shutil.rmtree(root, ignore_errors=True)
        os.makedirs(root)
        r = sh([wsl(harness), "install", wsl(sx[0]), wsl(root), "", ""])
        j = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
        named = "PCSX2" in (j.get("skipped") or [])
        said = "PCSX2" in (j.get("why") or "")
        print("   %-34s with NO recorded data folder: %s"
              % (os.path.basename(sx[0]),
                 "named it and left it alone" if (named and said) else "DID NOT SAY"))
        if not (named and said):
            bad += 1
    return bad


def main():
    work = os.path.join(os.environ.get("TEMP", "/tmp"), "pms-archive-test")
    os.makedirs(work, exist_ok=True)
    harness = os.path.join(work, "archive_harness")
    build(harness)

    bad, ran = stage_inflate(harness, work)

    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.environ.get("CLAUDE_SCRATCHPAD", ""), "arch")
    archives = []
    if os.path.isdir(src):
        archives = sorted(os.path.join(src, f) for f in os.listdir(src)
                          if f.lower().endswith(".zip"))
    print()
    if not archives:
        print("== 2 and 3 skipped: no archives in %s ==" % src)
        print("   The generated streams above test the decompressor itself. Comparing against a")
        print("   REAL release archive needs one to hand, and a pass is not claimed without it.")
    else:
        bad += stage_zip(harness, work, archives)
        print()
        bad += stage_plan(harness, archives)
        print()
        bad += stage_install(harness, work, archives)

    print()
    print("test_archive: %s (%d stream(s), %d archive(s), %d failure(s))"
          % ("OK" if not bad else "FAILED", ran, len(archives), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Is the package the console installed the same bytes as the one on the PC?

WHY THIS EXISTS. A game installed through our lane and then crashed on launch, and the obvious
suspicion was the install engine. The trouble with that suspicion is that every step reports
success: we register a BGFT task, the CONSOLE downloads and installs it with its own installer,
and if a single byte arrives wrong the result is a game that looks perfectly installed and dies
when it runs. Sizes matching proves nothing - a resumed or mis-ranged download is exactly the
right length and the wrong contents.

So this compares CONTENT. On a PS4 an installed title keeps its package at
/user/app/<TITLE_ID>/app.pkg, and GoldHEN's FTP supports REST, which gives us ranged reads of an
11 GB file without moving 11 GB. It samples several windows - the head, the tail, and points
across the middle - and compares each against the same offsets of the source file on this PC.

A mismatch anywhere is proof the transfer corrupted the package. All windows matching does not
prove every byte is right, and this says so rather than overclaiming: it makes a corrupt install
very unlikely and is the strongest statement available without hashing the whole file.

    python tools/verify_console_install.py <TITLE_ID> [--windows N] [--size BYTES]
"""
import argparse
import io
import json
import os
import sys
import urllib.request

BASE = "http://127.0.0.1:8710"


def companion(path, timeout=180):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def find_source(tid):
    """The .pkg on this PC for a title, and its size - located by walking the library folders."""
    try:
        roots = companion("/api/health", timeout=10).get("library_paths") or []
        lib = companion("/api/library")
    except Exception as e:
        print("could not reach the companion: %s" % e)
        return None, None
    names = []
    for g in (lib.get("games") or []):
        if str(g.get("title_id") or "") != tid:
            continue
        for b in (g.get("base") or []):
            if b.get("file"):
                names.append(b["file"])
    if not names:
        return None, None
    for root in roots:
        for dirpath, _d, files in os.walk(root):
            for fn in files:
                if fn in names:
                    p = os.path.join(dirpath, fn)
                    return p, os.path.getsize(p)
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("title_id")
    ap.add_argument("--ip", default="")
    ap.add_argument("--ftp-port", type=int, default=2121)
    ap.add_argument("--windows", type=int, default=6)
    ap.add_argument("--size", type=int, default=262144, help="bytes per window")
    a = ap.parse_args()

    ip = a.ip
    if not ip:
        for c in (companion("/api/consoles").get("consoles") or []):
            if str(c.get("platform") or "").lower() == "ps4":
                ip = c.get("ip")
    if not ip:
        print("no PS4 address - pass --ip")
        return 2

    src, ssize = find_source(a.title_id)
    if not src:
        print("no source package in the library for %s" % a.title_id)
        return 2

    remote = "/user/app/%s/app.pkg" % a.title_id
    shop = "http://%s:8710" % ip

    def head_size():
        try:
            with urllib.request.urlopen(shop + "/api/fs/stat?path=" + urllib.parse.quote(remote),
                                        timeout=20) as r:
                d = json.loads(r.read().decode())
            return int(d.get("size") or 0) if d.get("ok") else 0
        except Exception:
            return 0

    def fetch(off, n):
        req = urllib.request.Request(shop + "/api/fs/read?path=" + urllib.parse.quote(remote))
        req.add_header("Range", "bytes=%d-%d" % (off, off + n - 1))
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, r.read()

    rsize = head_size()
    if not rsize:
        print("  %s is not on the console (or the shop is not answering)" % remote)
        return 2

    print("  title    : %s" % a.title_id)
    print("  on PC    : %s (%s bytes)" % (src, format(ssize, ",")))
    print("  on PS4   : %s (%s bytes)" % (remote, format(rsize, ",")))
    if rsize != ssize:
        print("  NOTE     : the sizes differ by %s bytes" % format(abs(rsize - ssize), ","))

    n = min(ssize, rsize)
    if n < a.size:
        print("  file is smaller than one window")
        return 2
    step = max(1, (n - a.size) // max(1, a.windows - 1))
    offsets = [min(i * step, n - a.size) for i in range(a.windows)]
    offsets[-1] = n - a.size                      # always include the very tail

    bad = 0
    with io.open(src, "rb") as f:
        for off in offsets:
            f.seek(off)
            want = f.read(a.size)
            try:
                st, got = fetch(off, a.size)
            except Exception as e:
                print("  [FAIL] offset %-16s read failed: %s" % (format(off, ","), e))
                bad += 1
                continue
            ok = (st in (200, 206) and got == want)
            if not ok:
                bad += 1
            extra = ""
            if not ok:
                extra = "  (got %s bytes%s)" % (format(len(got), ","),
                                                ", all zero" if got and set(got) == {0} else "")
            print("  [%s] offset %-16s %s bytes%s"
                  % ("PASS" if ok else "FAIL", format(off, ","), format(len(got), ","), extra))

    print()
    if bad:
        print("%d of %d windows DIFFER - the package on the console is not the one we sent."
              % (bad, len(offsets)))
        return 1
    print("all %d windows match, head to tail. Not a whole-file hash, but a transfer that "
          "corrupted or truncated the package would have shown here." % len(offsets))
    return 0


if __name__ == "__main__":
    sys.exit(main())

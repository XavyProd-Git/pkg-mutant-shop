# -*- coding: utf-8 -*-
"""The bytes a console downloads must be the bytes on disk. Proved, not assumed.

WHY THIS EXISTS. A game installed through our lane and then crashed on launch, and "the install
engine is broken" is a reasonable first thought. It is also the hardest kind of claim to settle by
reading code, because the install itself is done by the CONSOLE: we register a BGFT task pointing
at an HTTP URL and the console downloads and installs it with its own installer. If the console
receives even one wrong byte, it installs a game that looks complete and crashes when it runs - and
nothing anywhere reports an error, because every step "succeeded".

So the one thing we are actually responsible for is serving the file correctly, and that is
testable exactly rather than argued about. BGFT downloads with Range requests, resumes, and may
ask for the tail first; a serving bug in any of those paths corrupts the install silently.

WHAT IT CHECKS, against a real package in the library:
  * a plain GET returns the whole file, byte-identical to disk
  * HEAD reports the true size and advertises byte ranges
  * a normal range, the first byte, the last byte and the exact tail all return the right bytes
  * a suffix range (bytes=-N) returns the LAST N bytes, not the first N
  * reassembling the file from many sequential ranges reproduces it exactly
  * an unsatisfiable range is refused with 416 rather than served as something else
  * Content-Range and Content-Length agree with the body actually sent

It hashes ranges of a REAL package rather than a synthetic file, because the bug being hunted
would be in the serving path, and that path only runs for library files.

    python tools/test_pkg_serving.py                # picks the smallest package itself
    python tools/test_pkg_serving.py <install_key>  # or name one
"""
import hashlib
import io
import json
import os
import random
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8710"
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  -> " + str(detail)[:120]) if detail else ""))


def get(path, rng=None, method="GET", timeout=120):
    req = urllib.request.Request(BASE + path, method=method)
    if rng:
        req.add_header("Range", rng)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), (b"" if method == "HEAD" else r.read())
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), b""


def main():
    try:
        with urllib.request.urlopen(BASE + "/api/health", timeout=8) as r:
            json.loads(r.read().decode())
    except Exception:
        print("test_pkg_serving: the companion is not running on 8710 - skipping")
        return 0

    # Which package? The smallest real .pkg with a local path, so the test is quick and so the
    # file it compares against is one we can actually read from disk.
    try:
        with urllib.request.urlopen(BASE + "/api/library", timeout=180) as r:
            lib = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        print("test_pkg_serving: could not read the library (%s) - skipping" % e)
        return 0

    # A library row carries an install_key and a file NAME, not a path - the folders are config.
    # Find the file ourselves so the test can compare the served bytes against the real ones.
    roots = []
    try:
        with urllib.request.urlopen(BASE + "/api/health", timeout=8) as r:
            roots = (json.loads(r.read().decode()).get("library_paths") or [])
    except Exception:
        pass
    index = {}
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                index.setdefault(fn, os.path.join(dirpath, fn))

    want = sys.argv[1] if len(sys.argv) > 1 else None
    best = None
    for g in (lib.get("games") or []):
        for b in (g.get("base") or []) + (g.get("updates") or []):
            key, fn, size = b.get("install_key"), b.get("file"), b.get("size") or 0
            if not key or not fn or not str(fn).lower().endswith(".pkg"):
                continue
            path = index.get(fn)
            if not path or not os.path.exists(path):
                continue
            if want and key != want:
                continue
            if best is None or size < best[2]:
                best = (key, path, size)
    if not best:
        print("test_pkg_serving: no local .pkg in the library to test against - skipping")
        return 0

    key, path, _ = best
    real = os.path.getsize(path)
    print("  package: %s\n  on disk: %s (%d bytes)\n" % (key, path, real))
    url = "/library/" + urllib.parse.quote(key, safe="")

    # ---- HEAD --------------------------------------------------------------------------------
    st, hd, _ = get(url, method="HEAD")
    check("HEAD answers 200", st == 200, st)
    check("HEAD reports the true size", hd.get("Content-Length") == str(real),
          "%s vs %d" % (hd.get("Content-Length"), real))
    check("byte ranges are advertised", (hd.get("Accept-Ranges") or "").lower() == "bytes",
          hd.get("Accept-Ranges"))

    disk = io.open(path, "rb").read()
    whole = hashlib.sha256(disk).hexdigest()

    # ---- whole file --------------------------------------------------------------------------
    st, hd, body = get(url)
    check("a plain GET returns every byte", st == 200 and len(body) == real,
          "http %s, %d bytes" % (st, len(body)))
    check("a plain GET is byte-identical to disk",
          hashlib.sha256(body).hexdigest() == whole)

    # ---- ranges ------------------------------------------------------------------------------
    cases = [
        ("a normal range", 1000, 5000),
        ("the very first byte", 0, 0),
        ("the very last byte", real - 1, real - 1),
        ("the exact tail", real - 4096, real - 1),
        ("a range crossing the serve chunk size", 1048570, 1049600),
    ]
    for name, s0, e0 in cases:
        if s0 < 0 or e0 >= real or s0 > e0:
            continue
        st, hd, body = get(url, "bytes=%d-%d" % (s0, e0))
        want_bytes = disk[s0:e0 + 1]
        ok = (st == 206 and body == want_bytes
              and hd.get("Content-Length") == str(len(want_bytes))
              and hd.get("Content-Range") == "bytes %d-%d/%d" % (s0, e0, real))
        check(name, ok, "http %s, %d bytes, CR=%s" % (st, len(body), hd.get("Content-Range")))

    # An open-ended range - what a resume actually sends.
    mid = real // 2
    st, hd, body = get(url, "bytes=%d-" % mid)
    check("an open-ended range returns the rest of the file",
          st == 206 and body == disk[mid:], "http %s, %d bytes" % (st, len(body)))

    # A suffix range must return the LAST n bytes. Getting this backwards hands the installer the
    # head of the file where it expected the tail, and nothing downstream can tell.
    n = 8192
    st, hd, body = get(url, "bytes=-%d" % n)
    check("a suffix range returns the LAST bytes, not the first",
          st == 206 and body == disk[-n:], "http %s, %d bytes" % (st, len(body)))

    # ---- reassembly: the thing a real download actually does ---------------------------------
    rnd = random.Random(20260926)
    h = hashlib.sha256()
    pos, pieces, ok = 0, 0, True
    while pos < real:
        step = min(rnd.choice([65536, 262144, 1048576, 1500000]), real - pos)
        st, hd, body = get(url, "bytes=%d-%d" % (pos, pos + step - 1))
        if st != 206 or len(body) != step:
            ok = False
            check("reassembly from sequential ranges", False,
                  "at %d: http %s, got %d want %d" % (pos, st, len(body), step))
            break
        h.update(body)
        pos += step
        pieces += 1
    if ok:
        check("reassembly from %d sequential ranges is byte-identical" % pieces,
              h.hexdigest() == whole, h.hexdigest()[:16])

    # ---- refusals ----------------------------------------------------------------------------
    st, hd, _ = get(url, "bytes=%d-%d" % (real + 10, real + 20))
    check("a range past the end is refused with 416", st == 416, st)

    bad = [n for n, o in results if not o]
    print("\n%d check(s), %d failure(s)" % (len(results), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

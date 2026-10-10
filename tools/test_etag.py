# -*- coding: utf-8 -*-
"""Compile etag_matches() OUT OF BOTH PAYLOADS and run it, so the 304 lane cannot rot quietly.

Both payloads had always sent `ETag: "<size>-<mtime>"` on every static file, and the comment above
send_file() in the PS5 source claimed an unchanged shell then "costs only a 304". It never did:
nothing read If-None-Match back, so the 304 the comment describes could not happen and every reload
re-sent the whole ~810 KB page down one console's accept loop. etag_matches() is the half that was
missing.

It is the kind of function that looks obviously right and is easy to get subtly wrong, and a
mistake is INVISIBLE in both directions - matching too eagerly serves a stale page forever and
matching never just puts the bytes back on the wire. Neither shows up as an error anywhere. So it
is tested, against the two cases that actually caught a bug while it was being written: a tag
sitting in some OTHER header, and a match found past the end of the If-None-Match line.

The function is COPIED into the two payloads, which have never shared a translation unit. This
extracts both copies and refuses if they have drifted apart - the same guard tools/ps4_sync_sqmini.py
exists to provide for the SQLite block, for the same reason.

    python tools/test_etag.py          # build and run
Needs a C compiler on PATH (gcc or clang); skips with a notice if there is none.
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SOURCES = (
    ("PS5", os.path.join(ROOT, "ps5-app", "onconsole", "server.c")),
    ("PS4", os.path.join(ROOT, "ps4-app", "onconsole", "server_ps4.c")),
)

PRELUDE = r"""
#include <stdio.h>
#include <string.h>
#include <strings.h>
static const char *strcasestr_local(const char *hay, const char *needle) {
    size_t n = strlen(needle);
    for (; *hay; hay++) if (!strncasecmp(hay, needle, n)) return hay;
    return NULL;
}
"""

# (raw request, the tag the server holds, expected, what it is checking)
CASES = (
    ('GET / HTTP/1.1\r\nIf-None-Match: "c5a2f-68d1"\r\n\r\n', '"c5a2f-68d1"', 1,
     "the ordinary hit"),
    ('GET / HTTP/1.1\r\nif-none-match: "c5a2f-68d1"\r\n\r\n', '"c5a2f-68d1"', 1,
     "header name lowercased"),
    ('GET / HTTP/1.1\r\nIF-NONE-MATCH: "c5a2f-68d1"\r\n\r\n', '"c5a2f-68d1"', 1,
     "header name uppercased"),
    ('GET / HTTP/1.1\r\nIf-None-Match: W/"c5a2f-68d1"\r\n\r\n', '"c5a2f-68d1"', 1,
     "a cache weakened the tag"),
    ('GET / HTTP/1.1\r\nIf-None-Match: "aaa", "c5a2f-68d1", "bbb"\r\n\r\n', '"c5a2f-68d1"', 1,
     "ours is in the middle of a list"),
    ('GET / HTTP/1.1\r\nIf-None-Match: *\r\n\r\n', '"c5a2f-68d1"', 1,
     "* means whatever you have"),
    ('GET / HTTP/1.1\r\nIf-None-Match: "OTHER-1"\r\n\r\n', '"c5a2f-68d1"', 0,
     "a different file"),
    ('GET / HTTP/1.1\r\nHost: x\r\n\r\n', '"c5a2f-68d1"', 0,
     "no such header"),
    ('GET / HTTP/1.1\r\nIf-None-Match: "c5a2f-68d1"\r\n\r\n', '"c5a2f-68d2"', 0,
     "the file changed - one hex digit"),
    ('GET / HTTP/1.1\r\nHost: x\r\nX-Other: "c5a2f-68d1"\r\n\r\n', '"c5a2f-68d1"', 0,
     "the tag sits in a DIFFERENT header"),
    ('GET / HTTP/1.1\r\nIf-None-Match: "aaa"\r\nX-Other: "c5a2f-68d1"\r\n\r\n', '"c5a2f-68d1"', 0,
     "a match past the end of the line"),
)


def extract(path):
    """The etag_matches body, verbatim, from a payload source."""
    s = io.open(path, encoding="utf-8", errors="surrogateescape").read()
    m = re.search(r"^static int etag_matches\(.*?^\}\n", s, re.S | re.M)
    if not m:
        return None
    return m.group(0)


def cstr(v):
    out = []
    for ch in v:
        if ch == "\r":
            out.append("\\r")
        elif ch == "\n":
            out.append("\\n")
        elif ch in ('"', "\\"):
            out.append("\\" + ch)
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def main():
    cc = shutil.which("gcc") or shutil.which("clang")
    bodies = {}
    for name, path in SOURCES:
        if not os.path.exists(path):
            print("MISSING: %s" % path)
            return 1
        b = extract(path)
        if b is None:
            print("FAIL: %s carries no etag_matches() - the 304 lane is gone from %s"
                  % (name, os.path.basename(path)))
            return 1
        bodies[name] = b

    if bodies["PS5"] != bodies["PS4"]:
        print("FAIL: the two copies of etag_matches() have drifted apart.")
        print("      They are a deliberate copy, not a shared header - keep them identical or")
        print("      one console starts serving a stale page while the other does not.")
        return 1
    print("etag_matches() is identical in both payloads (%d bytes)" % len(bodies["PS5"]))

    if not cc:
        print("SKIP: no C compiler on PATH, so the behaviour was not run (the drift check did).")
        return 0

    rows = []
    for req, tag, want, why in CASES:
        rows.append(" {%s, %s, %d, %s}," % (cstr(req), cstr(tag), want, cstr(why)))
    prog = PRELUDE + bodies["PS5"] + """
struct { const char *req; const char *tag; int want; const char *why; } T[] = {
%s
 {0,0,0,0}
};
int main(void){
  int bad=0;
  for(int i=0;T[i].req;i++){
    int got = etag_matches(T[i].req, T[i].tag);
    if(got!=T[i].want){ printf("FAIL  %%-34s want=%%d got=%%d\\n", T[i].why, T[i].want, got); bad++; }
    else printf("ok    %%-34s -> %%d\\n", T[i].why, got);
  }
  printf("%%d check(s), %%d failure(s)\\n", (int)(sizeof(T)/sizeof(T[0]))-1, bad);
  return bad?1:0;
}
""" % ("\n".join(rows))

    tmp = tempfile.mkdtemp(prefix="pms-etag-")
    try:
        csrc = os.path.join(tmp, "t.c")
        exe = os.path.join(tmp, "t.exe")
        io.open(csrc, "w", encoding="utf-8", newline="\n").write(prog)
        r = subprocess.run([cc, "-O1", "-o", exe, csrc], capture_output=True, text=True)
        if r.returncode != 0:
            print("FAIL: etag_matches() does not compile on its own")
            print(r.stderr[:2000])
            return 1
        r = subprocess.run([exe], capture_output=True, text=True)
        sys.stdout.write(r.stdout)
        if r.returncode != 0:
            sys.stderr.write(r.stderr)
            return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("OK: conditional GET behaves on both payloads.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

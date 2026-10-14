# -*- coding: utf-8 -*-
"""Gate: a PC that has never held the owner's folder can still get everything.

WHAT WAS REPORTED. A friend installed the public build on a PC of his own, with a PS4, and none of
it worked: every homebrew tile said the file was not on this PC, "Download" said "Working..." and
never said anything else, and Updates ended at "could not reach GitHub". It all worked here, on
the one machine that already had every file.

Each of those was a real defect and each has a section below that RUNS the code that fixes it:

    the lanes          GitHub read from its own pages (no hourly allowance), the API second
    the cache          a failure never replaces a good answer; what is known survives a restart
    the transfer       length, sha256 and kind are checked before a download counts; a dropped
                       connection is continued; a kept partial says so
    the job            a download is a row in the queue with a percentage and a Cancel, not a
                       request the page waits on
    the console        the named files a homebrew reads are delivered with it, and never over the
                       owner's own; a placed file goes to exactly the path its entry names
    the page           a pressed control is let go however the work ends

NOTHING HERE TOUCHES THE INTERNET OR A CONSOLE. GitHub is a server on 127.0.0.1 (PMS_GITHUB_WEB /
PMS_GITHUB_API), the console is an object with three methods, and the companion is never started -
its handler and its queue are driven in-process. A gate that needed the owner's network would be a
coin toss, and this repo has had one of those.

    python tools/test_fresh_pc.py
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "companion"))
os.environ.setdefault("PMS_TEST_MODE", "1")

fails = []
n = 0


def ok(cond, what, detail=""):
    global n
    n += 1
    if not cond:
        fails.append("%s%s" % (what, (" - " + str(detail)) if detail else ""))


# --------------------------------------------------------------------------- #
# a GitHub that lives on 127.0.0.1                                              #
# --------------------------------------------------------------------------- #
def elf(mark, size=6000):
    body = b"\x7fELF\x02\x01\x01" + b"\x00" * 9 + mark.encode() + b"\x00"
    return body + bytes(bytearray((i * 7) & 0xFF for i in range(max(0, size - len(body)))))


def pkg(mark, size=9000):
    body = b"\x7fCNT" + b"\x00" * 60 + mark.encode() + b"\x00"
    return body + bytes(bytearray((i * 13) & 0xFF for i in range(max(0, size - len(body)))))


def zip_of(members):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_STORED) as z:
        for k in sorted(members):
            z.writestr(k, members[k])
    return b.getvalue()


def rel(tag, when, pre, assets):
    return {"tag": tag, "when": when, "pre": pre, "assets": assets}


REPOS = {
    "drakmor/nanoDNS": [
        rel("0.4", "2026-08-04T18:27:26Z", False,
            {"nanodns-ps4.elf": elf("nanodns for the PS4"), "nanodns.elf": elf("nanodns")}),
        rel("0.3", "2026-06-03T09:37:48Z", False, {"nanodns.elf": elf("nanodns old")}),
    ],
    "gasb07/ps4mount": [
        rel("v0.4.1", "2026-10-13T01:00:00Z", False,
            {"ps4mount.elf": elf("PS4Mount v0.4.1"),
             "ps4mount.elf.sha256": b"c4c2  ps4mount.elf\n"}),
    ],
    "itsPLK/ps5-y2jb-autoloader": [
        rel("v1.0.0-fb17d16", "2026-10-09T10:00:00Z", False,
            {"y2jb_update.zip": zip_of({"update-info.txt": b"y2jb 1.0.0\n", "main.js": b"//\n"}),
             "download0.dat": b"\x00DAT" + b"\x55" * 4000}),
    ],
    "iHaiDeeZ/Crema": [],
    # THE LIST IS NOT IN ORDER. Measured on a real project: index 0 was published two hours
    # BEFORE index 1.
    "test/order": [
        rel("1.7beta5", "2026-10-10T16:12:00Z", True, {"a.elf": elf("a")}),
        rel("1.7-beta5fix1", "2026-10-10T18:36:00Z", False, {"a.elf": elf("a")}),
    ],
    "test/xplorer": [
        rel("2.07", "2026-05-01T00:00:00Z", False, {"PS4-Xplorer_2.07.pkg": pkg("LAPY20009")}),
    ],
}

UP = {"web": "ok", "api": "ok", "hits": {}, "cut_once": {}, "cut_always": {}, "slow": 0.0,
      "ranges": [], "lie": {}}


def _hit(what):
    UP["hits"][what] = UP["hits"].get(what, 0) + 1


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):                                                       # noqa: C901
        path = self.path.split("?", 1)[0]
        parts = [p for p in path.split("/") if p]
        # ---- the API ---------------------------------------------------------------------------
        if parts[:1] == ["repos"] and len(parts) >= 4 and parts[3] == "releases":
            repo = "/".join(parts[1:3])
            _hit("api:" + repo)
            if UP["api"] == "403":
                return self._send(403, b'{"message":"API rate limit exceeded"}', "application/json",
                                  {"X-RateLimit-Remaining": "0",
                                   "X-RateLimit-Reset": str(int(time.time()) + 1800)})
            if UP["api"] == "500":
                return self._send(500, b"{}", "application/json")
            if repo not in REPOS:
                return self._send(404, b'{"message":"Not Found"}', "application/json")
            base = "http://127.0.0.1:%d" % self.server.server_address[1]
            doc = []
            for r in REPOS[repo]:
                doc.append({
                    "tag_name": r["tag"], "prerelease": r["pre"], "draft": False,
                    "published_at": r["when"], "created_at": r["when"],
                    "html_url": "%s/%s/releases/tag/%s" % (base, repo, r["tag"]),
                    "assets": [{"name": k, "size": len(v),
                                "browser_download_url": "%s/%s/releases/download/%s/%s"
                                                        % (base, repo, r["tag"], k),
                                "url": "%s/repos/%s/releases/assets/1" % (base, repo),
                                "digest": "sha256:" + sha(v)}
                               for k, v in sorted(r["assets"].items())]})
            return self._send(200, json.dumps(doc).encode(), "application/json")
        if len(parts) < 3 or parts[2] != "releases":
            return self._send(404, b"nothing here")
        repo = "/".join(parts[:2])
        # ---- a file ----------------------------------------------------------------------------
        if len(parts) == 6 and parts[3] == "download":
            name = parts[5]
            _hit("file:" + name)
            rels = [r for r in REPOS.get(repo, []) if r["tag"] == parts[4]]
            data = (rels[0]["assets"].get(name) if rels else None)
            if data is None:
                return self._send(404, b"Not Found")
            if name in UP["lie"]:
                data = UP["lie"][name]
            start = 0
            rng = self.headers.get("Range") or ""
            UP["ranges"].append((name, rng))
            if rng.startswith("bytes="):
                try:
                    start = int(rng[6:].split("-")[0])
                except ValueError:
                    start = 0
            if start >= len(data) and start:
                return self._send(416, b"", extra={"Content-Range": "bytes */%d" % len(data)})
            body = data[start:]
            self.send_response(206 if start else 200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            if start:
                self.send_header("Content-Range",
                                 "bytes %d-%d/%d" % (start, len(data) - 1, len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            stop = len(body)
            if name in UP["cut_always"]:
                stop = min(stop, UP["cut_always"][name])
            elif UP["cut_once"].get(name):
                stop = min(stop, UP["cut_once"].pop(name))
            sent = 0
            try:
                while sent < stop:
                    chunk = body[sent:min(stop, sent + 1024)]
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    sent += len(chunk)
                    if UP["slow"]:
                        time.sleep(UP["slow"])
            except Exception:
                pass
            return
        # ---- github.com's own pages ------------------------------------------------------------
        if UP.get("page_delay"):
            time.sleep(UP["page_delay"])
        if UP["web"] == "500":
            _hit("web:" + repo)
            return self._send(500, b"oops")
        if len(parts) == 3:
            _hit("web:" + repo)
            if repo not in REPOS:
                return self._send(404, b"Not Found")
            if UP["web"] == "shape":
                return self._send(200, b"<html><body>a page with a new layout</body></html>")
            if not REPOS[repo]:
                return self._send(200, "<div>There aren\u2019t any releases here</div>".encode())
            out = []
            for i, r in enumerate(REPOS[repo]):
                out.append(
                    '<section id="release-%d" aria-labelledby="h"><h2 class="sr-only">%s</h2>'
                    '<a href="/%s/releases/tag/%s" class="Link--primary">%s</a>%s'
                    '<relative-time datetime="%s" class="no-wrap">then</relative-time>'
                    # ...and the author's own notes may say anything at all:
                    '<div class="markdown-body">this is not a '
                    '<span class="Label Label--warning">Pre-release</span>, honest</div>'
                    '</section>' % (
                        i, r["tag"], repo, r["tag"], r["tag"],
                        ('<span class="Label Label--warning Label--large">Pre-release</span>'
                         if r["pre"] else '<span class="Label Label--success">Latest</span>'),
                        r["when"]))
            return self._send(200, ("<turbo-frame>" + "".join(out) + "</turbo-frame>").encode())
        if len(parts) == 5 and parts[3] == "expanded_assets":
            _hit("assets:" + repo)
            rels = [r for r in REPOS.get(repo, []) if r["tag"] == parts[4]]
            if not rels:
                return self._send(404, b"Not Found")
            r = rels[0]
            rows = []
            for k, v in sorted(r["assets"].items()):
                rows.append(
                    '<li class="Box-row d-flex"><a href="/%s/releases/download/%s/%s" '
                    'rel="nofollow" class="Truncate"><span class="text-bold">%s</span></a>'
                    '<clipboard-copy value="sha256:%s"></clipboard-copy>'
                    '<span>1.66 MB</span></li>' % (repo, r["tag"], k, k, sha(v)))
            for ext in ("zip", "tar.gz"):
                rows.append('<li class="Box-row"><a href="/%s/archive/refs/tags/%s.%s" '
                            'rel="nofollow">Source code (%s)</a></li>' % (repo, r["tag"], ext, ext))
            return self._send(200, ("<ul>" + "".join(rows) + "</ul>").encode())
        return self._send(404, b"nothing here")


class FakePldmgr(BaseHTTPRequestHandler):
    """Payload Manager as each version answers. server.mode is "0.5" or "0.6"; every request is
    recorded as (method, path, has X-PLDMGR-Request)."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/plain"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b)
        self.close_connection = True

    def _handle(self, method):
        srv = self.server
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        srv.seen.append((method, self.path, self.headers.get("X-PLDMGR-Request") == "1"))
        path = self.path
        changes = path.startswith("/loadpayload:") or path.startswith("/process_kill")
        if path == "/version":
            return self._send(200, srv.mode + ".0" if srv.mode == "0.6" else "0.5.2")
        if path == "/list_payloads":
            return self._send(200, json.dumps(srv.listing()), "application/json")
        if changes:
            if srv.mode == "0.6":
                if method != "POST":
                    return self._send(405, "Method Not Allowed")
                if self.headers.get("X-PLDMGR-Request") != "1":
                    return self._send(403, "Forbidden")
                return self._send(200, "OK")
            if method != "GET":
                return self._send(405, "Method Not Allowed")      # 0.5.x has no POST route
            return self._send(200, "OK")
        return self._send(404, "Not Found")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def start_pldmgr(mode):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakePldmgr)
    srv.daemon_threads = True
    srv.mode = mode
    srv.seen = []
    if mode == "0.6":
        slots = [("shadowmountplus", "shadowmountplus.elf"), ("pkg-mutant-shop", "PKG-MUTANT-SHOP.elf"),
                 ("ftpsrv", "ftpsrv-ps5.elf"), ("pldmgr", "pldmgr_v0.6.0.elf")]
        items = [{"slot": "local/" + a, "path": "/data/pldmgr/payloads/local/%s/%s" % (a, b),
                  "kind": "local", "filename": b} for a, b in slots]
        srv.listing = lambda: {"self": {"id": "pldmgr", "running_version": "0.6.0"},
                               "items": items, "usb": [], "payloads": [i["path"] for i in items]}
    else:
        srv.listing = lambda: {"payloads": ["/data/pldmgr/payloads/ftpsrv-ps5/ftpsrv-ps5.elf",
                                            "/data/pldmgr/payloads/PKG-MUTANT-SHOP/PKG-MUTANT-SHOP.elf"]}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def start_upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --------------------------------------------------------------------------- #
# a console with three methods                                                  #
# --------------------------------------------------------------------------- #
class FakeBridge(object):
    """What payloads.py asks of a console: list a folder, read a file, write a file."""

    def __init__(self, files=None, dirs=None, alive=True):
        self.files = dict(files or {})
        self.dirs = set(dirs or ["/data"])
        self.alive = alive
        self.writes = []

    def _all_dirs(self):
        out = set(self.dirs)
        for p in self.files:
            parts = p.split("/")
            for i in range(2, len(parts)):
                out.add("/".join(parts[:i]))
        return out

    def fs_list(self, path, timeout=15):
        if not self.alive:
            return None
        path = path.rstrip("/") or "/"
        dirs = self._all_dirs()
        if path != "/" and path not in dirs:
            return None
        rows, pre = [], (path.rstrip("/") + "/")
        seen = set()
        for d in sorted(dirs):
            if d.startswith(pre) and "/" not in d[len(pre):] and d[len(pre):]:
                seen.add(d[len(pre):])
                rows.append({"name": d[len(pre):], "dir": True, "size": 0})
        for f in sorted(self.files):
            if f.startswith(pre) and "/" not in f[len(pre):]:
                rows.append({"name": f[len(pre):], "dir": False, "size": len(self.files[f])})
        return rows

    def fs_read(self, path, timeout=20):
        if not self.alive:
            raise IOError("console did not answer")
        return self.files[path]

    def fs_write(self, dest, body, size=0, timeout=60):
        if not self.alive:
            return False
        data = body.read() if hasattr(body, "read") else body
        self.files[dest] = bytes(data)
        self.writes.append(dest)
        return True


def age(P, key, secs=30):
    """Make a cached answer look `secs` old, so the next ask is a real ask."""
    with P._rel_lock:
        c = P._rel_cache.get(key)
        if c:
            for k in ("at", "tried_at"):
                if c.get(k):
                    c[k] = float(c[k]) - secs


def jsfn(src, name):
    i = src.index("function %s(" % name)
    d, started = 0, False
    for j in range(i, len(src)):
        if src[j] == "{":
            d += 1
            started = True
        elif src[j] == "}":
            d -= 1
            if started and d == 0:
                return src[i:j + 1]
    return ""


def main():                                                                 # noqa: C901
    up = start_upstream()
    base = "http://127.0.0.1:%d" % up.server_address[1]
    os.environ["PMS_GITHUB_WEB"] = base
    os.environ["PMS_GITHUB_API"] = base
    import payloads as P
    import netlane
    work = tempfile.mkdtemp(prefix="pms-fresh-")
    try:
        run(P, netlane, base, work)
    except Exception:
        # WHAT FAILED BEFORE THE CRASH IS THE USEFUL PART. A broken rule usually breaks the steps
        # after it too, and a bare traceback from the fortieth line down hides the first cause.
        import traceback
        fails.append("the suite stopped part-way:\n      "
                     + traceback.format_exc().strip().replace("\n", "\n      "))
    finally:
        up.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    if fails:
        print("test_fresh_pc: FAIL (%d of %d)" % (len(fails), n))
        for f in fails:
            print("  - " + f)
        return 1
    print("test_fresh_pc: OK (%d checks)" % n)
    return 0


def run(P, netlane, base, work):                                            # noqa: C901
    # THE SUITE WRITES NOTHING INTO THE REPOSITORY. remember_version() records a version proven by
    # a release in assets/payloads/known-versions.json, which ships inside the next build - and the
    # fake downloads below are proven by fake releases. The first run of this suite after placed
    # files began recording theirs put nine invented checksums into that file, and they were
    # committed. Every record goes to a copy in the work folder, and the end of the run checks the
    # real file did not move.
    kv_real = os.path.join(ROOT, "assets", "payloads", "known-versions.json")
    kv_before = hashlib.sha256(io.open(kv_real, "rb").read()).hexdigest()
    kv_root = os.path.join(work, "kvroot")
    os.makedirs(os.path.join(kv_root, "assets", "payloads"))
    shutil.copyfile(kv_real, os.path.join(kv_root, "assets", "payloads", "known-versions.json"))
    real_rv = P.remember_version
    P.remember_version = (lambda item, tag, repo, asset_name, root=None:
                          real_rv(item, tag, repo, asset_name, root=kv_root))
    try:
        run_all(P, netlane, base, work)
    finally:
        P.remember_version = real_rv
    ok(hashlib.sha256(io.open(kv_real, "rb").read()).hexdigest() == kv_before,
       "the suite wrote nothing into the repository's own version record")
    kv_copy = json.load(io.open(os.path.join(kv_root, "assets", "payloads", "known-versions.json"),
                                encoding="utf-8"))
    ok(any(v.get("from") == "itsPLK/ps5-y2jb-autoloader" and v.get("version") == "v1.0.0-fb17d16"
           for v in (kv_copy.get("versions") or {}).values()),
       "...and a placed file's release WAS recorded - in the copy")


def run_all(P, netlane, base, work):                                        # noqa: C901
    # =========================================================================================
    # 1. THE PAGE READER, AGAINST A PAGE GITHUB REALLY SERVED
    # =========================================================================================
    fx = os.path.join(ROOT, "tools", "fixtures", "web")
    page = io.open(os.path.join(fx, "nanoDNS-releases.html"), encoding="utf-8").read()
    frag = io.open(os.path.join(fx, "nanoDNS-expanded_assets-0.4.html"), encoding="utf-8").read()
    rels = P.web_release_list(page)
    ok([r["tag"] for r in rels] == ["0.4", "0.3", "0.2", "0.1"],
       "every release on a real GitHub page is read, in page order", [r["tag"] for r in rels])
    ok(all(r["published_at"].endswith("Z") and not r["prerelease"] for r in rels),
       "...each with when it was published, and none of these mistaken for a pre-release")
    assets = P.web_release_assets(frag, "https://github.com")
    known = json.load(io.open(os.path.join(ROOT, "tools", "fixtures", "releases.json"),
                              encoding="utf-8"))["drakmor/nanoDNS"]["assets"]
    ok(sorted(a["name"] for a in assets) == sorted(a["name"] for a in known),
       "the files of a real release are read off its page - and the two source archives are not",
       [a["name"] for a in assets])
    ok(all(a["digest"] == "sha256:" + next(k["digest"] for k in known if k["name"] == a["name"])
           for a in assets),
       "...with the same sha256 the API states for each, which is what proves a download")
    ok(all(a["url"].startswith("https://github.com/drakmor/nanoDNS/releases/download/0.4/")
           for a in assets), "...and a link to the file itself")

    # =========================================================================================
    # 2. TWO LANES TO ONE ANSWER
    # =========================================================================================
    P._rel_cache.clear()
    r = P.github_latest("drakmor/nanoDNS", force=True)
    ok(r.get("tag") == "0.4" and r.get("lane") == "web" and not r.get("error"),
       "a project's newest release is read from its own page", repr(r)[:200])
    ok(not any(k.startswith("api:") for k in UP["hits"]),
       "...without spending one request of the API's hourly allowance", UP["hits"])
    ok(sorted(a["name"] for a in r.get("assets") or []) == ["nanodns-ps4.elf", "nanodns.elf"]
       and all(len(a.get("digest") or "") == 71 for a in r["assets"]),
       "...with its files and their sha256")
    before = dict(UP["hits"])
    P.github_latest("drakmor/nanoDNS")
    ok(UP["hits"] == before, "asking again a moment later asks nobody")

    r = P.github_latest("test/order", force=True)
    ok(r.get("tag") == "1.7-beta5fix1",
       "the newest release is the one PUBLISHED last, not the one listed first", r.get("tag"))
    ok(r.get("prerelease") is False,
       "a release is a pre-release when GitHub labels it one, not when its notes use the word",
       r.get("prerelease"))

    # The page changes shape -> the API answers instead.
    UP["web"] = "shape"
    age(P, "drakmor/nanoDNS")
    P._rel_cache.pop("drakmor/nanoDNS", None)
    r = P.github_latest("drakmor/nanoDNS", force=True)
    ok(r.get("tag") == "0.4" and r.get("lane") == "api",
       "when GitHub's page cannot be read the API is asked instead", repr(r)[:160])
    ok(any(a.get("size") for a in r.get("assets") or []),
       "...and that lane states each file's exact length too")
    UP["web"] = "ok"

    # Nothing published is an ANSWER, kept for an hour.
    r = P.github_latest("iHaiDeeZ/Crema", force=True)
    ok(r.get("error_code") == "no-releases" and r.get("error") == "no releases",
       "a project that has published nothing says so, and is not 'could not reach GitHub'",
       repr(r)[:160])
    before = dict(UP["hits"])
    age(P, "iHaiDeeZ/Crema", 600)                  # ten minutes later
    P.github_latest("iHaiDeeZ/Crema")
    ok(UP["hits"] == before, "...and is not asked about again ten minutes later")
    age(P, "iHaiDeeZ/Crema", 3600)
    P.github_latest("iHaiDeeZ/Crema")
    ok(UP["hits"] != before, "...but is an hour later, so a first release is noticed")
    it_crema = {"id": "crema", "title": "Crema", "repo": "iHaiDeeZ/Crema"}
    said, code, _st = P.upstream_refusal(it_crema, r)
    ok(code == "no-releases" and "published nothing" in said and "GitHub" not in said,
       "the sentence for it names the project, not a network", said)

    r = P.github_latest("nobody/nothing", force=True)
    ok(r.get("error_code") == "not-visible",
       "a project this address cannot see is 'not visible' - never 'no releases', which is a "
       "claim about a repository that may only be private", repr(r)[:160])

    # A token turns the order round, and a rate limit on the API is survived by the page.
    UP["api"] = "403"
    P._rel_cache.clear()
    r = P.github_latest("gasb07/ps4mount", force=True, token="t0ken")
    ok(r.get("tag") == "v0.4.1" and r.get("lane") == "web",
       "a rate-limited API is not the end: the page still answers", repr(r)[:160])
    UP["api"] = "ok"

    # =========================================================================================
    # 3. WHAT IS KNOWN SURVIVES A BAD MOMENT, AND A RESTART
    # =========================================================================================
    cdir = os.path.join(work, "cfg")
    os.makedirs(cdir)
    P._rel_cache.clear()
    P.set_cache_dir(cdir)
    good = P.github_latest("drakmor/nanoDNS", force=True)
    ok(good.get("tag") == "0.4" and os.path.isfile(os.path.join(cdir, P.RELEASE_CACHE_NAME)),
       "a good answer is written beside the config")
    UP["web"], UP["api"] = "500", "500"
    age(P, "drakmor/nanoDNS")
    bad = P.github_latest("drakmor/nanoDNS", force=True)
    ok(bad.get("tag") == "0.4" and bad.get("stale") is True and not bad.get("error"),
       "a failed refresh hands back the last good answer instead of replacing it with an error",
       repr(bad)[:200])
    ok(bad.get("last_error_code") == "http-500" and bad.get("last_error_host"),
       "...and still says what went wrong and with whom", repr(bad)[:200])
    fresh = P.github_latest("never/asked", force=True)
    ok(fresh.get("error_code") == "http-500" and fresh.get("host") == "127.0.0.1",
       "a project never answered for reports the transport's own reason, not a class name",
       repr(fresh)[:200])
    said, code, _st = P.upstream_refusal({"title": "X", "repo": "never/asked"}, fresh)
    ok(code == "http-500" and "URLError" not in said and "127.0.0.1" in said,
       "...as a sentence that names the host", said)
    # a restart
    P._rel_cache.clear()
    P.set_cache_dir(cdir)
    before = dict(UP["hits"])
    again = P.github_latest("drakmor/nanoDNS")
    ok(again.get("tag") == "0.4" and UP["hits"] == before,
       "after a restart the PC still knows what it knew, with GitHub unreachable",
       repr(again)[:120])
    saved = json.load(io.open(os.path.join(cdir, P.RELEASE_CACHE_NAME), encoding="utf-8"))
    ok(not any(k.endswith("|auth") for k in saved.get("releases") or {}),
       "an answer obtained with the owner's token is never written to disk")
    UP["web"], UP["api"] = "ok", "ok"

    # The whole catalogue at once, inside one budget.
    UP["slow_page"] = 0
    items = [{"id": "a", "repo": "drakmor/nanoDNS", "asset": "nanodns", "platform": "PS5",
              "kind": "payload"},
             {"id": "b", "repo": "gasb07/ps4mount", "asset": "ps4mount", "ext": ".elf",
              "platform": "PS5", "kind": "payload"},
             {"id": "c", "repo": "iHaiDeeZ/Crema", "asset": "Crema", "ext": ".pkg",
              "platform": "PS4", "kind": "homebrew"}]
    P._rel_cache.clear()
    t0 = time.time()
    out = P.sweep_upstreams(items, deadline=20.0)
    ok(len(out) == 3 and out[0].get("tag") == "0.4" and out[1].get("tag") == "v0.4.1"
       and out[2].get("error_code") == "no-releases" and time.time() - t0 < 15,
       "a sweep asks about every project side by side and answers in the items' order",
       [(_o.get("tag"), _o.get("error_code")) for _o in out])

    # ...AND WHEN THE BUDGET RUNS OUT, WHAT IS ALREADY KNOWN IS WHAT IS SHOWN. A host that has
    # stopped answering takes its whole timeout to say so; the row for a project whose release was
    # read an hour ago should show that release, not an error about it.
    age(P, "drakmor/nanoDNS", 7 * 3600)                # known, and due to be asked again
    P._rel_cache.pop("gasb07/ps4mount", None)          # never answered for
    UP["page_delay"] = 2.5
    t0 = time.time()
    out = P.sweep_upstreams(items[:2], deadline=1.0)
    took = time.time() - t0
    UP["page_delay"] = 0
    ok(took < 2.2, "a sweep gives up waiting at its deadline", "%.1fs" % took)
    ok(out[0].get("tag") == "0.4" and out[0].get("stale") is True and not out[0].get("error"),
       "...and a project it already knows about shows what is known, not an error",
       repr(out[0])[:160])
    ok(out[1].get("error_code") == "timeout" and out[1].get("pending") is True,
       "...while one it has never had an answer for says it did not answer in time",
       repr(out[1])[:160])
    time.sleep(3.0)                                    # let the two late askers finish

    # =========================================================================================
    # 4. A DOWNLOAD COUNTS ONLY WHEN IT IS THE FILE
    # =========================================================================================
    dl = os.path.join(work, "dl")
    os.makedirs(dl)
    data = REPOS["drakmor/nanoDNS"][0]["assets"]["nanodns-ps4.elf"]
    url = base + "/drakmor/nanoDNS/releases/download/0.4/nanodns-ps4.elf"
    part = os.path.join(dl, "a.elf.part")
    seen = []
    good_ok, got, info = P.fetch_file(url, part, digest="sha256:" + sha(data),
                                      name="nanodns-ps4.elf",
                                      progress=lambda d, t: seen.append((d, t)))
    ok(good_ok and got == len(data) and io.open(part, "rb").read() == data and info["code"] == "",
       "a file that arrives whole, with the sha256 its release states, is accepted", info)
    ok(bool(seen) and seen[-1][0] == len(data) and seen[-1][1] == len(data),
       "...and its progress was reported as it came", seen[-3:])

    bad_ok, _g, info = P.fetch_file(url, part, digest="sha256:" + "0" * 64, name="nanodns-ps4.elf")
    ok(not bad_ok and info["code"] == "digest-mismatch" and not os.path.exists(part),
       "a file that is not the one the release publishes is refused and not kept", info)

    bad_ok, _g, info = P.fetch_file(url, part, expect_size=len(data) + 1, name="nanodns-ps4.elf")
    ok(not bad_ok and info["code"] == "size-mismatch" and not os.path.exists(part),
       "a file of the wrong length is refused", info)

    # an error page saved under a package's name
    UP["lie"]["PS4-Xplorer_2.07.pkg"] = b"<html><body>Too many requests</body></html>" * 40
    purl = base + "/test/xplorer/releases/download/2.07/PS4-Xplorer_2.07.pkg"
    bad_ok, _g, info = P.fetch_file(purl, part, name="PS4-Xplorer_2.07.pkg")
    ok(not bad_ok and info["code"] == "wrong-kind" and not os.path.exists(part),
       "a web page that arrived where a package was expected is not a package", info)
    UP["lie"].clear()

    bad_ok, _g, info = P.fetch_file(base + "/drakmor/nanoDNS/releases/download/0.4/gone.elf",
                                    part, name="gone.elf")
    ok(not bad_ok and info["code"] == "http-404" and info["host"] == "127.0.0.1",
       "a file that is no longer there says so, with the host", info)

    # THE TRANSFER STOPS HALF WAY. A read that ends early returns b"" - it does not raise - so
    # this used to be saved as the whole file.
    UP["cut_always"]["nanodns-ps4.elf"] = 2000
    bad_ok, got, info = P.fetch_file(url, part, name="nanodns-ps4.elf")
    ok(not bad_ok and info["code"] == "stopped" and info["keep"] is True and os.path.exists(part),
       "a transfer that stops half way is not a file - and what came down is kept, and said so",
       info)
    ok("press again" in info["sentence"], "...in words that say what to do", info["sentence"])
    UP["cut_always"].clear()

    stop = {"now": False}

    def prog(d, t):
        stop["now"] = True

    # (A file big enough to arrive in more than one piece: Cancel is looked at between pieces.)
    REPOS["test/order"][0]["assets"]["big.elf"] = elf("big", 900000)
    bad_ok, _g, info = P.fetch_file(base + "/test/order/releases/download/1.7beta5/big.elf", part,
                                    name="big.elf", progress=prog, cancel=lambda: stop["now"])
    ok(not bad_ok and info["code"] == "cancelled" and info["keep"] is True,
       "Cancel stops a transfer and is not reported as a failure of the network", info)

    # ---- the transport's own resume, on the lane the internet uses --------------------------
    dest = os.path.join(dl, "resume.part")
    for junk in (dest, dest + ".key"):
        if os.path.exists(junk):
            os.remove(junk)
    del UP["ranges"][:]
    UP["cut_once"]["nanodns-ps4.elf"] = 2500
    res = netlane.download(url, dest, timeout=10, lanes=("py",), https_only=False,
                           use_proxy=False, resume_key="k1")
    asked = [rg for nm, rg in UP["ranges"] if nm == "nanodns-ps4.elf"]
    ok(io.open(dest, "rb").read() == data and res.get("bytes") == len(data),
       "a connection that drops mid-file is picked up where it stopped, inside one press",
       (res, asked))
    ok(any(rg.startswith("bytes=") and rg != "bytes=0-" for rg in asked),
       "...by asking for the rest, not for the whole file again", asked)

    # across presses: only under the same key
    io.open(dest, "wb").write(data[:3000])
    io.open(dest + ".key", "w").write("k1")
    del UP["ranges"][:]
    res = netlane.download(url, dest, timeout=10, lanes=("py",), https_only=False,
                           use_proxy=False, resume_key="k1")
    ok(io.open(dest, "rb").read() == data and UP["ranges"][0][1] == "bytes=3000-",
       "a second press continues the first one's file", UP["ranges"][:2])
    io.open(dest, "wb").write(b"Y" * 3000)                     # yesterday's release, same name
    io.open(dest + ".key", "w").write("yesterday")
    del UP["ranges"][:]
    res = netlane.download(url, dest, timeout=10, lanes=("py",), https_only=False,
                           use_proxy=False, resume_key="k1")
    ok(io.open(dest, "rb").read() == data and not UP["ranges"][0][1],
       "...but half of a DIFFERENT release is never continued - it starts again",
       UP["ranges"][:2])

    # =========================================================================================
    # 5. WHICH FILE OF A RELEASE IS THIS CONSOLE'S
    # =========================================================================================
    ok(P._names_console("nanodns-ps4.elf", "ps4") and not P._names_console("ps4mount.elf", "ps4")
       and P._names_console("ftpsrv-ps5.elf", "ps5") and not P._names_console("ps5sx2.zip", "ps5"),
       "'ps4' names a console when it stands alone - PS4Mount is a PS5 payload with ps4 in its "
       "name")
    known_all = json.load(io.open(os.path.join(ROOT, "tools", "fixtures", "releases.json"),
                                  encoding="utf-8"))
    picks = [
        ("gasb07/ps4mount", {"asset": "ps4mount", "ext": ".elf", "platform": "PS5",
                             "kind": "payload"}, "ps4mount.elf"),
        ("itsPLK/ps5-y2jb-autoloader", {"asset": "y2jb_update", "ext": ".zip", "platform": "PS5",
                                        "kind": "payload"}, "y2jb_update.zip"),
        ("itsPLK/ps5-y2jb-autoloader", {"asset": "download0", "ext": ".dat", "platform": "PS5",
                                        "kind": "payload"}, "download0.dat"),
        ("RafaelNGP/xbox-cloud-gaming-ps5", {"asset": "PPSA99810", "ext": ".zip",
                                             "platform": "PS5", "kind": "homebrew"},
         "PPSA99810.zip"),
        ("tsuramatsu1/Vita3K-PS5", {"asset": "PPSA99300", "ext": ".zip", "platform": "PS5",
                                    "kind": "homebrew"}, "PPSA99300.zip"),
        ("tsuramatsu1/psp5", {"asset": "PPSA99131", "ext": ".zip", "platform": "PS5",
                              "kind": "homebrew"}, "PPSA99131.zip"),
        ("ItsJokerZz/FPKGi", {"asset": "pkgi", "ext": ".pkg", "platform": "PS4",
                              "kind": "homebrew"}, "FPKGi_v1.10.0-release.pkg"),
        ("ItsJokerZz/FPKGi", {"asset": "pkgi", "ext": ".pkg", "platform": "PS5",
                              "kind": "homebrew"}, "FPKGi_v1.10.0-release.pkg"),
    ]
    for repo, item, want in picks:
        a = P.pick_asset(known_all[repo], item)
        ok((a or {}).get("name") == want,
           "%s: the %s file of the real release is %s" % (repo, item["platform"], want),
           (a or {}).get("name"))
    ok(P.version_tuple("v1.0.0-fb17d16") == P.version_tuple("1.0.0")
       and not P.newer_than("v1.0.0-a1b2c3d", "v1.0.0-fb17d16"),
       "a commit hash on the end of a tag is not part of the version",
       P.version_tuple("v1.0.0-fb17d16"))
    ok(P.newer_than("v1.0.1-a1b2c3d", "v1.0.0-fb17d16"),
       "...and the version in front of it still compares")
    ok(P.version_tuple("1.2-20261012") != P.version_tuple("1.2"),
       "...while a date after a dash is not a hash and is left alone",
       P.version_tuple("1.2-20261012"))
    ok(P.version_tuple("vk-285-163") and P.newer_than("vk-285-163", "vk-285-130"),
       "...nor is a build number that happens to follow a dash", P.version_tuple("vk-285-163"))

    # =========================================================================================
    # 6. THE PRESS ITSELF: a download is a job, run by the companion's own handler
    # =========================================================================================
    import server as S
    root = os.path.join(work, "Mutant")
    os.makedirs(root)
    cfg = json.loads(json.dumps(S.DEFAULT_CONFIG))
    cfg.setdefault("payloads", {})["root"] = root
    cfg["consoles"] = []
    fleet = types.SimpleNamespace(consoles=[], bridge=lambda cid: None)
    srv = types.SimpleNamespace(cfg=cfg, fleet=fleet, transfers={}, peers=None, hashes={},
                                library=types.SimpleNamespace(file_registry={}, file_sizes={}))
    srv.queue = S.Queue(fleet, srv.transfers, cfg)
    srv.queue.server = srv
    S.defer_relaunch = lambda *a, **k: None
    h = S._DetachedHandler(srv)
    P._rel_cache.clear()

    def wait_job(job, secs=30):
        end = time.time() + secs
        st = {}
        while time.time() < end:
            st = S.download_status(srv, job)
            if st.get("state") in ("done", "error", "idle"):
                return st
            time.sleep(0.1)
        return st

    cat, _sig = P.live_catalog(cfg, S.WEB_DIR)
    nano = [i for i in cat["items"] if i.get("id") == "nanodns" and i.get("platform") == "PS4"]
    ok(len(nano) == 1 and nano[0].get("have") is False,
       "on a PC with an empty folder the catalogue says this PC does not hold the file",
       [(i.get("have"), i.get("path")) for i in nano])

    res = h._payloads_get({"id": "nanodns", "platform": "PS4", "async": 1})
    obj = res[0] if isinstance(res, tuple) else res
    ok(isinstance(obj, dict) and obj.get("ok") and obj.get("job_id") and obj.get("lane") == "fetch",
       "pressing Download answers at once with a job to watch", repr(obj)[:200])
    job = (obj or {}).get("job_id")
    st = wait_job(job)
    want_path = os.path.join(root, "Payloads", "PS4", "nanodns", "nanodns-ps4.elf")
    ok(st.get("state") == "done" and os.path.isfile(want_path)
       and io.open(want_path, "rb").read() == data,
       "...and the job ends with the file in the folder, where the catalogue says it belongs",
       repr(st)[:240])
    ok(st.get("job_id") == job and isinstance(st.get("result"), dict) and st["result"].get("ok"),
       "...reporting the lane's own answer, in the shape a console's download uses")
    ok(not [f for f in os.listdir(os.path.dirname(want_path)) if f.endswith(".part")],
       "...with no working file left behind")
    cat, _sig = P.live_catalog(cfg, S.WEB_DIR)
    nano = [i for i in cat["items"] if i.get("id") == "nanodns" and i.get("platform") == "PS4"]
    ok(nano and nano[0].get("have") is True, "the catalogue notices the file without being told")

    res = h._payloads_get({"id": "nanodns", "platform": "PS4", "async": 1})
    obj, code = res if isinstance(res, tuple) else (res, 200)
    ok(code == 409 and obj.get("already") and not obj.get("job_id"),
       "a second press on something already here asks first instead of downloading 200 MB again",
       (code, repr(obj)[:120]))

    # TWO PRESSES ARE ONE DOWNLOAD.
    UP["slow"] = 0.02
    a1 = h._payloads_get({"id": "ps4mount", "platform": "PS5", "async": 1})
    a2 = h._payloads_get({"id": "ps4mount", "platform": "PS5", "async": 1})
    o1 = a1[0] if isinstance(a1, tuple) else a1
    o2 = a2[0] if isinstance(a2, tuple) else a2
    ok(o1.get("job_id") and o1.get("job_id") == o2.get("job_id") and o2.get("already_running"),
       "two presses on one item are one download, not two writers on one file",
       (o1.get("job_id"), o2.get("job_id")))
    st = wait_job(o1.get("job_id"))
    UP["slow"] = 0.0
    pm = os.path.join(root, "Payloads", "PS5", "PS4Mount", "ps4mount.elf")
    ok(st.get("state") == "done" and os.path.isfile(pm),
       "something that was only ever an OFFER lands in the folder its entry names",
       repr(st)[:200])
    ok(os.path.isdir(os.path.dirname(pm))
       and os.listdir(os.path.dirname(pm)) == ["ps4mount.elf"],
       "...and the checksum file beside it in the release is not mistaken for the payload")

    # CANCEL, AND WHAT IS KEPT.
    big = elf("nanodns", 400000)
    REPOS["drakmor/nanoDNS"][0]["assets"]["nanodns.elf"] = big
    P._rel_cache.clear()
    UP["slow"] = 0.02
    a = h._payloads_get({"id": "nanodns", "platform": "PS5", "async": 1})
    o = a[0] if isinstance(a, tuple) else a
    jid = o.get("job_id")
    end = time.time() + 15
    moving = {}
    while time.time() < end:
        moving = S.download_status(srv, jid)
        if moving.get("state") == "downloading" and int(moving.get("done") or 0) > 0:
            break
        time.sleep(0.05)
    ok(moving.get("state") == "downloading" and moving.get("total") == len(big)
       and 0 < int(moving.get("done") or 0) < len(big),
       "a running download reports how much has arrived and of how much", repr(moving)[:200])
    ok(srv.queue.cancel(jid) is True, "Cancel is accepted while it runs")
    st = wait_job(jid)
    UP["slow"] = 0.0
    ok(st.get("state") == "idle",
       "a cancelled download reads idle - what a console's own reports, and what the page's "
       "watcher treats as stopped on purpose", repr(st)[:200])
    ok(not os.path.exists(os.path.join(root, "Payloads", "PS5", "nanodns", "nanodns.elf")),
       "...and nothing half-finished was moved into place")

    # NOTHING PUBLISHED YET: a sentence and a code, in the request, with no job.
    a = h._payloads_get({"id": "crema", "platform": "PS4", "async": 1})
    o, code = a if isinstance(a, tuple) else (a, 200)
    if o.get("job_id"):
        o = wait_job(o["job_id"]).get("result") or {}
    ok(o.get("ok") is False and o.get("error_code") == "no-releases"
       and "published nothing" in str(o.get("message")),
       "a project with no release yet answers with that, and heals itself when one appears",
       repr(o)[:200])

    # THE NETWORK IS DOWN: the job fails with a reason the page can translate.
    UP["web"], UP["api"] = "500", "500"
    P._rel_cache.clear()
    a = h._payloads_get({"id": "y2jb-autoloader", "platform": "PS5", "async": 1})
    o = a[0] if isinstance(a, tuple) else a
    st = wait_job(o.get("job_id")) if o.get("job_id") else {"state": "error", "result": o}
    res_o = st.get("result") or {}
    ok(st.get("state") == "error" and res_o.get("error_code") == "http-500"
       and res_o.get("host") == "127.0.0.1",
       "when the upstream cannot be reached the job ends in an error that carries WHY and WHO",
       repr(st)[:240])
    UP["web"], UP["api"] = "ok", "ok"

    # A PLACED FILE AND THE FILE IT NEEDS BESIDE IT.
    P._rel_cache.clear()
    a = h._payloads_get({"id": "y2jb-autoloader", "platform": "PS5", "async": 1})
    o = a[0] if isinstance(a, tuple) else a
    st = wait_job(o.get("job_id"))
    ydir = os.path.join(root, "Payloads", "PS5", "Y2JB Autoloader")
    ok(st.get("state") == "done" and os.path.isfile(os.path.join(ydir, "y2jb_update.zip"))
       and os.path.isfile(os.path.join(ydir, "download0.dat")),
       "an entry that needs two files gets both", (repr(st)[:160], os.path.isdir(ydir)
                                                  and os.listdir(ydir)))
    ok(os.path.isfile(os.path.join(ydir, "y2jb_update.zip"))
       and not os.path.isdir(os.path.join(ydir, "y2jb_update")),
       "...and an archive that is DELIVERED as an archive is not opened")

    # A NEWER COPY THE OWNER HOLDS IS NEVER REPLACED BY AN OLDER DOWNLOAD.
    xdir = os.path.join(root, "Homebrews", "PS4", "PS4-Xplorer")
    os.makedirs(xdir)
    mine = os.path.join(xdir, "PS4-Xplorer_2.08.pkg")
    io.open(mine, "wb").write(pkg("LAPY20009 two-oh-eight"))
    real_live = P.live_catalog
    xit = {"id": "LAPY20009", "platform": "PS4", "kind": "homebrew", "shape": "pkg",
           "title": "PS4-Xplorer", "path": "Homebrews/PS4/PS4-Xplorer/PS4-Xplorer_2.08.pkg",
           "file": "PS4-Xplorer_2.08.pkg", "repo": "test/xplorer", "asset": "Xplorer",
           "ext": ".pkg", "have": True, "version": "2.08", "title_id": "LAPY20009"}
    P.live_catalog = lambda c, w: ({"items": [dict(xit)]}, "x")
    try:
        P._rel_cache.clear()
        a = h._payloads_get({"id": "LAPY20009", "platform": "PS4", "force": True})
        o, code = a if isinstance(a, tuple) else (a, 200)
    finally:
        P.live_catalog = real_live
    ok(code == 409 and o.get("error_code") == "older" and o.get("kept") is True
       and os.path.isfile(mine) and os.listdir(xdir) == ["PS4-Xplorer_2.08.pkg"],
       "'Download again' never swaps a newer copy the owner holds for the older one published",
       (code, repr(o)[:160], os.listdir(xdir)))

    # =========================================================================================
    # 7. WHAT GOES TO THE CONSOLE WITH IT
    # =========================================================================================
    fdir = os.path.join(root, "Homebrews", "PS4", "PKGI PS4")
    os.makedirs(fdir)
    io.open(os.path.join(fdir, "FPKGi_v1.10.0-release.pkg"), "wb").write(pkg("PKGI13337"))
    DEST = "/data/FPKGi/ContentJSONs/GAMES.json"
    EMPTY = '{"DATA": {}}'
    STOCK = b'{"DATA":{"https://www.web.site/game.pkg":{"title_id":"CUSA00000"}}}'
    fp = {"id": "PKGI13337", "platform": "PS4", "title": "FPKGi",
          "path": "Homebrews/PS4/PKGI PS4/FPKGi_v1.10.0-release.pkg",
          "data_files": {"GAMES.json": {"dest": DEST, "create": EMPTY, "json_key": "DATA",
                                        "stock": "https://www.web.site/", "stock_max": 4096}}}
    own = os.path.join(fdir, "games.json")                      # the owner typed it lower-case
    OWN = b'{"DATA":{"http://10.0.0.5/a.pkg":{"title_id":"CUSA12345","name":"Mine"}}}'
    REAL = b'{"DATA":{"http://10.0.0.9/z.pkg":{"title_id":"CUSA99999","name":"On the console"}}}'

    # console has nothing, owner has no file -> an empty valid one
    b = FakeBridge()
    okk, said, wrote = P.send_data_files(b, cfg, fp)
    ok(okk and wrote == 1 and b.files.get(DEST) == EMPTY.encode(),
       "with no list anywhere an empty, valid one is created - at the exact path, in the exact "
       "case, the homebrew opens", (said, b.files))
    # console has nothing, owner has a file -> the owner's
    io.open(own, "wb").write(OWN)
    b = FakeBridge()
    okk, said, wrote = P.send_data_files(b, cfg, fp)
    ok(wrote == 1 and b.files.get(DEST) == OWN and "GAMES.json" in said,
       "the owner's own list beside the package is the one that goes", said)
    # console has the app's own placeholder -> replaced
    b = FakeBridge({DEST: STOCK})
    okk, said, wrote = P.send_data_files(b, cfg, fp)
    ok(wrote == 1 and b.files[DEST] == OWN,
       "the placeholder the homebrew writes for itself is nobody's data, and is replaced", said)
    # console has a REAL list -> left alone, said so; the deliberate second press replaces it
    b = FakeBridge({DEST: REAL})
    okk, said, wrote = P.send_data_files(b, cfg, fp)
    ok(wrote == 0 and b.files[DEST] == REAL and "send again" in said,
       "a real list already on the console is never overwritten by a first press", said)
    okk, said, wrote = P.send_data_files(b, cfg, fp, force=True)
    ok(wrote == 1 and b.files[DEST] == OWN, "...and is by the deliberate second one", said)
    # the same bytes -> nothing to do
    okk, said, wrote = P.send_data_files(b, cfg, fp)
    ok(wrote == 0 and "already on the console" in said, "the same list twice writes nothing", said)
    # the owner's file is not a list the homebrew can read -> not sent
    io.open(own, "wb").write(b'["not", "a", "list"]')
    b = FakeBridge()
    okk, said, wrote = P.send_data_files(b, cfg, fp)
    ok(not okk and wrote == 0 and DEST not in b.files,
       "a file that is not a list the homebrew can read is not delivered as if it were", said)
    # no owner's file, console has a real list -> `create` never replaces
    os.remove(own)
    b = FakeBridge({DEST: REAL})
    okk, said, wrote = P.send_data_files(b, cfg, fp)
    ok(wrote == 0 and b.files[DEST] == REAL, "an empty list is never written over a real one")
    # dry run writes nothing; a console that does not answer is said, not guessed
    b = FakeBridge()
    okk, said, wrote = P.send_data_files(b, cfg, fp, dry_run=True)
    ok(wrote == 0 and not b.files and "would be written" in said, "a dry run writes nothing", said)
    b = FakeBridge(alive=False)
    okk, said, wrote = P.send_data_files(b, cfg, fp)
    ok(not okk and wrote == 0 and "did not answer" in said,
       "a console that does not answer is not mistaken for one with no list", said)
    # a destination outside /data is never written, whatever the catalogue says
    evil = dict(fp)
    evil["data_files"] = {"GAMES.json": {"dest": "/system/x.json", "create": EMPTY}}
    b = FakeBridge()
    P.send_data_files(b, cfg, evil)
    evil["data_files"] = {"GAMES.json": {"dest": "/data/../system/x.json", "create": EMPTY}}
    P.send_data_files(b, cfg, evil)
    ok(not b.files, "nothing is ever written outside /data, whatever an entry names", b.files)

    # ---- a line in somebody else's text file -------------------------------------------------
    WL = "/data/whitelist.txt"
    v3k = {"title": "Vita3K", "console_lines": {WL: ["PPSA99300"]}}
    b = FakeBridge()
    okk, said, added = P.ensure_console_lines(b, v3k)
    ok(added == 1 and b.files.get(WL) == b"PPSA99300\n",
       "the line an app's helper needs is written with a bare line feed", repr(b.files.get(WL)))
    b = FakeBridge({WL: b"PPSA00001\r\n# mine\r\nPPSA00002"})
    okk, said, added = P.ensure_console_lines(b, v3k)
    ok(added == 1 and b.files[WL] == b"PPSA00001\r\n# mine\r\nPPSA00002\nPPSA99300\n",
       "what is already in the file is left exactly as it was, and the line is added after it",
       repr(b.files[WL]))
    okk, said, added = P.ensure_console_lines(b, v3k)
    ok(added == 0 and len(b.writes) == 1, "a line that is already there is not added twice")
    b = FakeBridge({WL: b"PPSA99300 # vita\n"})
    okk, said, added = P.ensure_console_lines(b, v3k)
    ok(added == 0 and not b.writes, "...including when it carries a comment")

    # ---- a file that is PLACED --------------------------------------------------------------
    b = FakeBridge(dirs=["/data", "/user", "/user/download", "/user/download/PPSA01650"])
    ok(P.resolve_place(b, "/data/y2jb_update.zip") == ("/data/y2jb_update.zip", ""),
       "a fixed path is used as written")
    ok(P.resolve_place(b, "/user/download/PPSA0165?/download0.dat")[0]
       == "/user/download/PPSA01650/download0.dat",
       "a '?' stands for one character of a folder that already exists on THIS console")
    b2 = FakeBridge(dirs=["/data", "/user", "/user/download"])
    p, why = P.resolve_place(b2, "/user/download/PPSA0165?/download0.dat")
    ok(p == "" and "not installed" in why,
       "...and when the app it belongs to is not installed, nothing is written", why)
    b3 = FakeBridge(dirs=["/data", "/user", "/user/download", "/user/download/PPSA01650",
                          "/user/download/PPSA01651"])
    p, why = P.resolve_place(b3, "/user/download/PPSA0165?/download0.dat")
    ok(p == "" and "More than one" in why, "...nor when two folders match", why)
    for bad in ("/system/x", "/data/../x", "/data/a*", "relative/x", "/data/x/", "/mnt/usb0/x"):
        ok(P.resolve_place(b, bad)[0] == "", "a placed file is never written to %s" % bad)

    cat, _sig = P.live_catalog(cfg, S.WEB_DIR)
    y2 = [i for i in cat["items"] if i.get("id") == "y2jb-autoloader"]
    ok(len(y2) == 1 and y2[0].get("have") is True and y2[0].get("shape") == "file"
       and y2[0].get("place") == "/data/y2jb_update.zip",
       "once downloaded, a placed file is a row of its own that knows where it goes",
       [(i.get("have"), i.get("shape"), i.get("place")) for i in y2])
    if y2:
        okk, said = P.send_placed(b, cfg, y2[0], dry_run=True)
        ok(okk and not b.writes and "/data/y2jb_update.zip" in said
           and "/user/download/PPSA01650/download0.dat" in said,
           "a dry run of Send names both destinations and writes neither", said)
        okk, said = P.send_placed(b, cfg, y2[0])
        ok(okk and sorted(b.writes) == ["/data/y2jb_update.zip",
                                        "/user/download/PPSA01650/download0.dat"]
           and b.files["/data/y2jb_update.zip"][:2] == b"PK",
           "Send writes the file and the one it needs, each to its own path", (said, b.writes))
        del b.writes[:]
        okk, said = P.send_placed(b, cfg, y2[0])
        ok(okk and b.writes == ["/data/y2jb_update.zip"] and "left as it is" in said,
           "a second Send refreshes the small file and leaves the data image that is already "
           "there alone", (said, b.writes))
        okk, said = P.send_placed(b2, cfg, y2[0])
        ok(okk and b2.writes == ["/data/y2jb_update.zip"] and "not placed" in said,
           "on a console without that app the main file still goes and the sentence says what "
           "did not", said)

    # ---- an update keeps what the owner put inside the app ------------------------------------
    app = os.path.join(work, "app", "PPSA99131")
    os.makedirs(os.path.join(app, "memstick", "PSP", "GAME"))
    io.open(os.path.join(app, "memstick", "PSP", "GAME", "mine.iso"), "wb").write(b"game")
    io.open(os.path.join(app, "eboot.bin"), "wb").write(b"old")
    staged = os.path.join(work, "app", "staged")
    os.makedirs(os.path.join(staged, "memstick"))
    io.open(os.path.join(staged, "eboot.bin"), "wb").write(b"new")
    io.open(os.path.join(staged, "memstick", "readme.txt"), "wb").write(b"shipped")
    P._replace_dir(app, staged, keep_extra=True)
    ok(io.open(os.path.join(app, "eboot.bin"), "rb").read() == b"new"
       and os.path.isfile(os.path.join(app, "memstick", "PSP", "GAME", "mine.iso"))
       and os.path.isfile(os.path.join(app, "memstick", "readme.txt")),
       "updating an app folder replaces what the release ships and keeps the games and saves the "
       "owner keeps inside it")
    ok(not [d for d in os.listdir(os.path.join(work, "app")) if ".old-" in d],
       "...and leaves no old copy behind")

    # ---- the seed store answers only from its own folder --------------------------------------
    ok(P.seed_source({"bundled": "seed/../../companion/server.py"}) is None
       and P.seed_source({"bundled": "C:/Windows/win.ini"}) is None
       and P.seed_source({"bundled": "assets/payloads/curated.json"}) is None,
       "a carried file is only ever looked for inside the seed folder")

    # ---- "already installed" knows a newer build when it sees one ----------------------------
    br = object.__new__(S.Ps5Bridge)
    br.c = {"name": "PS4"}
    br.installed_titles = lambda: ["DLPH00010"]
    br.console_apps = lambda: [{"title_id": "DLPH00010", "app_ver": "04.04", "name": "DolphinPS4"}]
    br.installed_app_pkg = lambda tid: 204996608
    br._name_of = lambda tid: "DolphinPS4"
    try:
        br.name
    except Exception:
        S.Ps5Bridge.name = property(lambda self: self.c.get("name"))
    ok(br.already_installed("base", "DLPH00010", "04.06", pkg_bytes=206569472) == "",
       "a newer build of an installed homebrew is an update, not 'already installed' - "
       "whatever it weighs")
    ok(br.already_installed("base", "DLPH00010", "04.04", pkg_bytes=204996608) != "",
       "...the same build still is")
    ok(br.already_installed("base", "DLPH00010", "", pkg_bytes=206569472) != "",
       "...and with no version stated the byte count decides, exactly as before")

    # ---- which networks a console is looked for on --------------------------------------------
    ok(S._is_private_v4("10.0.0.5") and S._is_private_v4("192.168.1.5")
       and S._is_private_v4("172.20.80.1") and not S._is_private_v4("172.32.0.1")
       and not S._is_private_v4("8.8.8.8") and not S._is_private_v4("100.64.0.1"),
       "only a home network's addresses are ever swept for a console")
    _li, _lo = S.lan_ip, S.local_ips
    try:
        S.lan_ip = lambda: "100.64.3.7"                         # a VPN holds the default route
        S.local_ips = lambda: ["100.64.3.7", "192.168.1.20", "10.0.0.5", "172.20.80.1",
                               "192.168.56.1", "127.0.0.1"]
        bases = S.sweep_bases()
    finally:
        S.lan_ip, S.local_ips = _li, _lo
    ok(bases == ["192.168.1", "10.0.0", "172.20.80"],
       "with a VPN holding the default route the PC's home networks are still swept - three at "
       "most", bases)

    # =========================================================================================
    # 7b. PAYLOAD MANAGER 0.6.0 - AND 0.5.x, WHICH MUST KEEP WORKING
    # =========================================================================================
    # MEASURED THE DAY 0.6.0 CAME OUT: a GET of /loadpayload or /process_kill answers 405, its own
    # page sends both as POST with X-PLDMGR-Request: 1, and it moved every payload into
    # /data/pldmgr/payloads/local/<slot>/ with a meta.json. Every load this app made was the GET.
    import deploy as D
    for mode in ("0.6", "0.5"):
        pmsrv = start_pldmgr(mode)
        try:
            pm = S.PayloadManager("127.0.0.1", pmsrv.server_address[1], timeout=5)
            okl = pm.load("/data/pldmgr/payloads/pms-installer/pms-installer.elf")
            okk = pm.kill(4242)
            loads = [x for x in pmsrv.seen if x[1].startswith("/loadpayload:")]
            if mode == "0.6":
                ok(okl and loads and loads[0][0] == "POST" and loads[0][2] and len(loads) == 1,
                   "on Payload Manager 0.6.0 a load is ONE POST carrying its own header", loads)
                ok(okk and [x for x in pmsrv.seen if x[1].startswith("/process_kill")][0][0] == "POST",
                   "...and so is stopping a process")
                ok(pm.layout() == 2, "...and its slots are recognised")
            else:
                ok(okl and [m for m, _p, _h in loads] == ["POST", "GET"],
                   "on 0.5.x a load is refused as a POST and asked again as the GET it always was",
                   loads)
                ok(okk, "...and stopping a process still works there")
                ok(pm.layout() == 1, "...and the old folder layout is kept")

            br = types.SimpleNamespace(pldmgr=pm)
            if mode == "0.6":
                dest, slot = P.ps5_payload_dest_for(br, {"file": "ftpsrv-ps5.elf"})
                ok(dest == "/data/pldmgr/payloads/local/ftpsrv/ftpsrv-ps5.elf" and slot == "",
                   "a payload that already has a slot is written INTO it", dest)
                dest, slot = P.ps5_payload_dest_for(br, {"file": "ftpsrv.elf"})
                ok(dest == "/data/pldmgr/payloads/local/ftpsrv/ftpsrv-ps5.elf",
                   "...under the slot's own filename, whatever this side calls it", dest)
                dest, slot = P.ps5_payload_dest_for(br, {"file": "pldmgr_v0.6.1.elf"})
                ok(dest == "/data/pldmgr/payloads/local/pldmgr/pldmgr_v0.6.0.elf",
                   "...a newer version of the same payload replaces the one in its slot", dest)
                dest, slot = P.ps5_payload_dest_for(br, {"file": "PS4Mount.elf"})
                ok(dest == "/data/pldmgr/payloads/local/ps4mount/PS4Mount.elf"
                   and slot == "/data/pldmgr/payloads/local/ps4mount",
                   "a payload with no slot yet gets one named after it", (dest, slot))
                # our own build: into its slot, and no old-style sidecar beside it
                fb = FakeBridge()
                fb.pldmgr = pm
                own = os.path.join(work, "own")
                os.makedirs(own, exist_ok=True)
                elf_path = os.path.join(own, "PKG-MUTANT-SHOP.elf")
                io.open(elf_path, "wb").write(elf("pkg-mutant-shop", 3000))
                okio, where = P.install_ours(fb, cfg, {"ours": True, "platform": "PS5",
                                                       "file": "PKG-MUTANT-SHOP.elf"}, src=elf_path)
                ok(okio and where == "/data/pldmgr/payloads/local/pkg-mutant-shop/PKG-MUTANT-SHOP.elf"
                   and not [f for f in fb.files if f.endswith(".json")],
                   "our own build goes into the slot Payload Manager autoloads, with nothing beside "
                   "it Payload Manager does not write itself", (where, sorted(fb.files)))
                # a helper with no slot: the file, and the meta.json that makes it listed
                fb2 = FakeBridge()
                fb2.pldmgr = pm
                hp = os.path.join(own, "PS5SXHelper.elf")
                io.open(hp, "wb").write(elf("helper", 2000))
                real_hf = P.helper_files
                P.helper_files = lambda c, it: [{"name": "PS5SXHelper.elf", "local": hp, "size": 2000}]
                try:
                    okh, said, sent = P.send_helpers(fb2, cfg, {"platform": "PS5", "title": "x"})
                finally:
                    P.helper_files = real_hf
                meta = fb2.files.get("/data/pldmgr/payloads/local/ps5sxhelper/meta.json")
                ok(okh and sent == 1
                   and "/data/pldmgr/payloads/local/ps5sxhelper/PS5SXHelper.elf" in fb2.files
                   and meta and json.loads(meta.decode("utf-8")).get("schema") == 2
                   and json.loads(meta.decode("utf-8")).get("filename") == "PS5SXHelper.elf",
                   "a helper with no slot gets one, with a meta.json in Payload Manager's own shape",
                   sorted(fb2.files))
            else:
                dest, slot = P.ps5_payload_dest_for(br, {"file": "ftpsrv-ps5.elf"})
                ok(dest == "/data/pldmgr/payloads/ftpsrv-ps5/ftpsrv-ps5.elf" and slot == "",
                   "on 0.5.x a payload goes where it always went", dest)

            # the deploy tool, against the same Payload Manager
            real_port = D.PLDMGR_PORT
            D.PLDMGR_PORT = pmsrv.server_address[1]
            try:
                del pmsrv.seen[:]
                target, newslot = D.pldmgr_target("127.0.0.1")
                okd, _body = D.pm_send("127.0.0.1", "/loadpayload:%s" % target, timeout=5)
            finally:
                D.PLDMGR_PORT = real_port
            if mode == "0.6":
                ok(target == "/data/pldmgr/payloads/local/pkg-mutant-shop/PKG-MUTANT-SHOP.elf"
                   and not newslot and okd,
                   "the deploy tool writes the build Payload Manager 0.6.0 autoloads, and loads it",
                   (target, newslot, okd))
            else:
                ok(target == D.PLDMGR_ELF and okd,
                   "...and on 0.5.x it writes and loads exactly what it always did", (target, okd))
        finally:
            pmsrv.shutdown()

    # =========================================================================================
    # 8. THE PAGE LETS GO OF WHAT WAS PRESSED
    # =========================================================================================
    ui = io.open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    js = r"""
var NOW=1000000, REAL=setTimeout;
Date.now=function(){ return NOW; };
setTimeout=function(f,ms){ return REAL(function(){ NOW+=(ms||0); f(); },0); };
var PHB={hold:{},busy:{},busyAt:{},dl:null}, TOASTS=[], PAINTS=0;
function $(s){ return null; }
function t(k){ return k; }
function toast(m,k){ TOASTS.push([m,k]); }
function phbUpsPaint(){ PAINTS++; }
function errText(r,f){ return (r&&r.error_code)?("CODE:"+r.error_code):(f||"fallback"); }
var SCRIPT=[], ASKED=[];
function api(u){ ASKED.push(u); var s=SCRIPT.length>1?SCRIPT.shift():SCRIPT[0];
  if(s==="throw") return Promise.reject(new Error("net"));
  return Promise.resolve(s); }
""" + "\n".join(jsfn(ui, f) for f in ("phbHoldPaint", "phbHolding", "phbHoldSay", "phbHoldSweep",
                                       "phbHeld", "phbDlDone", "phbJobWatch")) + r"""
var PHB_HOLD_MS=30*60*1000, OUT={};
function settle(p){ return p.then(function(v){ return {v:v}; }, function(e){ return {e:String(e&&e.message||e)}; }); }
(async function(){
  // 1. work resolves
  var p=phbHeld("g:a","Working",function(){ return Promise.resolve(7); });
  OUT.held_during=!!PHB.hold["g:a"]; OUT.holding=phbHolding("g:");
  OUT.r1=await settle(p); OUT.after1=!!PHB.hold["g:a"];
  // 2. work rejects
  OUT.r2=await settle(phbHeld("g:b","Working",function(){ return Promise.reject(new Error("no")); }));
  OUT.after2=!!PHB.hold["g:b"];
  // 3. work THROWS before it returns a promise
  OUT.r3=await settle(phbHeld("g:c","Working",function(){ throw new Error("boom"); }));
  OUT.after3=!!PHB.hold["g:c"];
  // 4. work never settles: the watchdog lets go, and says so
  phbHeld("g:d","Working",function(){ return new Promise(function(){}); });
  PHB.hold["g:arm"]={txt:"again?",until:NOW+6000,arm:true};
  OUT.arm_not_holding=!phbHolding("g:arm");
  NOW+=PHB_HOLD_MS-1000; phbHoldSweep();
  OUT.d_before=!!PHB.hold["g:d"]; OUT.arm_gone=!PHB.hold["g:arm"]; OUT.toasts_before=TOASTS.length;
  NOW+=2000; phbHoldSweep();
  OUT.d_after=!!PHB.hold["g:d"]; OUT.toast=TOASTS.slice(-1)[0];
  // 5. a late answer must not let go of a NEWER press on the same row
  var gate; var late=phbHeld("g:e","one",function(){ return new Promise(function(r){ gate=r; }); });
  delete PHB.hold["g:e"];
  var newer=phbHeld("g:e","two",function(){ return new Promise(function(){}); });
  gate(1); await late;
  OUT.newer_kept=!!PHB.hold["g:e"] && PHB.hold["g:e"].txt==="two";
  // 6. a tile's busy flag is swept too
  PHB.busy["k"]=1; PHB.busyAt["k"]=NOW; NOW+=PHB_HOLD_MS+1; phbHoldSweep();
  OUT.busy_swept=!PHB.busy["k"];

  // ---- the watcher ----
  function watch(script,id){ SCRIPT=script; ASKED=[]; return phbJobWatch(id,null,"Get",null); }
  OUT.w_done=await watch([{ok:true,job_id:"job-3",state:"downloading",pct:10,done:1,total:10},
                          {ok:true,job_id:"job-3",state:"installing",pct:99,msg:"Opening"},
                          {ok:true,job_id:"job-3",state:"done",msg:"Downloaded x.",result:{ok:true,queued:1}}],"job-3");
  OUT.w_url=ASKED[0]; OUT.w_dl_cleared=(PHB.dl===null);
  OUT.w_err=await watch([{ok:true,job_id:"job-4",state:"error",msg:"English from the PC",
                          result:{ok:false,error_code:"dns",host:"github.com"}}],"job-4");
  OUT.w_idle=await watch([{ok:true,job_id:"job-5",state:"idle",msg:""}],"job-5");
  OUT.w_stall=await watch([{ok:true,job_id:"job-6",state:"downloading",pct:5,done:500,total:9000,msg:"x"}],"job-6");
  OUT.w_never=await watch([{ok:true,job_id:0,state:"idle"}],"job-7");
  OUT.w_gone=await watch(["throw"],"job-8");
  OUT.w_console=await watch([{ok:true,job_id:12,state:"done",msg:"ok"}],12);
  OUT.w_console_url=ASKED[0];
  // an unpack moves the words and the stage, not the byte count: that is not a stall
  var steps=[]; for(var i=0;i<200;i++) steps.push({ok:true,job_id:"job-9",state:"installing",pct:99,done:5,total:5,msg:"Opening "+i});
  steps.push({ok:true,job_id:"job-9",state:"done",msg:"fine"});
  OUT.w_unpack=await watch(steps,"job-9");
  console.log(JSON.stringify(OUT));
})().catch(function(e){ console.log(JSON.stringify({crash:String(e&&e.stack||e)})); });
"""
    jf = os.path.join(work, "hold.js")
    io.open(jf, "w", encoding="utf-8").write(js)
    out, why = {}, ""
    try:
        rr = subprocess.run(["node", jf], capture_output=True, text=True, timeout=60)
        why = (rr.stderr or "")[:300]
        try:
            out = json.loads((rr.stdout or "").strip().splitlines()[-1])
        except Exception:
            why = why or (rr.stdout or "")[:200] or "it printed nothing"
    except subprocess.TimeoutExpired:
        # Every watch in that script is on a fake clock and ends in milliseconds. One that is
        # still going after a minute is a watch with no way out - which is the defect itself.
        why = "it did not finish: a watch that never ends (stalled, never appeared, or gone)"
    ok(bool(out) and not out.get("crash") and not why,
       "the page's hold registry and job watcher run clean - and a download that stops moving "
       "is called stalled", why or out.get("crash"))
    if out and not out.get("crash"):
        ok(out["held_during"] and out["holding"] and out["r1"] == {"v": 7} and not out["after1"],
           "a pressed control is held while the work runs and let go when it resolves")
        ok(out["r2"] == {"e": "no"} and not out["after2"],
           "...and when it is refused - with the refusal passed on, not swallowed")
        ok(out["r3"] == {"e": "boom"} and not out["after3"],
           "...and when the work throws before it has even returned a promise")
        ok(out["d_before"] and not out["d_after"] and out["toast"] == ["phb_gave_up", "err"]
           and out["toasts_before"] == 0,
           "work that never reports is let go at its ceiling, and the page says so", out)
        ok(out["arm_not_holding"] and out["arm_gone"],
           "a row that is only asking 'again?' is not busy, and goes back to rest by itself")
        ok(out["newer_kept"], "a late answer never lets go of a newer press on the same row")
        ok(out["busy_swept"], "a tile's own busy flag has the same ceiling")
        ok(out["w_done"].get("ok") is True and out["w_done"].get("result", {}).get("queued") == 1
           and out["w_url"] == "/api/download/status?job=job-3" and out["w_dl_cleared"],
           "a PC's download is watched by its job id to the end, and the answer is handed on",
           out["w_done"])
        ok(out["w_err"] == {"ok": False, "error": "CODE:dns"},
           "a failed job is reported by its CODE, so the sentence is in the reader's language",
           out["w_err"])
        ok(out["w_idle"].get("ok") is False, "a cancelled job ends the watch")
        ok(out["w_stall"] == {"ok": False, "error": "phb_stalled"},
           "a download that stops moving is called stalled, not left at Working", out["w_stall"])
        ok(out["w_never"] == {"ok": False, "error": "phb_failed"},
           "a job that never appears ends the watch in seconds", out["w_never"])
        ok(out["w_gone"].get("ok") is False, "a PC that stops answering ends the watch")
        ok(out["w_console"].get("ok") is True and out["w_console_url"] == "/api/download/status",
           "a console's own download is watched the way it always was", out["w_console_url"])
        ok(out["w_unpack"].get("ok") is True,
           "opening a big archive - the words move, the byte count does not - is not a stall",
           out["w_unpack"])

    # ...and both drawers and the tiles really go through it.
    for fn in ("phbGetOne", "phbUpOne"):
        src = jsfn(ui, fn)
        ok("phbHeld(hk," in src and "async:1" in src and "phbJobWatch(" in src,
           "%s posts the press as a job and watches it, inside a hold" % fn)
    ok("phbHoldSweep()" in ui.split("function phbHeld(", 1)[1],
       "the panel's poll runs the watchdog")
    ok(ui.count('phb_no_tls:"PC NEEDED"') == 1 and ui.count('phb_needs_pc:"PC NEEDED"') == 1,
       "when a PC is needed the page says exactly that, in two words")


if __name__ == "__main__":
    sys.exit(main())

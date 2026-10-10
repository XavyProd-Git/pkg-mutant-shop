# -*- coding: utf-8 -*-
"""A stand-in for the PS4 payload, speaking exactly the API the real one speaks.

It exists so the companion's PS4 lane can be driven end to end with no console: the four fixes made
while the console was unreachable (the verdict contract, the queue reading the console's own error,
the scan lock, the missing routes) are all companion-facing, and this is what proves them.

It answers with the REAL app.db and addcont.db pulled off the console over FTP, so the schema work
is exercised against real data rather than a hand-made fixture.

    python fake_console.py <port> <mode>      mode: ok | psn-update
"""
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
MODE = "ok"

JOB = {"active": False, "state": "idle", "msg": "", "done": 0, "total": 0,
       "tid": "", "name": "", "uri": "", "task": 0, "job_id": 0, "started": 0.0}
def _installed_from_fixture():
    """The games the fixture says are installed, so one file defines them for both halves."""
    import sqlite3
    out = {}
    db = os.path.join(HERE, "app.db")
    if not os.path.isfile(db):
        return {"CUSA02365": 107806720}
    con = sqlite3.connect(db)
    for t in [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'tbl_appbrowse%'")][:1]:
        for tid, size in con.execute(
                "SELECT titleId, contentSize FROM %s WHERE category='gd'" % t):
            out[tid] = int(size or 0)
    con.close()
    return out


INSTALLED = _installed_from_fixture()
LOCK = threading.Lock()


def _pull(uri, want):
    """Download the package the companion offered, so its byte counter really moves."""
    got = 0
    try:
        with urllib.request.urlopen(uri, timeout=120) as r:
            while True:
                b = r.read(65536)
                if not b:
                    break
                got += len(b)
                with LOCK:
                    JOB["done"] = got
                if MODE == "psn-update" and got >= 5 * 1024 * 1024:
                    break                      # the console stops part-way, as it really does
    except Exception as e:
        print("[fake] pull failed: %r" % e)
    with LOCK:
        if MODE == "psn-update":
            JOB["state"] = "error"
            JOB["msg"] = ("The PS4 found a newer version of this game on PlayStation Network and "
                          "tried to merge it instead of installing this package. Stop the console "
                          "reaching PlayStation Network, or install the update package as well, "
                          "then try again")
        else:
            JOB["state"] = "installed"
            JOB["msg"] = "Installed on this PS4"
            if JOB["tid"]:
                INSTALLED[JOB["tid"]] = want or got
        print("[fake] job finished: %s (%d bytes)" % (JOB["state"], got))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _j(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            body = {}
        p = urlparse(self.path).path
        if p == "/api/install":
            key = str(body.get("install_key") or "")
            path = key[6:] if key.startswith("local:") else str(body.get("path") or "")
            if not path:
                return self._j({"ok": False, "error": "no package was named"})
            return self._start("file://" + path, body.get("name") or os.path.basename(path),
                               "", 0, local=True)
        return self._j({})

    def _start(self, uri, name, cid, size, local=False):
        with LOCK:
            if JOB["active"] and JOB["state"] == "downloading":
                return self._j({"ok": False, "busy": True,
                                "error": "An install is already running on this PS4"}, 409)
            tid = ""
            for src in (cid, name, uri):
                i = (src or "").find("CUSA")
                if i >= 0 and (src[i + 4:i + 9]).isdigit():
                    tid = src[i:i + 9]
                    break
            JOB.update({"active": True, "state": "downloading", "msg": "The PS4 is downloading it",
                        "done": 0, "total": size, "tid": tid, "name": name, "uri": uri,
                        "task": 200 + int(time.time()) % 50, "job_id": int(time.time() * 1000),
                        "started": time.time()})
        if local:
            with LOCK:
                JOB["state"] = "installed"
                JOB["msg"] = "Installed on this PS4"
                if JOB["tid"]:
                    INSTALLED[JOB["tid"]] = size or 1
        else:
            threading.Thread(target=_pull, args=(uri, size), daemon=True).start()
        return self._j({"ok": True, "queued": True, "job_id": JOB["job_id"], "task": JOB["task"],
                        "rc": "0x00000000"})

    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        one = lambda k: (q.get(k) or [""])[0]

        if p == "/api/health":
            return self._j({"ok": True, "on_console": True, "server": "on-console",
                            "platform": "ps4", "connected": True, "version": "3.62.0",
                            "engine_ready": True})
        if p == "/api/library":
            games = []
            for tid, sz in sorted(INSTALLED.items()):
                games.append({"title_id": tid, "name": "Test " + tid, "content_id": "",
                              "platform": "PS4", "size": sz, "installed": True,
                              "installed_version": "01.00", "has_icon": False,
                              "on_console": True, "source": "console"})
            return self._j({"ok": True, "platform": "ps4", "games": games, "count": len(games)})
        if p == "/api/installed":
            return self._j({"ok": True, "installed": sorted(INSTALLED), "count": len(INSTALLED)})
        if p == "/api/storage":
            return self._j({"ok": True, "reachable": True, "drives": [
                {"id": "internal", "label": "Internal HDD", "used": 500 << 30,
                 "games_bytes": 400 << 30, "free": 400 << 30, "total": 900 << 30,
                 "count": len(INSTALLED), "kind": "console"}]})
        if p == "/api/devices":
            return self._j({"ok": True, "platform": "ps4", "ps5": [
                {"id": "internal", "label": "Internal HDD", "path": "/user",
                 "free": 400 << 30, "total": 900 << 30, "connected": True}]})
        if p == "/api/fs/list":
            path = unquote(one("path"))
            tid = path.rsplit("/", 1)[-1]
            if path.startswith("/user/app/") and tid in INSTALLED:
                return self._j({"ok": True, "count": 1, "truncated": False, "entries": [
                    {"name": "app.pkg", "dir": False, "size": INSTALLED[tid], "mtime": 0}]})
            return self._j({"ok": False, "error": "cannot open directory"}, 404)
        if p == "/api/fs/read":
            path = unquote(one("path"))
            local = {"/system_data/priv/mms/app.db": os.path.join(HERE, "app.db"),
                     "/system_data/priv/mms/addcont.db": os.path.join(HERE, "addcont.db")}.get(path)
            if local and os.path.isfile(local):
                data = open(local, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            return self._j({"ok": False, "error": "no such file"}, 404)
        if p == "/api/engine/install-spawn":
            return self._start(unquote(one("uri")), unquote(one("name")),
                               unquote(one("cid")), int(one("size") or 0))
        if p == "/api/engine/job":
            with LOCK:
                return self._j({"ok": True, "active": JOB["active"], "job_id": JOB["job_id"],
                                "task": JOB["task"], "title_id": JOB["tid"], "name": JOB["name"],
                                "done": JOB["done"], "total": JOB["total"],
                                "state": JOB["state"], "msg": JOB["msg"], "rc": "0x00000000"})
        if p == "/api/engine/spawn-status":
            with LOCK:
                run = JOB["active"] and JOB["state"] == "downloading"
                res = JOB["active"] and JOB["state"] in ("installed", "error")
                return self._j({"ok": True, "busy": run, "stale": False, "has_result": res,
                                "busy_for": int(time.time() - (JOB["started"] or time.time()))})
        if p == "/api/engine/spawn-result":
            with LOCK:
                done = JOB["active"] and JOB["state"] in ("installed", "error")
                if not JOB["active"]:
                    return self._j({"ok": False, "pending": True, "state": "idle"})
                if not done:
                    return self._j({"ok": True, "rc": "0x00000000", "accepted": True,
                                    "state": JOB["state"], "content_id": JOB["tid"],
                                    "uri": JOB["uri"], "via": "bgft"})
                return self._j({"ok": JOB["state"] == "installed",
                                "rc": "0x00000000" if JOB["state"] == "installed" else "0x80990004",
                                "content_id": JOB["tid"], "uri": JOB["uri"], "via": "bgft",
                                "msg": JOB["msg"]})
        if p == "/api/engine/spawn-cleanup":
            with LOCK:
                if JOB["active"] and JOB["state"] in ("installed", "error"):
                    JOB["active"] = False
            return self._j({"ok": True, "cleaned": True, "freed": 0})
        if p == "/api/install/status":
            with LOCK:
                done = JOB["active"] and JOB["state"] in ("installed", "error")
                return self._j({"active": JOB["active"] and JOB["state"] == "downloading",
                                "done": done, "ok": JOB["state"] == "installed",
                                "accepted_only": False, "name": JOB["name"], "message": JOB["msg"]})
        if p == "/api/engine/log":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"fake console\n")
            return
        return self._j({})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    MODE = sys.argv[2] if len(sys.argv) > 2 else "ok"
    print("[fake] PS4 stand-in on :%d mode=%s" % (port, MODE))
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()

#!/usr/bin/env python3
"""End-to-end verification against a live PS5, in an order that ISOLATES VARIABLES.

WHY THE ORDER MATTERS
---------------------
On 2026-08-25 the console hard-crashed during an install and there was no way to tell which of
several changes did it, because everything had been exercised together. This runs the same ground
in stages, cheapest and safest first, and probes the console's health BETWEEN EVERY STAGE - so if
it dies, the stage number names the culprit.

The install stages are deliberately ordered to separate the open hypotheses:

    stage 4  install with NO display name      - reproduces the lane that is PROVEN good
    stage 5  install with a plain ASCII name   - adds only the name passthrough
    stage 6  install with a non-ASCII name     - adds only unusual bytes in MetaInfo.content_name
    stage 7  the same install driven by the PC companion - adds only the PC lane
    stage 8  /api/open (launches the console browser) - LAST, because it is a panic suspect and
             the least important feature in the app

Usage
    python tools/verify_console.py                 # stages 0-4: one install, the proven lane
    python tools/verify_console.py --stage 0-3     # read-only stages, no installs at all
    python tools/verify_console.py --all           # stages 0-8: the name-passthrough installs
                                                   # (5, 6), the PC-driven lane (7) and /api/open
    python tools/verify_console.py --all --force   # let stage 7 re-install over a registered title
    python tools/verify_console.py --ip 10.0.0.99 --title CUSA02365 --key Riptide-GP2-CUSA02365.pkg

ONE INSTALL BY DEFAULT. Stages 4-7 used to run back to back, and each hands the SAME package to
the console again as soon as the spawned installer's verdict says "accepted" - which is before
BGFT has finished downloading and registering it. Submitting a package on top of its own live
BGFT job is the shape of the 2026-08-25 panic. So 5-7 are opt-in (--all), and with --all the
tool reads bgft.db between install stages and waits for the title's newest row to settle before
spawning again.

WHERE THE CONSOLE ADDRESS COMES FROM (first hit wins):
    1. --ip
    2. ~\\Desktop\\PKG MUTANT SHOP\\config.json   - beside the exe the user actually runs
    3. companion/dist/config.json              - beside a freshly built exe
    4. companion/config.json                   - running from source
The exe reads the config.json beside itself, not the repo's; the two have held different console
addresses before, which is how the wrong machine gets measured (memory: a peer PC was adopted as
the console). Nothing here ever defaults to loopback or to a guessed address.

Nothing here writes to the console except the install stages, and every install is of a package
already in the library. Stage 3 is read-only unless the lane is STALE (latch set past the console's
own 600 s rule): a merely busy lane is left alone, because clearing it is what let a duplicate
install through.
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

G, Y, R, B, X = "\033[92m", "\033[93m", "\033[91m", "\033[94m", "\033[0m"
PASS, FAIL, WARN, SKIP = G + "PASS" + X, R + "FAIL" + X, Y + "WARN" + X, B + "skip" + X

RESULTS = []
STOP = False


def rec(stage, name, ok, detail=""):
    RESULTS.append({"stage": stage, "name": name, "ok": ok, "detail": detail})
    tag = PASS if ok is True else (FAIL if ok is False else (WARN if ok == "warn" else SKIP))
    print("  [%s] %-46s %s" % (tag, name, detail[:96]))
    return ok


def port_open(ip, port, timeout=2.0):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def http(url, timeout=25, data=None, method=None):
    """Returns (ok, parsed_or_text, error). Never raises."""
    try:
        rq = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            rq.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(rq, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        try:
            return True, json.loads(body), None
        except ValueError:
            return True, body, None
    except Exception as e:
        return False, None, repr(e)[:180]


class Ctx(object):
    def __init__(self, a):
        self.ip = a.ip
        self.shop = a.shop_port
        self.pc = a.companion
        self.title = a.title
        self.key = a.key

    def con(self, path, timeout=25):
        return http("http://%s:%d%s" % (self.ip, self.shop, path), timeout)

    def pcapi(self, path, timeout=40, body=None):
        data = json.dumps(body).encode() if body is not None else None
        return http("%s%s" % (self.pc, path), timeout, data, "POST" if body is not None else None)


# ---------------------------------------------------------------------------------------------
# The health gate. Run between every stage: if the console has gone, we stop immediately and the
# last stage that completed is the one that killed it.
# ---------------------------------------------------------------------------------------------
def alive(c, stage, why):
    global STOP
    ok, j, err = c.con("/api/health", timeout=8)
    if ok and isinstance(j, dict) and j.get("ok"):
        rec(stage, "console still alive %s" % why, True, "v%s" % j.get("version"))
        return True
    # Distinguish "our payload died" from "the whole console died" - that difference is the whole
    # reason the last crash was diagnosable at all. Only LAN-visible ports can say it: elfldr's
    # 9021 binds loopback on the console (so did ShadowMount's 10101), and probing it from here
    # was always False - which made a dead payload read as a dead console.
    others = {p: port_open(c.ip, p, 2.0) for p in (8084, 2121, 1337)}
    if any(others.values()):
        rec(stage, "console still alive %s" % why, False,
            "OUR payload is gone but the console is up (pldmgr=%s ftp2121=%s ftp1337=%s)"
            % (others[8084], others[2121], others[1337]))
    else:
        rec(stage, "console still alive %s" % why, False,
            "THE WHOLE CONSOLE IS GONE - every port closed. %s" % (err or ""))
    STOP = True
    return False


# ---------------------------------------------------------------------------------------------
def stage0(c):
    print("\n%s== stage 0  reachability and independence ==%s" % (B, X))
    ours = port_open(c.ip, c.shop)
    rec(0, "our shop is listening on :%d" % c.shop, ours)
    rec(0, "Payload Manager is listening on :8084", port_open(c.ip, 8084),
        "our engine needs it to spawn each install")
    # THE INDEPENDENCE PROOF the whole project has been working toward.
    third = {"etaHEN DPI v2": 12800, "etaHEN klog": 9081, "etaHEN FTP": 1337,
             "etaHEN v1": 9090, "Arsenal ezremote": 9040}
    live = [n for n, p in third.items() if port_open(c.ip, p, 1.5)]
    rec(0, "no third-party install host is running", not live,
        ("still up: " + ", ".join(live)) if live else "12800/9090/9081/1337/9040 all closed")
    if not ours:
        return False
    ok, j, err = c.con("/api/health", 10)
    if not (ok and isinstance(j, dict)):
        return rec(0, "GET /api/health", False, err or "no JSON")
    rec(0, "GET /api/health", True, "v%s engine=%s ready=%s"
        % (j.get("version"), j.get("engine"), j.get("engine_ready")))
    rec(0, "engine reports itself ready", bool(j.get("engine_ready")),
        "" if j.get("engine_ready") else "Payload Manager is not answering - installs cannot start")
    rec(0, "console reports OUR engine, not a third party",
        j.get("engine") in ("pms-spawn", "pms"), str(j.get("engine")))
    return True


def stage1(c):
    print("\n%s== stage 1  read-only endpoints ==%s" % (B, X))
    # /api/engine/state USED TO BE companion-only. It was, and this comment used to say so: the
    # console answered {} through its unknown-/api/ stub, so checking it here tested the harness
    # rather than the app. Both payloads answer it for real now - which is the whole point, since
    # the Settings engine panel is read most often on a console with no PC in the room - so it is
    # checked in both places. The companion's own answer is still checked further down.
    checks = [
        ("/api/engine/state", lambda j: isinstance(j, dict) and j.get("ok")
                                        and isinstance(j.get("state"), str)),
        ("/api/engine/spawn-status", lambda j: isinstance(j, dict) and "busy" in j),
        ("/api/engine/log", lambda j: True),
        ("/api/sources", lambda j: isinstance(j, dict)),
        ("/api/helpers", lambda j: isinstance(j, dict)),
        ("/api/storage", lambda j: isinstance(j, dict) and "drives" in j),
        ("/api/installed", lambda j: isinstance(j, dict) and "installed" in j),
        ("/api/cheats/paths", lambda j: isinstance(j, dict)),
        ("/api/tile/status", lambda j: True),
        ("/api/power", lambda j: True),
    ]
    for path, pred in checks:
        ok, j, err = c.con(path, 25)
        good = ok and pred(j)
        summary = ""
        if ok and isinstance(j, dict):
            summary = ", ".join("%s=%s" % (k, str(j[k])[:22])
                                for k in list(j)[:3] if not isinstance(j[k], (list, dict)))
        elif ok:
            summary = "%d bytes of text" % len(j or "")
        rec(1, "GET %s" % path, good, summary or (err or ""))
    # The PC-side view of the engine. Asked of the companion, which is what owns it.
    ok, j, err = http("%s/api/engine/state" % c.pc, 45)
    good = ok and isinstance(j, dict) and j.get("ok")
    rec(1, "GET /api/engine/state (companion)", good,
        ("%s - %s" % (j.get("state"), j.get("detail"))) if good else (err or str(j)[:70]))

    # The console's library scan is the slowest read; time it, because a regression here is felt.
    t0 = time.time()
    ok, j, err = c.con("/api/library", 90)
    n = len((j or {}).get("games", [])) if ok and isinstance(j, dict) else 0
    rec(1, "GET /api/library", ok and n > 0, "%d titles in %.1fs" % (n, time.time() - t0))


def stage2(c):
    print("\n%s== stage 2  the file API (how everything reaches the console) ==%s" % (B, X))
    ok, j, err = c.con("/api/fs/list?path=%s" % urllib.parse.quote("/data/pkg-mutant-shop", safe=""))
    rec(2, "GET /api/fs/list", ok and isinstance(j, dict) and j.get("ok"),
        "%d entries" % len((j or {}).get("entries", [])) if ok else (err or ""))
    # The installer lives in the SHOP's payload folder, not Payload Manager's. It was taken out of
    # Payload Manager on purpose - it is not something to load by hand, and a stray copy there is
    # how someone ends up running an old build (see the basename-load trap).
    ok, j, err = c.con("/api/fs/list?path=%s"
                       % urllib.parse.quote("/data/pkg-mutant-shop/payloads", safe=""))
    ents = (j or {}).get("entries", []) if ok and isinstance(j, dict) else []
    names = [e.get("name") for e in ents]
    inst = next((e for e in ents if e.get("name") == "pms-installer.elf"), None)
    rec(2, "the spawned installer is on disk", bool(inst and inst.get("size", 0) > 0),
        ("%d bytes" % inst["size"]) if inst else ", ".join(names[:4]))
    # and Payload Manager must NOT be offering it
    ok2, j2, _ = c.con("/api/fs/list?path=%s"
                       % urllib.parse.quote("/data/pldmgr/payloads/pms-installer", safe=""))
    gone = not (ok2 and isinstance(j2, dict) and j2.get("ok"))
    rec(2, "the installer is not in Payload Manager", gone,
        "absent" if gone else "STILL THERE - it should not be loadable by hand")
    # A directory traversal must be refused. This endpoint can read anything on the console.
    ok, j, err = c.con("/api/fs/read?path=%s" % urllib.parse.quote("../../etc/passwd", safe=""))
    refused = (not ok) or (isinstance(j, dict) and j.get("ok") is False)
    rec(2, "relative paths are refused", refused, "" if refused else "IT SERVED A RELATIVE PATH")


def stage3(c):
    print("\n%s== stage 3  the install lane is idle and clean ==%s" % (B, X))
    ok, j, err = c.con("/api/engine/spawn-status")
    if not (ok and isinstance(j, dict)):
        return rec(3, "spawn-status", False, err or "")
    rec(3, "no install is marked in flight", not j.get("busy"),
        "busy_for=%ss stale=%s" % (j.get("busy_for"), j.get("stale")))
    # ONLY A STALE LATCH IS CLEARED. `busy` means the console is refusing duplicates because an
    # install may still be running - and spawn-cleanup zeroes exactly that refusal. Clearing it on
    # "busy" is the reload-the-ELF mistake of 2026-08-25 done from a script. `stale` is the
    # console's own verdict that the latch outlived its 600 s rule with no verdict behind it; a
    # leftover verdict file (has_result, not busy) is finished work and safe to sweep.
    if j.get("busy"):
        rec(3, "left the busy lane alone", "warn",
            "an install may still be running - re-run when busy_for stops growing")
        return
    if j.get("stale") or j.get("has_result"):
        c.con("/api/engine/spawn-cleanup", 20)
        ok2, j2, _ = c.con("/api/engine/spawn-status")
        rec(3, "cleanup released the lane",
            ok2 and isinstance(j2, dict) and not j2.get("busy") and not j2.get("stale"),
            "after spawn-cleanup (stale=%s has_result=%s)" % (j.get("stale"), j.get("has_result")))


def _route_ip(console_ip):
    """This PC's address on the route to the console - the one the console can call back on."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((console_ip, 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def config_candidates():
    """Where the console's address is looked for, in the order the header documents."""
    return [
        os.path.expanduser(r"~\Desktop\PKG MUTANT SHOP\config.json"),
        os.path.join(ROOT, "companion", "dist", "config.json"),
        os.path.join(ROOT, "companion", "config.json"),
    ]


def console_ip_from_config():
    """(ip, path) from the first config that names a console, or (None, None). consoles[0].ip
    wins over ps5_ip, the way the companion resolves it. The defaults' placeholder address is
    not a hit: measuring 192.168.1.50 because nobody set anything is the wrong-machine trap."""
    for p in config_candidates():
        if not os.path.exists(p):
            continue
        try:
            cfg = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        cons = cfg.get("consoles") or []
        ip = (cons[0].get("ip") if cons and isinstance(cons[0], dict) else None) or cfg.get("ps5_ip")
        if ip and ip != "192.168.1.50":
            return ip, p
    return None, None


# bgft.db statuses. 1036 = full title done, 1026 = update/add-on done, 1021 = failed;
# 1007 and 1009 are a download still in flight (a fresh spawn shows as 1007 with 0 bytes).
BGFT_DONE = (1036, 1026, 1021)


def _bgft_rows(c, limit=5, title=None):
    """Newest rows of tbl_downloads through our own file API. [] means the ledger was read and
    holds no matching row; None means it could NOT be read (network, a refused /api/fs/read, a
    sqlite error). The two used to be the same [], and the settle gate below read both as "no
    row = settled" - so a console it could not see was exactly the console it re-submitted to.
    Absence of evidence is never a verdict, in either direction: the callers decide."""
    import sqlite3
    import tempfile
    url = "http://%s:%d/api/fs/read?path=%s" % (
        c.ip, c.shop, urllib.parse.quote("/system_data/priv/mms/bgft.db", safe=""))
    tmp = os.path.join(tempfile.gettempdir(), "pms_verify_bgft.db")
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            f.write(r.read())
        con = sqlite3.connect(tmp)
        q = ("SELECT title_id,status,transferred_total,length_total,title FROM tbl_downloads "
             + ("WHERE title_id=? " if title else "")
             + "ORDER BY last_updated DESC, rowid DESC LIMIT %d" % limit)
        rows = con.execute(q, (title,) if title else ()).fetchall()
        con.close()
        return rows
    except Exception:
        return None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _wait_bgft_settled(c, stage, timeout=300):
    """Before handing the SAME package to the console again, wait until BGFT is done with the
    last one. The spawned installer's rc=0 means "accepted", not "installed": the download and
    the registration run in the system's own queue afterwards, and a second submission on top of
    a live job is the duplicate-install shape that killed the console on 2026-08-25."""
    t0 = time.time()
    last = None
    unreadable = 0
    while time.time() - t0 < timeout:
        rows = _bgft_rows(c, 1, c.title)
        if rows is None:
            # A guard whose whole job is to withhold a dangerous action must fail CLOSED: "could
            # not read the ledger" is not "no job", it is "cannot tell". Say so once, keep
            # looking, and if it never becomes readable, do not submit again blind.
            unreadable += 1
            if unreadable == 1:
                rec(stage, "BGFT settled before the next install", "warn",
                    "could not read bgft.db - cannot tell whether the last install finished; waiting")
            time.sleep(5)
            continue
        st = rows[0][1] if rows else None
        if st != last:
            last = st
        if not rows or st in BGFT_DONE:
            rec(stage, "BGFT settled before the next install", True,
                "newest %s row: status %s" % (c.title, st if st is not None else "(no row)"))
            return True
        time.sleep(5)
    if last is None and unreadable:
        rec(stage, "BGFT settled before the next install", False,
            "could not read bgft.db for %ds - not submitting again blind" % timeout)
    else:
        rec(stage, "BGFT settled before the next install", False,
            "still status %s after %ds - not submitting again on top of it" % (last, timeout))
    return False


def _spawn_install(c, stage, label, name):
    """One install through the console's own async lane. Returns (ok, verdict_dict)."""
    q = "/api/engine/install-spawn?uri=" + urllib.parse.quote(c.url_for_pkg, safe="")
    if name is not None:
        q += "&name=" + urllib.parse.quote(name, safe="")
    ok, j, err = c.con(q, 90)
    if not (ok and isinstance(j, dict) and j.get("ok")):
        return rec(stage, "%s: accepted for spawning" % label, False,
                   err or json.dumps(j)[:110]), None
    rec(stage, "%s: accepted for spawning" % label, True, "spawn_rc=%s" % j.get("spawn_rc"))
    # 404 on spawn-result is the DOCUMENTED "no result yet" - server.c says so at the handler:
    # "Absent = it has not finished (or never ran)". Treating the first one as a dead console
    # failed the stage the moment an install took longer than the first poll, and - worse - it
    # returned WITHOUT calling spawn-cleanup, so the busy latch stayed set and every later stage
    # got a legitimate 409. One misread status code, three red stages and a console that looked
    # wedged. Keep polling; only a real transport error is a failure.
    for _ in range(45):
        time.sleep(2)
        ok, r, err = c.con("/api/engine/spawn-result", 20)
        if ok and isinstance(r, dict) and r.get("rc") is not None:
            good = r.get("rc") == "0x00000000"
            rec(stage, "%s: console verdict" % label, good,
                "rc=%s content_id=%s" % (r.get("rc"), (r.get("content_id") or "(empty)")[:34]))
            c.con("/api/engine/spawn-cleanup", 20)
            return good, r
        if not ok and "404" not in str(err):
            c.con("/api/engine/spawn-cleanup", 20)      # never leave the latch set behind us
            return rec(stage, "%s: console verdict" % label, False,
                       "the console stopped answering mid-install: %s" % err), None
    c.con("/api/engine/spawn-cleanup", 20)
    return rec(stage, "%s: console verdict" % label, False,
               "no verdict after 90s - the installer never reported back"), None


def stage4(c):
    print("\n%s== stage 4  install with NO display name (the proven-good lane) ==%s" % (B, X))
    _spawn_install(c, 4, "no name", None)


def stage5(c):
    print("\n%s== stage 5  install WITH a plain ASCII name ==%s" % (B, X))
    _spawn_install(c, 5, "ascii name", "Riptide GP2")


def stage6(c):
    print("\n%s== stage 6  install with a NON-ASCII name (content_name stress) ==%s" % (B, X))
    # This is the discriminating test for "did the game name reaching MetaInfo.content_name do it".
    # Real library entries contain these characters.
    _spawn_install(c, 6, "non-ascii name", u"Star Wars™: Racer Revenge®")


def stage7(c):
    print("\n%s== stage 7  the full PC-driven lane (queue -> console -> confirmed) ==%s" % (B, X))
    ok, j, err = http("%s/api/health" % c.pc, 20)
    if not ok:
        return rec(7, "the PC companion is running", False,
                   "%s - start PKG-MUTANT-SHOP.exe" % (err or ""))
    rec(7, "the PC companion is running", True, "v%s" % (j or {}).get("version"))
    # force ONLY when asked. force:true skips already_installed(), the space refusal and the
    # in-flight debounce - the three guards the companion has between a click and the console.
    # Running past them by default meant this stage never tested them, and could re-submit a
    # package the console was still installing. Without it, a title stage 4 just installed is
    # refused as already installed - which is the guard working, and is reported as such.
    body = {"title_id": c.title, "kind": "base", "install_key": c.key}
    if c.force:
        body["force"] = True
    ok, j, err = c.pcapi("/api/install", 90, body)
    if not (ok and isinstance(j, dict) and j.get("ok")):
        text = (err or "") + json.dumps(j or {})
        if "already" in text.lower() or "409" in text:
            return rec(7, "queued through the companion", "warn",
                       "refused: %s - the already-installed guard held; pass --force to test the "
                       "lane over a registered title" % (json.dumps(j or {})[:80] or err))
        return rec(7, "queued through the companion", False, err or json.dumps(j)[:120])
    if j.get("skipped"):
        # ok:true with nothing queued - the companion's already-installed guard. Without this the
        # stage waited seven minutes for a job that was never created and then called it a failure.
        return rec(7, "queued through the companion", "warn",
                   "not queued: %s - pass --force to test the lane over a registered title"
                   % (j.get("message") or j.get("reason") or "skipped")[:70])
    rec(7, "queued through the companion", True, "job %s" % ",".join(j.get("ids") or []))
    t0, last = time.time(), None
    while time.time() - t0 < 420:
        ok, q, err = http("%s/api/queue" % c.pc, 30)
        if not ok:
            return rec(7, "install finished", False, "the companion stopped answering: %s" % err)
        tasks = (q or {}).get("tasks", [])
        for t in tasks:
            k = (t.get("state"), t.get("msg"))
            if k != last:
                last = k
                print("       %6.0fs  %-12s %4s%%  %s"
                      % (time.time() - t0, t.get("state"), t.get("pct"), (t.get("msg") or "")[:74]))
        if tasks and all(t.get("state") in ("playable", "error", "canceled") for t in tasks):
            good = all(t.get("state") == "playable" for t in tasks)
            return rec(7, "install finished", good,
                       "; ".join((t.get("msg") or "")[:60] for t in tasks))
        time.sleep(2)
    return rec(7, "install finished", False, "still running after 7 minutes")


def stage8(c):
    print("\n%s== stage 8  /api/open - launches the console browser (panic suspect) ==%s" % (B, X))
    ok, j, err = c.con("/api/open?url=" + urllib.parse.quote(c.pc + "/", safe=""), 25)
    if not (ok and isinstance(j, dict)):
        return rec(8, "GET /api/open", False, err or "")
    rec(8, "GET /api/open", bool(j.get("ok")),
        "launched=%s rc=%s available=%s" % (j.get("launched"), j.get("rc"), j.get("available")))


def stage9(c):
    print("\n%s== stage 9  evidence on the console itself ==%s" % (B, X))
    ok, txt, err = c.con("/api/engine/log", 25)
    if ok and isinstance(txt, str):
        tail = [l for l in txt.strip().split("\n") if l][-8:]
        rec(9, "the console kept its own install log", True, "%d lines" % len(txt.split("\n")))
        for l in tail:
            print("       " + l[:132])
    else:
        rec(9, "the console kept its own install log", False, err or "")

    # bgft.db IS THE PROOF. app.db presence is not: a title already registered stays registered
    # whether or not the install we just ran did anything. What proves it is a fresh row whose
    # status is 1036 (full title) or 1026 (update/add-on), and whose `title` column names US.
    rows = _bgft_rows(c, 5)
    rec(9, "bgft.db pulled through our own file API", bool(rows),
        "%d rows" % len(rows) if rows else
        ("read fine, but tbl_downloads is empty" if rows == [] else
         "could not read it - not a verdict either way"))
    if not rows:
        return rec(9, "a recent install is recorded", False, "no rows read from tbl_downloads")
    print("       %-11s %-6s %-14s %s" % ("title_id", "status", "bytes", "recorded by"))
    for t, st, tr, ln, who in rows:
        print("       %-11s %-6s %-14s %s" % (t, st, "%s/%s" % (tr, ln), who))
    top = rows[0]
    rec(9, "the newest row completed", top[1] in (1026, 1036),
        "status %s (1036=full title, 1026=update, 1009=in flight, 1021=failed)" % top[1])
    # What this column actually holds is the DISPLAY NAME of the thing installed - the name our
    # lane passes in - so "Riptide GP2" here is the pass, not a miss. It used to demand the string
    # "MUTANT", which nothing has written there since the lane started sending real game names.
    #
    # The independence proof is not in this column anyway: it is the verdict's own authid and via
    # fields (stage 4-7), plus the fact that no third-party port was ever open. What this row can
    # honestly show is that SOMETHING named the install, which an empty column would not.
    title = str(top[4] or "")
    rec(9, "the row carries the name our lane sent", bool(title.strip()),
        "title column = %r%s" % (title, "" if title.strip() else " - nothing named the install"))


def forensics(c):
    """Read what the console wrote before it died, and change nothing.

    /data/pkg-mutant-shop/install.log is opened append-only with O_SYNC and is only trimmed at
    startup, and then only above 512 KB - so the last lines written before a crash are still on
    disk after it. This is the only first-hand account of what the console was doing, and it is
    worth reading BEFORE anything else touches the machine.
    """
    print("%sforensics: reading what the console wrote before it stopped%s\n" % (Y, X))
    ok, txt, err = c.con("/api/engine/log", 30)
    if not ok:
        print("  the console is not answering: %s" % err)
        return 1
    lines = [l for l in str(txt).split("\n") if l.strip()]
    if not lines:
        print("  the log is empty - nothing has been installed since it was last trimmed")
        return 0
    # Everything since the boot before last, so the run that crashed and the run after it are both
    # visible and can be told apart.
    boots = [i for i, l in enumerate(lines) if "BOOT:" in l]
    start = boots[-2] if len(boots) >= 2 else 0
    print("  %d lines total, showing from the previous boot marker:\n" % len(lines))
    for l in lines[start:]:
        mark = "  "
        low = l.lower()
        if "boot:" in low:
            mark = Y + "**" + X
        elif "failed" in low or "refused" in low or "timeout" in low:
            mark = R + "!!" + X
        print("  %s %s" % (mark, l[:150]))
    # A request written but never answered by a verdict is the signature of an installer that died.
    last_req = max([i for i, l in enumerate(lines) if "install: requested" in l] or [-1])
    last_verdict = max([i for i, l in enumerate(lines) if "install: verdict" in l] or [-1])
    print("")
    if last_req > last_verdict:
        print("  %sThe last install was REQUESTED and never produced a verdict - the spawned"
              " installer did not survive it.%s" % (R, X))
        for l in lines[last_req:]:
            print("     " + l[:150])
    else:
        print("  %sEvery install that was requested also produced a verdict.%s" % (G, X))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default=None, help="PS5 address (default: from config.json)")
    ap.add_argument("--shop-port", type=int, default=8710)
    # NOT loopback. This address is handed to the CONSOLE, and on the console 127.0.0.1 is the
    # console - so the fetch failed with 0x80B22404 and read as an install-engine fault. Default to
    # the address this PC actually answers on from the console's side.
    ap.add_argument("--companion", default=None,
                    help="base URL of the PC companion AS THE CONSOLE SEES IT "
                         "(default: this PC's LAN address)")
    ap.add_argument("--title", default="CUSA02365")
    ap.add_argument("--key", default="Riptide-GP2-CUSA02365.pkg")
    ap.add_argument("--stage", default=None, help='e.g. "0-3" or "4" - default is 0-4')
    ap.add_argument("--all", action="store_true",
                    help="stages 0-8: adds the name-passthrough installs (5, 6), the PC-driven "
                         "lane (7) and /api/open (8)")
    ap.add_argument("--force", action="store_true",
                    help="stage 7 passes force:true (re-installs over a registered title, "
                         "skipping the companion's guards)")
    ap.add_argument("--forensics", action="store_true",
                    help="read the console's install log and stop. Run this FIRST after a crash.")
    a = ap.parse_args()

    if not a.ip:
        a.ip, src = console_ip_from_config()
        if a.ip:
            print("console address %s from %s" % (a.ip, src))
    if not a.ip:
        sys.exit("no --ip, and no console address in any of:\n  " + "\n  ".join(config_candidates()))

    lo, hi = 0, (8 if a.all else 4)
    if a.stage:
        parts = a.stage.split("-")
        lo = int(parts[0])
        hi = int(parts[-1])

    # Resolve the companion URL BEFORE Ctx copies it into c.pc - otherwise every PC-side call in
    # the run goes to the string "None".
    if not a.companion:
        a.companion = "http://%s:8710" % _route_ip(a.ip)
    host = urllib.parse.urlsplit(a.companion).hostname or ""
    if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        raise SystemExit(
            "--companion is %s, which the console cannot reach - on the console that address IS the\n"
            "console. Pass this PC's LAN address, e.g. --companion http://%s:8710"
            % (a.companion, _route_ip(a.ip)))
    c = Ctx(a)
    c.force = bool(a.force)
    c.url_for_pkg = "%s/library/%s" % (a.companion.rstrip("/"), a.key)
    print("%sPKG MUTANT SHOP - console verification%s" % (B, X))
    print("console %s:%d   companion %s   stages %d-%d\n" % (a.ip, a.shop_port, a.companion, lo, hi))

    if a.forensics:
        return forensics(c)

    stages = [stage0, stage1, stage2, stage3, stage4, stage5, stage6, stage7, stage8]
    # If the shop is not there, every later stage fails identically with the same connection error
    # and buries the one fact that matters. Say it once and stop.
    if lo == 0 and not port_open(a.ip, a.shop_port, 3.0):
        stage0(c)
        print("\n%sThe shop is not answering on %s:%d. Load PKG-MUTANT-SHOP.elf from Payload "
              "Manager, then run this again.%s" % (Y, a.ip, a.shop_port, X))
        return 1
    installed_once = False
    for n in range(lo, min(hi, 8) + 1):
        if STOP:
            print("\n%sSTOPPED: the console stopped responding. The last stage that ran is the "
                  "one to investigate.%s" % (R, X))
            break
        # Never hand the same package to the console while BGFT may still hold the last one.
        if n in (5, 6, 7) and installed_once and not _wait_bgft_settled(c, n):
            print("\n%sSTOPPED before stage %d: BGFT has not finished the previous install of %s."
                  " Submitting it again now is the duplicate-install shape.%s" % (R, n, c.title, X))
            break
        stages[n](c)
        if n in (4, 5, 6, 7):
            installed_once = True
        # Only the install stages onward can plausibly take the console down, and the gate costs a
        # round trip - so it starts where the risk starts.
        if n >= 3 and not STOP:
            alive(c, n, "after stage %d" % n)
    # Stage 9 is pure evidence-gathering and read-only, so it always runs when anything installed
    # and the console is still there. It is what turns "the app said it worked" into proof.
    if not STOP and hi >= 4:
        stage9(c)

    print("\n%s== summary ==%s" % (B, X))
    bad = [r for r in RESULTS if r["ok"] is False]
    warn = [r for r in RESULTS if r["ok"] == "warn"]
    print("  %d checks, %s%d failed%s, %d warnings"
          % (len(RESULTS), R if bad else G, len(bad), X, len(warn)))
    for r in bad:
        print("   %sstage %d  %s%s  %s" % (R, r["stage"], r["name"], X, r["detail"][:110]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

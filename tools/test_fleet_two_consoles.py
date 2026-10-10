# -*- coding: utf-8 -*-
"""Two-console regression suite for the PC companion, run from source on a spare port.

SUITE 1 - one PS5 configured, the PS4 awake on the LAN. Auto-discovery must ADD the PS4 as a second
          console and leave the configured PS5 completely alone, including ps5_ip.
SUITE 2 - both consoles configured (the user's real setup). Discovery must not run at all, install
          targeting must pick the console that can actually install, an explicit target must win,
          and a PS5 package must be refused for a PS4.

Nothing is installed: every install probe is queued (held), and the server is killed afterwards.
companion/config.json is saved and restored. The shipped exe keeps its own config on the Desktop.
"""
import json
import os
import socket
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = r"C:\PS5 Projects\PS4 PKG STORE\pkg-mutant-shop"
CFG = os.path.join(REPO, "companion", "config.json")
BAK = CFG + ".regress-backup"
PORT = 8791
BASE = "http://127.0.0.1:%d" % PORT
DEAD_IP = "10.0.0.250"        # nothing answers here - used to force the auto-discovery path


def find_consoles():
    """Ask the network where the consoles are, instead of believing a constant.

    These two addresses used to be hardcoded, and the day the PS4 took a new DHCP lease this whole
    suite began failing on a console that was working perfectly - which is the exact fault 3.67.0
    exists to fix everywhere else in the app. A test that asserts an address it invented is testing
    the network's lease table.
    """
    import concurrent.futures as _f
    here = socket.gethostbyname(socket.gethostname())
    base = here.rsplit(".", 1)[0]

    def probe(i, timeout=1.0):
        ip = "%s.%d" % (base, i)
        try:
            with urllib.request.urlopen("http://%s:8710/api/health" % ip, timeout=timeout) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            if d.get("on_console"):
                return ip, ("ps4" if str(d.get("platform") or "").lower() == "ps4" else "ps5")
        except Exception:
            pass
        return None

    # TWO SWEEPS, THE SECOND PATIENT. One pass at a second per address across 254 of them missed a
    # console that was demonstrably awake and answering, and the suite then SKIPPED - which reads
    # exactly like a pass. That is how a perturbation that forced a real cross-platform repoint came
    # back green: the suite had not run at all. A sweep that decides whether anything gets tested
    # cannot be the flakiest part of the file.
    out = {}
    for timeout in (1.0, 3.0):
        with _f.ThreadPoolExecutor(max_workers=64) as ex:
            for got in ex.map(lambda i: probe(i, timeout), range(1, 255)):
                if got and got[1] not in out:
                    out[got[1]] = got[0]
        if len(out) >= 2:
            break
    return out


_found = find_consoles()
PS5_IP = _found.get("ps5")
PS4_IP = _found.get("ps4")
if not PS5_IP or not PS4_IP:
    print("test_fleet_two_consoles: needs both consoles awake and running the shop "
          "(found %s) - skipping" % (_found or "none"))
    sys.exit(0)
print("  consoles found: PS5 %s, PS4 %s" % (PS5_IP, PS4_IP))
REAL_KEY = "Riptide-GP2-CUSA02365.pkg"

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  -> " + str(detail)[:150]) if detail else ""))


def req(path, body=None, timeout=60):
    """Returns (status, parsed-or-text). A 4xx is a legitimate answer here, not an exception."""
    if body is None:
        r = urllib.request.Request(BASE + path)
    else:
        r = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                   headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            code = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        code = e.code
    try:
        return code, json.loads(raw)
    except Exception:
        return code, raw


def write_cfg(consoles, ps5_ip, ps4_ip=""):
    # BOTH address fields, always. reconcile_consoles folds these two settings into the console
    # list and removes the entry a cleared address owns - which is correct and is what it is
    # for. An earlier version of this file wrote the list without ps4_ip, the PS4 entry was duly
    # removed, and the suite only passed because auto-discovery added it back - which in turn
    # only happened while the PS5 was switched off. A test whose result depends on which console
    # is powered is not a test.
    json.dump({
        "companion": {"host": "127.0.0.1", "port": PORT},
        "ftp": {"port": 2121},
        "ps5_ip": ps5_ip,
        "ps4_ip": ps4_ip,
        "consoles": consoles,
        "library": {},
        "device": {"id": "regress0000000001"},
    }, open(CFG, "w"), indent=2)


def start():
    # PMS_TEST_MODE: this companion serves every route and announces itself to NOTHING.
    #
    # Without it, the instance this suite starts told both real consoles "the PC is at
    # 10.0.0.76:8791" every eight seconds - and the console kept that address after the suite
    # exited and the port died. The owner's PS5 was still handing its browser that dead PC hours
    # later. A test that leaves a trace on the hardware is a test that has changed the thing it
    # was measuring.
    p = subprocess.Popen([sys.executable, os.path.join(REPO, "companion", "server.py")],
                         cwd=os.path.join(REPO, "companion"),
                         env=dict(os.environ, PMS_TEST_MODE="1"),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(45):
        time.sleep(2)
        try:
            req("/api/health", timeout=6)
            return p
        except Exception:
            if p.poll() is not None:
                print("SERVER DIED:\n" + (p.communicate()[0] or "")[-2500:])
                return None
    print("SERVER NEVER CAME UP")
    return None


def await_discovery(want=2, secs=90):
    """Wait until the console list has grown, or give up. Returns the list.

    THIS SUITE WAS A COIN TOSS WITHOUT IT. Discovery is the expensive half of track_consoles, it is
    rate-limited by its caller, and it runs on a background thread - so asserting as soon as
    /api/health answers asks what the config looked like BEFORE the thing being tested happened.
    The same code gave a pass and a failure on consecutive runs, and a perturbation that forced a
    real cross-platform repoint came back green because the pass had not run yet. A test that only
    sometimes exercises its subject reports on nothing.
    """
    cons = []
    for _ in range(int(secs / 3)):
        try:
            _, c = req("/api/consoles")
            cons = c.get("consoles") or []
            if len(cons) >= want:
                return cons
        except Exception:
            pass
        time.sleep(3)
    return cons


def stop(p):
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()


proc = None
try:
    shutil.copy2(CFG, BAK)

    # ======================================================== SUITE 1
    print("\n=== SUITE 1: the configured console unreachable, another awake on the LAN ===")
    # The configured address is one nothing answers on, deliberately: auto-discovery only runs
    # when the configured console cannot be reached, and that is the only state in which the
    # bug this suite exists for - discovery OVERWRITING the configured console rather than
    # adding the one it found - can happen at all.
    # AN IDENTITY THAT MATCHES NO REAL CONSOLE, deliberately - and this is the whole reason this
    # entry survives. track_consoles is ALLOWED to follow a console that moved, and it now does so
    # even for an entry that has no id of its own (see test_console_tracker.py). So a nameless PS5
    # entry at a dead address would be followed straight to the owner's real PS5, which is sitting on
    # this very network - and this suite would be asserting the opposite of the feature.
    # Giving it an id nothing answers to makes the refusal happen for the RIGHT reason: identity says
    # the console on the network is not this one. The result no longer depends on which consoles
    # happen to be powered, which is this file's own rule two functions up.
    write_cfg([{"id": "ps5", "name": "PS5", "ip": DEAD_IP, "platform": "ps5",
                "console_id": "dead0000deadbeef",
                "ftp_port": 2121, "dpi_port": 12800}], DEAD_IP)
    proc = start()
    if proc:
        # The PS4 appearing IS the discovery pass having run; everything below is about what that
        # pass did and must not be asked before it.
        cons = await_discovery(2)
        by_plat = {x.get("platform"): x for x in cons}
        check("discovery ran at all - the PS4 was found within the wait",
              len(cons) >= 2, "%d console(s): %s" % (len(cons), json.dumps(cons)))
        # WHAT THIS SUITE GUARDS IS "ADD, NEVER REPLACE" - and for a while it asserted something
        # else: that an entry whose saved id matches nothing on the network is left on its dead
        # address for ever. That was the behaviour once, it was measured to be wrong on the owner's
        # own PS4 (the console had regenerated its id, so the saved pair matched nothing and the
        # companion asked a dead address for days), and following such an entry is now deliberate -
        # see track_consoles and test_console_tracker.py, which covers it with no network at all.
        #
        # Worse, the old assertion only held while the real PS5 was switched OFF: powered on, it is
        # the one unaccounted-for PS5 and the entry is correctly followed to it. This file's own
        # rule, written thirty lines up, is that a test whose result depends on which console is
        # powered is not a test - and that rule applied to this check too.
        #
        # So these assert the part that is true however the room is arranged: discovery ADDS, it
        # never drops an entry, and it never mixes the platforms up - which is the actual bug this
        # suite was written for, and the shape of "discovery adopted a peer PC".
        check("the configured PS5 entry still exists - discovery never drops one",
              "ps5" in by_plat, json.dumps(cons))
        check("the PS4 was ADDED as a second console",
              by_plat.get("ps4", {}).get("ip") == PS4_IP, "%d console(s)" % len(cons))
        check("the PS5 entry was never pointed at the PS4",
              by_plat.get("ps5", {}).get("ip") != PS4_IP, json.dumps(cons))
        _, h = req("/api/health")
        check("ps5_ip was not repointed at the PS4",
              h.get("ps5_ip") != PS4_IP, str(h.get("ps5_ip")))
        # An entry that IS followed must be followed to a real PS5, so the address is either the one
        # configured or one a PS5 actually answered on. Never a PS4, never invented.
        check("ps5_ip is either the configured address or a discovered PS5",
              h.get("ps5_ip") in (DEAD_IP, PS5_IP) or not PS5_IP,
              "%s (configured %s, PS5 found at %s)" % (h.get("ps5_ip"), DEAD_IP, PS5_IP))
    stop(proc)
    proc = None

    # ======================================================== SUITE 2
    print("\n=== SUITE 2: both consoles configured (the real setup) ===")
    write_cfg([{"id": "ps5", "name": "PS5", "ip": PS5_IP, "platform": "ps5",
                "ftp_port": 2121, "dpi_port": 12800},
               {"id": "ps4", "name": "PS4", "ip": PS4_IP, "platform": "ps4",
                "ftp_port": 2121}], PS5_IP, PS4_IP)
    proc = start()
    if proc:
        _, c = req("/api/consoles")
        cons = {x["id"]: x for x in (c.get("consoles") or [])}
        check("both consoles present", set(cons) == {"ps5", "ps4"}, sorted(cons))
        check("the PS5 address was not rewritten", cons.get("ps5", {}).get("ip") == PS5_IP,
              cons.get("ps5", {}).get("ip"))
        check("the PS4 address was not rewritten", cons.get("ps4", {}).get("ip") == PS4_IP,
              cons.get("ps4", {}).get("ip"))
        # Reachability is a fact about the room, not about this code: assert only that each
        # console is reported on, with a boolean, whichever of them is powered today.
        check("each console reports a reachability, whichever is powered",
              isinstance(cons.get("ps4", {}).get("online"), bool)
              and isinstance(cons.get("ps5", {}).get("online"), bool),
              {k: v.get("online") for k, v in cons.items()})

        _, d = req("/api/devices", timeout=60)
        check("/api/devices keeps the legacy `ps5` key", "ps5" in d, sorted(d.keys()))
        check("/api/devices lists both consoles", len(d.get("consoles") or []) == 2,
              len(d.get("consoles") or []))
        ps4row = [x for x in (d.get("consoles") or []) if x.get("platform") == "ps4"]
        check("the PS4 row carries its drives", ps4row and len(ps4row[0].get("devices") or []) > 0,
              len(ps4row[0].get("devices") or []) if ps4row else 0)

        st, r = req("/api/install", {"install_key": REAL_KEY, "title_id": "CUSA02365",
                                     "name": "Regression Probe", "kind": "base", "mode": "queued", "dry_run": True})
        # EVERY INSTALL PROBE BELOW IS A DRY RUN, and that is not a convenience.
        #
        # This suite used to POST real installs at whichever consoles were awake - with force:true,
        # so that "already installed" could not even short-circuit them - and the owner's PS5
        # stopped answering twice in one hour, each time within minutes of this file running. A
        # test that breaks the machine it is run beside is not a test, and these checks never
        # needed the install: what they are about is TARGETING, which /api/install decides and
        # reports before it touches anything.
        #
        # No console is named here on purpose: the answer has to come from the resolver rather than
        # from the caller, which is the whole point of the check.
        check("no console named -> exactly one console is chosen",
              isinstance(r.get("consoles"), list) and len(r.get("consoles") or []) == 1,
              json.dumps(r)[:160])

        st, r = req("/api/install", {"install_key": REAL_KEY, "title_id": "CUSA02365",
                                     "name": "Regression Probe", "kind": "base", "mode": "queued",
                                     "console": "ps5", "dry_run": True})
        check("explicit console=ps5 is honoured", r.get("consoles") == ["ps5"], json.dumps(r)[:160])

        st, r = req("/api/install", {"install_key": REAL_KEY, "title_id": "PPSA00000",
                                     "name": "PS5 Probe", "kind": "base", "mode": "queued",
                                     "console": "ps4", "dry_run": True})
        check("a PS5 package aimed at the PS4 is refused with a reason",
              r.get("ok") is False and "PS4" in str(r.get("error", "")), json.dumps(r)[:200])

        st, r = req("/api/install", {"install_key": REAL_KEY, "title_id": "CUSA02365",
                                     "name": "Both Probe", "kind": "base", "mode": "queued",
                                     "console": "all", "dry_run": True})
        check("console=all targets both", sorted(r.get("consoles") or []) == ["ps4", "ps5"],
              json.dumps(r)[:160])

        code, page = req("/", timeout=60)
        check("the page is served", isinstance(page, str) and len(page) > 200000,
              len(page) if isinstance(page, str) else type(page).__name__)
finally:
    stop(proc)
    if os.path.exists(BAK):
        shutil.move(BAK, CFG)
        print("\nrestored companion/config.json")

bad = [n for n, ok in results if not ok]
print("\n%d checks, %d failed" % (len(results), len(bad)))
for n in bad:
    print("  FAILED: " + n)
sys.exit(1 if bad else 0)

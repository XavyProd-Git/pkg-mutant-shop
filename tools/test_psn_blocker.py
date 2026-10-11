# -*- coding: utf-8 -*-
"""Does the companion's DNS actually answer, and actually block PlayStation Network?

Runs companion/server.py from source with the PSN blocker on, then asks it real DNS questions the
way a console would: one Sony name that must come back NXDOMAIN, and one ordinary name that must be
forwarded and answered. Both matter equally - a blocker that breaks ordinary DNS is what turned the
console's browser into WV-33920-7 for every address.

companion/config.json is saved and restored. The shipped exe must be stopped first, because both
would want UDP 53 on the same address.
"""
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(REPO, "companion", "config.json")
BAK = CFG + ".psntest-backup"
HTTP_PORT = 8792

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  -> " + str(detail)[:130]) if detail else ""))


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    finally:
        s.close()


def query(server, name, timeout=5.0):
    """One A query. Returns (rcode, answer_count) or None on no reply."""
    qid = 0x4242
    head = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    pkt = head + qname + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(pkt, (server, 53))
        data, _ = s.recvfrom(4096)
    except Exception:
        return None
    finally:
        s.close()
    if len(data) < 12:
        return None
    _, flags, _, ancount, _, _ = struct.unpack(">HHHHHH", data[:12])
    return flags & 0x000F, ancount


proc = None
try:
    ip = lan_ip()
    print("this PC on the LAN: %s\n" % ip)
    shutil.copy2(CFG, BAK)
    json.dump({
        "companion": {"host": "127.0.0.1", "port": HTTP_PORT},
        "ftp": {"port": 2121},
        "psn_block": {"enabled": True, "listen_ip": ip, "upstream": "1.1.1.1",
                      "extra_blocked": []},
        "consoles": [], "library": {}, "device": {"id": "psntest00000001"},
    }, open(CFG, "w"), indent=2)

    # Announces itself to nothing - see the note in test_fleet_two_consoles.py.
    proc = subprocess.Popen([sys.executable, os.path.join(REPO, "companion", "server.py")],
                            cwd=os.path.join(REPO, "companion"),
                            env=dict(os.environ, PMS_TEST_MODE="1"),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    up = False
    for _ in range(40):
        time.sleep(2)
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/api/health" % HTTP_PORT, timeout=5).read()
            up = True
            break
        except Exception:
            if proc.poll() is not None:
                print("SERVER DIED:\n" + (proc.communicate()[0] or "")[-2500:])
                raise SystemExit(1)
    if not up:
        raise SystemExit("server never came up")

    with urllib.request.urlopen("http://127.0.0.1:%d/api/psn-block" % HTTP_PORT, timeout=15) as r:
        st = json.loads(r.read().decode())
    # WHOSE BLOCKER IS ANSWERING. There is one port 53 on this machine, and the companion the
    # user is running already owns it - so this test's own copy cannot bind it and reports
    # WinError 10048. That is not a fault in the blocker: the DNS checks below still exercise a
    # real blocker at this PC's address, just the other one. Failing here turned "the app is
    # already running" into two red lines that looked like a regression, twice.
    #
    # So: if this instance bound the port, hold it to everything. If it could not, say which
    # process is under test and keep checking the behaviour that is still observable - the
    # refusals and the forwards - because those are what the suite is actually for.
    _own = (st.get("listening_on") == ip)
    _busy = "10048" in str(st.get("error") or "")
    if _own or not _busy:
        check("the blocker reports itself listening", _own,
              json.dumps({k: st.get(k) for k in ("listening_on", "upstream", "error")}))
    else:
        print("  [ .. ] port 53 is held by the companion already running - testing THAT blocker")
    check("it tells you what to set the console's DNS to", st.get("set_console_dns_to") == ip,
          st.get("set_console_dns_to"))

    # ---- the names that must be refused: the console's patch-check and update hosts
    for host in ("gs2.ww.prod.dl.playstation.net",
                 "dus01.ps4.update.playstation.net",
                 "playstation.net",
                 "account.sonyentertainmentnetwork.com"):
        got = query(ip, host)
        check("BLOCKED  %s" % host, got is not None and got[0] == 3,
              "rcode=%s answers=%s" % (got if got else ("no reply",)))

    # ---- the names that must still work, or the console's browser dies like it did with 127.0.0.1
    for host in ("example.com", "cloudflare.com"):
        got = query(ip, host)
        check("ALLOWED  %s" % host, got is not None and got[0] == 0 and got[1] > 0,
              "rcode=%s answers=%s" % (got if got else ("no reply",)))

    # ---- and it must not have broken the ordinary HTTP side
    with urllib.request.urlopen("http://127.0.0.1:%d/api/psn-block" % HTTP_PORT, timeout=15) as r:
        st2 = json.loads(r.read().decode())
    # Only this instance's own counters mean anything here; the other companion's counters live
    # in the other process and this one has never seen a packet.
    if _own:
        check("it counted what it refused and what it passed",
              st2.get("blocked", 0) >= 4 and st2.get("forwarded", 0) >= 2,
              "blocked=%s forwarded=%s last=%s" % (st2.get("blocked"), st2.get("forwarded"),
                                                   st2.get("last_blocked")))
    else:
        print("  [ .. ] counters not checked - they belong to the other process")
finally:
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    if os.path.exists(BAK):
        shutil.move(BAK, CFG)
        print("\nrestored companion/config.json")

bad = [n for n, ok in results if not ok]
print("\n%d checks, %d failed" % (len(results), len(bad)))
for n in bad:
    print("  FAILED: " + n)
sys.exit(1 if bad else 0)

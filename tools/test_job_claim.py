# -*- coding: utf-8 -*-
"""Race the PS4's install slot on real hardware, and prove only one caller can take it.

WHAT THIS CATCHES. Every route that starts a transfer used to read "is an install running" under the
lock, RELEASE the lock, register a task with the console, and only then take the lock again to fill
the slot in. Two requests inside that gap both read "not busy", both registered, and the second
overwrote the slot - so the first task went on downloading with nothing following it, while
progress, the finished check and the cancel button all described the second package.

Measured on the owner's PS4 at 3.64.0, twelve concurrent presses of Start queue, in the console's
own install log:

    without the claim   12 x "install: register failed ... RACEPROBE"   in the same second
    with the claim       1 x

NOTHING IS INSTALLED BY THIS TEST. The url it queues has no .pkg extension, and BGFT reads the
extension out of the url and refuses such a task at REGISTRATION with 0x80990033 ("Not supported
extension") - measured, and recorded in server_ps4.c. So every attempt dies before a byte moves. No
console-owned task is touched: the only task this could ever create is one of ours, and it never
gets that far.

It also checks the three things that are easy to get wrong once a claim exists:
  * the losers are turned away rather than queued behind
  * the row is HELD again afterwards - the abort has to put back what it took
  * the slot is not left wedged, which would need the payload reloaded to clear

    python tools/test_job_claim.py <console-ip> [concurrency]
"""
import json
import sys
import threading
import urllib.error
import urllib.request

def _ps4_ip():
    """The PS4's address, ASKED OF THE COMPANION - never a constant in a test.

    This file had 10.0.0.87 baked in as its default. The PS4 has been at 10.0.0.86 since it moved
    house, so every run since printed "no PS4 answering ... skipping" and passed. A test that
    skips itself by default is not a test, and a hardcoded console address is the exact thing this
    project has a standing rule against.
    """
    try:
        with urllib.request.urlopen("http://127.0.0.1:8710/api/devices", timeout=8) as f:
            d = json.loads(f.read().decode())
    except Exception:
        return ""
    for c in (d.get("consoles") or []):
        if str(c.get("platform") or "").lower() == "ps4" and c.get("ip"):
            return str(c["ip"])
    return ""


IP = sys.argv[1] if len(sys.argv) > 1 else _ps4_ip()
N = int(sys.argv[2]) if len(sys.argv) > 2 else 12
if not IP:
    print("test_job_claim: no PS4 in the companion's device list, and none given - skipping")
    sys.exit(0)
BASE = "http://%s:8710" % IP
HDRS = {"Content-Type": "application/json", "Origin": BASE,
        "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
ROW = "console-local"


def call(path, body=None, method=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=HDRS, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, "%s: %s" % (type(e).__name__, str(e)[:70])


def clear():
    call("/api/queue/clear", {}, "POST")
    call("/api/queue/%s/dismiss" % ROW, {}, "POST")


def job():
    _, b = call("/api/engine/job")
    try:
        return json.loads(b)
    except Exception:
        return {}


def main():
    st, _ = call("/api/health")
    if st != 200:
        print("no PS4 answering on %s - skipping" % BASE)
        return 0

    clear()
    # BUSY IS THE PAYLOAD'S OWN RULE, not `active`. job_claim() treats a row whose state is
    # "installed" or "error" as free, and after any install the slot keeps that finished row -
    # including the icon repair the payload does at boot. Reading `active` alone made this refuse to
    # run on any console that had ever installed anything, which is every console.
    _j = job()
    if _j.get("active") and _j.get("state") not in ("installed", "error"):
        print("FAIL: something is already installing on this console - not racing it")
        return 1

    bad = "%s/pkgfile/nosuchtoken" % BASE          # no .pkg -> refused at registration
    st, b = call("/api/install", {"url": bad, "name": "race probe", "mode": "queued",
                                  "content_id": "UP0000-CUSA00000_00-RACEPROBE0000000",
                                  "size": 1024, "type": "PS4GD"}, "POST")
    if '"held":true' not in b:
        print("FAIL: could not queue the probe row: %s %s" % (st, b[:160]))
        clear()
        return 1

    results = [None] * N
    barrier = threading.Barrier(N)

    def worker(i):
        barrier.wait()                    # everyone lets go at the same instant
        results[i] = call("/api/queue/start", {}, "POST")

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    started = sum(1 for _, b in results if '"started":1' in b)
    tried = sum(1 for _, b in results if '"error"' in b)
    away = sum(1 for _, b in results if '"started":0' in b and '"error"' not in b)

    after = job()
    _, q = call("/api/queue")
    held_again = '"state":"held"' in q
    wedged = bool(after.get("active"))

    print("  %d concurrent Start presses" % N)
    print("    began an install        : %d   (must be 0 - that url cannot register)" % started)
    print("    reached the registration: %d   (must be 1)" % tried)
    print("    turned away by the claim: %d" % away)
    print("    the row is held again   : %s   (the abort has to put it back)" % held_again)
    print("    the slot is wedged      : %s" % wedged)

    clear()

    bad_ = []
    if started:
        bad_.append("an install began from a url that cannot register")
    if tried != 1:
        bad_.append("%d callers reached the registration, not 1 - the slot is not exclusive" % tried)
    if away != N - 1:
        bad_.append("%d turned away, expected %d" % (away, N - 1))
    if not held_again:
        bad_.append("the row was not restored - the abort lost it")
    if wedged:
        bad_.append("the slot is still active - it is wedged")

    if bad_:
        for m in bad_:
            print("  FAIL: %s" % m)
        return 1
    print("  OK: one caller takes the slot, the rest are refused, the row survives.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

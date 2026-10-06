# -*- coding: utf-8 -*-
"""Put the freshly built payload on every console, and prove each one is running it.

WHY THIS EXISTS. Three artifacts are built from one source tree, and until now only the PC's was
shipped by a tool that checks its work - the two consoles were updated by hand, one curl at a time,
remembering a different procedure for each. That is how a console ends up quietly running an older
build than the PC talking to it, which this project has already paid for twice: a 3.18.2 upload that
ran 3.18.0, and two PCs on "the same" version behaving differently.

    python tools/push_consoles.py              # push to every console the companion knows
    python tools/push_consoles.py --check      # say what each console runs; push nothing
    python tools/push_consoles.py --only ps4
    python tools/push_consoles.py --force      # push even while an install is running

WHAT IT REFUSES TO DO. It will not interrupt a running install. Loading a new payload kills the
shop, and the shop is what is feeding the console the bytes - so a push during a download is a
half-installed game. Both consoles are asked what they are doing first, and a busy one is skipped
with a sentence rather than forced. --force is there for when you mean it.

IT NEVER TOUCHES A CONSOLE-OWNED TRANSFER. Nothing here starts, resumes or cancels a BGFT task: it
reads the shop's own job state to decide whether to wait, and that is all. A firmware update or a
Store download is not ours to stop, and stopping one is how a jailbreak is lost.

THE TWO CONSOLES ARE DIFFERENT AND THAT IS NOT HIDDEN.
  * PS5 - delegated to companion/deploy.py, which knows the traps: Payload Manager resolves
    /loadpayload by BASENAME to its own registered copy, so the upload has to go over that exact
    file, and "the shop stopped" means a REFUSED connect, not one timed-out request.
  * PS4 - GoldHEN's payload loader on :9090. The POST of the ELF *is* the probe; a bare TCP connect
    to that port stops the loader listening, so this never opens one.
"""
import argparse
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PS5_ELF = os.path.join(ROOT, "ps5-app", "onconsole", "PKG-MUTANT-SHOP.elf")
PS4_ELF = os.path.join(ROOT, "ps4-app", "onconsole", "PKG-MUTANT-SHOP-PS4.elf")
GOLDHEN_LOADER_PORT = 9090
SHOP_PORT = 8710

G = "\033[92m"
R = "\033[91m"
Y = "\033[93m"
Z = "\033[0m"


def want_version():
    """The one version every artifact was stamped with - companion/server.py is the source."""
    s = io.open(os.path.join(ROOT, "companion", "server.py"), encoding="utf-8").read()
    i = s.index('VERSION = "') + len('VERSION = "')
    return s[i:s.index('"', i)]


def get(url, timeout=6):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def consoles():
    """Ask the running companion, then fall back to config.json.

    The companion's list is the better source: it is what the app itself is talking to, it carries
    the platform of each console, and on a zero-config PC it holds consoles that were discovered
    and never written down.
    """
    d = get("http://127.0.0.1:%d/api/consoles" % SHOP_PORT)
    if d and d.get("consoles"):
        return d["consoles"]
    # CONFIG_PATH is "beside server.py, or beside the exe when frozen" - so a PC that runs the
    # SHIPPED app keeps its console list next to that exe, and the repo's own config.json is the
    # developer one. Both are tried, newest first, because this tool is run from the repo on a
    # machine where the shipped app is what has actually been configured.
    cfg = {}
    cands = [os.path.join(ROOT, "companion", "config.json"),
             os.path.join(os.path.expanduser("~"), "Desktop", "PKG MUTANT SHOP", "config.json")]
    cands = [p for p in cands if os.path.exists(p)]
    cands.sort(key=os.path.getmtime, reverse=True)
    for p in cands:
        try:
            d = json.load(io.open(p, encoding="utf-8"))
        except Exception:
            continue
        if d.get("consoles") or d.get("ps5_ip") or d.get("ps4_ip"):
            cfg = d
            break
    out = list(cfg.get("consoles") or [])
    if not out:
        for key, plat in (("ps5_ip", "ps5"), ("ps4_ip", "ps4")):
            if cfg.get(key):
                out.append({"id": plat, "name": plat.upper(), "ip": cfg[key], "platform": plat})
    return out


def health(ip):
    return get("http://%s:%d/api/health" % (ip, SHOP_PORT))


def built_at(h):
    """When the payload a console is running was COMPILED, as a timestamp, or None.

    A VERSION IS NOT A BUILD. Both consoles can report 3.65.0 and be running code from two hours
    ago - that is the whole reason /api/health grew a build fingerprint on the PC side, after two
    machines on "the same version" behaved differently. The payloads stamp __DATE__ " " __TIME__
    into `built`, which is the same fact in the form the consoles already had, so a rebuilt ELF is
    recognised as new even when the version string has not moved.
    """
    try:
        return time.mktime(time.strptime(str(h.get("built") or "").strip(), "%b %d %Y %H:%M:%S"))
    except Exception:
        return None


def is_current(h, elf):
    """Is this console running THIS build - not merely this version?

    Compares the console's compile stamp against the ELF's own mtime, which is when that file was
    produced. Unknown either way means "push": doing the work twice costs a minute, and skipping it
    wrongly is how a console quietly runs last week's code.
    """
    b = built_at(h)
    if b is None or not os.path.exists(elf):
        return False
    return b + 120 >= os.path.getmtime(elf)


def busy(ip):
    """A sentence if this console is mid-install, else None.

    `state` is the shop's own word for what its job is doing. "installed" and "error" are finished
    rows that sit in the slot until something clears them - they are not a reason to refuse.
    """
    j = get("http://%s:%d/api/engine/job" % (ip, SHOP_PORT))
    if not j or not j.get("active"):
        return None
    st = str(j.get("state") or "")
    if st in ("installed", "error", ""):
        return None
    return "%s (%s)" % (j.get("name") or j.get("title_id") or "an install", st)


def wait_for(ip, version, elf, secs=90):
    """Poll until the shop is answering FROM THE FILE WE JUST SENT.

    Waiting on the version string alone is wrong for the commonest case there is - a rebuild of the
    same version - because the OLD payload answers with that version immediately and the push is
    reported live before the console has even restarted. is_current() compares compile stamps.
    """
    end = time.time() + secs
    last = None
    while time.time() < end:
        h = health(ip)
        if h:
            last = h.get("version")
            if is_current(h, elf) and last == version:
                return h
        time.sleep(2)
    return None if last is None else {"version": last, "_stale": True}


def push_ps4(ip, version, elf):
    """POST the ELF to GoldHEN's loader. The POST is the probe - never open a bare connect."""
    body = io.open(elf, "rb").read()
    req = urllib.request.Request("http://%s:%d/" % (ip, GOLDHEN_LOADER_PORT), data=body,
                                 headers={"Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            r.read()
    except urllib.error.HTTPError as e:
        return "the loader answered %s" % e.code
    except Exception as e:
        return "the loader did not take it: %s" % e
    h = wait_for(ip, version, elf)
    if not h:
        return "it never came back on :%d" % SHOP_PORT
    if h.get("_stale"):
        return "it came back still running v%s" % h["version"]
    return None


def push_ps5(ip, version, elf, force):
    """Delegate: companion/deploy.py already encodes every trap this lane has."""
    cmd = [sys.executable, os.path.join(ROOT, "companion", "deploy.py"), "elf", elf,
           "--ip", ip, "--expect", version]
    if force:
        cmd.append("--force")
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if p.returncode != 0:
        tail = (p.stdout or "") + (p.stderr or "")
        return tail.strip().splitlines()[-1][:160] if tail.strip() else "deploy.py failed"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report only, push nothing")
    ap.add_argument("--force", action="store_true", help="push even while an install is running")
    ap.add_argument("--only", default="", help="one console id or platform (ps4 / ps5)")
    a = ap.parse_args()

    version = want_version()
    fleet = consoles()
    if not fleet:
        print("%sNo consoles configured, and no companion running to ask.%s" % (R, Z))
        return 1

    print("PKG MUTANT SHOP %s -> %d console(s)\n" % (version, len(fleet)))
    rc = 0
    for c in fleet:
        ip = c.get("ip") or ""
        plat = "ps4" if str(c.get("platform") or "").lower() == "ps4" else "ps5"
        name = c.get("name") or plat.upper()
        if a.only and a.only.lower() not in (str(c.get("id") or "").lower(), plat):
            continue

        h = health(ip)
        if not h:
            print("  %-6s %-14s %snot answering%s - skipped" % (name, ip, Y, Z))
            continue
        have = h.get("version")
        elf = PS4_ELF if plat == "ps4" else PS5_ELF
        cur = is_current(h, elf)
        if a.check:
            mark = (G + "current" + Z) if cur else (Y + "older build" + Z)
            print("  %-6s %-14s v%-8s built %-22s %s"
                  % (name, ip, have, h.get("built") or "?", mark))
            continue
        if cur and not a.force:
            print("  %-6s %-14s v%s %salready running this build%s" % (name, ip, have, G, Z))
            continue

        b = busy(ip)
        if b and not a.force:
            print("  %-6s %-14s %sbusy%s - %s is running, not interrupting it "
                  "(--force overrides)" % (name, ip, Y, Z, b))
            rc = 1
            continue

        if not os.path.exists(elf):
            print("  %-6s %-14s %sno build at %s%s" % (name, ip, R, elf, Z))
            rc = 1
            continue

        print("  %-6s %-14s v%s -> %s ..." % (name, ip, have, version))
        err = (push_ps4 if plat == "ps4" else
               (lambda i, v, e: push_ps5(i, v, e, a.force)))(ip, version, elf)
        if err:
            print("         %sFAILED%s %s" % (R, Z, err))
            rc = 1
        else:
            print("         %sLIVE%s v%s" % (G, Z, version))

    return rc


if __name__ == "__main__":
    sys.exit(main())

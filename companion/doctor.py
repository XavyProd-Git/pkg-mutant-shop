#!/usr/bin/env python3
"""
PKG MUTANT SHOP - doctor / test kit
===================================
One command to check everything is wired before (and during) the first real install.

    python doctor.py                     # run all diagnostics
    python doctor.py --install CUSA08692 # do a REAL install of that title and watch live progress
                                         # (companion server must be running in another window)

Direct checks need nothing running. The --install test drives the companion API.

WHAT THE CONSOLE SIDE IS, TODAY. This used to probe :12800 for a "DPI v2 host" and tell the user
to start Elf Arsenal / ps5-dpi-v2 / etaHEN when nothing answered. None of that ships any more, and
the engine REQUIRES that port to be closed - so the first console line was red on every correctly
configured console, and named the software we removed. The engine as it is:

    the shop ELF        :8710   /api/health answers with on_console:true, version, engine_ready
    Payload Manager     :8084   spawns pms-installer.elf for every install; engine_ready means this
    ShadowMountPlus     loopback-only on the console; the PC cannot probe it. The shop reports it
                                in /api/health as "shadowmount" (true/false/null = unknown)
    FTP                 optional. 2121 (ftpsrv) or 1337 - probe both, never assume
    the companion       :8710 on THIS PC
"""
import json
import socket
import sys
import time
import urllib.request
import urllib.error

import pkg_meta
from server import load_config, lan_ip, Library

G, Y, R, X = "\033[92m", "\033[93m", "\033[91m", "\033[0m"
OK, WARN, BAD = G + "  OK " + X, Y + " WARN" + X, R + " FAIL" + X


def line(status, label, detail=""):
    print(" [%s] %-30s %s" % (status, label, detail))


def tcp(ip, port, timeout=1.5):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def api(base, path, method="GET", body=None, timeout=8):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def diagnostics(cfg):
    print("\n" + "=" * 60)
    print(" PKG MUTANT SHOP - doctor")
    print("=" * 60)

    cons = cfg.get("consoles") or []
    ip = (cons[0].get("ip") if cons and isinstance(cons[0], dict) else None) or cfg.get("ps5_ip")
    # .get() all the way down: an older config.json may carry a "dpi" section with none of these
    # keys, and a KeyError here used to be the whole output of the doctor.
    pl_port = int((cfg.get("dpi") or {}).get("pldmgr_port", 8084) or 8084)
    ftp = int((cfg.get("ftp") or {}).get("port", 2121) or 2121)
    shop_port = int((cfg.get("console") or {}).get("shop_port", 8710) or 8710)
    cport = int((cfg.get("companion") or {}).get("port", 8710) or 8710)

    print("\n- Config")
    line("OK" if ip else "WARN", "PS5 IP", ip or "(not set - edit config.json or let the app discover it)")
    line("OK", "Payload Manager port", str(pl_port))
    line("OK", "FTP port (configured)", str(ftp))
    line("OK", "This PC (LAN)", lan_ip())

    print("\n- Companion")
    base = "http://127.0.0.1:%d" % cport
    server_up = False
    try:
        h = api(base, "/api/health")
        server_up = True
        line("OK", "server running", "v%s on :%d" % (h.get("version"), cport))
    except Exception:
        line("WARN", "server running", "not detected - run: python server.py")

    print("\n- Console (%s)" % (ip or "?"))
    if ip:
        # THE SHOP ELF FIRST. It is what we ship, and its health answer is the honest source for
        # everything below it - engine readiness, ShadowMount, which FTP is live.
        health = None
        if tcp(ip, shop_port, 2.5):
            try:
                health = api("http://%s:%d" % (ip, shop_port), "/api/health")
            except Exception:
                health = None
        if health and health.get("on_console"):
            line("OK", "shop ELF :%d" % shop_port,
                 "v%s built %s" % (health.get("version"), health.get("built")))
        elif health:
            line("FAIL", "shop ELF :%d" % shop_port,
                 "something answers on :%d but it is not the console - is this address a PC?"
                 % shop_port)
        else:
            line("FAIL", "shop ELF :%d" % shop_port,
                 "not answering - load PKG-MUTANT-SHOP.elf from Payload Manager")

        pl_up = tcp(ip, pl_port)
        line("OK" if pl_up else "FAIL", "Payload Manager :%d" % pl_port,
             "reachable - installs can be spawned" if pl_up
             else "no route - installs cannot start until Payload Manager is running")
        if health is not None:
            ready = bool(health.get("engine_ready"))
            line("OK" if ready else "FAIL", "engine ready",
                 "%s (engine=%s)" % ("yes" if ready else "no", health.get("engine")))
            # ShadowMount binds loopback on the console; only the console can see it. null means
            # the ELF could not tell, which is not the same as "not running".
            smp = health.get("shadowmount")
            line("OK" if smp else ("WARN" if smp is None else "FAIL"), "ShadowMountPlus",
                 "running (reported by the shop)" if smp
                 else ("unknown - the shop did not say" if smp is None
                       else "not running - PS5 backups will not mount"))
        # FTP is optional: nothing in the install lane needs it any more. Probe both known ports
        # rather than only the configured one - which FTP is up depends on which payload is loaded.
        live_ftp = next((p for p in dict.fromkeys((ftp, 2121, 1337)) if tcp(ip, p)), 0)
        if live_ftp:
            line("OK", "FTP :%d" % live_ftp,
                 "reachable" + ("" if live_ftp == ftp else "  (config says :%d)" % ftp)
                 + " - optional, used by deploy.py elf")
        else:
            line("WARN", "FTP", "none on 2121 or 1337 - optional; only deploy.py elf needs it")
    else:
        line("WARN", "console", "set ps5_ip first")

    print("\n- Library (real param.sfo parse)")
    lib = Library(cfg)
    lib.scan()
    if lib.is_empty:
        line("WARN", "titles", "0 found - point library.local_paths at your PKG folder")
    else:
        line("OK", "titles", "%d game(s)" % len(lib.games))
        for g in lib.games[:12]:
            tag = "%s %s" % (g["platform"], g["region"])
            extras = "  +%du +%ddlc" % (len(g["updates"]), len(g["dlc"])) if (g["updates"] or g["dlc"]) else ""
            print("        %-11s %-34s %s%s" % (g["title_id"] or "-", g["name"][:34], tag, extras))

    print("\n- Install path the PS5 will use")
    line("OK", "companion file host", "http://%s:%d/library/<file>" % (lan_ip(), cport))
    print("\nNext: python doctor.py --install <TITLE_ID>   (with the server running)\n")
    return server_up


def do_install(cfg, title_id):
    base = "http://127.0.0.1:%d" % int((cfg.get("companion") or {}).get("port", 8710) or 8710)
    try:
        lib = api(base, "/api/library")
    except Exception:
        print(R + "Companion not running. Start it: python server.py" + X)
        return
    game = next((g for g in lib["games"] if g.get("title_id") == title_id), None)
    if not game:
        print(R + "Title %s not in library." % title_id + X)
        print("Available:", ", ".join(g.get("title_id") or "-" for g in lib["games"]))
        return
    if not game["base"] or not game["base"][0].get("install_key"):
        print(R + "No installable base file with a real key for %s (demo entry?)." % title_id + X)
        return

    item = game["base"][0]
    print("\nInstalling: %s  (%s)" % (game["name"], title_id))
    # No "drive" field: the PKG destination is the CONSOLE's own setting and no API selects it.
    # The route ignored the field, and sending it taught readers that it meant something.
    r = api(base, "/api/install", "POST", {
        "install_key": item["install_key"], "title_id": title_id,
        "name": game["name"], "kind": "base"})
    if r.get("error"):
        print(R + "Install rejected: %s" % (r.get("message") or r["error"]) + X)
        return
    job = r["ids"][0]
    print("job %s  source %s  url %s\n" % (job, r.get("source"), r.get("url")))

    last = None
    while True:
        q = api(base, "/api/queue")
        t = next((x for x in q["tasks"] if x["id"] == job), None)
        if not t:
            break
        bar = int((t.get("pct", 0)) / 4)
        stamp = "%-12s [%-25s] %3d%%  %s" % (t["state"], "#" * bar, t.get("pct", 0), t.get("msg", ""))
        if stamp != last:
            print("  " + stamp)
            last = stamp
        if t["state"] in ("playable", "error", "canceled", "submitted"):
            break
        time.sleep(1)
    print("\nDone.\n")


if __name__ == "__main__":
    cfg = load_config()
    if len(sys.argv) >= 3 and sys.argv[1] == "--install":
        do_install(cfg, sys.argv[2].upper())
    else:
        diagnostics(cfg)

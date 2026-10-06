#!/usr/bin/env python3
"""
PKG MUTANT SHOP - deploy tool
=============================
Push things to the jailbroken PS5. Stdlib only. Reads defaults from config.json.

  python deploy.py check [--ip IP] [--ftp-port 2121]
      Is the console there? Probes the shop ELF (:8710), Payload Manager (:8084) and FTP
      (2121, then 1337). elfldr's :9021 is NOT probed - it binds loopback-only on the console, so
      from a PC it always reads "no route" and proves nothing.

  python deploy.py elf [path/to/PKG-MUTANT-SHOP.elf] [--ip IP] [--ftp-port 2121] [--expect 3.60.0] [--force]
      THE WAY A NEW ON-CONSOLE BUILD GOES LIVE. Checks that Payload Manager can reload the shop
      and that the shop is idle, uploads the ELF over FTP to Payload Manager's own registered
      copy, asks the running shop to quit, waits for :8710 to close, loads the new build through
      Payload Manager and waits until /api/health reports the expected version.
      Default path: ps5-app/onconsole/PKG-MUTANT-SHOP.elf; the expected version is read from
      server.c (SHOP_VERSION) unless --expect is given. --force quits the shop even while a
      download, install hand-off or move is running (the transfer is lost).

  python deploy.py payload <file.elf> [--ip IP] [--port 9021]
      Send an .elf to a raw ELF-loader port (netcat style). elfldr on the console listens on
      :9021 for LOCALHOST ONLY, so this only works against a loader that was deliberately opened
      to the LAN. For the shop itself use `elf` above; for anything else use Payload Manager's
      web UI on the console.

  python deploy.py app
      RETIRED. It bundled the whole ps5-app/ tree (92 MB: the ELF, the cheat pack, old payloads)
      into /data/homebrew - a ShadowMount watch folder - twice. The ELF embeds web/ itself, so a
      UI change reaches the console by loading a new ELF (`elf`). The command prints this and
      does nothing.

WHY `elf` WRITES WHERE IT WRITES. Payload Manager resolves /loadpayload:<path> by BASENAME to the
copy it registered under /data/pldmgr/payloads/<NAME>/<NAME>.elf, whatever path it is given. An
upload anywhere else, followed by a load that says OK, silently runs the OLD build (it happened:
3.18.2 uploaded, 3.18.0 running). So the new build goes over that exact file - as <name>.part first
and renamed only once every byte has landed, the way every writer in this project treats a file
the console might open early. And after /api/quit the tool waits until a TCP connect to :8710 is
REFUSED before loading: one failed health request is not "it stopped" - a wedged-but-listening
shop and a stopped shop look identical to a single timed-out GET, and loading on top of a live one
leaves the old process owning the port (also happened: 3.56.0 loaded, old build still serving).

WHY IT LOOKS BEFORE IT QUITS. /api/quit is an immediate _exit(0) on the console. The tool used to
send it before finding out whether Payload Manager was even answering, so with :8084 down it killed
the only shop and then printed "uploaded but not loaded" - a console with no shop until someone
loaded one by hand. And it never asked what the shop was doing: a download, an install hand-off or
a drive-to-drive move was torn down mid-write, leaving a stray .part in a homebrew folder and a
lost transfer; the "wait for the current install or move to finish" line only appeared afterwards,
when the port stayed owned, i.e. after the damage. Now :8084 is probed and the shop's job, spawn
lane and move are read BEFORE anything is uploaded or quit, and a busy shop is left alone unless
--force says otherwise. (A PC upload in flight is not visible from outside - the console exposes
no reader for it - so that one still relies on the person running this.)

The previous build is kept as /data/pkg-mutant-shop/PKG-MUTANT-SHOP.elf.prev, OUTSIDE Payload
Manager's registered directory: pldmgr resolves loads by basename against that directory and how
it enumerates it is undocumented, so it holds exactly one ELF-shaped file, ours.
"""
import argparse
import ftplib
import os
import re
import socket
import sys
import time
import urllib.parse
import urllib.request

from server import load_config

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ONCONSOLE = os.path.join(ROOT, "ps5-app", "onconsole")
DEFAULT_ELF = os.path.join(ONCONSOLE, "PKG-MUTANT-SHOP.elf")
SHOP_C = os.path.join(ONCONSOLE, "server.c")

# The registered copy Payload Manager actually runs - see the header.
PLDMGR_ELF = "/data/pldmgr/payloads/PKG-MUTANT-SHOP/PKG-MUTANT-SHOP.elf"
# Where the build being replaced is kept - our own data directory, not pldmgr's (see the header).
PREV_ELF = "/data/pkg-mutant-shop/PKG-MUTANT-SHOP.elf.prev"
SHOP_PORT = 8710
PLDMGR_PORT = 8084

G, Y, R, X = "\033[92m", "\033[93m", "\033[91m", "\033[0m"


def tcp_ok(ip, port, timeout=2.0):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def tcp_refused(ip, port, timeout=2.0):
    """True only when the connect is actively REFUSED - the port is closed, nobody owns it. A
    timeout is not a refusal: it is what a wedged listener looks like from outside."""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return False
    except ConnectionRefusedError:
        return True
    except OSError:
        return False
    finally:
        s.close()


def http_get(url, timeout=8):
    """(ok, text). Never raises."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return True, r.read().decode("utf-8", "replace")
    except Exception as e:
        return False, repr(e)[:160]


def shop_health(ip, port=SHOP_PORT, timeout=5):
    """The console's /api/health as a dict, or None."""
    import json
    ok, body = http_get("http://%s:%d/api/health" % (ip, port), timeout)
    if not ok:
        return None
    try:
        j = json.loads(body)
    except ValueError:
        return None
    return j if isinstance(j, dict) else None


def shop_busy(ip, port=SHOP_PORT):
    """One sentence naming the transfer the shop is in the middle of, or None when it is idle.
    Reads the same three things /api/rest/prepare refuses on: the download job, the spawn lane
    and a running move. A reader that cannot be reached counts as idle - the caller has already
    established that the shop answers, and an unknown /api/ path answers 200 {} on the console,
    which reads as idle too."""
    import json

    def get(path):
        ok, body = http_get("http://%s:%d%s" % (ip, port, path), timeout=8)
        if not ok:
            return {}
        try:
            j = json.loads(body)
        except ValueError:
            return {}
        return j if isinstance(j, dict) else {}

    job = get("/api/engine/job")
    if job.get("state") in ("downloading", "installing"):
        return "a download is running (%s, %s%%)" % (job.get("name") or "?", job.get("pct", 0))
    if get("/api/engine/spawn-status").get("busy"):
        return "an install is being handed to the console"
    mv = get("/api/move/status")
    if mv.get("active"):
        return "a game is being moved between drives (%s, %s%%)" % (mv.get("name") or "?", mv.get("percent", 0))
    return None


# --------------------------------------------------------------------------- #
def send_payload(path, ip, port):
    if not os.path.isfile(path):
        print(R + "no such file: %s" % path + X)
        return False
    data = open(path, "rb").read()
    print("Sending %s (%d bytes) -> %s:%d" % (os.path.basename(path), len(data), ip, port))
    print(Y + "note: elfldr's :9021 is localhost-only on the console; this reaches only a loader "
          "that was opened to the LAN." + X)
    try:
        with socket.create_connection((ip, port), timeout=8) as s:
            s.sendall(data)
        print(G + "sent." + X + " (watch the PS5 - the payload runs on receipt)")
        return True
    except OSError as e:
        print(R + "send failed: %s" % e + X)
        return False


# --------------------------------------------------------------------------- #
# Which FTP the console is running decides the port: ftpsrv serves 2121, etaHEN's own FTP served
# 1337. Never dial only the configured port - probe 2121 then 1337 and remember which answered.
FTP_FALLBACKS = (2121, 1337)


def ftp_ports(configured):
    seen, order = set(), []
    for p in [configured] + list(FTP_FALLBACKS):
        if p and p not in seen:
            seen.add(p)
            order.append(p)
    return order


def ftp_login(ip, port):
    last = None
    for p in ftp_ports(port):
        ftp = ftplib.FTP()
        try:
            ftp.connect(ip, p, timeout=8)
            ftp.login()
            if p != port:
                print("  (FTP answered on :%d, not the configured :%d)" % (p, port))
            return ftp
        except Exception as e:
            last = e
            try:
                ftp.close()
            except Exception:
                pass
    raise last if last else OSError("no FTP port reachable on %s" % ip)


def ftp_mkd(ftp, path):
    try:
        ftp.mkd(path)
    except ftplib.error_perm:
        pass  # already exists


def ftp_exists(ftp, path):
    try:
        ftp.voidcmd("TYPE I")
        ftp.size(path)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
def elf_source_version():
    """SHOP_VERSION as compiled into server.c - what /api/health will say once the new build runs."""
    try:
        with open(SHOP_C, "rb") as f:
            m = re.search(rb'#define\s+SHOP_VERSION\s+"([^"]+)"', f.read())
        return m.group(1).decode() if m else None
    except OSError:
        return None


def deploy_elf(path, ip, ftp_port, expect, quit_wait=120, up_wait=90, force=False):
    if not os.path.isfile(path):
        print(R + "no such file: %s" % path + X)
        return False
    size = os.path.getsize(path)
    expect = expect or elf_source_version()
    print("ELF %s (%d bytes) -> %s  expecting version %s" % (path, size, ip, expect or "?"))

    before = shop_health(ip)
    if before:
        print("  running now: v%s built %s" % (before.get("version"), before.get("built")))
    else:
        print("  the shop is not answering on :%d right now (that is fine - it will be loaded)" % SHOP_PORT)

    # 0. look before touching anything. Both of these used to be discovered AFTER /api/quit had
    #    already killed the shop - see the header. Nothing has been uploaded yet either, so a
    #    refusal here leaves the console exactly as it was.
    if not tcp_ok(ip, PLDMGR_PORT, 3.0):
        print(R + "  Payload Manager is not answering on :%d - not quitting the shop, it could not "
              "be reloaded. Nothing was changed." % PLDMGR_PORT + X)
        return False
    shop_up = tcp_ok(ip, SHOP_PORT, 2.0)
    if shop_up:
        why = shop_busy(ip)
        if why and not force:
            print(R + "  %s - try again when it is done. Nothing was changed." % why + X)
            print(Y + "  (--force quits the shop anyway; the transfer is lost and a stray .part "
                  "may be left in a homebrew folder)" + X)
            return False
        if why:
            print(Y + "  --force: %s, quitting the shop anyway" % why + X)

    # 1. upload over Payload Manager's registered copy, .part first
    try:
        ftp = ftp_login(ip, ftp_port)
    except ftplib.all_errors as e:   # a login REFUSAL is ftplib.error_perm, not an OSError
        print(R + "FTP connect/login failed: %s" % e + X)
        print(Y + "FTP is the only way to write Payload Manager's copy. Load ftpsrv from Payload "
              "Manager, then run this again." + X)
        return False
    remote_dir = PLDMGR_ELF.rsplit("/", 1)[0]
    part = PLDMGR_ELF + ".part"
    try:
        ftp_mkd(ftp, "/data/pldmgr/payloads")
        ftp_mkd(ftp, remote_dir)
        with open(path, "rb") as f:
            ftp.storbinary("STOR " + part, f, blocksize=1 << 16)
        ftp.voidcmd("TYPE I")
        landed = ftp.size(part)
        if landed != size:
            print(R + "  short upload: %s of %d bytes landed - not renaming, nothing changed"
                  % (landed, size) + X)
            try:
                ftp.delete(part)
            except Exception:
                pass
            ftp.quit()
            return False
        # keep the previous build; the basename trap makes "which build is registered" a question
        # worth being able to answer after the fact. It used to stay beside the live copy as
        # <name>.elf.prev, i.e. a second ELF-shaped file for ever inside the one directory pldmgr
        # resolves loads from by basename - so it lives in our own data directory instead, and
        # pldmgr's directory holds exactly one ELF.
        if ftp_exists(ftp, PLDMGR_ELF):
            try:
                ftp.delete(PREV_ELF)
            except Exception:
                pass
            ftp_mkd(ftp, PREV_ELF.rsplit("/", 1)[0])
            try:
                ftp.rename(PLDMGR_ELF, PREV_ELF)
            except Exception as e:
                print(Y + "  could not keep the old build as %s (%s) - replacing it without a backup"
                      % (PREV_ELF, e) + X)
        ftp.rename(part, PLDMGR_ELF)
        ftp.quit()
        print(G + "  uploaded" + X + " -> %s (%d bytes)" % (PLDMGR_ELF, size))
    except (ftplib.all_errors + (OSError,)) as e:
        print(R + "upload error: %s" % e + X)
        try:
            ftp.quit()
        except Exception:
            pass
        return False

    # 2. ask the running shop to quit, then WAIT until :8710 is refused. Payload Manager and the
    #    shop's own state were checked in step 0, before the upload - this is the point of no return.
    if shop_up or tcp_ok(ip, SHOP_PORT, 2.0):
        print("  asking the running shop to quit (/api/quit) ...")
        ok_quit, quit_body = http_get("http://%s:%d/api/quit" % (ip, SHOP_PORT), timeout=8)
        acknowledged = ok_quit and '"bye"' in (quit_body or "")
        # THIS CONSOLE DOES NOT REFUSE. A connect to a port nobody owns on the PS5 is dropped, not
        # reset, so it TIMES OUT - and the first version of this loop waited for an active
        # ConnectionRefusedError that never came, gave up after two minutes, and left the shop
        # down with the new file uploaded and nothing loaded (measured 2026-09-04: the shop had
        # answered {"ok":true,"bye":true} and _exit()ed within a second). "Gone" is therefore:
        # the shop acknowledged the quit, and the port has stopped accepting - twice in a row,
        # because a single failed connect can also be a busy accept loop. A wedged listener still
        # accepts (the socket connects), so it still keeps this loop waiting, which is the point.
        t0 = time.time()
        misses = 0
        while time.time() - t0 < quit_wait:
            if tcp_ok(ip, SHOP_PORT, 2.0):
                misses = 0
            else:
                misses += 1
                if misses >= 2 and (acknowledged or time.time() - t0 > 10):
                    break
            time.sleep(1.5)
        else:
            print(R + "  :%d is still owned after %ds. A live shop cannot be replaced - the new "
                  "process would fail to bind and the OLD build would keep serving. Wait for "
                  "the current install or move to finish, then run this again." % (SHOP_PORT, quit_wait) + X)
            return False
        print("  :%d closed after %.0fs" % (SHOP_PORT, time.time() - t0))

    # 3. load through Payload Manager (its port was proven open in step 0, before the quit; if it
    #    went away in the seconds since, the load below fails and says so - the ELF is uploaded,
    #    load it from Payload Manager's UI)
    url = "http://%s:%d/loadpayload:%s" % (ip, PLDMGR_PORT, urllib.parse.quote(PLDMGR_ELF, safe="/:"))
    ok, body = http_get(url, timeout=30)
    print("  loadpayload -> %s %s" % ("ok" if ok else "FAILED", body.strip()[:80]))
    if not ok:
        return False

    # 4. the only proof that the load took: /api/health says the new version
    t0 = time.time()
    while time.time() - t0 < up_wait:
        h = shop_health(ip, timeout=4)
        if h and h.get("on_console"):
            if not expect or h.get("version") == expect:
                print(G + "LIVE" + X + ": v%s built %s on %s:%d"
                      % (h.get("version"), h.get("built"), ip, SHOP_PORT))
                return True
            # the old process still answering is the port problem the header describes
            if before and h.get("built") == before.get("built"):
                print(Y + "  the OLD process is still answering (same build stamp %s) - waiting"
                      % h.get("built") + X)
            else:
                print(Y + "  answering with v%s built %s, wanted %s - waiting"
                      % (h.get("version"), h.get("built"), expect) + X)
        time.sleep(2)
    print(R + "TIMEOUT after %ds: the shop did not come up as version %s on %s:%d." % (up_wait, expect, ip, SHOP_PORT) + X)
    print(Y + "  Check http://%s:%d/api/health yourself. If it reports an older version, Payload "
          "Manager ran a different copy: the file at %s is what it loads by basename, and it was "
          "just written - so compare `built` with this ELF's build time before loading again." % (ip, SHOP_PORT, PLDMGR_ELF) + X)
    return False


# --------------------------------------------------------------------------- #
def main():
    cfg = load_config()
    ip_default = (cfg.get("consoles") or [{}])[0].get("ip") if cfg.get("consoles") else cfg.get("ps5_ip")
    ftp_default = (cfg.get("ftp") or {}).get("port", 2121)

    ap = argparse.ArgumentParser(description="PKG MUTANT SHOP deploy tool")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check")
    c.add_argument("--ip", default=ip_default)
    c.add_argument("--ftp-port", type=int, default=ftp_default)

    e = sub.add_parser("elf")
    e.add_argument("file", nargs="?", default=DEFAULT_ELF)
    e.add_argument("--ip", default=ip_default)
    e.add_argument("--ftp-port", type=int, default=ftp_default)
    e.add_argument("--expect", default=None,
                   help="version /api/health must report afterwards (default: SHOP_VERSION in server.c)")
    e.add_argument("--force", action="store_true",
                   help="quit the shop even while a download, install hand-off or move is running")

    p = sub.add_parser("payload")
    p.add_argument("file")
    p.add_argument("--ip", default=ip_default)
    p.add_argument("--port", type=int, default=9021)

    a = sub.add_parser("app")
    a.add_argument("--ip", default=ip_default)
    a.add_argument("--ftp-port", type=int, default=ftp_default)
    a.add_argument("--dest", default=None)
    a.add_argument("--companion", default=None)

    args = ap.parse_args()

    if args.cmd == "app":
        # Retired on purpose - see the header. Say why, and what to do instead.
        print(Y + "deploy.py app is retired." + X)
        print("  It uploaded the whole ps5-app/ tree (the 38 MB ELF, the 32 MB cheat pack, old payloads)")
        print("  into /data/homebrew - a ShadowMount watch folder - and mirrored it a second time.")
        print("  The ELF embeds web/ itself; a UI change reaches the console by loading a new ELF:")
        print("      bash ps5-app/onconsole/build-wsl.sh        (in WSL)")
        print("      python companion/deploy.py elf             (from here)")
        sys.exit(2)

    if not args.__dict__.get("ip"):
        print(R + "No PS5 IP. Set ps5_ip/consoles in config.json or pass --ip." + X)
        sys.exit(2)

    if args.cmd == "check":
        print("PS5 %s:" % args.ip)
        h = shop_health(args.ip)
        if h and h.get("on_console"):
            print("  shop ELF :%d  %sreachable%s  v%s built %s  engine_ready=%s"
                  % (SHOP_PORT, G, X, h.get("version"), h.get("built"), h.get("engine_ready")))
        else:
            print("  shop ELF :%d  %sno route%s  (load PKG-MUTANT-SHOP.elf from Payload Manager)" % (SHOP_PORT, R, X))
        print(("  Payload Manager :%d  " % PLDMGR_PORT) + (G + "reachable" + X if tcp_ok(args.ip, PLDMGR_PORT) else R + "no route" + X))
        try:
            f = ftp_login(args.ip, args.ftp_port)
            live = f.sock.getpeername()[1]
            f.quit()
            print("  FTP :%d  %sreachable%s" % (live, G, X))
        except ftplib.all_errors:   # a login REFUSAL is ftplib.error_perm, not an OSError
            print("  FTP  %sno route on %s%s  (optional - only `elf` needs it)"
                  % (R, "/".join(str(p) for p in ftp_ports(args.ftp_port)), X))
        print("  (elfldr :9021 is localhost-only on the console and is not probed)")
    elif args.cmd == "elf":
        sys.exit(0 if deploy_elf(args.file, args.ip, args.ftp_port, args.expect, force=args.force) else 1)
    elif args.cmd == "payload":
        send_payload(args.file, args.ip, args.port)


if __name__ == "__main__":
    main()

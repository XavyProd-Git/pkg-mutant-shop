#!/usr/bin/env python3
"""
DEPRECATED - this route is dead on FW 12.70 and the tile no longer needs it.
===========================================================================
The shipping tile is pms-tile.pkg, embedded in the shop ELF (tile_bundle.h) and installed by
the console's own installer on boot (tile_install() in server.c). To put it back by hand:

    GET http://<ps5>:8710/api/tile/install          (?force=1 to reinstall over a stale one)

What this script does - MTRW-mounting the system partition and writing /system_ex/app and
/user/app by hand, then relying on install-tile-payload.elf and sceAppInstUtilAppInstallTitleDir,
which is not exported on 12.70 and whose toasts never render - cannot produce a working tile any
more. It is kept for the record only and exits before touching the console.

Deploy the PKG MUTANT SHOP dashboard tile to the PS5 over FTP.
  python deploy-tile.py [ps5_ip]
Then send install-tile-payload.elf to the PS5 (Payload Manager) to register the tile.
Reads ps5_ip / ftp port from companion/config.json if not given.
"""
import ftplib, json, os, sys

print("deploy-tile.py is DEPRECATED: the tile is embedded in the shop ELF and installs itself.")
print("Use  GET http://<ps5>:8710/api/tile/install  instead. Nothing was written to the console.")
if "--i-know-this-is-dead" not in sys.argv:
    sys.exit(2)
sys.argv = [a for a in sys.argv if a != "--i-know-this-is-dead"]

HERE = os.path.dirname(os.path.abspath(__file__))
TID = "PKGM00001"

ip, port = "10.0.0.99", 2121
try:
    c = json.load(open(os.path.join(HERE, "..", "..", "companion", "config.json")))
    ip = c.get("ps5_ip", ip)
    port = c.get("ftp", {}).get("port", port)
except Exception:
    pass
if len(sys.argv) > 1:
    ip = sys.argv[1]

tile = os.path.join(HERE, TID)
if not os.path.isfile(os.path.join(tile, "eboot.bin")):
    print("Tile not built. In WSL run:  bash ps5-app/tile/build-wsl.sh")
    sys.exit(1)

print("Deploying tile -> %s:%d" % (ip, port))
ftp = ftplib.FTP()
ftp.connect(ip, port, timeout=8)
ftp.login()
try:
    ftp.sendcmd("MTRW")            # ftpsrv.elf: mount the system partition read-write
    print("  MTRW ok (system partition writable)")
except Exception as e:
    print("  MTRW:", e, "(if this fails, the /system_ex writes below may be denied)")


def mkd(p):
    try:
        ftp.mkd(p)
    except ftplib.error_perm:
        pass


def put(local, remote):
    with open(local, "rb") as f:
        ftp.storbinary("STOR " + remote, f)
    print("  put", remote)


for d in ["/system_ex/app/" + TID, "/system_ex/app/" + TID + "/sce_sys",
          "/user/app/" + TID, "/user/app/" + TID + "/sce_sys"]:
    mkd(d)

put(os.path.join(tile, "eboot.bin"),             "/system_ex/app/%s/eboot.bin" % TID)
put(os.path.join(tile, "sce_sys", "param.json"), "/user/app/%s/sce_sys/param.json" % TID)
put(os.path.join(tile, "icon0.png"),             "/user/app/%s/sce_sys/icon0.png" % TID)
# NOTE: the system-side /system_ex/.../param.json is written by install-tile-payload.elf
# AFTER it registers the app (SDK-sample ordering) - don't upload it here.
ftp.quit()

print("\nApp files uploaded (system partition left writable). FINAL STEP (registers the tile):")
print("  Send  ps5-app/tile/install-tile-payload.elf  to the PS5 via Payload Manager.")
print("  It now REPORTS on-screen what happened:")
print("    'Tile installed!'                  -> success, check your dashboard")
print("    'No app files on console...'       -> this deploy didn't land; re-run it")
print("    'Register failed: 0x....'          -> tell Claude the exact code")
print("  Launch the tile -> it opens PKG MUTANT SHOP in the PS5 browser.")
